"""Copy the standard's preferred image to devices ahead of their upgrade window.

A prestage policy names a device model. On each run, every active device of
that model gets a staging job for the preferred version of the software
standard that applies to it (Lifecycle plugin), unless it already runs that
version, is marked do not upgrade, already has a queued or active job, or
was given the same image within the policy's interval. A new preferred
version is a new image, so the next run stages it without waiting out the
interval.

Staging jobs go through the ordinary upgrade queue: the owning poller claims
them with --apply, checks flash, copies and verifies the image, and never
installs or reloads. They carry no redundancy groups, because a copy takes
nothing out of service.
"""
import uuid
from datetime import timedelta

from dcim.models import Device
from django.apps import apps
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import DiscoveryPoller, PrestagePolicy, UpgradeJob
from .upgrade_choices import ACTIVE, WAITING
from .upgrade_queue import QueueError, address_of, owners, validate_profile

STAGE = 'stage'
SCHEDULE = 'schedule'
SKIP = 'skip'
CURRENT = 'current'


def free_space_floor(image):
    # The worker's own floors: an EOS .swi is mounted in place, everything else unpacks.
    return 100_000_000 if image.lower().endswith('.swi') else 1_500_000_000


def image_problem(version):
    """Why this version cannot be staged, or '' when it can."""
    if not version.image_filename:
        return f'{version} has no image filename'
    if version.checksum_type != 'md5' or not version.checksum:
        return f'{version} has no MD5 checksum'
    if not version.download_url:
        return f'{version} has no image URL'
    return ''


def stack_models(device):
    """Every model in the stack, since the worker checks each member's PID."""
    if not device.virtual_chassis_id:
        return [device.device_type.model]
    models = Device.objects.filter(virtual_chassis_id=device.virtual_chassis_id).values_list(
        'device_type__model', flat=True)
    return sorted(set(models) | {device.device_type.model})


def profile_for(policy, device, version):
    return {'name': f'prestage-{device.device_type.slug}-{version.version}'[:100],
            'models': stack_models(device), 'starting_versions': [],
            'target_version': version.version, 'image': version.image_filename,
            'md5': version.checksum.lower(),
            'minimum_free_bytes': policy.minimum_free_bytes or free_space_floor(version.image_filename),
            'image_source': version.download_url}


def candidates(policy):
    return (Device.objects.filter(device_type=policy.device_type, status='active')
            # One job per stack, on its master, as when scheduling by hand.
            .filter(Q(virtual_chassis__isnull=True) | Q(virtual_chassis__master=F('pk')))
            .select_related('device_type', 'platform', 'tenant', 'site__region', 'primary_ip4', 'primary_ip6',
                            'software__software_version')
            .prefetch_related('tags').order_by('name'))


