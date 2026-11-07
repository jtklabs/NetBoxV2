"""REST API for the Lifecycle plugin.

The interesting part here is `DeviceSoftwareViewSet.report`, the endpoint the
SNMP inventory collector pushes running-version readings to. Everything else is
standard NetBox CRUD.
"""

from dcim.models import Device, Platform
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from netbox.api.viewsets import NetBoxModelViewSet
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from netbox_refresh import filtersets
from netbox_refresh.api.serializers import (
    ReplacementPriceSerializer,
    DeviceSoftwareSerializer,
    ModelLifecycleSerializer,
    SoftwareReportSerializer,
    SoftwareStandardSerializer,
    SoftwareVersionSerializer,
)
from netbox_refresh.models import (
    DeviceSoftware,
    ModelLifecycle,
    ReplacementPrice,
    SoftwareStandard,
    SoftwareVersion,
)

__all__ = (
    'ModelLifecycleViewSet',
    'SoftwareVersionViewSet',
    'SoftwareStandardViewSet',
    'DeviceSoftwareViewSet',
)


class ModelLifecycleViewSet(NetBoxModelViewSet):
    queryset = ModelLifecycle.objects.prefetch_related(
        'assigned_object_type', 'replacement_device_type', 'replacement_module_type', 'tags'
    )
    serializer_class = ModelLifecycleSerializer
    filterset_class = filtersets.ModelLifecycleFilterSet


class ReplacementPriceViewSet(NetBoxModelViewSet):
    queryset = ReplacementPrice.objects.select_related(
        'device_type', 'module_type', 'region', 'site'
    ).prefetch_related('tags')
    serializer_class = ReplacementPriceSerializer
    filterset_class = filtersets.ReplacementPriceFilterSet


class SoftwareVersionViewSet(NetBoxModelViewSet):
    queryset = SoftwareVersion.objects.select_related('platform').prefetch_related('tags')
    serializer_class = SoftwareVersionSerializer
    filterset_class = filtersets.SoftwareVersionFilterSet


class SoftwareStandardViewSet(NetBoxModelViewSet):
    queryset = SoftwareStandard.objects.prefetch_related(
        'device_types', 'platforms', 'approved_versions', 'preferred_version', 'tags'
    )
    serializer_class = SoftwareStandardSerializer
    filterset_class = filtersets.SoftwareStandardFilterSet


class DeviceSoftwareViewSet(NetBoxModelViewSet):
    queryset = DeviceSoftware.objects.select_related(
        'device', 'software_version', 'software_version__platform'
    ).prefetch_related('tags')
    serializer_class = DeviceSoftwareSerializer
    filterset_class = filtersets.DeviceSoftwareFilterSet

    @action(detail=False, methods=['post'], url_path='report')
    def report(self, request):
        """Idempotent upsert of running-version readings, one device per item.

        Accepts a single object or a list, so a fleet sweep is one call.

        Why this exists rather than "just PATCH the record": a collector would
        otherwise have to look up whether a record exists, look up or create the
        SoftwareVersion for the platform, and diff the version itself to avoid
        writing on every scan. That last part matters more than it looks —
        DeviceSoftware is a NetBoxModel, so every save() writes an ObjectChange.
        A collector re-reporting the same version nightly would produce a
        changelog entry per device per night and bury the changes that matter.

        So: an unchanged version bumps last_checked with a queryset update,
        which bypasses signals and writes NO changelog entry; a changed version
        goes through a real save() and is fully changelogged, which is exactly
        the "catch changes submitted via the API" requirement.

        Each item is resolved independently and reported on independently — one
        unknown device in a batch of 500 does not fail the other 499.
        """
        if not request.user.has_perm('netbox_refresh.add_devicesoftware') or \
                not request.user.has_perm('netbox_refresh.change_devicesoftware'):
            return Response(
                {'detail': 'This token needs add and change permission on device software.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        payload = request.data
        many = isinstance(payload, list)
        serializer = SoftwareReportSerializer(data=payload, many=many)
        serializer.is_valid(raise_exception=True)
        items = serializer.validated_data if many else [serializer.validated_data]

        results = [self._apply_report(item) for item in items]
        summary = {}
        for entry in results:
            summary[entry['result']] = summary.get(entry['result'], 0) + 1

        return Response({'summary': summary, 'results': results}, status=status.HTTP_200_OK)

    # ------------------------------------------------------------------ #
    @transaction.atomic
    def _apply_report(self, item):
        device = self._resolve_device(item)
        if device is None:
            return {
                'device': item.get('device') or item.get('device_name'),
                'result': 'error',
                'detail': 'No such device in NetBox.',
            }

        raw_version = (item.get('version') or '').strip()
        now = timezone.now()
        collected_at = item.get('collected_at') or now
        record = DeviceSoftware.objects.filter(device=device).first()
        if record and record.collected_at and collected_at < record.collected_at:
            return {
                'device': device.name or device.pk,
                'id': record.pk,
                'version': record.raw_version or None,
                'result': 'stale',
            }

        software_version = None
        if raw_version:
            platform = self._resolve_platform(item, device)
            if platform is None:
                return {
                    'device': device.name or device.pk,
                    'result': 'error',
                    'detail': (
                        'Cannot record a version without a platform: %s is not a known '
                        'platform and the device has none set.'
                        % (item.get('platform') or '(none given)')
                    ),
                }
            software_version, _created = SoftwareVersion.objects.get_or_create(
                platform=platform, version=raw_version,
            )

        if record is None:
            record = DeviceSoftware(device=device)

        unchanged = (
            record.pk is not None
            and record.software_version_id == (software_version.pk if software_version else None)
            and record.raw_version == raw_version
        )

        if unchanged:
            # No changelog entry on purpose — nothing about the device changed,
            # we only confirmed it again.
            DeviceSoftware.objects.filter(pk=record.pk).update(
                last_checked=now, collected_at=collected_at,
            )
            result = 'unchanged'
        else:
            was_new = record.pk is None
            previous = record.raw_version
            record.software_version = software_version
            record.raw_version = raw_version
            record.raw_report = item.get('raw') or ''
            record.source = item['source']
            record.collected_at = collected_at
            record.last_checked = now
            record.save()
            result = 'created' if was_new else 'updated'
            if not was_new and previous and previous != raw_version:
                result = 'changed'

        return {
            'device': device.name or device.pk,
            'id': record.pk,
            'version': raw_version or None,
            'result': result,
        }

    def _resolve_device(self, item):
        # Serialize reports for the same device, including its first reading
        # when there is no DeviceSoftware row to lock yet.
        devices = Device.objects.select_for_update()
        if item.get('device'):
            return devices.filter(pk=item['device']).first()
        name = (item.get('device_name') or '').strip()
        if not name:
            return None
        # Device names are not unique across sites in NetBox, so an ambiguous
        # name is treated as unresolvable rather than silently picking one.
        matches = list(devices.filter(name__iexact=name)[:2])
        return matches[0] if len(matches) == 1 else None

    def _resolve_platform(self, item, device):
        """Never auto-creates: a typo would otherwise become a real platform."""
        name = (item.get('platform') or '').strip()
        if name:
            return Platform.objects.filter(Q(name__iexact=name) | Q(slug__iexact=name)).first()
        return device.platform
