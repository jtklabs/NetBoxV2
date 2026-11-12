"""Ingest collection evidence and derive actionable lifecycle work."""

from datetime import timedelta

from dcim.models import Device, DeviceType
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from netbox_refresh.compliance import StandardResolver
from netbox_refresh.models import (
    DeviceCollectionState,
    DeviceInventoryChange,
    DeviceObservation,
    DeviceRelationship,
    DeviceSoftware,
    LifecycleAction,
    LicenseAssignment,
    ModelLifecycle,
    SupportContract,
)

FACT_FIELDS = {
    "hostname",
    "serial",
    "model",
    "platform",
    "software_version",
    "management_ip",
    "management_interface",
    "prefix_source",
    "interface_source",
    "license_products",
    "license_expiration",
    "ha_state",
}
COMPONENTS = {
    "identity",
    "address",
    "software",
    "licenses",
    "neighbors",
    "ha",
    "readiness",
    "netbox",
}
COMPONENT_STATUSES = {"success", "partial", "failed", "skipped", "unsupported"}
READINESS_CHECKS = {"ssh", "storage", "backup", "ha", "trust", "credentials"}


def collection_status(components):
    statuses = [v["status"] for v in components.values()]
    if "failed" in statuses:
        return "partial" if "success" in statuses else "failed"
    if "partial" in statuses:
        return "partial"
    return "success" if "success" in statuses else "unknown"


@transaction.atomic
def ingest_observation(device, data):
    # A device lock serializes competing pollers and their first snapshot.
    device = Device.objects.select_for_update().get(pk=device.pk)
    existing = DeviceObservation.objects.filter(
        device=device, poller=data["poller"], job_id=data["job_id"]
    ).first()
    if existing:
        return existing, False
    components = data.get("components", {})
    status = collection_status(components)
    observation = DeviceObservation.objects.create(
        device=device,
        job_id=data["job_id"],
        poller=data["poller"],
        executor_version=data.get("executor_version", ""),
        observed_at=data["observed_at"],
        status=status,
        components=components,
        facts=data.get("facts", {}),
        field_sources=data.get("field_sources", {}),
        readiness=data.get("readiness", {}),
    )
    current = DeviceCollectionState.objects.filter(device=device).first()
    if current and current.last_attempt > observation.observed_at:
        return observation, True
    previous_facts = current.facts if current else {}
    # Missing fields mean "not observed", never "delete the previous value".
    facts = {**previous_facts, **observation.facts}
    if current:
        for field, value in observation.facts.items():
            if field in previous_facts and previous_facts[field] != value:
                DeviceInventoryChange.objects.create(
                    device=device,
                    observation=observation,
                    field=field,
                    before=previous_facts[field],
                    after=value,
                    observed_at=observation.observed_at,
                    poller=observation.poller,
                )
    component_history = dict(current.components) if current else {}
    for key, value in components.items():
        old = component_history.get(key, {})
        component_history[key] = {
            **value,
            "last_attempt": observation.observed_at.isoformat(),
            "last_success": observation.observed_at.isoformat()
            if value["status"] == "success"
            else old.get("last_success"),
        }
    last_success = (
        observation.observed_at
        if status == "success"
        else (current.last_success if current else None)
    )
    DeviceCollectionState.objects.update_or_create(
        device=device,
        defaults={
            "poller": observation.poller,
            "executor_version": observation.executor_version,
            "last_attempt": observation.observed_at,
            "last_success": last_success,
            "status": status,
            "components": component_history,
            "facts": facts,
            "field_sources": {
                **(current.field_sources if current else {}),
                **observation.field_sources,
            },
            # A fresh attempt replaces readiness: old successful checks must not certify a new poll.
            "readiness": observation.readiness,
        },
    )
    seen = []
    for item in data.get("relationships", []):
        peers = Device.objects.filter(
            name__iexact=item["peer_name"], site=device.site
        ).exclude(pk=device.pk)
        peer = peers.first() if peers.count() == 1 else None
        identity = {
            key: item.get(key, "")
            for key in ("kind", "local_interface", "peer_name", "peer_interface")
        }
        relationship, _ = DeviceRelationship.objects.update_or_create(
            device=device,
            poller=observation.poller,
            **identity,
            defaults={
                **{
                    key: item.get(key, "")
                    for key in ("peer_address", "chassis_id", "group", "role", "state")
                },
                "peer_device": peer,
                "last_seen": observation.observed_at,
                "present": True,
            },
        )
        seen.append(relationship.pk)
        if peer and item["kind"] in ("lldp", "cdp"):
            reverse = DeviceRelationship.objects.filter(
                device=peer,
                peer_device=device,
                present=True,
                kind__in=("lldp", "cdp"),
                local_interface=item.get("peer_interface", ""),
                peer_interface=item.get("local_interface", ""),
            )
            relationship.confirmed = reverse.exists()
            relationship.save(update_fields=["confirmed"])
            reverse.update(confirmed=True)
    for component, kinds in (("neighbors", ("lldp", "cdp")), ("ha", ("ha", "mlag"))):
        if (
            components.get(component, {}).get("status") == "success"
            and "relationships" in data
        ):
            stale = DeviceRelationship.objects.filter(
                device=device, poller=observation.poller, kind__in=kinds
            ).exclude(pk__in=seen)
            # Remove reciprocal confirmation too when a previously seen link disappears.
            for rel in stale.filter(confirmed=True):
                DeviceRelationship.objects.filter(
                    device_id=rel.peer_device_id,
                    peer_device=device,
                    local_interface=rel.peer_interface,
                    peer_interface=rel.local_interface,
                ).update(confirmed=False)
            stale.update(present=False, confirmed=False)
    return observation, True


