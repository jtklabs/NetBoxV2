"""Forms, filters, and views for device license and support coverage."""

from datetime import timedelta

import django_filters
import django_tables2 as tables
from dcim.models import Device, Manufacturer
from django import forms
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef, Q, Sum
from django.shortcuts import render
from django.urls import path
from django.utils import timezone
from django.views import View
from netbox.filtersets import NetBoxModelFilterSet
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm
from netbox.tables import NetBoxTable
from netbox.views.generic import (
    ObjectChangeLogView, ObjectDeleteView, ObjectEditView, ObjectListView, ObjectView,
)
from utilities.forms.fields import DynamicModelChoiceField, DynamicModelMultipleChoiceField
from utilities.forms.widgets import DatePicker, DateTimePicker
from utilities.views import register_model_view

from netbox_refresh.models import LicenseAssignment, LicenseEntitlement, SupportContract

ENTITLEMENT_FIELDS = (
    'manufacturer', 'product', 'reference', 'seats', 'expires_on', 'renew_by', 'source_url', 'vault_reference',
)
ASSIGNMENT_FIELDS = (
    'entitlement', 'device', 'seats', 'installed_modules', 'reported_expiration', 'service_check_date', 'verified_at',
)
CONTRACT_FIELDS = ('provider', 'reference', 'service_level', 'starts_on', 'ends_on', 'renew_by', 'source_url', 'devices')


class LicenseEntitlementForm(NetBoxModelForm):
    manufacturer = DynamicModelChoiceField(queryset=Manufacturer.objects.all())

    class Meta:
        model = LicenseEntitlement
        fields = (*ENTITLEMENT_FIELDS, 'description', 'comments', 'tags')
        widgets = {'expires_on': DatePicker(), 'renew_by': DatePicker()}


class LicenseAssignmentForm(NetBoxModelForm):
    entitlement = DynamicModelChoiceField(queryset=LicenseEntitlement.objects.all())
    device = DynamicModelChoiceField(queryset=Device.objects.all())

    class Meta:
        model = LicenseAssignment
        fields = (*ASSIGNMENT_FIELDS, 'description', 'comments', 'tags')
        widgets = {
            'reported_expiration': DatePicker(), 'service_check_date': DatePicker(),
            'verified_at': DateTimePicker(),
        }


class SupportContractForm(NetBoxModelForm):
    devices = DynamicModelMultipleChoiceField(queryset=Device.objects.all(), required=False)

    class Meta:
        model = SupportContract
        fields = (*CONTRACT_FIELDS, 'description', 'comments', 'tags')
        widgets = {'starts_on': DatePicker(), 'ends_on': DatePicker(), 'renew_by': DatePicker()}


class LicenseEntitlementFilterSet(NetBoxModelFilterSet):
    class Meta:
        model = LicenseEntitlement
        fields = {'manufacturer': ['exact'], 'expires_on': ['gte', 'lte'], 'renew_by': ['gte', 'lte']}

    def search(self, queryset, name, value):
        return queryset.filter(Q(product__icontains=value) | Q(reference__icontains=value))


class LicenseAssignmentFilterSet(NetBoxModelFilterSet):
    class Meta:
        model = LicenseAssignment
        fields = {'device': ['exact'], 'entitlement': ['exact'], 'reported_expiration': ['gte', 'lte']}

    def search(self, queryset, name, value):
        return queryset.filter(
            Q(device__name__icontains=value) | Q(entitlement__reference__icontains=value)
            | Q(entitlement__product__icontains=value)
        )


class SupportContractFilterSet(NetBoxModelFilterSet):
    devices = django_filters.ModelMultipleChoiceFilter(queryset=Device.objects.all())
    active = django_filters.BooleanFilter(method='filter_active')

    class Meta:
        model = SupportContract
        fields = {'ends_on': ['gte', 'lte'], 'renew_by': ['gte', 'lte']}

    def search(self, queryset, name, value):
        return queryset.filter(Q(provider__icontains=value) | Q(reference__icontains=value))

    def filter_active(self, queryset, name, value):
        today = timezone.localdate()
        active = Q(starts_on__lte=today, ends_on__gte=today)
        return queryset.filter(active) if value else queryset.exclude(active)


class RenewalFilterForm(NetBoxModelFilterSetForm):
    renew_by__gte = forms.DateField(required=False, widget=DatePicker(), label='Renewal on or after')
    renew_by__lte = forms.DateField(required=False, widget=DatePicker(), label='Renewal on or before')


class LicenseEntitlementFilterForm(RenewalFilterForm):
    model = LicenseEntitlement
    expires_on__gte = forms.DateField(required=False, widget=DatePicker(), label='Expires on or after')
    expires_on__lte = forms.DateField(required=False, widget=DatePicker(), label='Expires on or before')


class LicenseAssignmentFilterForm(NetBoxModelFilterSetForm):
    model = LicenseAssignment
    reported_expiration__lte = forms.DateField(required=False, widget=DatePicker(), label='Reported expiration by')


class SupportContractFilterForm(RenewalFilterForm):
    model = SupportContract
    active = forms.NullBooleanField(required=False, label='Active')
    ends_on__gte = forms.DateField(required=False, widget=DatePicker(), label='Ends on or after')
    ends_on__lte = forms.DateField(required=False, widget=DatePicker(), label='Ends on or before')


