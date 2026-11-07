"""Hardware lifecycle and software compliance records.

Two halves, one question ("is this device current and supported?"):

  ModelLifecycle    vendor EoL/EoS dates for a hardware model, its successor,
                    and what replacing it costs.
  SoftwareVersion   a released version of an OS family, plus where to get its
                    image and how to verify it.
  SoftwareStandard  the versions approved for a set of device types and/or
                    platforms, effective-dated so history is queryable.
  DeviceSoftware    what a device is actually running, where that reading came
                    from, and whether it is exempt from the whole exercise.

Everything here inherits PrimaryModel, so NetBox writes an ObjectChange for
every create/update/delete — including writes that arrive over the REST API and
through bulk edits, not just UI form submissions. That is what satisfies the
"full changelog even when a device's running code changes via API" requirement;
there is deliberately no hand-rolled audit trail duplicating it.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.db.models.functions import Least
from django.urls import reverse
from django.utils import timezone
from netbox.models import PrimaryModel

from netbox_refresh.choices import (
    ChecksumTypeChoices,
    ComplianceStatusChoices,
    LifecycleSourceChoices,
    LifecycleStatusChoices,
    SoftwareSourceChoices,
)

__all__ = (
    'ModelLifecycle',
    'ReplacementPrice',
    'SoftwareVersion',
    'SoftwareStandard',
    'DeviceSoftware',
    'LicenseEntitlement',
    'LicenseAssignment',
    'SupportContract',
)

SOFTWARE_LIFECYCLE_FIELDS = (
    'end_of_maintenance', 'end_of_security_support', 'end_of_support', 'lifecycle_source_url',
)

# EoL is tracked per hardware MODEL, not per unit: a device type or a module
# type. That matches how vendors publish it (one bulletin per PID).
LIFECYCLE_ASSIGNMENT_MODELS = Q(app_label='dcim', model__in=('devicetype', 'moduletype'))

# Every vendor-published lifecycle date on ModelLifecycle, in bulletin order.
# The status logic, the forms and the Cisco sync all iterate this rather than
# each keeping its own list, so a date added here reaches all three.
LIFECYCLE_DATE_FIELDS = (
    'announcement_date',
    'end_of_sale',
    'end_of_sw_maintenance',
    'end_of_security_support',
    'end_of_routine_failure_analysis',
    'end_of_service_attach',
    'end_of_service_contract_renewal',
    'end_of_support',
)

# The annotation deliberately does not share the property's name. Django
# assigns an annotation onto each instance it loads, and `effective_end_of_life`
# is a read-only property, so a same-named annotation raises "property has no
# setter" the moment a query returns a row -- passing cleanly on an empty one.
EFFECTIVE_EOL_ALIAS = 'effective_eol'


def effective_end_of_life_expression():
    """The ORM form of ModelLifecycle.effective_end_of_life.

    A property cannot be filtered or sorted on in the database, and the refresh
    report does both. Postgres's LEAST ignores NULLs — verified against the
    running instance, because Django's docs note MySQL, Oracle and SQLite
    return NULL instead, and on those this expression would silently hide every
    model with only one of the two dates published.
    """
    return Least('end_of_security_support', 'end_of_support')


class ModelLifecycle(PrimaryModel):
    """End-of-life dates, successor model and replacement cost for one hardware model."""

    assigned_object_type = models.ForeignKey(
        to=ContentType,
        limit_choices_to=LIFECYCLE_ASSIGNMENT_MODELS,
        on_delete=models.CASCADE,
        related_name='+',
    )
    assigned_object_id = models.PositiveBigIntegerField()
    assigned_object = GenericForeignKey(
        ct_field='assigned_object_type', fk_field='assigned_object_id'
    )

    # --- Lifecycle dates. Names follow Cisco's EoX vocabulary because that is
    # the most detailed vendor feed we consume; they map cleanly onto other
    # vendors' coarser announcements.
    announcement_date = models.DateField(
        blank=True, null=True, help_text='Date the end-of-life was announced'
    )
    end_of_sale = models.DateField(blank=True, null=True)
    end_of_sw_maintenance = models.DateField(
        blank=True, null=True, verbose_name='End of software maintenance'
    )
    end_of_security_support = models.DateField(blank=True, null=True)
    end_of_routine_failure_analysis = models.DateField(blank=True, null=True)
    end_of_service_attach = models.DateField(
        blank=True, null=True, help_text='Last date to attach a new support contract'
    )
    end_of_service_contract_renewal = models.DateField(blank=True, null=True)
    end_of_support = models.DateField(
        blank=True, null=True,
        help_text='Last date of support — the date refresh reporting works from',
    )

    bulletin_number = models.CharField(max_length=50, blank=True)
    bulletin_url = models.URLField(max_length=500, blank=True)

    # --- Replacement. Two nullable FKs rather than a second generic relation:
    # a device type is replaced by a device type, a module type by a module
    # type, and explicit FKs keep filtering and forms simple.
    replacement_device_type = models.ForeignKey(
        to='dcim.DeviceType', on_delete=models.SET_NULL,
        blank=True, null=True, related_name='replaces_lifecycles',
    )
    replacement_module_type = models.ForeignKey(
        to='dcim.ModuleType', on_delete=models.SET_NULL,
        blank=True, null=True, related_name='replaces_lifecycles',
    )
    replacement_notes = models.TextField(
        blank=True,
        help_text="Vendor migration guidance when no single successor model applies",
    )

    # --- Cost of replacing ONE unit of this model. This is the baseline;
    # ReplacementPrice records override it per region or site, because the
    # same box does not cost the same in every country.
    replacement_cost = models.DecimalField(
        max_digits=12, decimal_places=2, blank=True, null=True,
        help_text='Baseline cost to replace a single unit. Regional prices, '
                  'where defined, override this for their sites.',
    )
    currency = models.CharField(max_length=3, default='USD')
    cost_updated = models.DateField(blank=True, null=True)

    source = models.CharField(
        max_length=30,
        choices=LifecycleSourceChoices,
        default=LifecycleSourceChoices.SOURCE_MANUAL,
    )
    last_synced = models.DateTimeField(blank=True, null=True)
    # When somebody last went looking for this model's end-of-life dates —
    # by hand on the vendor's site, or the Cisco sync. The point is the
    # empty-dates case: a record that was checked and found nothing is
    # "EoL not announced", which is an answer; one that was never checked is
    # "unknown", which is a to-do. Without this date the two are
    # indistinguishable and the to-do list never shrinks.
    last_checked = models.DateField(
        blank=True, null=True, verbose_name='EoL last checked',
        help_text="When the vendor was last checked for this model's "
                  'end-of-life dates. Set it even when nothing was found — '
                  'especially then.',
    )

    clone_fields = ('currency', 'source')

    class Meta:
        ordering = ('end_of_support', 'pk')
        verbose_name = 'model lifecycle'
        verbose_name_plural = 'model lifecycles'
        constraints = (
            models.UniqueConstraint(
                'assigned_object_type', 'assigned_object_id',
                name='%(app_label)s_%(class)s_unique_object',
                violation_error_message='This hardware model already has a lifecycle record.',
            ),
        )
        indexes = (
            models.Index(fields=('assigned_object_type', 'assigned_object_id')),
            models.Index(fields=('end_of_support',)),
        )

    def __str__(self):
        if self.assigned_object:
            return str(self.assigned_object)
        return 'Lifecycle %s' % self.pk

    def get_absolute_url(self):
        return reverse('plugins:netbox_refresh:modellifecycle', args=[self.pk])

    def clean(self):
        super().clean()
        if self.replacement_device_type and self.replacement_module_type:
            raise ValidationError(
                'Set only one replacement — a device type or a module type, not both.'
            )
        obj = self.assigned_object
        if obj is not None:
            model_name = obj._meta.model_name
            if model_name == 'devicetype' and self.replacement_module_type:
                raise ValidationError(
                    {'replacement_module_type': 'A device type must be replaced by a device type.'}
                )
            if model_name == 'moduletype' and self.replacement_device_type:
                raise ValidationError(
                    {'replacement_device_type': 'A module type must be replaced by a module type.'}
                )
            if self.replacement_device_type_id == self.assigned_object_id and model_name == 'devicetype':
                raise ValidationError({'replacement_device_type': 'A model cannot replace itself.'})

    # ------------------------------------------------------------------ #
    @property
    def replacement(self):
        return self.replacement_device_type or self.replacement_module_type

    @property
    def part_number(self):
        return getattr(self.assigned_object, 'part_number', '') or ''

    @property
    def manufacturer(self):
        return getattr(self.assigned_object, 'manufacturer', None)

    @property
    def installed_count(self):
        """How many units of this model are installed."""
        obj = self.assigned_object
        if obj is None:
            return 0
        from dcim.models import Device, Module

        if obj._meta.model_name == 'devicetype':
            return Device.objects.filter(device_type=obj).count()
        return Module.objects.filter(module_type=obj).count()

    @property
    def extended_cost(self):
        """Replacement cost for every installed unit of this model.

        The cost is coerced rather than used as-is: an import or API write can
        leave a string on the in-memory instance, and multiplying a string by an
        int repeats it instead of failing.
        """
        if self.replacement_cost is None:
            return None
        return Decimal(self.replacement_cost) * self.installed_count

    @property
    def effective_end_of_life(self):
        """The date this model stops being safe to run, for refresh planning.

        The soonest of end-of-security-support and end-of-support, because
        those are two different kinds of over and the earlier one is what
        binds. A model can stay under a support contract for years after its
        last security fix: TAC will still take the call, and the box still
        cannot be patched. For anything reachable from a network that is the
        date the hardware has to be gone by, so planning against
        end-of-support alone quietly schedules a refresh for after the point
        the estate is already carrying unpatchable devices.

        Either date may be missing — vendors publish them at different times —
        so whichever is known wins, and None means neither has been published.
        """
        published = [d for d in (self.end_of_security_support, self.end_of_support) if d]
        return min(published) if published else None

    @property
    def has_any_date(self):
        """Has the vendor published anything at all for this model?"""
        return any(getattr(self, field) for field in LIFECYCLE_DATE_FIELDS)

    @property
    def checked_on(self):
        """The most recent date anyone looked for EoL data on this model.

        A Cisco sync counts as a check even on records created before
        last_checked existed, so older synced rows read as checked too.
        """
        synced = self.last_synced.date() if self.last_synced else None
        candidates = [d for d in (self.last_checked, synced) if d]
        return max(candidates) if candidates else None

    @property
    def status(self):
        today = date.today()
        if self.effective_end_of_life and self.effective_end_of_life <= today:
            return LifecycleStatusChoices.STATUS_END_OF_SUPPORT
        if self.end_of_sale and self.end_of_sale <= today:
            return LifecycleStatusChoices.STATUS_END_OF_SALE
        if self.has_any_date:
            return LifecycleStatusChoices.STATUS_EOS_ANNOUNCED
        # No dates. Whether that is an answer or a gap depends on whether
        # anyone has looked.
        if self.checked_on:
            return LifecycleStatusChoices.STATUS_NOT_ANNOUNCED
        return LifecycleStatusChoices.STATUS_UNKNOWN

    def get_status_display(self):
        # CHOICES entries are (value, label, color) triples, so build the label
        # map explicitly rather than calling dict() on them.
        labels = {entry[0]: entry[1] for entry in LifecycleStatusChoices.CHOICES}
        return labels.get(self.status, self.status)

    def get_status_color(self):
        return LifecycleStatusChoices.colors.get(self.status)

    def get_source_color(self):
        return LifecycleSourceChoices.colors.get(self.source)


class ReplacementPrice(PrimaryModel):
    """What one unit of a hardware model COSTS TO BUY in one part of the world.

    Keyed to the model being purchased — the replacement, not the hardware on
    its way out. "A 9350 costs 1,200 EUR in EMEA" is one fact however many
    outgoing models funnel into that purchase, and pricing it once means a
    model later pointed at the same replacement picks the price up with no
    re-entry. (Per Jason: "It's less what the replacement cost. It's more
    what the new one cost.")

    A price scoped to a site covers that site alone; one scoped to a region
    covers every site at or below that region. Resolution for a device being
    refreshed is most-specific-wins over its lifecycle's replacement model —
    the site's price, else the nearest enclosing region's walking up the
    tree, else the lifecycle's own baseline replacement_cost. That mirrors
    how vendors quote: a global list price, a country uplift, and the
    occasional odd site (an island, a high-duty zone) priced on its own.

    Currency lives on each price rather than being inherited, because a
    country-specific price is usually IN that country's currency. The refresh
    report never converts — it sums per currency and shows each total, since
    any exchange rate this plugin hardcoded would be wrong by the time it was
    read.
    """

    # The hardware model being bought. Two nullable FKs rather than a generic
    # relation, matching replacement_device_type/module_type above: exactly
    # one is set, and explicit FKs keep filtering and forms simple.
    device_type = models.ForeignKey(
        to='dcim.DeviceType', on_delete=models.CASCADE,
        blank=True, null=True, related_name='replacement_prices',
    )
    module_type = models.ForeignKey(
        to='dcim.ModuleType', on_delete=models.CASCADE,
        blank=True, null=True, related_name='replacement_prices',
    )
    region = models.ForeignKey(
        to='dcim.Region', on_delete=models.CASCADE,
        blank=True, null=True, related_name='replacement_prices',
        help_text='Covers every site at or below this region',
    )
    site = models.ForeignKey(
        to='dcim.Site', on_delete=models.CASCADE,
        blank=True, null=True, related_name='replacement_prices',
        help_text='Covers exactly this site, overriding any region price',
    )
    cost = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text='Purchase cost of a single unit within this scope',
    )
    currency = models.CharField(max_length=3, default='USD')
    cost_updated = models.DateField(blank=True, null=True)

    clone_fields = ('device_type', 'module_type', 'currency')

    class Meta:
        ordering = ('device_type', 'module_type', 'region', 'site', 'pk')
        verbose_name = 'replacement price'
        verbose_name_plural = 'replacement prices'
        constraints = (
            models.CheckConstraint(
                condition=(
                    (Q(device_type__isnull=False) & Q(module_type__isnull=True))
                    | (Q(device_type__isnull=True) & Q(module_type__isnull=False))
                ),
                name='%(app_label)s_%(class)s_exactly_one_model',
                violation_error_message='A price is for a device type or a module type — exactly one.',
            ),
            models.CheckConstraint(
                condition=(
                    (Q(region__isnull=False) & Q(site__isnull=True))
                    | (Q(region__isnull=True) & Q(site__isnull=False))
                ),
                name='%(app_label)s_%(class)s_exactly_one_scope',
                violation_error_message='A price is scoped to a region or a site — exactly one.',
            ),
            models.UniqueConstraint(
                'device_type', 'region',
                condition=Q(device_type__isnull=False) & Q(region__isnull=False),
                name='%(app_label)s_%(class)s_unique_dt_region',
                violation_error_message='This model already has a price for that region.',
            ),
            models.UniqueConstraint(
                'device_type', 'site',
                condition=Q(device_type__isnull=False) & Q(site__isnull=False),
                name='%(app_label)s_%(class)s_unique_dt_site',
                violation_error_message='This model already has a price for that site.',
            ),
            models.UniqueConstraint(
                'module_type', 'region',
                condition=Q(module_type__isnull=False) & Q(region__isnull=False),
                name='%(app_label)s_%(class)s_unique_mt_region',
                violation_error_message='This model already has a price for that region.',
            ),
            models.UniqueConstraint(
                'module_type', 'site',
                condition=Q(module_type__isnull=False) & Q(site__isnull=False),
                name='%(app_label)s_%(class)s_unique_mt_site',
                violation_error_message='This model already has a price for that site.',
            ),
        )

    def __str__(self):
        return '%s @ %s: %s %s' % (self.hardware_model, self.scope, self.cost, self.currency)

    def get_absolute_url(self):
        return reverse('plugins:netbox_refresh:replacementprice', args=[self.pk])

    def clean(self):
        super().clean()
        if bool(self.device_type_id) == bool(self.module_type_id):
            raise ValidationError('Price a device type or a module type — exactly one.')
        if bool(self.region_id) == bool(self.site_id):
            raise ValidationError('Scope the price to a region or a site — exactly one.')

    @property
    def hardware_model(self):
        return self.device_type or self.module_type

    @property
    def scope(self):
        return self.region or self.site


def _plugin_settings():
    return settings.PLUGINS_CONFIG.get('netbox_refresh', {})


class SoftwareVersion(PrimaryModel):
    """One released software version of one OS family, and where to get its image.

    Version strings are stored EXACTLY as the vendor writes them and are never
    parsed, padded or normalised. Compliance is decided by explicit set
    membership (see SoftwareStandard), so nothing in this plugin needs to know
    that 15.2(4)E10 is newer than 15.2(4)E8 — which is the one piece of logic
    in this domain that is genuinely hard to get right across Cisco IOS, IOS-XE,
    NX-OS, PAN-OS and ArubaOS at once, and the reason we do not attempt it.
    A collector that pre-normalises versions before sending them takes that
    choice away from us, so the ingest API asks for raw strings.
    """

    platform = models.ForeignKey(
        to='dcim.Platform',
        on_delete=models.PROTECT,
        related_name='software_versions',
        help_text='The OS family this version belongs to',
    )
    version = models.CharField(
        max_length=100, help_text='Exactly as the vendor writes it, e.g. 17.09.04a'
    )
    release_date = models.DateField(
        blank=True, null=True, help_text='Date the vendor released this version'
    )
    end_of_maintenance = models.DateField(blank=True, null=True)
    end_of_security_support = models.DateField(blank=True, null=True)
    end_of_support = models.DateField(blank=True, null=True, db_index=True)
    lifecycle_source_url = models.URLField(max_length=500, blank=True)

    # --- Where the image is, not the image. Images are published to the
    # internal HTTP file server by whatever process builds them; NetBox stores
    # the link and enough metadata to verify a download. It deliberately never
    # holds the bytes: at 300MB–1GB an upload through Apache and gunicorn is a
    # timeout waiting to happen, and a second copy is a second thing to be
    # wrong about.
    image_filename = models.CharField(
        max_length=255, blank=True, help_text='e.g. cat9k_iosxe.17.09.04a.SPA.bin'
    )
    image_url = models.URLField(
        max_length=500, blank=True,
        help_text='Direct download link on the image server. Leave empty to derive '
                  'it from the filename and the image_base_url plugin setting.',
    )
    image_size = models.BigIntegerField(
        blank=True, null=True, help_text='Image size in bytes, as published by the vendor'
    )
    checksum_type = models.CharField(max_length=10, choices=ChecksumTypeChoices, blank=True)
    checksum = models.CharField(
        max_length=128, blank=True, help_text='Vendor-published digest of the image'
    )

    clone_fields = ('platform', 'checksum_type')

    class Meta:
        ordering = ('platform', 'version')
        verbose_name = 'software version'
        verbose_name_plural = 'software versions'
        constraints = (
            models.UniqueConstraint(
                'platform', 'version',
                name='%(app_label)s_%(class)s_unique_platform_version',
                violation_error_message='That version already exists for this platform.',
            ),
        )
        indexes = (models.Index(fields=('version',)),)

    def __str__(self):
        return '%s %s' % (self.platform, self.version)

    def get_absolute_url(self):
        return reverse('plugins:netbox_refresh:softwareversion', args=[self.pk])

    def clean(self):
        super().clean()
        # A digest with no algorithm cannot be checked against anything, and an
        # algorithm with no digest is noise. Require them together or not at all.
        if self.checksum and not self.checksum_type:
            raise ValidationError({'checksum_type': 'Say which digest this checksum is.'})
        if self.checksum_type and not self.checksum:
            raise ValidationError({'checksum': 'Enter the checksum, or clear the type.'})

    @property
    def download_url(self):
        """Where a human should click to get this image.

        An explicit image_url wins; otherwise it is derived from the filename
        and the image_base_url setting, so populating a hundred versions means
        typing a hundred filenames rather than a hundred URLs, and moving the
        image server later is one config change instead of a bulk edit.

        A plain http:// link here is expected and works. NetBox is served over
        https, but clicking a download link is a top-level navigation, which
        browsers do not treat as blocked mixed content. Never fetch this URL
        from JavaScript — that *would* be blocked.
        """
        if self.image_url:
            return self.image_url
        base = _plugin_settings().get('image_base_url', '')
        if base and self.image_filename:
            return '%s/%s' % (base.rstrip('/'), self.image_filename.lstrip('/'))
        return ''

    @property
    def has_image(self):
        return bool(self.download_url)

    @property
    def installed_count(self):
        """How many devices are recorded as running this version."""
        return self.devices.count()


class SoftwareStandard(PrimaryModel):
    """The versions approved for a device type or a platform, effective-dated.

    Approved versions are enumerated EXPLICITLY — "17.09.04a or 17.12.03", not
    "anything at or above 17.09.04a". Real estates routinely have two or more
    blessed versions at once (a stable one and a newer one being rolled out),
    and expressing that as a floor both overstates compliance and requires the
    version-ordering logic we deliberately do not have.

    Standards are effective-dated instead of edited in place. Closing one out
    (valid_to) and opening its successor means "what was our standard on
    2026-03-01?" is a query against data, rather than reading changelog entries
    backwards and hoping. valid_from doubles as the date we adopted it.
    """

    # Many-to-many, deliberately: fleets bless one image across a family of
    # types ("every 2960X variant runs 15.2(7)E3"), and one-standard-per-type
    # meant N copies of the same standard drifting apart at every edit. A
    # standard may scope to device types, platforms, or both; the resolver
    # still lets a device-type standard override a platform one.
    device_types = models.ManyToManyField(
        to='dcim.DeviceType',
        related_name='software_standards',
        blank=True,
        help_text='Every device type this standard covers',
    )
    platforms = models.ManyToManyField(
        to='dcim.Platform',
        related_name='software_standards',
        blank=True,
        help_text='Every platform this standard covers',
    )
    device_tags = models.ManyToManyField(
        to='extras.Tag',
        related_name='scoped_software_standards',
        blank=True,
        help_text='Devices carrying any of these tags use this standard ahead of type or platform standards.',
    )

    approved_versions = models.ManyToManyField(
        to=SoftwareVersion,
        related_name='approved_by_standards',
        help_text='Every version that counts as compliant under this standard',
    )
    preferred_version = models.ForeignKey(
        to=SoftwareVersion,
        on_delete=models.SET_NULL,
        blank=True, null=True,
        related_name='preferred_by_standards',
        help_text='Which approved version to deploy on new or rebuilt kit',
    )

    valid_from = models.DateField(
        default=date.today, help_text='Date this became our standard'
    )
    valid_to = models.DateField(
        blank=True, null=True, help_text='Leave empty while this standard is current'
    )

    clone_fields = ('device_types', 'platforms', 'device_tags', 'preferred_version')

    class Meta:
        ordering = ('-valid_from', 'pk')
        verbose_name = 'software standard'
        verbose_name_plural = 'software standards'
        # The old single-scope unique constraint is gone with the generic FK.
        # Overlap prevention lives in validation (see conflicting_standards):
        # a database constraint cannot span an M2M, and what must be unique is
        # not a column tuple but "no scope covered twice on the same day".
        indexes = (
            models.Index(fields=('valid_from', 'valid_to')),
        )

    def __str__(self):
        label = self.scope_summary() if self.pk else 'Software standard'
        if self.valid_to:
            return '%s (%s to %s)' % (label, self.valid_from, self.valid_to)
        return '%s (from %s)' % (label, self.valid_from)

    def scope_objects(self):
        """Everything this standard covers, device types first."""
        return list(self.device_types.all()) + list(self.platforms.all()) + list(self.device_tags.all())

    def scope_summary(self, limit: int = 2) -> str:
        scopes = self.scope_objects()
        if not scopes:
            return 'Standard %s (no scope)' % self.pk
        shown = ', '.join(str(obj) for obj in scopes[:limit])
        extra = len(scopes) - limit
        return '%s +%d more' % (shown, extra) if extra > 0 else shown

    @classmethod
    def conflicting_standards(cls, device_types, platforms, valid_from, valid_to,
                              exclude_pk=None, *, device_tags=()):
        """Standards whose validity window overlaps AND that share any scope.

        This is the overlap rule the old per-scope unique constraint only
        approximated: two standards may never both claim the same device type
        or platform on the same day, or compliance has two answers.
        """
        from django.db.models import Q
        window = Q()
        if valid_to is not None:
            window &= Q(valid_from__lte=valid_to)
        candidates = cls.objects.filter(window).filter(
            Q(valid_to__isnull=True) | Q(valid_to__gte=valid_from)
        )
        scope = Q()
        if device_tags:
            scope = Q(device_tags__in=device_tags)
        else:
            candidates = candidates.filter(device_tags__isnull=True)
            if device_types:
                scope |= Q(device_types__in=device_types)
            if platforms:
                scope |= Q(platforms__in=platforms)
        if not scope:
            return cls.objects.none()
        candidates = candidates.filter(scope).distinct()
        if exclude_pk is not None:
            candidates = candidates.exclude(pk=exclude_pk)
        return candidates

    def get_absolute_url(self):
        return reverse('plugins:netbox_refresh:softwarestandard', args=[self.pk])

    def clean(self):
        super().clean()
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({'valid_to': 'The end date cannot be before the start date.'})

        # Two standards covering the same device type or platform must not both
        # apply on the same day, or "the standard on date X" has no single
        # answer. The M2M scopes are only readable once the row exists, so this
        # covers the common in-place edit (shifting valid_from/valid_to on a
        # saved standard); the form and API serializer run the same check
        # against the SUBMITTED scopes, which is what protects creates and
        # scope changes.
        if self.pk and self.valid_from:
            conflict = SoftwareStandard.conflicting_standards(
                self.device_types.all(), self.platforms.all(),
                self.valid_from, self.valid_to, exclude_pk=self.pk, device_tags=self.device_tags.all(),
            ).first()
            if conflict:
                raise ValidationError(
                    'This overlaps an existing standard sharing part of its scope '
                    '(%s). Close that one out with an end date first.' % conflict
                )

        # The M2M is unavailable until the row exists, so this only fires on
        # edit; SoftwareStandardForm re-checks it against the submitted data.
        if self.pk and self.preferred_version_id:
            if not self.approved_versions.filter(pk=self.preferred_version_id).exists():
                raise ValidationError(
                    {'preferred_version': 'The preferred version must be one of the approved versions.'}
                )

    def overlaps(self, other):
        """Do these two standards both apply on some day? Open-ended means forever."""
        if self.valid_to is not None and self.valid_to < other.valid_from:
            return False
        if other.valid_to is not None and other.valid_to < self.valid_from:
            return False
        return True

    def applies_on(self, on_date):
        if self.valid_from > on_date:
            return False
        return self.valid_to is None or self.valid_to >= on_date

    @property
    def is_active(self):
        return self.applies_on(date.today())


class DeviceSoftware(PrimaryModel):
    """What one device is actually running, and whether it is allowed to be.

    One row per device. The row exists even when the version is unknown,
    because "we have never collected this" is a state the compliance report has
    to be able to show — and because an exemption ("do not upgrade") has to be
    recordable for a device we have never scanned.

    Freshness is kept separate from the compliance state on purpose. A device
    can be compliant against a reading taken six months ago; the report says
    both things rather than rendering it green and confident. last_checked is
    bumped by the ingest API with a queryset update so that re-confirming an
    unchanged version does not write a changelog entry per device per scan —
    see api/views.py.
    """

    device = models.OneToOneField(
        to='dcim.Device', on_delete=models.CASCADE, related_name='software'
    )
    software_version = models.ForeignKey(
        to=SoftwareVersion,
        on_delete=models.PROTECT,
        blank=True, null=True,
        related_name='devices',
        help_text='Empty means we have never collected a version from this device',
    )
    raw_version = models.CharField(
        max_length=200, blank=True,
        help_text='The version string exactly as reported, kept even when it '
                  'could not be matched to a known version',
    )
    raw_report = models.TextField(
        blank=True,
        help_text='Verbatim collector output the version was read from, e.g. sysDescr. '
                  'Kept so a wrong-looking version can be traced to what the device '
                  'actually said, rather than argued about.',
    )
    source = models.CharField(
        max_length=30,
        choices=SoftwareSourceChoices,
        default=SoftwareSourceChoices.SOURCE_MANUAL,
    )
    collected_at = models.DateTimeField(
        blank=True, null=True, help_text='When this reading was taken at the device'
    )
    last_checked = models.DateTimeField(
        blank=True, null=True, help_text='When a collector last confirmed this version'
    )

    # --- Exemption. "Do not upgrade" is a real answer, but a silent permanent
    # one is how compliance programs rot, so it carries who said so, when, and
    # when it should be looked at again.
    exempt = models.BooleanField(
        default=False, verbose_name='Do not upgrade',
        help_text='Exclude from compliance, but keep showing it as exempt',
    )
    exempt_reason = models.TextField(blank=True)
    exempt_approved_by = models.CharField(
        max_length=100, blank=True,
        help_text='Free text — approvers are often not NetBox users',
    )
    exempt_approved_on = models.DateField(blank=True, null=True)
    exempt_review_by = models.DateField(
        blank=True, null=True, help_text='When this exemption should be revisited'
    )

    clone_fields = ('source',)

    class Meta:
        ordering = ('device',)
        verbose_name = 'device software'
        verbose_name_plural = 'device software'
        indexes = (
            models.Index(fields=('software_version',)),
            models.Index(fields=('exempt',)),
        )

    def __str__(self):
        return '%s — %s' % (self.device, self.version_label or 'unknown')

    def get_absolute_url(self):
        return reverse('plugins:netbox_refresh:devicesoftware', args=[self.pk])

    def clean(self):
        super().clean()
        # An exemption without a reason is indistinguishable from a mistake.
        if self.exempt and not self.exempt_reason.strip():
            raise ValidationError(
                {'exempt_reason': 'Give a reason — an unexplained exemption cannot be reviewed.'}
            )

    # ------------------------------------------------------------------ #
    @property
    def version_label(self):
        if self.software_version_id:
            return self.software_version.version
        return self.raw_version

    @property
    def standard(self):
        from netbox_refresh.compliance import standard_for_device

        return standard_for_device(self.device)

    @property
    def compliance_status(self):
        from netbox_refresh.compliance import evaluate

        return evaluate(self)

    def get_compliance_status_display(self):
        labels = {entry[0]: entry[1] for entry in ComplianceStatusChoices.CHOICES}
        return labels.get(self.compliance_status, self.compliance_status)

    def get_compliance_status_color(self):
        return ComplianceStatusChoices.colors.get(self.compliance_status)

    def get_source_color(self):
        return SoftwareSourceChoices.colors.get(self.source)

    @property
    def exemption_expired(self):
        return bool(
            self.exempt and self.exempt_review_by and self.exempt_review_by < date.today()
        )

    @property
    def as_of(self):
        """Best estimate of when the recorded version was last known true.

        Falls back to last_updated so a hand-entered record is dated from when
        somebody typed it rather than being reported as never-confirmed.
        """
        return self.collected_at or self.last_checked or self.last_updated

    @property
    def age_days(self):
        stamp = self.as_of
        if stamp is None:
            return None
        return (timezone.now() - stamp).days

    @property
    def is_stale(self):
        """Is the reading old enough that it should not be trusted at face value?"""
        if not self.software_version_id and not self.raw_version:
            return False  # nothing recorded; Unknown already says everything
        days = self.age_days
        if days is None:
            return True
        return days > _plugin_settings().get('stale_after_days', 90)

    @property
    def stale_threshold(self):
        return timezone.now() - timedelta(
            days=_plugin_settings().get('stale_after_days', 90)
        )


class LicenseEntitlement(PrimaryModel):
    manufacturer = models.ForeignKey('dcim.Manufacturer', on_delete=models.PROTECT)
    product = models.CharField(max_length=150)
    reference = models.CharField(
        max_length=100, help_text='Vendor entitlement ID or support serial. Keep activation keys in the vault.',
    )
    vault_reference = models.CharField(
        max_length=200, blank=True, help_text='Reference to the vault item that holds the activation key.',
    )
    seats = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    expires_on = models.DateField(blank=True, null=True, db_index=True)
    renew_by = models.DateField(blank=True, null=True, db_index=True)
    source_url = models.URLField(max_length=500, blank=True)

    class Meta:
        ordering = ('manufacturer', 'product', 'reference')
        constraints = (
            models.UniqueConstraint(
                fields=('manufacturer', 'reference'), name='refresh_entitlement_unique_reference',
            ),
            models.CheckConstraint(condition=Q(seats__gte=1), name='refresh_entitlement_positive_seats'),
        )

    def __str__(self):
        return f'{self.product} ({self.reference})'

    def get_absolute_url(self):
        return reverse('plugins:netbox_refresh:licenseentitlement', args=[self.pk])


class LicenseAssignment(PrimaryModel):
    entitlement = models.ForeignKey(
        LicenseEntitlement, on_delete=models.PROTECT, related_name='assignments',
    )
    device = models.ForeignKey('dcim.Device', on_delete=models.CASCADE, related_name='license_assignments')
    seats = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    installed_modules = models.TextField(blank=True)
    reported_expiration = models.DateField(blank=True, null=True)
    service_check_date = models.DateField(blank=True, null=True)
    verified_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ('device', 'entitlement')
        constraints = (
            models.UniqueConstraint(fields=('entitlement', 'device'), name='refresh_license_unique_assignment'),
            models.CheckConstraint(condition=Q(seats__gte=1), name='refresh_assignment_positive_seats'),
        )

    def __str__(self):
        return f'{self.entitlement} on {self.device}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_refresh:licenseassignment', args=[self.pk])


class SupportContract(PrimaryModel):
    provider = models.CharField(max_length=150)
    reference = models.CharField(max_length=100)
    service_level = models.CharField(max_length=100, blank=True)
    starts_on = models.DateField(blank=True, null=True)
    ends_on = models.DateField(blank=True, null=True, db_index=True)
    renew_by = models.DateField(blank=True, null=True, db_index=True)
    source_url = models.URLField(max_length=500, blank=True)
    devices = models.ManyToManyField('dcim.Device', related_name='support_contracts', blank=True)

    class Meta:
        ordering = ('provider', 'reference')
        constraints = (
            models.UniqueConstraint(fields=('provider', 'reference'), name='refresh_support_unique_reference'),
            models.CheckConstraint(
                condition=Q(starts_on__lte=models.F('ends_on')),
                name='refresh_support_ordered_term',
                violation_error_message='The end date must be on or after the start date.',
            ),
        )

    def __str__(self):
        return f'{self.provider} {self.reference}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_refresh:supportcontract', args=[self.pk])

    @property
    def coverage_status(self):
        today = timezone.localdate()
        if self.ends_on and self.ends_on < today:
            return 'Expired'
        if self.starts_on and self.starts_on > today:
            return 'Scheduled'
        if self.starts_on and self.ends_on:
            return 'Active'
        return 'Dates unverified'