def readiness_for_device(device, resolver=None, state=None, software=None):
    standard, conflict = (resolver or StandardResolver()).resolve(device)
    target = standard.preferred_version if standard else None
    if software is None:
        software = (
            DeviceSoftware.objects.filter(device=device)
            .select_related("software_version")
            .first()
        )
    if state is None:
        state = DeviceCollectionState.objects.filter(device=device).first()
    reasons = []
    approved = list(standard.approved_versions.all()) if standard else []
    compliant = bool(
        software
        and software.software_version_id
        and software.software_version_id in {v.pk for v in approved}
    )
    if conflict:
        reasons.append("Conflicting software standards")
    elif not standard:
        reasons.append("No software standard")
    elif not target:
        reasons.append("No preferred version")
    elif not target.download_url:
        reasons.append("Preferred image has no download URL")
    evidence = state.readiness if state else {}
    state = state or None
    software = software or None
    stale_days = settings.PLUGINS_CONFIG.get("netbox_refresh", {}).get(
        "readiness_stale_days", 1
    )
    stale = not state or state.last_attempt < timezone.now() - timedelta(
        days=stale_days
    )
    blocked = bool(reasons)
    unknown = stale or not software or software.is_stale
    if stale:
        reasons.append("Readiness evidence missing or stale")
    for name in sorted(READINESS_CHECKS):
        check = evidence.get(name, {})
        result = check.get("status", "unknown")
        if result == "failed":
            blocked = True
            reasons.append(f"{name}: {check.get('reason') or 'failed'}")
        elif result not in ("passed", "not_applicable"):
            unknown = True
            reasons.append(f"{name}: {check.get('reason') or 'not checked'}")
    status = (
        "blocked"
        if blocked
        else ("unknown" if unknown else ("already-compliant" if compliant else "ready"))
    )
    return {
        "device": device.pk,
        "name": device.name,
        "site": device.site_id,
        "poller": state.poller if state else None,
        "status": status,
        "reasons": reasons,
        "standard": standard.pk if standard else None,
        "standard_found": bool(standard),
        "compliance": "unknown"
        if not software or not software.software_version_id
        else ("compliant" if compliant else "non-compliant"),
        "approved_versions": [
            {"id": v.pk, "version": v.version, "download_url": v.download_url}
            for v in approved
        ],
        "preferred_version": {
            "id": target.pk,
            "version": target.version,
            "download_url": target.download_url,
            "filename": target.image_filename,
            "size": target.image_size,
            "checksum_type": target.checksum_type,
            "checksum": target.checksum,
        }
        if target
        else None,
        "last_attempt": state.last_attempt if state else None,
        "checks": evidence,
    }


