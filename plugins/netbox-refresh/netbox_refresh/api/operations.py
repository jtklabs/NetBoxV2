"""Collection reports, read-only evidence, readiness and editable action planning."""

from dcim.models import Device
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils import timezone
from netbox.api.serializers import NetBoxModelSerializer
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from netbox_refresh.compliance import StandardResolver
from netbox_refresh.models import (
    DeviceCollectionState,
    DeviceInventoryChange,
    DeviceObservation,
    DeviceRelationship,
    LifecycleAction,
    SoftwareStandard,
    SoftwareVersion,
    DeviceSoftware,
)
from netbox_refresh.operations import (
    COMPONENTS,
    COMPONENT_STATUSES,
    FACT_FIELDS,
    READINESS_CHECKS,
    ingest_observation,
    readiness_for_device,
    refresh_actions,
)


def require_permissions(user, *permissions):
    if not user.is_authenticated or not all(user.has_perm(p) for p in permissions):
        raise PermissionDenied


def scoped_devices(user, params):
    devices = Device.objects.restrict(user, "view").select_related(
        "site", "platform", "device_type"
    )
    for name, field in (
        ("device", "pk"),
        ("name", "name"),
        ("site", "site_id"),
        ("platform", "platform_id"),
        ("device_type", "device_type_id"),
        ("tag", "tags__slug"),
        ("poller", "collection_state__poller"),
    ):
        if params.get(name):
            if (
                field in ("pk", "site_id", "platform_id", "device_type_id")
                and not str(params[name]).isdecimal()
            ):
                raise serializers.ValidationError({name: "Use a positive numeric ID"})
            devices = devices.filter(**{field: params[name]})
    return devices.distinct()


class ComponentSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=sorted(COMPONENT_STATUSES))
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class CheckSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=("passed", "failed", "unknown", "not_applicable")
    )
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class RelationshipSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=("lldp", "cdp", "ha", "mlag"))
    peer_name = serializers.CharField(max_length=255)
    local_interface = serializers.CharField(
        max_length=100, required=False, allow_blank=True
    )
    peer_interface = serializers.CharField(
        max_length=100, required=False, allow_blank=True
    )
    peer_address = serializers.CharField(
        max_length=64, required=False, allow_blank=True
    )
    chassis_id = serializers.CharField(max_length=128, required=False, allow_blank=True)
    group = serializers.CharField(max_length=200, required=False, allow_blank=True)
    role = serializers.CharField(max_length=40, required=False, allow_blank=True)
    state = serializers.CharField(max_length=80, required=False, allow_blank=True)


class ObservationReportSerializer(serializers.Serializer):
    device = serializers.IntegerField(min_value=1)
    job_id = serializers.CharField(max_length=100)
    poller = serializers.CharField(max_length=80)
    executor_version = serializers.CharField(
        max_length=50, required=False, allow_blank=True
    )
    observed_at = serializers.DateTimeField(default=timezone.now)
    components = serializers.DictField(child=ComponentSerializer(), allow_empty=False)
    field_sources = serializers.DictField(
        child=serializers.ChoiceField(
            choices=("observed", "inferred", "manual", "netbox-existing")
        ),
        required=False,
    )
    facts = serializers.DictField(required=False)
    readiness = serializers.DictField(child=CheckSerializer(), required=False)
    relationships = RelationshipSerializer(many=True, required=False, max_length=2000)

    def validate(self, attrs):
        for key, allowed in (
            ("components", COMPONENTS),
            ("facts", FACT_FIELDS),
            ("field_sources", FACT_FIELDS),
            ("readiness", READINESS_CHECKS),
        ):
            if set(attrs.get(key, {})) - allowed:
                raise serializers.ValidationError(
                    {
                        key: "Unrecognized fields; raw output and credentials are not accepted."
                    }
                )
        for key, value in attrs.get("facts", {}).items():
            if key == "license_products":
                if (
                    not isinstance(value, list)
                    or len(value) > 100
                    or any(not isinstance(v, str) or len(v) > 200 for v in value)
                ):
                    raise serializers.ValidationError(
                        {"facts": "license_products must be a list of product names."}
                    )
            elif not isinstance(value, str) or len(value) > 500:
                raise serializers.ValidationError(
                    {"facts": "Fact values must be strings of at most 500 characters."}
                )
        if attrs["observed_at"] > timezone.now() + timezone.timedelta(minutes=5):
            raise serializers.ValidationError(
                {"observed_at": "Timestamp is in the future."}
            )
        return attrs