def plan(policy, now=None, *, force=False):
    """One row per device: what the next run does with it, and why.

    Rows are dicts with device, action (schedule, current or skip), detail and,
    for scheduled devices, the version, poller name, address and profile.
    """
    now = now or timezone.now()
    if not apps.is_installed('netbox_refresh'):
        raise QueueError('Prestaging needs the Lifecycle (netbox_refresh) plugin for software standards.')
    from netbox_refresh.compliance import StandardResolver

    resolver = StandardResolver(on_date=timezone.localdate(now))
    busy = set(UpgradeJob.objects.filter(status__in=ACTIVE + WAITING).values_list('device_id', flat=True))
    since = now - timedelta(hours=policy.interval_hours)
    devices = list(candidates(policy))
    # Newest first, so the first match per device and image is the latest.
    recent_jobs = {}
    if not force:
        for job in (UpgradeJob.objects.filter(device__in=devices, operation__in=(STAGE, 'upgrade'),
                                              scheduled_at__gte=since)
                    .exclude(status='cancelled').order_by('-scheduled_at')):
            recent_jobs.setdefault((job.device_id, job.profile.get('image')), job)
    rows = []
    for device in devices:
        row = {'device': device, 'action': SKIP, 'detail': ''}
        rows.append(row)
        software = getattr(device, 'software', None)
        if software is not None and software.exempt:
            row['detail'] = 'Marked do not upgrade in Lifecycle'
            continue
        standard, conflict = resolver.resolve(device)
        if conflict:
            row['detail'] = 'Conflicting tag-scoped software standards'
            continue
        version = standard.preferred_version if standard is not None else None
        if version is None:
            row['detail'] = 'No software standard with a preferred version applies'
            continue
        row['version'] = version
        if software is not None and software.software_version_id == version.pk:
            row['action'], row['detail'] = CURRENT, f'Already running {version.version}'
            continue
        problem = image_problem(version)
        if problem:
            row['detail'] = problem
            continue
        if device.pk in busy:
            row['detail'] = 'Already has a queued or active upgrade job'
            continue
        recent = recent_jobs.get((device.pk, version.image_filename))
        if recent is not None:
            row['detail'] = f'{version.image_filename} was scheduled {recent.scheduled_at:%Y-%m-%d %H:%M} ({recent.get_status_display()})'
            row['reason'] = 'Given this image within the interval'
            row['recent_job'] = recent
            continue
        pollers = owners(device)
        if len(pollers) != 1:
            row['detail'] = 'No poller tag' if not pollers else f'More than one poller tag: {", ".join(sorted(pollers))}'
            row['reason'] = 'No unambiguous poller'
            continue
        poller_name = next(iter(pollers))
        poller = DiscoveryPoller.objects.filter(name=poller_name).first()
        if poller and poller.tenant_id and poller.tenant_id != device.tenant_id:
            row['detail'] = 'Poller tenant does not match the device'
            continue
        try:
            address = address_of(device)
            profile = profile_for(policy, device, version)
            validate_profile(profile, STAGE)
        except QueueError as exc:
            row['detail'] = str(exc)
            row['reason'] = str(exc).removeprefix(f'{device}: ')
            continue
        row.update(action=SCHEDULE, detail=f'Stage {version.image_filename}', poller_name=poller_name,
                   address=address, profile=profile)
    return rows


def summarize(rows):
    summary = {'scheduled': 0, 'current': 0, 'skipped': 0, 'reasons': {}}
    for row in rows:
        if row['action'] == SCHEDULE:
            summary['scheduled'] += 1
        elif row['action'] == CURRENT:
            summary['current'] += 1
        else:
            summary['skipped'] += 1
            reason = row.get('reason', row['detail'])
            summary['reasons'][reason] = summary['reasons'].get(reason, 0) + 1
    return summary


@transaction.atomic
def run_policy(policy, now=None, *, force=False):
    """Schedule a batch; manual runs bypass only the per-image repeat interval.

    New jobs' scheduled_at restarts that interval. Busy devices are still
    skipped, and their existing jobs and interval are never changed.
    """
    now = now or timezone.now()
    policy = PrestagePolicy.objects.select_for_update().get(pk=policy.pk)
    rows = plan(policy, now, force=force)
    batch = uuid.uuid4()
    jobs = []
    for row in rows:
        if row['action'] != SCHEDULE:
            continue
        poller, _ = DiscoveryPoller.objects.get_or_create(name=row['poller_name'])
        device = row['device']
        job = UpgradeJob(device=device, device_name=device.name, address=row['address'], poller=poller,
                         batch_id=batch, profile=row['profile'], operation=STAGE,
                         scheduled_at=now, start_before=now + timedelta(hours=policy.window_hours),
                         description=f'Prestage {row["version"].version} ({policy.device_type})'[:200])
        # No person requested it; the policy is recorded in the description.
        job.full_clean(exclude=['requested_by'])
        job.save()
        jobs.append(job)
    summary = summarize(rows)
    if jobs:
        summary['batch_id'] = str(batch)
    # A queryset update: a run is not a change to the policy and should not fill its changelog.
    PrestagePolicy.objects.filter(pk=policy.pk).update(last_run_at=now, last_summary=summary)
    return jobs, summary


def run(now=None, logger=None, *, force=False):
    """Every enabled policy; one policy's failure does not stop the others."""
    results = {}
    for policy in PrestagePolicy.objects.filter(enabled=True).select_related('device_type'):
        try:
            jobs, summary = run_policy(policy, now, force=force)
        except Exception as exc:
            summary = {'error': str(exc)}
            PrestagePolicy.objects.filter(pk=policy.pk).update(last_run_at=now or timezone.now(), last_summary=summary)
        results[str(policy.device_type)] = summary
        if logger is not None:
            logger(f'{policy.device_type}: {summary}')
    return results
