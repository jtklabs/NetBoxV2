"""Populate ModelLifecycle records from the Cisco EoX API.

Manually-maintained records are never overwritten: a record whose source is
`manual` is skipped entirely unless the caller forces it. That keeps hand-entered
data for non-Cisco kit (and hand-corrected Cisco data) safe from the sync.
"""

import logging
from os import environ

from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.utils import timezone

from netbox_refresh.choices import LifecycleSourceChoices
from netbox_refresh.cisco import CiscoEoxClient, CiscoEoxError, batch_product_ids
from netbox_refresh.models import LIFECYCLE_DATE_FIELDS, ModelLifecycle

logger = logging.getLogger('netbox.plugins.netbox_refresh')

# One list for the status logic, the forms and the sync — see models.py.
DATE_FIELDS = LIFECYCLE_DATE_FIELDS


def get_settings():
    return settings.PLUGINS_CONFIG.get('netbox_refresh', {})


def get_credentials():
    config = get_settings()
    return (
        config.get('cisco_client_id') or environ.get('CISCO_CLIENT_ID', ''),
        config.get('cisco_client_secret') or environ.get('CISCO_CLIENT_SECRET', ''),
    )


def pid_for(obj):
    """The Cisco product ID to look up for a device or module type.

    part_number when it is filled in; otherwise the model name. The SNMP
    scanner creates types verbatim from entPhysicalModelName — which for
    Cisco IS the PID ("WS-C3650-48PS-L", "C9300-48P") — and leaves
    part_number empty, so requiring part_number looked up nothing at all.
    """
    return (obj.part_number or obj.model or '').strip()


def _manufacturer_query(names):
    """Match the configured manufacturer names loosely.

    Case-insensitive, and "Cisco" also matches "Cisco Systems" and "Cisco
    Systems, Inc." — the scanner takes the name from entPhysicalMfgName, and
    Cisco gear spells itself all three ways across platforms.
    """
    from django.db.models import Q

    query = Q()
    for name in names:
        name = (name or '').strip()
        if not name:
            continue
        query |= Q(manufacturer__name__iexact=name) | Q(manufacturer__name__istartswith=name + ' ')
    return query


def candidate_types():
    """Device/module types whose PID we can look up — see pid_for()."""
    from dcim.models import DeviceType, ModuleType

    manufacturers = get_settings().get('cisco_manufacturers') or ['Cisco']
    query = _manufacturer_query(manufacturers)
    found = []
    for model in (DeviceType, ModuleType):
        for obj in model.objects.filter(query).select_related('manufacturer'):
            if pid_for(obj):
                found.append(obj)
    return found


def _resolve_replacement(obj, pid):
    """Find the DeviceType/ModuleType matching a Cisco migration PID, if we stock it.

    By part_number or by model, for the same reason pid_for() accepts either.
    """
    from django.db.models import Q

    if not pid:
        return None
    model = type(obj)
    return model.objects.filter(
        Q(part_number__iexact=pid) | Q(model__iexact=pid)
    ).order_by('pk').first()


