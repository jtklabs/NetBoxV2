"""Evidence reports and lifecycle planning in the NetBox UI."""

import json

import django_filters
import django_tables2 as tables
from dcim.models import Device
from django import forms
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import Q
from django.shortcuts import render
from django.http import HttpResponseBadRequest
from django.urls import path
from django.views import View
from netbox.filtersets import NetBoxModelFilterSet
from netbox.forms import NetBoxModelForm, NetBoxModelFilterSetForm
from netbox.tables import NetBoxTable
from netbox.views.generic import (
    ObjectChangeLogView,
    ObjectEditView,
    ObjectListView,
    ObjectView,
)
from utilities.forms.widgets import DatePicker
from utilities.forms.fields import DynamicModelChoiceField
from utilities.views import register_model_view

from netbox_refresh.models import (
    DeviceCollectionState,
    DeviceInventoryChange,
    DeviceObservation,
    DeviceRelationship,
    LifecycleAction,
)


class ActionForm(NetBoxModelForm):
    class Meta:
        model = LifecycleAction
        fields = ("owner", "status", "due_override")
        widgets = {"due_override": DatePicker()}


class ActionEdit(ObjectEditView):
    queryset = LifecycleAction.objects.all()
    form = ActionForm


class ScopedEvidence:
    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .filter(device__in=Device.objects.restrict(request.user, "view"))
        )


urlpatterns = []
for prefix, model, fields in (
    (
        "collection-health",
        DeviceCollectionState,
        (
            "device",
            "status",
            "poller",
            "executor_version",
            "last_attempt",
            "last_success",
        ),
    ),
    (
        "observations",
        DeviceObservation,
        ("device", "status", "poller", "observed_at", "job_id"),
    ),
    (
        "inventory-changes",
        DeviceInventoryChange,
        ("device", "field", "before", "after", "observed_at", "poller"),
    ),
    (
        "relationships",
        DeviceRelationship,
        (
            "device",
            "kind",
            "local_interface",
            "peer_name",
            "peer_interface",
            "state",
            "present",
            "confirmed",
            "last_seen",
        ),
    ),
    (
        "actions",
        LifecycleAction,
        (
            "device",
            "title",
            "kind",
            "status",
            "priority",
            "owner",
            "due_on",
            "reminder_stage",
            "condition_active",
        ),
    ),
):
    name = model._meta.model_name
    meta = type(
        "Meta",
        (NetBoxTable.Meta,),
        {
            "model": model,
            "fields": ("pk", "id", *fields),
            "default_columns": ("id", *fields),
        },
    )
    table = type(
        f"{model.__name__}Table",
        (NetBoxTable,),
        {
            "Meta": meta,
            "id": tables.Column(linkify=True),
            "device": tables.Column(linkify=True),
            "actions": tables.Column(empty_values=(), visible=False),
        },
    )
    filter_fields = {
        f: ["exact"]
        for f in fields
        if f
        not in (
            "before",
            "after",
            "last_attempt",
            "last_success",
            "observed_at",
            "last_seen",
            "title",
        )
    }

    def manual_filter(self, qs, name, value):
        from netbox_refresh.operations import FACT_FIELDS

        condition = Q(pk__in=[])
        for field in FACT_FIELDS:
            condition |= Q(**{f"field_sources__{field}": "manual"})
        return qs.filter(condition) if value else qs.exclude(condition)

    manual_attrs = (
        {
            "manual": django_filters.BooleanFilter(method="filter_manual"),
            "filter_manual": manual_filter,
        }
        if model in (DeviceObservation, DeviceCollectionState)
        else {}
    )
    filterset = type(
        f"{model.__name__}FilterSet",
        (NetBoxModelFilterSet,),
        {
            "Meta": type("Meta", (), {"model": model, "fields": filter_fields}),
            **manual_attrs,
        },
    )
    form_attrs = {
        "model": model,
        "device": DynamicModelChoiceField(
            queryset=Device.objects.all(), required=False
        ),
    }
    for field in ("poller", "status", "kind", "priority", "reminder_stage"):
        if field in filter_fields:
            form_attrs[field] = forms.CharField(required=False)
    if model in (DeviceCollectionState, DeviceObservation):
        form_attrs["manual"] = forms.NullBooleanField(
            required=False, label="Has manually sourced fields"
        )
    if model is LifecycleAction:
        form_attrs["condition_active"] = forms.NullBooleanField(
            required=False, label="Condition still active"
        )
    filter_form = type(
        f"{model.__name__}FilterForm", (NetBoxModelFilterSetForm,), form_attrs
    )
    list_view = type(
        f"{model.__name__}List",
        (ScopedEvidence, ObjectListView),
        {
            "queryset": model.objects.select_related("device"),
            "table": table,
            "filterset": filterset,
            "filterset_form": filter_form,
            "actions": (),
        },
    )

    def context(self, request, instance):
        rows = []
        for field in instance._meta.fields:
            if field.name in ("id", "device", "custom_field_data"):
                continue
            value = getattr(instance, field.name)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, indent=2, default=str)
            rows.append((field.verbose_name, value))
        return {"rows": rows}

    detail = type(
        f"{model.__name__}View",
        (ScopedEvidence, ObjectView),
        {
            "queryset": model.objects.select_related("device"),
            "template_name": "netbox_refresh/operations_object.html",
            "get_extra_context": context,
        },
    )
    register_model_view(model)(detail)
    urlpatterns += [
        path(f"{prefix}/", list_view.as_view(), name=f"{name}_list"),
        path(f"{prefix}/<int:pk>/", detail.as_view(), name=name),
        path(
            f"{prefix}/<int:pk>/changelog/",
            ObjectChangeLogView.as_view(),
            {"model": model},
            name=f"{name}_changelog",
        ),
    ]
urlpatterns.append(
    path("actions/<int:pk>/edit/", ActionEdit.as_view(), name="lifecycleaction_edit")
)


class ReadinessReport(PermissionRequiredMixin, View):
    permission_required = (
        "dcim.view_device",
        "netbox_refresh.view_devicecollectionstate",
        "netbox_refresh.view_devicesoftware",
        "netbox_refresh.view_softwarestandard",
        "netbox_refresh.view_softwareversion",
    )

    def get(self, request):
        from netbox_refresh.api.operations import ReadinessView
        from rest_framework.request import Request

        api_request = Request(request)
        api_request.user = request.user
        from rest_framework.exceptions import APIException

        try:
            data = ReadinessView().get(api_request).data
        except APIException:
            return HttpResponseBadRequest("Invalid report filters")
        return render(request, "netbox_refresh/readiness_report.html", data)


urlpatterns.append(
    path("readiness/", ReadinessReport.as_view(), name="readiness_report")
)