def evidence_serializer(model):
    class EvidenceSerializer(NetBoxModelSerializer):
        class Meta:
            fields = "__all__"

    EvidenceSerializer.Meta.model = model
    return EvidenceSerializer


class EvidenceViewSet(NetBoxModelViewSet):
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .filter(device__in=Device.objects.restrict(self.request.user, "view"))
        )
        for name in ("device_id", "poller", "status", "kind", "present", "field"):
            if name in self.request.query_params and name in {
                f.name for f in qs.model._meta.fields
            } | {"device_id"}:
                value = self.request.query_params[name]
                if name == "present":
                    value = value.lower() in ("true", "1")
                qs = qs.filter(**{name: value})
        if (
            "field_sources" in {f.name for f in qs.model._meta.fields}
            and "manual" in self.request.query_params
        ):
            condition = Q(pk__in=[])
            for field in FACT_FIELDS:
                condition |= Q(**{f"field_sources__{field}": "manual"})
            qs = (
                qs.filter(condition)
                if self.request.query_params["manual"].lower() in ("true", "1")
                else qs.exclude(condition)
            )
        return qs


class DeviceCollectionStateViewSet(EvidenceViewSet):
    queryset = DeviceCollectionState.objects.all()
    serializer_class = evidence_serializer(DeviceCollectionState)


class DeviceObservationViewSet(EvidenceViewSet):
    queryset = DeviceObservation.objects.all()
    serializer_class = evidence_serializer(DeviceObservation)

    @action(detail=False, methods=["post"])
    def report(self, request):
        require_permissions(
            request.user,
            "dcim.change_device",
            "netbox_refresh.add_deviceobservation",
            "netbox_refresh.add_devicecollectionstate",
            "netbox_refresh.change_devicecollectionstate",
            "netbox_refresh.add_deviceinventorychange",
            "netbox_refresh.add_devicerelationship",
            "netbox_refresh.change_devicerelationship",
        )
        serializer = ObservationReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device = (
            Device.objects.restrict(request.user, "change")
            .filter(pk=serializer.validated_data["device"])
            .first()
        )
        if device is None:
            raise PermissionDenied
        observation, created = ingest_observation(device, serializer.validated_data)
        return Response(
            {
                "id": observation.pk,
                "created": created,
                "status": observation.status,
                "changes": list(observation.changes.values("field", "before", "after")),
            },
            status=201 if created else 200,
        )

    # POST is limited to the validated report action; generic create remains disabled.
    http_method_names = ["get", "post", "head", "options"]

    def create(self, request, *args, **kwargs):
        from rest_framework.exceptions import MethodNotAllowed

        raise MethodNotAllowed("POST")


class DeviceInventoryChangeViewSet(EvidenceViewSet):
    queryset = DeviceInventoryChange.objects.all()
    serializer_class = evidence_serializer(DeviceInventoryChange)


class DeviceRelationshipViewSet(EvidenceViewSet):
    queryset = DeviceRelationship.objects.all()
    serializer_class = evidence_serializer(DeviceRelationship)


class LifecycleActionSerializer(NetBoxModelSerializer):
    effective_due_date = serializers.DateField(read_only=True)

    class Meta:
        model = LifecycleAction
        fields = "__all__"
        read_only_fields = tuple(
            f.name
            for f in LifecycleAction._meta.fields
            if f.name not in ("owner", "due_override", "status")
        ) + (
            "custom_fields",
            "tags",
        )