def sync(dry_run=False, force=False, limit=None, logger_fn=None):
    """Sync EoL data for every Cisco device/module type. Returns a summary dict."""
    emit = logger_fn or (lambda msg: logger.info('netbox_refresh: %s', msg))

    client_id, client_secret = get_credentials()
    client = CiscoEoxClient(client_id, client_secret)

    targets = candidate_types()
    if limit:
        targets = targets[:limit]
    names = get_settings().get('cisco_manufacturers') or ['Cisco']
    emit('%d Cisco device/module types to look up (manufacturer %s; PID from '
         'part_number, else model)' % (len(targets), ', '.join(names)))
    if not targets:
        emit('Nothing to do: no device or module type has a manufacturer matching '
             '%s. Check the manufacturer name on your Cisco device types, or set '
             'cisco_manufacturers in the plugin settings.' % ', '.join(names))

    # Several types can share a PID; look each PID up once.
    by_pid = {}
    for obj in targets:
        by_pid.setdefault(pid_for(obj).upper(), []).append(obj)
    pids = sorted(by_pid)

    summary = {'types': len(targets), 'pids': len(pids), 'updated': 0, 'created': 0,
               'no_data': 0, 'skipped_manual': 0, 'replacements_linked': 0, 'errors': 0}

    done = 0
    for batch in batch_product_ids(pids):
        emit('looking up PIDs %d-%d of %d' % (done + 1, done + len(batch), len(pids)))
        done += len(batch)
        try:
            results = client.fetch(batch)
        except CiscoEoxError as exc:
            summary['errors'] += len(batch)
            emit('batch failed (%s) — leaving these records untouched' % exc)
            continue

        for pid in batch:
            data = results.get(pid)
            if data is None:
                # Cisco answered "no EoL data" — which for current hardware
                # means nothing is announced. That is a finding, not a miss:
                # record that the model was checked today so it reads as
                # "EoL not announced" rather than "unknown", and so the
                # check date is there to re-check from.
                summary['no_data'] += 1
                for obj in by_pid[pid]:
                    outcome = _record_checked(obj, dry_run=dry_run, force=force)
                    if outcome != 'skipped_manual':
                        summary['not_announced'] = summary.get('not_announced', 0) + 1
                continue
            for obj in by_pid[pid]:
                outcome = _apply(obj, data, dry_run=dry_run, force=force)
                summary[outcome] = summary.get(outcome, 0) + 1
                if outcome != 'skipped_manual' and data.get('replacement_pid'):
                    if _resolve_replacement(obj, data['replacement_pid']):
                        summary['replacements_linked'] += 1

    return summary


def _record_checked(obj, dry_run=False, force=False):
    """Stamp a model as checked today when Cisco had nothing to announce.

    Dates already on the record are left alone: a no-data answer for a
    model that previously had dates is a feed oddity to look at, not a
    reason to erase what the vendor once published. Manual records are
    respected exactly as _apply respects them.
    """
    content_type = ContentType.objects.get_for_model(obj)
    record = ModelLifecycle.objects.filter(
        assigned_object_type=content_type, assigned_object_id=obj.pk
    ).first()
    if record and record.source == LifecycleSourceChoices.SOURCE_MANUAL and not force:
        return 'skipped_manual'
    created = record is None
    if created:
        record = ModelLifecycle(
            assigned_object_type=content_type, assigned_object_id=obj.pk,
            source=LifecycleSourceChoices.SOURCE_CISCO,
        )
    record.last_synced = timezone.now()
    record.last_checked = timezone.localdate()
    if not dry_run:
        record.full_clean(exclude=['assigned_object_type', 'assigned_object_id'])
        record.save()
    return 'created' if created else 'updated'


def _apply(obj, data, dry_run=False, force=False):
    content_type = ContentType.objects.get_for_model(obj)
    record = ModelLifecycle.objects.filter(
        assigned_object_type=content_type, assigned_object_id=obj.pk
    ).first()

    if record and record.source == LifecycleSourceChoices.SOURCE_MANUAL and not force:
        return 'skipped_manual'

    created = record is None
    if created:
        record = ModelLifecycle(assigned_object_type=content_type, assigned_object_id=obj.pk)

    for field in DATE_FIELDS:
        setattr(record, field, data.get(field))
    record.bulletin_number = data.get('bulletin_number') or ''
    record.bulletin_url = data.get('bulletin_url') or ''
    record.source = LifecycleSourceChoices.SOURCE_CISCO
    record.last_synced = timezone.now()
    record.last_checked = timezone.localdate()

    replacement = _resolve_replacement(obj, data.get('replacement_pid'))
    if replacement is not None:
        if obj._meta.model_name == 'devicetype':
            record.replacement_device_type = replacement
        else:
            record.replacement_module_type = replacement
    # Keep the vendor's prose either way — it explains cases where no single
    # successor model exists, and names a PID we do not stock yet.
    notes = data.get('replacement_notes') or ''
    if data.get('replacement_pid') and replacement is None:
        notes = ('Cisco migration PID %s (not in NetBox). %s'
                 % (data['replacement_pid'], notes)).strip()
    record.replacement_notes = notes[:2000]

    if not dry_run:
        record.full_clean(exclude=['assigned_object_type', 'assigned_object_id'])
        record.save()
    return 'created' if created else 'updated'
