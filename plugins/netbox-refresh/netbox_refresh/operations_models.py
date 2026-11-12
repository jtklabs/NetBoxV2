"""Collection evidence, relationships and lifecycle work attached to NetBox devices."""

from django.db import models
from django.urls import reverse
from django.utils import timezone
from netbox.models import PrimaryModel


class DeviceCollectionState(PrimaryModel):
    device = models.OneToOneField(
        "dcim.Device", on_delete=models.CASCADE, related_name="collection_state"
    )
    poller = models.CharField(max_length=80)
    executor_version = models.CharField(max_length=50, blank=True)
    last_attempt = models.DateTimeField()
    last_success = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, default="unknown", db_index=True)
    components = models.JSONField(default=dict, blank=True)
    field_sources = models.JSONField(default=dict, blank=True)
    facts = models.JSONField(default=dict, blank=True)
    readiness = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("device",)

    @property
    def manual_fields(self):
        return sorted(
            key for key, source in self.field_sources.items() if source == "manual"
        )

    def __str__(self):
        return f"Collection status for {self.device}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_refresh:devicecollectionstate", args=[self.pk])


class DeviceObservation(PrimaryModel):
    device = models.ForeignKey(
        "dcim.Device", on_delete=models.CASCADE, related_name="collection_observations"
    )
    job_id = models.CharField(max_length=100)
    poller = models.CharField(max_length=80)
    executor_version = models.CharField(max_length=50, blank=True)
    observed_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=20, default="unknown")
    components = models.JSONField(default=dict, blank=True)
    facts = models.JSONField(default=dict, blank=True)
    field_sources = models.JSONField(default=dict, blank=True)
    readiness = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-observed_at", "-pk")
        constraints = (
            models.UniqueConstraint(
                fields=("device", "poller", "job_id"),
                name="refresh_observation_unique_job",
            ),
        )

    def __str__(self):
        return f"{self.device} at {self.observed_at:%Y-%m-%d %H:%M}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_refresh:deviceobservation", args=[self.pk])


class DeviceInventoryChange(PrimaryModel):
    device = models.ForeignKey(
        "dcim.Device", on_delete=models.CASCADE, related_name="inventory_changes"
    )
    observation = models.ForeignKey(
        DeviceObservation, on_delete=models.CASCADE, related_name="changes"
    )
    field = models.CharField(max_length=80)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    observed_at = models.DateTimeField(db_index=True)
    poller = models.CharField(max_length=80)

    class Meta:
        ordering = ("-observed_at", "-pk")
        constraints = (
            models.UniqueConstraint(
                fields=("observation", "field"),
                name="refresh_inventory_change_unique_field",
            ),
        )

    def __str__(self):
        return f"{self.device} — {self.field}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_refresh:deviceinventorychange", args=[self.pk])


class DeviceRelationship(PrimaryModel):
    device = models.ForeignKey(
        "dcim.Device", on_delete=models.CASCADE, related_name="discovery_relationships"
    )
    peer_device = models.ForeignKey(
        "dcim.Device",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="incoming_discovery_relationships",
    )
    kind = models.CharField(
        max_length=20,
        choices=(("lldp", "LLDP"), ("cdp", "CDP"), ("ha", "HA"), ("mlag", "MLAG")),
    )
    local_interface = models.CharField(max_length=100, blank=True)
    peer_name = models.CharField(max_length=255)
    peer_interface = models.CharField(max_length=100, blank=True)
    peer_address = models.CharField(max_length=64, blank=True)
    chassis_id = models.CharField(max_length=128, blank=True)
    group = models.CharField(max_length=200, blank=True)
    role = models.CharField(max_length=40, blank=True)
    state = models.CharField(max_length=80, blank=True)
    poller = models.CharField(max_length=80)
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(db_index=True)
    present = models.BooleanField(default=True, db_index=True)
    confirmed = models.BooleanField(default=False)

    class Meta:
        ordering = ("device", "kind", "local_interface", "peer_name")
        constraints = (
            models.UniqueConstraint(
                fields=(
                    "device",
                    "kind",
                    "local_interface",
                    "peer_name",
                    "peer_interface",
                    "poller",
                ),
                name="refresh_relationship_unique_peer",
            ),
        )

    def __str__(self):
        return f"{self.device} → {self.peer_name} ({self.kind})"

    def get_absolute_url(self):
        return reverse("plugins:netbox_refresh:devicerelationship", args=[self.pk])


class LifecycleAction(PrimaryModel):
    device = models.ForeignKey(
        "dcim.Device", on_delete=models.CASCADE, related_name="lifecycle_actions"
    )
    source_key = models.CharField(max_length=160, unique=True)
    kind = models.CharField(max_length=40, db_index=True)
    title = models.CharField(max_length=200)
    details = models.TextField(blank=True)
    due_on = models.DateField(null=True, blank=True, db_index=True)
    due_override = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        default="open",
        db_index=True,
        choices=(("open", "Open"), ("planned", "Planned"), ("closed", "Closed")),
    )
    priority = models.CharField(
        max_length=10,
        default="medium",
        choices=(("high", "High"), ("medium", "Medium"), ("low", "Low")),
    )
    condition_active = models.BooleanField(default=True, db_index=True)
    target_version = models.ForeignKey(
        "netbox_refresh.SoftwareVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    last_evaluated = models.DateTimeField(default=timezone.now)
    reminder_stage = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ("due_on", "device", "kind")

    @property
    def effective_due_date(self):
        return self.due_override or self.due_on

    def __str__(self):
        return f"{self.device} — {self.title}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_refresh:lifecycleaction", args=[self.pk])
