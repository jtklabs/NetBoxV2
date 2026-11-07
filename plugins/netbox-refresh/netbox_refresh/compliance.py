"""Deciding whether a device is running approved code.

Compliance here is set membership, never comparison: a device is compliant if
the version it is running is one of the versions its standard explicitly
approves. There is no "at or above" mode and no version-ordering code anywhere
in this plugin, which is a deliberate choice — Cisco IOS 15.2(4)E10, IOS-XE
16.12.05, NX-OS 9.3(10), PAN-OS 10.2.9-h1 and ArubaOS 8.10.0.10 do not sort
lexically, per-vendor comparators are where this kind of tooling usually goes
quietly wrong, and an estate normally has two or three blessed versions at once
anyway rather than a floor.

The one thing that must not happen is a device reading as compliant when we
simply do not know what it runs, so "no version collected" and "no standard
defined" are their own states rather than being folded into pass or fail.
"""

from collections import defaultdict
from datetime import date

from django.db.models import Q, prefetch_related_objects

from netbox_refresh.choices import ComplianceStatusChoices
from netbox_refresh.models import DeviceSoftware, SoftwareStandard

__all__ = (
    'StandardResolver',
    'standard_for_device',
    'evaluate',
    'device_compliance_rows',
)


def active_standards(on_date=None, queryset=None):
    """Standards in force on a given day."""
    on_date = on_date or date.today()
    queryset = SoftwareStandard.objects.all() if queryset is None else queryset
    return queryset.filter(valid_from__lte=on_date).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gte=on_date)
    )


class StandardResolver:
    """Resolve matching tagged scopes first, then fall back to the general scopes."""

    def __init__(self, on_date=None, queryset=None):
        self.on_date = on_date or date.today()
        self.queryset = SoftwareStandard.objects.all() if queryset is None else queryset
        self._by_tag = defaultdict(list)
        self._by_device_type = {}
        self._by_platform = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        standards = active_standards(
            self.on_date, self.queryset.select_related('preferred_version').prefetch_related(
                'approved_versions', 'device_types', 'platforms', 'device_tags',
            ),
        )
        for standard in standards:
            tags = list(standard.device_tags.all())
            if tags:
                types = {item.pk for item in standard.device_types.all()}
                platforms = {item.pk for item in standard.platforms.all()}
                priority = (bool(types), bool(platforms))
                for tag in tags:
                    self._by_tag[tag.pk].append((priority, types, platforms, standard))
                continue
            for device_type in standard.device_types.all():
                self._by_device_type[device_type.pk] = standard
            for platform in standard.platforms.all():
                self._by_platform[platform.pk] = standard
        self._loaded = True

    @property
    def uses_device_tags(self):
        self._load()
        return bool(self._by_tag)

    def resolve(self, device, tag_ids=None):
        """Return (standard, conflict). Conflicting tags suppress general recommendations."""
        if device is None:
            return None, False
        if self.uses_device_tags:
            if tag_ids is None:
                tag_ids = [tag.pk for tag in device.tags.all()]
            matches = {}
            for tag in tag_ids:
                for priority, types, platforms, standard in self._by_tag.get(tag, ()):
                    if types and device.device_type_id not in types:
                        continue
                    if platforms and device.platform_id not in platforms:
                        continue
                    matches[standard.pk] = (priority, standard)
            if matches:
                highest = max(priority for priority, _ in matches.values())
                winners = [standard for priority, standard in matches.values() if priority == highest]
                return (winners[0], False) if len(winners) == 1 else (None, True)
        standard = self._by_device_type.get(device.device_type_id) or self._by_platform.get(device.platform_id)
        return standard, False

    def for_device(self, device):
        return self.resolve(device)[0]