class LifecycleActionViewSet(NetBoxModelViewSet):
    queryset = LifecycleAction.objects.all()
    serializer_class = LifecycleActionSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .filter(device__in=Device.objects.restrict(self.request.user, "view"))
        )
        for name in (
            "device_id",
            "status",
            "kind",
            "owner_id",
            "priority",
            "reminder_stage",
        ):
            if self.request.query_params.get(name):
                qs = qs.filter(**{name: self.request.query_params[name]})
        if "condition_active" in self.request.query_params:
            qs = qs.filter(
                condition_active=self.request.query_params["condition_active"].lower()
                in ("1", "true")
            )
        return qs


class ReadinessView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_permissions(
            request.user,
            "dcim.view_device",
            "netbox_refresh.view_devicecollectionstate",
            "netbox_refresh.view_devicesoftware",
            "netbox_refresh.view_softwarestandard",
            "netbox_refresh.view_softwareversion",
        )
        devices = scoped_devices(request.user, request.query_params)
        # Refuse to certify evidence the caller cannot see.
        states = {
            s.device_id: s
            for s in DeviceCollectionState.objects.restrict(
                request.user, "view"
            ).filter(device__in=devices)
        }
        software = {
            s.device_id: s
            for s in DeviceSoftware.objects.restrict(request.user, "view").filter(
                device__in=devices
            )
        }
        # Resolve with all rules first: a hidden specific rule must not silently
        # fall back to a different visible general standard.
        resolver = StandardResolver()
        visible_standards = set(
            SoftwareStandard.objects.restrict(request.user, "view").values_list(
                "pk", flat=True
            )
        )
        visible_versions = set(
            SoftwareVersion.objects.restrict(request.user, "view").values_list(
                "pk", flat=True
            )
        )
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
            limit = min(500, max(1, int(request.query_params.get("limit", 100))))
        except ValueError:
            raise serializers.ValidationError("offset and limit must be integers")
        rows = []
        candidates = devices.order_by("pk").prefetch_related("tags")
        if not request.query_params.get("status"):
            candidates = candidates[offset : offset + limit]
        for device in candidates:
            row = readiness_for_device(
                device,
                resolver,
                states.get(device.pk, False),
                software.get(device.pk, False),
            )
            if row["standard"] and row["standard"] not in visible_standards:
                row.update(
                    standard=None,
                    standard_found=None,
                    status="unknown",
                    compliance="unknown",
                    approved_versions=[],
                    preferred_version=None,
                    reasons=["Applicable standard is not visible"],
                )
            row["approved_versions"] = [
                v for v in row["approved_versions"] if v["id"] in visible_versions
            ]
            if (
                row["preferred_version"]
                and row["preferred_version"]["id"] not in visible_versions
            ):
                row["preferred_version"] = None
                row["status"] = "unknown"
                row["reasons"] = ["Preferred version is not visible"]
            rows.append(row)
        if request.query_params.get("status"):
            rows = [r for r in rows if r["status"] == request.query_params["status"]]
            count = len(rows)
            rows = rows[offset : offset + limit]
        else:
            count = devices.count()
        return Response(
            {
                "count": count,
                "offset": offset,
                "limit": limit,
                "results": rows,
                "next_offset": offset + limit if offset + limit < count else None,
                "previous_offset": max(0, offset - limit) if offset else None,
            }
        )


class ActionRefreshView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        require_permissions(
            request.user,
            "netbox_refresh.add_lifecycleaction",
            "netbox_refresh.change_lifecycleaction",
        )
        # Refresh reads all source records, so require an administrator. Scheduled jobs run with system scope.
        if not request.user.is_superuser:
            raise PermissionDenied
        return Response(refresh_actions(scoped_devices(request.user, request.data)))