def refresh_actions(devices=None, today=None, emit_events=False):
    """Update source-derived fields while preserving ownership and planning choices.

    Saving threshold transitions emits the normal NetBox object-change events;
    administrators can attach notification Event Rules to LifecycleAction.
    """
    today = today or timezone.localdate()
    notice_days = settings.PLUGINS_CONFIG.get("netbox_refresh", {}).get(
        "renewal_notice_days", 90
    )
    resolver = StandardResolver(today)
    count = 0
    device_ct = ContentType.objects.get_for_model(DeviceType)
    for device in (Device.objects.all() if devices is None else devices).iterator(
        chunk_size=100
    ):
        with transaction.atomic():
            Device.objects.select_for_update().get(pk=device.pk)
            desired = {}

            def add(key, kind, title, due=None, details="", target=None):
                if due and due > today + timedelta(days=notice_days):
                    return
                desired[key] = {
                    "kind": kind,
                    "title": title,
                    "due_on": due,
                    "details": details,
                    "target_version": target,
                }

            for assignment in LicenseAssignment.objects.filter(
                device=device
            ).select_related("entitlement"):
                entitlement = assignment.entitlement
                due = min(
                    (
                        d
                        for d in (
                            entitlement.renew_by,
                            assignment.reported_expiration,
                            entitlement.expires_on,
                        )
                        if d
                    ),
                    default=None,
                )
                if due:
                    add(
                        f"license:{assignment.pk}",
                        "license-renewal",
                        f"Renew {entitlement.product}",
                        due,
                        "Device-reported dates should be verified against the vendor entitlement.",
                    )
            for contract in SupportContract.objects.filter(devices=device):
                due = min(
                    (d for d in (contract.renew_by, contract.ends_on) if d),
                    default=None,
                )
                if due:
                    add(
                        f"contract:{contract.pk}",
                        "support-renewal",
                        f"Renew {contract.provider} support",
                        due,
                    )
            lifecycle = ModelLifecycle.objects.filter(
                assigned_object_type=device_ct, assigned_object_id=device.device_type_id
            ).first()
            if lifecycle and lifecycle.effective_end_of_life:
                add(
                    f"lifecycle:{lifecycle.pk}",
                    "hardware-refresh",
                    "Replace hardware before functional end of life",
                    lifecycle.effective_end_of_life,
                    lifecycle.bulletin_url,
                )
            standard, conflict = resolver.resolve(device)
            software = DeviceSoftware.objects.filter(device=device).first()
            if conflict:
                add(
                    "software:standard",
                    "software-standard",
                    "Resolve conflicting software standards",
                )
            elif not standard:
                add(
                    "software:standard",
                    "software-standard",
                    "Assign a software standard",
                )
            elif not software or not software.software_version_id:
                add(
                    "software:version",
                    "collection",
                    "Collect the running software version",
                    target=standard.preferred_version,
                )
            elif not software.exempt and software.software_version_id not in {
                v.pk for v in standard.approved_versions.all()
            }:
                add(
                    "software:version",
                    "software-upgrade",
                    "Install an approved software version",
                    target=standard.preferred_version,
                )
            if software and software.software_version_id:
                running = software.software_version
                due = min(
                    (
                        d
                        for d in (
                            running.end_of_security_support,
                            running.end_of_support,
                        )
                        if d
                    ),
                    default=None,
                )
                if due:
                    add(
                        f"software-eol:{running.pk}",
                        "software-refresh",
                        "Replace software before functional end of life",
                        due,
                        running.lifecycle_source_url,
                        standard.preferred_version if standard else None,
                    )
            for key, values in desired.items():
                source_key = f"device:{device.pk}:{key}"
                action, created = LifecycleAction.objects.get_or_create(
                    device=device,
                    source_key=source_key,
                    defaults={**values, "last_evaluated": timezone.now()},
                )
                action.snapshot()
                before = {
                    field: getattr(action, field)
                    for field in (
                        *values,
                        "condition_active",
                        "priority",
                        "reminder_stage",
                    )
                }
                for field, value in values.items():
                    setattr(action, field, value)
                action.condition_active = True
                action.last_evaluated = timezone.now()
                due = action.effective_due_date
                remaining = (due - today).days if due else None
                action.priority = (
                    "high" if remaining is not None and remaining <= 7 else "medium"
                )
                action.reminder_stage = (
                    (
                        "overdue"
                        if remaining < 0
                        else "due"
                        if remaining == 0
                        else "7-days"
                        if remaining <= 7
                        else "30-days"
                        if remaining <= 30
                        else "90-days"
                    )
                    if remaining is not None
                    else "undated"
                )
                changed = any(
                    getattr(action, field) != value for field, value in before.items()
                )
                if changed:
                    action.save()
                else:
                    LifecycleAction.objects.filter(pk=action.pk).update(
                        last_evaluated=action.last_evaluated
                    )
                if emit_events and (created or changed):
                    emit_action_event(action, created)
                count += 1
            for obsolete in LifecycleAction.objects.filter(
                device=device, condition_active=True
            ).exclude(source_key__in=[f"device:{device.pk}:{key}" for key in desired]):
                obsolete.snapshot()
                obsolete.condition_active = False
                obsolete.last_evaluated = timezone.now()
                obsolete.save()
                if emit_events:
                    emit_action_event(obsolete, False)
    return {"active_conditions_evaluated": count}


def emit_action_event(action, created):
    """System jobs have no authenticated user; preserve that fact in audit/events."""
    from types import SimpleNamespace
    from uuid import uuid4
    from core.choices import ObjectChangeActionChoices
    from core.events import OBJECT_CREATED, OBJECT_UPDATED
    from extras.events import enqueue_event
    from netbox.context import current_request, events_queue

    if current_request.get() is not None:
        return  # Normal HTTP writes already use NetBox's signal-based pipeline.
    change = action.to_objectchange(
        ObjectChangeActionChoices.ACTION_CREATE
        if created
        else ObjectChangeActionChoices.ACTION_UPDATE
    )
    change.user_name = "Lifecycle system job"
    change.request_id = uuid4()
    change.save()
    enqueue_event(
        events_queue.get(),
        action,
        SimpleNamespace(user=None),
        OBJECT_CREATED if created else OBJECT_UPDATED,
        change.pk,
    )
