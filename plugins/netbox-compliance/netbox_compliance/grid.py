"""One row per device, one column per check: the fleet at a glance.

The compliance report lists (device, standard) pairs, which answers "how many
devices are missing X". This answers the other question, "what is wrong with
this device", by pivoting the same verdicts into a row per device:

  Running code   the Lifecycle software verdict (netbox_refresh)
  Code staged    whether the standard's preferred image has been copied to
                 the device ahead of its upgrade (netbox_discovery staging jobs)
  <standard>     one column per configuration standard in scope

The two code columns appear only when their plugin is installed, so this
plugin keeps working on its own. Nothing here is stored: every cell is read
from the record that already holds the answer, and links to it.

A cell is None when the check does not apply to the device (the standard is
out of scope, or no software standard names a preferred version). A cell's
`ok` is True for a pass, False for something a person should act on, and None
for neither: not checked yet, or exempt. "Not checked" is never a pass.
"""

from django.apps import apps

from netbox_compliance.choices import ConfigComplianceStatusChoices as ConfigStatus
from netbox_compliance.scoping import StandardResolver, device_standard_rows

__all__ = ('build', 'SOFTWARE', 'STAGED')

SOFTWARE = 'software'
STAGED = 'staged'

CONFIG_OK = {
    ConfigStatus.STATUS_COMPLIANT: True,
    ConfigStatus.STATUS_NON_COMPLIANT: False,
    ConfigStatus.STATUS_ERROR: False,
    ConfigStatus.STATUS_EXEMPT_EXPIRED: False,
    ConfigStatus.STATUS_OUTDATED: False,
    ConfigStatus.STATUS_STALE: False,
}
SOFTWARE_OK = {'compliant': True, 'non-compliant': False, 'exempt-expired': False}


def cell(label, color, ok, url='', title=''):
    return {'label': label, 'color': color, 'ok': ok, 'url': url, 'title': title}


def software_cells(devices):
    """The Lifecycle verdict per device, the preferred version it points at, and what it runs."""
    from netbox_refresh.compliance import device_compliance_rows

    cells, preferred, running = {}, {}, {}
    for row in device_compliance_rows(devices):
        device, record, standard = row['device'], row['record'], row['standard']
        if record is not None:
            running[device.pk] = record.software_version_id
        if standard is not None and standard.preferred_version_id:
            preferred[device.pk] = standard.preferred_version
        # Lifecycle says Unknown for a device with no record before it looks for a
        # standard; with no standard there is nothing to measure, so it does not apply.
        if standard is None and row['status'] != 'standard-conflict':
            cells[device.pk] = None
            continue
        title = row['version'] or 'No version collected'
        if row['approved']:
            title += f' (approved: {row["approved"]})'
        url = record.get_absolute_url() if record is not None else ''
        cells[device.pk] = cell(row['status_label'], row['status_color'], SOFTWARE_OK.get(row['status']),
                                url, title)
    return cells, preferred, running


def staged_cells(devices, preferred, running):
    """Has the preferred image reached the device? From its latest staging or upgrade job."""
    from netbox_discovery.models import UpgradeJob

    latest = {}
    jobs = UpgradeJob.objects.filter(device__in=list(preferred), operation__in=('stage', 'upgrade'))
    for job in jobs.order_by('-scheduled_at', '-pk'):
        latest.setdefault((job.device_id, job.profile.get('image')), job)

    cells = {}
    for device in devices:
        version = preferred.get(device.pk)
        if version is None or not version.image_filename:
            cells[device.pk] = None
            continue
        if running.get(device.pk) == version.pk:
            cells[device.pk] = cell('Running', 'green', True, title=f'Already running {version.version}')
            continue
        job = latest.get((device.pk, version.image_filename))
        if job is None:
            cells[device.pk] = cell('Not staged', 'gray', None, title=f'No staging job for {version.image_filename}')
            continue
        title = f'{version.image_filename}: {job.message or job.get_status_display()}'
        if job.status in ('completed', 'completed_with_warnings'):
            state = cell('Staged', 'green', True)
        elif job.status in ('failed', 'recovery_required'):
            state = cell('Failed', 'red', False)
        elif job.status == 'expired':
            state = cell('Missed window', 'orange', False)
        elif job.status == 'cancelled':
            state = cell('Cancelled', 'gray', None)
        else:
            state = cell(job.get_status_display(), 'blue', None)
        state.update(url=job.get_absolute_url(), title=title)
        cells[device.pk] = state
    return cells


def config_cells(devices, standards=None, user=None):
    """Per device, the verdict for every configuration standard in scope, keyed by standard."""
    resolver = StandardResolver(standards=standards)
    cells, used = {}, {}
    for row in device_standard_rows(devices, standards=resolver.standards, user=user):
        standard, record = row['standard'], row['record']
        used[standard.pk] = standard
        title = f'{row["findings"]} finding(s)' if row['findings'] else ''
        if row['last_checked']:
            title = (title + '; ' if title else '') + f'checked {row["last_checked"]:%Y-%m-%d}'
        if row['is_stale']:
            title += ' (stale)'
        title += f'; checked v{row["checked_revision"] or "?"}, current v{row["current_revision"]}'
        if row['inheritance_note']:
            title += f'; {row["inheritance_note"]}'
        url = record.get_absolute_url() if record is not None else standard.get_absolute_url()
        cells[(row['device'].pk, standard.pk)] = cell(row['status_label'], row['status_color'],
                                                      CONFIG_OK.get(row['status']), url, title)
    return cells, sorted(used.values(), key=lambda standard: standard.name)


def build(devices, standards=None, code_columns=True, user=None):
    """Columns and one row per device. Rows are dicts: device, cells (in column order), problems."""
    devices = list(devices)
    columns, lookups = [], []
    if code_columns and apps.is_installed('netbox_refresh'):
        software, preferred, running = software_cells(devices)
        columns.append({'key': SOFTWARE, 'label': 'Running code'})
        lookups.append(lambda device, cells=software: cells.get(device.pk))
        if apps.is_installed('netbox_discovery'):
            staged = staged_cells(devices, preferred, running)
            columns.append({'key': STAGED, 'label': 'Code staged'})
            lookups.append(lambda device, cells=staged: cells.get(device.pk))
    config, used = config_cells(devices, standards, user=user)
    for standard in used:
        columns.append({'key': f'standard-{standard.pk}', 'label': standard.name, 'standard': standard})
        lookups.append(lambda device, pk=standard.pk: config.get((device.pk, pk)))

    rows = []
    for device in devices:
        cells = [lookup(device) for lookup in lookups]
        rows.append({'device': device, 'cells': cells,
                     'problems': sum(1 for item in cells if item is not None and item['ok'] is False)})

    for index, column in enumerate(columns):
        values = [row['cells'][index] for row in rows if row['cells'][index] is not None]
        column['passed'] = sum(1 for item in values if item['ok'] is True)
        column['failed'] = sum(1 for item in values if item['ok'] is False)
        column['other'] = len(values) - column['passed'] - column['failed']
    return columns, rows