def resolve_standard(device, on_date=None):
    if device is None:
        return None, False
    tag_ids = [tag.pk for tag in device.tags.all()]
    general = Q(device_types=device.device_type_id)
    if device.platform_id:
        general |= Q(platforms=device.platform_id)
    candidates = SoftwareStandard.objects.filter(
        Q(device_tags__in=tag_ids) | (Q(device_tags__isnull=True) & general),
    ).distinct()
    return StandardResolver(on_date, candidates).resolve(device, tag_ids)


def standard_for_device(device, on_date=None):
    """The standard applying to one device. Prefer StandardResolver in bulk."""
    return resolve_standard(device, on_date)[0]


def evaluate(device_software, standard=None, resolver=None, *, conflict=False):
    """The compliance state of one DeviceSoftware row.

    Order matters. Exemption is checked first because an exempt device is
    excluded from the pass/fail question entirely — but it is still reported,
    as its own state, never dropped from the view.
    """
    if device_software is not None and device_software.exempt:
        if device_software.exemption_expired:
            return ComplianceStatusChoices.STATUS_EXEMPT_EXPIRED
        return ComplianceStatusChoices.STATUS_EXEMPT

    if standard is None and not conflict and device_software is not None:
        standard, conflict = (
            resolver.resolve(device_software.device) if resolver is not None
            else resolve_standard(device_software.device)
        )
    if conflict:
        return ComplianceStatusChoices.STATUS_CONFLICT
    if device_software is None or not device_software.software_version_id:
        return ComplianceStatusChoices.STATUS_UNKNOWN
    if standard is None:
        return ComplianceStatusChoices.STATUS_NO_STANDARD

    # .all() rather than .filter().exists() so a prefetched standard costs no
    # extra query — the report prefetches, the detail pages do not.
    approved = {version.pk for version in standard.approved_versions.all()}
    if device_software.software_version_id in approved:
        return ComplianceStatusChoices.STATUS_COMPLIANT
    return ComplianceStatusChoices.STATUS_NON_COMPLIANT


def device_compliance_rows(devices, on_date=None):
    """Compliance for a queryset of devices, including ones we know nothing about.

    Iterating devices rather than DeviceSoftware records is the whole point: a
    device with no software record at all is exactly the device most likely to
    be missed, and it has to appear as Unknown rather than vanish from the
    report. Returns dicts, one per device, ready for the report table.
    """
    resolver = StandardResolver(on_date)
    devices = list(devices)
    if resolver.uses_device_tags:
        prefetch_related_objects(devices, 'tags')

    records = DeviceSoftware.objects.filter(
        device__in=[d.pk for d in devices]
    ).select_related('software_version', 'software_version__platform')
    by_device = {record.device_id: record for record in records}

    rows = []
    for device in devices:
        record = by_device.get(device.pk)
        if record is not None:
            # The resolver needs the device object; reuse the one we already
            # have rather than letting the FK re-fetch it.
            record.device = device
        standard, conflict = resolver.resolve(device)
        status = evaluate(record, standard=standard, resolver=resolver, conflict=conflict)
        rows.append({
            'device': device,
            'record': record,
            'standard': standard,
            'status': status,
            'status_label': status_label(status),
            'status_color': ComplianceStatusChoices.colors.get(status),
            'version': record.version_label if record else '',
            'source': record.get_source_display() if record else '',
            'as_of': record.as_of if record else None,
            'is_stale': record.is_stale if record else False,
            'approved': ', '.join(
                str(v.version) for v in standard.approved_versions.all()
            ) if standard else '',
        })
    return rows


def summarise(rows):
    """Count rows per compliance state, in the order the choices declare them."""
    counts = {value: 0 for value, _label, _color in ComplianceStatusChoices.CHOICES}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    return [
        {
            'status': value,
            'label': label,
            'color': color,
            'count': counts.get(value, 0),
        }
        for value, label, color in ComplianceStatusChoices.CHOICES
    ]


def status_label(status):
    labels = {entry[0]: entry[1] for entry in ComplianceStatusChoices.CHOICES}
    return labels.get(status, status)