class LicenseEntitlementTable(NetBoxTable):
    reference = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = LicenseEntitlement
        fields = ('pk', 'manufacturer', 'product', 'reference', 'seats', 'expires_on', 'renew_by')
        default_columns = fields[1:]


class LicenseAssignmentTable(NetBoxTable):
    device = tables.Column(linkify=True)
    entitlement = tables.Column(linkify=True)
    id = tables.Column(linkify=True, verbose_name='Assignment')

    class Meta(NetBoxTable.Meta):
        model = LicenseAssignment
        fields = ('pk', 'id', 'device', 'entitlement', 'seats', 'reported_expiration', 'service_check_date', 'verified_at')
        default_columns = fields[1:]


class SupportContractTable(NetBoxTable):
    reference = tables.Column(linkify=True)
    coverage_status = tables.Column(orderable=False, verbose_name='Coverage')

    class Meta(NetBoxTable.Meta):
        model = SupportContract
        fields = ('pk', 'provider', 'reference', 'service_level', 'starts_on', 'ends_on', 'renew_by', 'coverage_status')
        default_columns = fields[1:]


class LicenseEntitlementList(ObjectListView):
    queryset = LicenseEntitlement.objects.select_related('manufacturer')
    table = LicenseEntitlementTable
    filterset = LicenseEntitlementFilterSet
    filterset_form = LicenseEntitlementFilterForm


@register_model_view(LicenseEntitlement)
class LicenseEntitlementView(ObjectView):
    queryset = LicenseEntitlementList.queryset

    def get_extra_context(self, request, instance):
        # Quantities reflect only assignments the current user can view. Do
        # not present them as the total availability of a restricted pool.
        assignments = instance.assignments.restrict(request.user, 'view').select_related('device', 'entitlement')
        return {
            'assignments': assignments,
            'visible_seats': assignments.aggregate(total=Sum('seats'))['total'] or 0,
        }


class LicenseEntitlementEdit(ObjectEditView):
    queryset = LicenseEntitlement.objects.all()
    form = LicenseEntitlementForm


class LicenseEntitlementDelete(ObjectDeleteView):
    queryset = LicenseEntitlement.objects.all()


class LicenseAssignmentList(ObjectListView):
    queryset = LicenseAssignment.objects.select_related('entitlement', 'device')
    table = LicenseAssignmentTable
    filterset = LicenseAssignmentFilterSet
    filterset_form = LicenseAssignmentFilterForm


@register_model_view(LicenseAssignment)
class LicenseAssignmentView(ObjectView):
    queryset = LicenseAssignmentList.queryset


class LicenseAssignmentEdit(ObjectEditView):
    queryset = LicenseAssignment.objects.all()
    form = LicenseAssignmentForm


class LicenseAssignmentDelete(ObjectDeleteView):
    queryset = LicenseAssignment.objects.all()


class SupportContractList(ObjectListView):
    queryset = SupportContract.objects.all()
    table = SupportContractTable
    filterset = SupportContractFilterSet
    filterset_form = SupportContractFilterForm


@register_model_view(SupportContract)
class SupportContractView(ObjectView):
    queryset = SupportContractList.queryset

    def get_extra_context(self, request, instance):
        return {'devices': instance.devices.restrict(request.user, 'view')}


class SupportContractEdit(ObjectEditView):
    queryset = SupportContract.objects.all()
    form = SupportContractForm


class SupportContractDelete(ObjectDeleteView):
    queryset = SupportContract.objects.all()


class CoverageReport(PermissionRequiredMixin, View):
    permission_required = ('dcim.view_device', 'netbox_refresh.view_supportcontract')

    def get(self, request):
        today = timezone.localdate()
        contracts = SupportContract.objects.restrict(request.user, 'view').filter(
            devices=OuterRef('pk'), starts_on__lte=today, ends_on__gte=today,
        )
        devices = Device.objects.restrict(request.user, 'view').filter(
            ~Exists(contracts)
        ).select_related('site').order_by('name', 'pk')
        page = Paginator(devices, 50).get_page(request.GET.get('page'))
        return render(request, 'netbox_refresh/coverage_report.html', {
            'page': page,
            'today': today,
            'renewal_cutoff': today + timedelta(days=90),
        })


urlpatterns = [path('coverage/', CoverageReport.as_view(), name='coverage_report')]
for prefix, model, list_view, detail, edit, delete in (
    ('license-entitlements', LicenseEntitlement, LicenseEntitlementList, LicenseEntitlementView,
     LicenseEntitlementEdit, LicenseEntitlementDelete),
    ('license-assignments', LicenseAssignment, LicenseAssignmentList, LicenseAssignmentView,
     LicenseAssignmentEdit, LicenseAssignmentDelete),
    ('support-contracts', SupportContract, SupportContractList, SupportContractView,
     SupportContractEdit, SupportContractDelete),
):
    name = model._meta.model_name
    urlpatterns.extend((
        path(f'{prefix}/', list_view.as_view(), name=f'{name}_list'),
        path(f'{prefix}/add/', edit.as_view(), name=f'{name}_add'),
        path(f'{prefix}/<int:pk>/', detail.as_view(), name=name),
        path(f'{prefix}/<int:pk>/edit/', edit.as_view(), name=f'{name}_edit'),
        path(f'{prefix}/<int:pk>/delete/', delete.as_view(), name=f'{name}_delete'),
        path(f'{prefix}/<int:pk>/changelog/', ObjectChangeLogView.as_view(), {'model': model}, name=f'{name}_changelog'),
    ))
