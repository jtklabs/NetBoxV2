from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Manufacturer,
    ModuleType,
    Platform,
    Region,
    Site,
)
from django import forms
from django.contrib.contenttypes.models import ContentType
from extras.models import Tag
from netbox.forms import (
    NetBoxModelBulkEditForm,
    NetBoxModelFilterSetForm,
    NetBoxModelForm,
    NetBoxModelImportForm,
)
from utilities.forms.fields import (
    CSVChoiceField,
    CSVModelChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    TagFilterField,
)
from utilities.forms.rendering import FieldSet, TabbedGroups
from utilities.forms.widgets import DatePicker, DateTimePicker

from netbox_refresh.choices import (
    ChecksumTypeChoices,
    ComplianceStatusChoices,
    LifecycleSourceChoices,
    LifecycleStatusChoices,
    SoftwareSourceChoices,
)
from netbox_refresh.models import (
    LIFECYCLE_DATE_FIELDS,
    SOFTWARE_LIFECYCLE_FIELDS,
    DeviceSoftware,
    ModelLifecycle,
    ReplacementPrice,
    SoftwareStandard,
    SoftwareVersion,
)

__all__ = (
    'ModelLifecycleForm',
    'ModelLifecycleFilterForm',
    'ModelLifecycleImportForm',
    'ModelLifecycleBulkEditForm',
    'ReplacementPriceForm',
    'ReplacementPriceFilterForm',
    'ReplacementPriceImportForm',
    'RefreshReportForm',
    'SoftwareVersionForm',
    'SoftwareVersionFilterForm',
    'SoftwareVersionImportForm',
    'SoftwareVersionBulkEditForm',
    'SoftwareStandardForm',
    'SoftwareStandardFilterForm',
    'SoftwareStandardBulkEditForm',
    'DeviceSoftwareForm',
    'DeviceSoftwareFilterForm',
    'DeviceSoftwareImportForm',
    'DeviceSoftwareBulkEditForm',
    'ComplianceReportForm',
)

# One list for the status logic, the forms and the sync — see models.py.
DATE_FIELDS = LIFECYCLE_DATE_FIELDS


class ModelLifecycleForm(NetBoxModelForm):
    effective_end_of_life = forms.DateField(
        label='Functional end of life',
        disabled=True,
        required=False,
        widget=forms.DateInput(format='%Y-%m-%d'),
        help_text='Automatically uses the earlier of end of security support and end of support.',
    )
    device_type = DynamicModelChoiceField(
        queryset=DeviceType.objects.all(), required=False, selector=True,
    )
    module_type = DynamicModelChoiceField(
        queryset=ModuleType.objects.all(), required=False, selector=True,
    )
    replacement_device_type = DynamicModelChoiceField(
        queryset=DeviceType.objects.all(), required=False, selector=True,
        label='Replacement device type',
    )
    replacement_module_type = DynamicModelChoiceField(
        queryset=ModuleType.objects.all(), required=False, selector=True,
        label='Replacement module type',
    )

    fieldsets = (
        FieldSet(
            TabbedGroups(
                FieldSet('device_type', name='Device Type'),
                FieldSet('module_type', name='Module Type'),
            ),
            name='Hardware model',
        ),
        FieldSet(*DATE_FIELDS, 'effective_end_of_life', 'last_checked', name='Lifecycle dates'),
        FieldSet(
            TabbedGroups(
                FieldSet('replacement_device_type', name='Device Type'),
                FieldSet('replacement_module_type', name='Module Type'),
            ),
            'replacement_notes',
            name='Replacement',
        ),
        FieldSet('replacement_cost', 'currency', 'cost_updated', name='Cost'),
        FieldSet('bulletin_number', 'bulletin_url', 'source', 'description', name='Reference'),
        FieldSet('tags', name='Tags'),
    )

    class Meta:
        model = ModelLifecycle
        fields = DATE_FIELDS + (
            'last_checked',
            'replacement_device_type', 'replacement_module_type', 'replacement_notes',
            'replacement_cost', 'currency', 'cost_updated',
            'bulletin_number', 'bulletin_url', 'source',
            'description', 'comments', 'tags',
        )
        widgets = dict(
            {field: DatePicker() for field in DATE_FIELDS},
            cost_updated=DatePicker(),
            last_checked=DatePicker(),
        )

    def __init__(self, *args, **kwargs):
        instance = kwargs.get('instance')
        initial = kwargs.get('initial', {}).copy()
        if instance is not None:
            initial['effective_end_of_life'] = instance.effective_end_of_life
            if instance.assigned_object:
                field = ('device_type' if instance.assigned_object._meta.model_name == 'devicetype'
                         else 'module_type')
                initial[field] = instance.assigned_object
        kwargs['initial'] = initial
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        device_type = self.cleaned_data.get('device_type')
        module_type = self.cleaned_data.get('module_type')
        if device_type and module_type:
            raise forms.ValidationError(
                'Select a device type or a module type, not both.'
            )
        target = device_type or module_type
        if target is None:
            raise forms.ValidationError('Select the hardware model this lifecycle applies to.')
        self.instance.assigned_object = target


class ModelLifecycleFilterForm(NetBoxModelFilterSetForm):
    model = ModelLifecycle
    manufacturer_id = DynamicModelMultipleChoiceField(
        queryset=Manufacturer.objects.all(), required=False, label='Manufacturer',
    )
    end_of_support__gte = forms.DateField(
        required=False, widget=DatePicker(), label='End of support after',
    )
    end_of_support__lte = forms.DateField(
        required=False, widget=DatePicker(), label='End of support before',
    )
    end_of_sale__gte = forms.DateField(
        required=False, widget=DatePicker(), label='End of sale after',
    )
    end_of_sale__lte = forms.DateField(
        required=False, widget=DatePicker(), label='End of sale before',
    )
    source = forms.MultipleChoiceField(choices=LifecycleSourceChoices, required=False)
    status = forms.MultipleChoiceField(
        choices=LifecycleStatusChoices, required=False,
        help_text='Derived from the dates and the last-checked date',
    )
    last_checked__gte = forms.DateField(
        required=False, widget=DatePicker(), label='EoL checked after',
    )
    last_checked__lte = forms.DateField(
        required=False, widget=DatePicker(), label='EoL checked before',
    )
    has_replacement = forms.NullBooleanField(required=False)
    has_cost = forms.NullBooleanField(required=False)
    tag = TagFilterField(model)


# Accepted in an uploaded CSV, in order. US format first because that is what
# comes out of a spreadsheet here; ISO second because it is unambiguous and any
# CSV written before this still has to load. Day-first is deliberately absent:
# 03/04/2026 would parse silently as a different date under it, and a silently
# wrong end-of-support date is worse than a rejected row.
CSV_DATE_INPUT_FORMATS = ('%m/%d/%Y', '%m/%d/%y', '%Y-%m-%d')


class ModelLifecycleImportForm(NetBoxModelImportForm):
    device_type = CSVModelChoiceField(
        queryset=DeviceType.objects.all(), to_field_name='model', required=False,
        help_text='Device type model name',
    )
    module_type = CSVModelChoiceField(
        queryset=ModuleType.objects.all(), to_field_name='model', required=False,
        help_text='Module type model name',
    )
    replacement_device_type = CSVModelChoiceField(
        queryset=DeviceType.objects.all(), to_field_name='model', required=False,
    )
    replacement_module_type = CSVModelChoiceField(
        queryset=ModuleType.objects.all(), to_field_name='model', required=False,
    )
    source = CSVChoiceField(choices=LifecycleSourceChoices, required=False)

    def __init__(self, *args, headers=None, **kwargs):
        super().__init__(*args, headers=headers, **kwargs)
        # Keep only the columns the sheet actually has. NetBox's `headers`
        # sets to_field_name and nothing else, so every form field stays live
        # whether or not the CSV mentions it — which has two consequences,
        # both wrong for this form.
        #
        # A sheet without a `currency` column failed on "This field is
        # required", though the model has a default; and on an update, a field
        # with no column arrived empty and construct_instance would write that
        # emptiness over a value already recorded. A revised bulletin carrying
        # one date would have wiped the other seven.
        #
        # Dropping the field keeps it out of cleaned_data, which is what
        # construct_instance checks, so the stored value stands.
        if self.headers:
            for name in [f for f in self.fields if f not in self.headers]:
                del self.fields[name]

    # Declared explicitly so every lifecycle date takes a US-formatted value.
    # Generated from DATE_FIELDS rather than typed out eight times, so a field
    # added there cannot quietly go back to ISO-only.
    locals().update({
        name: forms.DateField(required=False, input_formats=CSV_DATE_INPUT_FORMATS)
        for name in DATE_FIELDS
    })
    cost_updated = forms.DateField(required=False, input_formats=CSV_DATE_INPUT_FORMATS)
    last_checked = forms.DateField(required=False, input_formats=CSV_DATE_INPUT_FORMATS)

    class Meta:
        model = ModelLifecycle
        fields = ('device_type', 'module_type') + DATE_FIELDS + (
            'last_checked',
            'replacement_device_type', 'replacement_module_type', 'replacement_notes',
            'replacement_cost', 'currency', 'cost_updated',
            'bulletin_number', 'bulletin_url', 'source', 'description', 'comments', 'tags',
        )

    def clean(self):
        super().clean()
        target = self.cleaned_data.get('device_type') or self.cleaned_data.get('module_type')
        if target is None:
            raise forms.ValidationError('Provide either device_type or module_type.')

        # Update the record this model already has rather than refusing the
        # row. A lifecycle record is unique per hardware model, so importing a
        # spreadsheet that mentions anything already loaded used to fail the
        # whole upload on "This hardware model already has a lifecycle record"
        # -- which makes a vendor's EoX bulletin, where most rows are dates
        # that have merely been revised, impossible to load without first
        # weeding out by hand every model already present.
        #
        # Switching self.instance here is what makes it an update: _post_clean
        # runs after this and builds onto whatever instance is set, so the save
        # becomes an UPDATE and the unique check excludes the row itself.
        #
        # Only columns present in the CSV are touched -- see __init__, which
        # is what makes that true. A sheet carrying just end_of_support
        # revises that and leaves the other dates, the cost and the bulletin
        # alone, which is what a revision usually is.
        if not self.instance.pk:
            existing = ModelLifecycle.objects.filter(
                assigned_object_type=ContentType.objects.get_for_model(target),
                assigned_object_id=target.pk,
            ).first()
            if existing is not None:
                self.instance = existing

        self.instance.assigned_object = target


class ModelLifecycleBulkEditForm(NetBoxModelBulkEditForm):
    model = ModelLifecycle
    end_of_sale = forms.DateField(required=False, widget=DatePicker())
    end_of_support = forms.DateField(required=False, widget=DatePicker())
    # Bulk on purpose: "I checked these thirty Juniper models this morning and
    # none has an announcement" is one edit, not thirty.
    last_checked = forms.DateField(
        required=False, widget=DatePicker(), label='EoL last checked',
    )
    replacement_cost = forms.DecimalField(required=False, max_digits=12, decimal_places=2)
    currency = forms.CharField(required=False, max_length=3)
    cost_updated = forms.DateField(required=False, widget=DatePicker())
    replacement_device_type = DynamicModelChoiceField(
        queryset=DeviceType.objects.all(), required=False,
    )
    description = forms.CharField(required=False, max_length=200)

    nullable_fields = ('replacement_cost', 'cost_updated', 'replacement_device_type',
                       'description')


class RefreshReportForm(forms.Form):
    """Filters for the refresh cost report."""

    DATE_FIELD_CHOICES = (
        # First and default: the one that actually binds a refresh. See
        # ModelLifecycle.effective_end_of_life — a model can sit under a
        # support contract for years after its last security fix, and it is
        # the security date that says when the hardware has to be gone.
        ('effective_eol', 'End of life (soonest of support / security)'),
        ('end_of_support', 'End of support'),
        ('end_of_sale', 'End of sale'),
        ('end_of_security_support', 'End of security support'),
        ('end_of_service_attach', 'End of service attach'),
        ('end_of_service_contract_renewal', 'End of contract renewal'),
    )

    date_field = forms.ChoiceField(
        choices=DATE_FIELD_CHOICES, initial='effective_eol', required=False,
        label='Milestone',
    )
    after = forms.DateField(required=False, widget=DatePicker(), label='Between')
    before = forms.DateField(required=False, widget=DatePicker(), label='and')
    manufacturer = DynamicModelMultipleChoiceField(
        queryset=Manufacturer.objects.all(), required=False,
    )
    region = DynamicModelMultipleChoiceField(
        queryset=Region.objects.all(), required=False,
        help_text='Only count installed units in these regions, including any '
                  'nested inside them',
    )
    site = DynamicModelMultipleChoiceField(
        queryset=Site.objects.all(), required=False,
        help_text='Only count installed units at these sites',
    )
    BREAKDOWN_CHOICES = (
        ('model', 'By hardware model'),
        ('site', 'By site'),
    )
    breakdown = forms.ChoiceField(
        choices=BREAKDOWN_CHOICES, initial='model', required=False,
        label='Break down',
        help_text='By site lists every model due at each site, with each site '
                  'priced at its own rate',
    )


# --------------------------------------------------------------------------- #
# Software
# --------------------------------------------------------------------------- #

class SoftwareVersionForm(NetBoxModelForm):
    platform = DynamicModelChoiceField(queryset=Platform.objects.all(), selector=True)

    fieldsets = (
        FieldSet('platform', 'version', 'release_date', name='Version'),
        FieldSet(*SOFTWARE_LIFECYCLE_FIELDS, name='Lifecycle'),
        FieldSet(
            'image_filename', 'image_url', 'image_size',
            'checksum_type', 'checksum', name='Image',
        ),
        FieldSet('description', name='Reference'),
        FieldSet('tags', name='Tags'),
    )

    class Meta:
        model = SoftwareVersion
        fields = (
            'platform', 'version', 'release_date', *SOFTWARE_LIFECYCLE_FIELDS,
            'image_filename', 'image_url', 'image_size',
            'checksum_type', 'checksum',
            'description', 'comments', 'tags',
        )
        widgets = {field: DatePicker() for field in ('release_date', *SOFTWARE_LIFECYCLE_FIELDS[:3])}
        help_texts = {
            'image_url': 'Direct link, typically the internal image server. '
                         'Plain http is fine — this is a download link, not a page resource.',
        }


class SoftwareVersionFilterForm(NetBoxModelFilterSetForm):
    model = SoftwareVersion
    platform_id = DynamicModelMultipleChoiceField(
        queryset=Platform.objects.all(), required=False, label='Platform',
    )
    release_date__gte = forms.DateField(
        required=False, widget=DatePicker(), label='Released after',
    )
    release_date__lte = forms.DateField(
        required=False, widget=DatePicker(), label='Released before',
    )
    end_of_support__lte = forms.DateField(required=False, widget=DatePicker(), label='Support ends by')
    checksum_type = forms.MultipleChoiceField(choices=ChecksumTypeChoices, required=False)
    has_image = forms.NullBooleanField(required=False, label='Has a downloadable image')
    tag = TagFilterField(model)


class SoftwareVersionImportForm(NetBoxModelImportForm):
    platform = CSVModelChoiceField(
        queryset=Platform.objects.all(), to_field_name='name',
        help_text='Platform name',
    )
    checksum_type = CSVChoiceField(choices=ChecksumTypeChoices, required=False)

    class Meta:
        model = SoftwareVersion
        fields = (
            'platform', 'version', 'release_date', *SOFTWARE_LIFECYCLE_FIELDS,
            'image_filename', 'image_url', 'image_size', 'checksum_type', 'checksum',
            'description', 'comments', 'tags',
        )


class SoftwareVersionBulkEditForm(NetBoxModelBulkEditForm):
    model = SoftwareVersion
    platform = DynamicModelChoiceField(queryset=Platform.objects.all(), required=False)
    release_date = forms.DateField(required=False, widget=DatePicker())
    image_url = forms.URLField(required=False, max_length=500)
    checksum_type = forms.ChoiceField(
        choices=[('', '---------')] + list(ChecksumTypeChoices), required=False,
    )
    description = forms.CharField(required=False, max_length=200)

    nullable_fields = ('release_date', 'image_url', 'checksum_type', 'description')


class SoftwareStandardForm(NetBoxModelForm):
    device_tags = DynamicModelMultipleChoiceField(
        queryset=Tag.objects.all(), required=False, label='Device tags',
        help_text='Use this field by itself for mode-specific code. A matching tag takes precedence over device type and platform.',
    )
    device_types = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(), required=False,
        help_text='Every device type this standard covers — a fleet usually '
                  'blesses one image across a family of types, e.g. all the '
                  '2960X variants.',
    )
    platforms = DynamicModelMultipleChoiceField(
        queryset=Platform.objects.all(), required=False,
        help_text='Every platform (OS family) this standard covers. A '
                  'device-type standard overrides a platform one.',
    )
    approved_versions = DynamicModelMultipleChoiceField(
        queryset=SoftwareVersion.objects.all(),
        help_text='Every version that counts as compliant. List them explicitly — '
                  'there is no "at or above" rule.',
    )
    preferred_version = DynamicModelChoiceField(
        queryset=SoftwareVersion.objects.all(), required=False,
        help_text='Which of the approved versions to deploy on new kit',
    )

    fieldsets = (
        FieldSet('device_types', 'platforms', 'device_tags', name='Applies to'),
        FieldSet('approved_versions', 'preferred_version', name='Approved software'),
        FieldSet('valid_from', 'valid_to', name='In force'),
        FieldSet('description', name='Reference'),
        FieldSet('tags', name='Tags'),
    )

    class Meta:
        model = SoftwareStandard
        fields = (
            'device_types', 'platforms', 'device_tags', 'approved_versions', 'preferred_version',
            'valid_from', 'valid_to', 'description', 'comments', 'tags',
        )
        widgets = {'valid_from': DatePicker(), 'valid_to': DatePicker()}
        help_texts = {
            'valid_from': 'The date we adopted this standard.',
            'valid_to': 'Leave empty while this standard is current. Set it when '
                        'superseding, so the history stays queryable.',
        }

    def clean(self):
        super().clean()
        device_types = self.cleaned_data.get('device_types')
        platforms = self.cleaned_data.get('platforms')
        device_tags = self.cleaned_data.get('device_tags')
        if not (device_types or platforms or device_tags):
            raise forms.ValidationError('Select at least one device type, platform, or device tag.')
        if device_tags and (device_types or platforms):
            raise forms.ValidationError('Use device tags on their own for a tag-scoped standard.')

        # The model cannot check these on create — the M2Ms do not exist until
        # the row is saved — so they are enforced here against the submitted
        # data.
        approved = self.cleaned_data.get('approved_versions')
        preferred = self.cleaned_data.get('preferred_version')
        if approved and preferred and preferred not in approved:
            raise forms.ValidationError(
                {'preferred_version': 'The preferred version must be one of the approved versions.'}
            )

        # A platform-only standard approving versions of some other OS family
        # is always a mistake, and it would silently never match anything.
        # When device types are also in scope the check is skipped: the types
        # say nothing about their OS family, so a cross-family version may be
        # exactly what was meant.
        if platforms and not device_types and approved:
            allowed = {p.pk for p in platforms}
            wrong = [v for v in approved if v.platform_id not in allowed]
            if wrong:
                raise forms.ValidationError({
                    'approved_versions': 'These versions belong to none of the '
                    'selected platforms: %s' % ', '.join(str(v) for v in wrong)
                })

        # No scope may be claimed by two standards on the same day, or
        # compliance has two answers. Checked here with the submitted scopes;
        # the model's clean() covers date edits made outside this form.
        valid_from = self.cleaned_data.get('valid_from')
        valid_to = self.cleaned_data.get('valid_to')
        if valid_from:
            conflict = SoftwareStandard.conflicting_standards(
                device_types or (), platforms or (), valid_from, valid_to,
                exclude_pk=self.instance.pk, device_tags=device_tags or (),
            ).first()
            if conflict:
                raise forms.ValidationError(
                    'This overlaps an existing standard sharing part of its scope '
                    '(%s). Close that one out with an end date first.' % conflict
                )


class SoftwareStandardFilterForm(NetBoxModelFilterSetForm):
    model = SoftwareStandard
    device_type_id = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(), required=False, label='Device type',
    )
    platform_id = DynamicModelMultipleChoiceField(
        queryset=Platform.objects.all(), required=False, label='Platform',
    )
    device_tag_id = DynamicModelMultipleChoiceField(
        queryset=Tag.objects.all(), required=False, label='Device tag',
    )
    approved_version_id = DynamicModelMultipleChoiceField(
        queryset=SoftwareVersion.objects.all(), required=False, label='Approves version',
    )
    is_active = forms.NullBooleanField(required=False, label='In force today')
    valid_from__gte = forms.DateField(required=False, widget=DatePicker(), label='Adopted after')
    valid_from__lte = forms.DateField(required=False, widget=DatePicker(), label='Adopted before')
    tag = TagFilterField(model)


class SoftwareStandardBulkEditForm(NetBoxModelBulkEditForm):
    model = SoftwareStandard
    valid_to = forms.DateField(
        required=False, widget=DatePicker(),
        help_text='Close out several standards at once when superseding them',
    )
    description = forms.CharField(required=False, max_length=200)

    nullable_fields = ('valid_to', 'description')


class DeviceSoftwareForm(NetBoxModelForm):
    device = DynamicModelChoiceField(queryset=Device.objects.all(), selector=True)
    software_version = DynamicModelChoiceField(
        queryset=SoftwareVersion.objects.all(), required=False,
        help_text='Leave empty if the running version is genuinely unknown',
    )

    fieldsets = (
        FieldSet('device', 'software_version', 'raw_version', name='Running software'),
        FieldSet('source', 'collected_at', 'last_checked', name='Provenance'),
        FieldSet(
            'exempt', 'exempt_reason', 'exempt_approved_by', 'exempt_approved_on',
            'exempt_review_by', name='Do not upgrade',
        ),
        FieldSet('description', name='Reference'),
        FieldSet('tags', name='Tags'),
    )

    class Meta:
        model = DeviceSoftware
        fields = (
            'device', 'software_version', 'raw_version', 'source',
            'collected_at', 'last_checked',
            'exempt', 'exempt_reason', 'exempt_approved_by', 'exempt_approved_on',
            'exempt_review_by', 'description', 'comments', 'tags',
        )
        widgets = {
            'collected_at': DateTimePicker(),
            'last_checked': DateTimePicker(),
            'exempt_approved_on': DatePicker(),
            'exempt_review_by': DatePicker(),
        }


class DeviceSoftwareFilterForm(NetBoxModelFilterSetForm):
    model = DeviceSoftware
    device_id = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), required=False, label='Device',
    )
    site_id = DynamicModelMultipleChoiceField(
        queryset=Site.objects.all(), required=False, label='Site',
    )
    platform_id = DynamicModelMultipleChoiceField(
        queryset=Platform.objects.all(), required=False, label='Platform',
    )
    software_version_id = DynamicModelMultipleChoiceField(
        queryset=SoftwareVersion.objects.all(), required=False, label='Version',
    )
    source = forms.MultipleChoiceField(choices=SoftwareSourceChoices, required=False)
    exempt = forms.NullBooleanField(required=False, label='Do not upgrade')
    has_version = forms.NullBooleanField(required=False, label='Version known')
    is_stale = forms.NullBooleanField(required=False, label='Reading is stale')
    tag = TagFilterField(model)


class DeviceSoftwareImportForm(NetBoxModelImportForm):
    """Bulk import of running versions.

    `platform` and `version` are matched against the version catalogue and the
    version is CREATED if it is not there yet. That is deliberate: the point of
    this importer is initial population from whatever inventory you already
    have, and demanding that every version be catalogued by hand first would
    make it useless for exactly that job.
    """

    device = CSVModelChoiceField(
        queryset=Device.objects.all(), to_field_name='name', help_text='Device name',
    )
    platform = CSVModelChoiceField(
        queryset=Platform.objects.all(), to_field_name='name', required=False,
        help_text='Platform of the running version; defaults to the device platform',
    )
    version = forms.CharField(
        required=False, help_text='Running version, exactly as the device reports it',
    )
    source = CSVChoiceField(choices=SoftwareSourceChoices, required=False)

    class Meta:
        model = DeviceSoftware
        fields = (
            'device', 'platform', 'version', 'source', 'collected_at',
            'exempt', 'exempt_reason', 'exempt_approved_by', 'exempt_approved_on',
            'exempt_review_by', 'description', 'comments', 'tags',
        )

    def clean(self):
        super().clean()
        device = self.cleaned_data.get('device')
        version = (self.cleaned_data.get('version') or '').strip()
        platform = self.cleaned_data.get('platform') or (device.platform if device else None)

        if not version:
            return  # a row that only sets an exemption is legitimate

        if platform is None:
            raise forms.ValidationError({
                'platform': 'Give a platform, or set one on the device — a version '
                            'string means nothing without knowing the OS family.'
            })

        software_version, _created = SoftwareVersion.objects.get_or_create(
            platform=platform, version=version,
        )
        self.instance.software_version = software_version
        self.instance.raw_version = version
        if not self.cleaned_data.get('source'):
            self.instance.source = SoftwareSourceChoices.SOURCE_IMPORT


class DeviceSoftwareBulkEditForm(NetBoxModelBulkEditForm):
    model = DeviceSoftware
    software_version = DynamicModelChoiceField(
        queryset=SoftwareVersion.objects.all(), required=False,
    )
    source = forms.ChoiceField(
        choices=[('', '---------')] + list(SoftwareSourceChoices), required=False,
    )
    exempt = forms.NullBooleanField(required=False, label='Do not upgrade')
    exempt_reason = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))
    exempt_approved_by = forms.CharField(required=False, max_length=100)
    exempt_review_by = forms.DateField(required=False, widget=DatePicker())
    description = forms.CharField(required=False, max_length=200)

    nullable_fields = (
        'software_version', 'exempt_reason', 'exempt_approved_by', 'exempt_review_by',
        'description',
    )


class ComplianceReportForm(forms.Form):
    """Filters for the software compliance report."""

    as_of = forms.DateField(
        required=False, widget=DatePicker(), label='Standard as of',
        help_text='Which standard was in force on this date. The running versions '
                  'shown are always the current ones — we do not snapshot those.',
    )
    site = DynamicModelMultipleChoiceField(queryset=Site.objects.all(), required=False)
    role = DynamicModelMultipleChoiceField(
        queryset=DeviceRole.objects.all(), required=False, label='Device role',
    )
    platform = DynamicModelMultipleChoiceField(
        queryset=Platform.objects.all(), required=False,
    )
    manufacturer = DynamicModelMultipleChoiceField(
        queryset=Manufacturer.objects.all(), required=False,
    )
    device_type = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(), required=False,
    )
    status = forms.MultipleChoiceField(
        choices=ComplianceStatusChoices, required=False,
        help_text='Leave empty to show every state, including exempt devices',
    )


# --------------------------------------------------------------------------- #
# Replacement prices
# --------------------------------------------------------------------------- #

class ReplacementPriceForm(NetBoxModelForm):
    device_type = DynamicModelChoiceField(
        queryset=DeviceType.objects.all(), required=False, selector=True,
        help_text='The model being BOUGHT — the replacement, not the hardware '
                  'on its way out',
    )
    module_type = DynamicModelChoiceField(
        queryset=ModuleType.objects.all(), required=False, selector=True,
    )
    region = DynamicModelChoiceField(
        queryset=Region.objects.all(), required=False,
        help_text='Covers every site at or below this region. A more specific '
                  'region or site price still wins.',
    )
    site = DynamicModelChoiceField(
        queryset=Site.objects.all(), required=False,
        help_text='Covers exactly this site',
    )

    fieldsets = (
        FieldSet(
            TabbedGroups(
                FieldSet('device_type', name='Device Type'),
                FieldSet('module_type', name='Module Type'),
            ),
            name='Hardware model being purchased',
        ),
        FieldSet('region', 'site', name='Where this price applies (pick one)'),
        FieldSet('cost', 'currency', 'cost_updated', name='Price'),
        FieldSet('description', name='Reference'),
        FieldSet('tags', name='Tags'),
    )

    class Meta:
        model = ReplacementPrice
        fields = ('device_type', 'module_type', 'region', 'site', 'cost', 'currency',
                  'cost_updated', 'description', 'comments', 'tags')
        widgets = {'cost_updated': DatePicker()}


class ReplacementPriceFilterForm(NetBoxModelFilterSetForm):
    model = ReplacementPrice
    device_type_id = DynamicModelMultipleChoiceField(
        queryset=DeviceType.objects.all(), required=False, label='Device type',
    )
    module_type_id = DynamicModelMultipleChoiceField(
        queryset=ModuleType.objects.all(), required=False, label='Module type',
    )
    region_id = DynamicModelMultipleChoiceField(
        queryset=Region.objects.all(), required=False, label='Region',
    )
    site_id = DynamicModelMultipleChoiceField(
        queryset=Site.objects.all(), required=False, label='Site',
    )
    currency = forms.CharField(required=False, max_length=3)
    tag = TagFilterField(model)


class ReplacementPriceImportForm(NetBoxModelImportForm):
    """CSV import, because country price lists arrive as spreadsheets.

    Rows name the hardware model being bought and the region or site the
    price holds in. A row whose (model, scope) already has a price UPDATES
    it, so re-importing next year's sheet revises prices instead of erroring
    on every line.
    """

    device_type = CSVModelChoiceField(
        queryset=DeviceType.objects.all(), to_field_name='model', required=False,
        help_text='Model name of the device type being bought (this or module_type)',
    )
    module_type = CSVModelChoiceField(
        queryset=ModuleType.objects.all(), to_field_name='model', required=False,
        help_text='Model name of the module type being bought',
    )
    region = CSVModelChoiceField(
        queryset=Region.objects.all(), to_field_name='name', required=False,
        help_text='Region name (this or site)',
    )
    site = CSVModelChoiceField(
        queryset=Site.objects.all(), to_field_name='name', required=False,
        help_text='Site name',
    )
    cost_updated = forms.DateField(required=False, input_formats=CSV_DATE_INPUT_FORMATS)

    class Meta:
        model = ReplacementPrice
        fields = ('device_type', 'module_type', 'region', 'site',
                  'cost', 'currency', 'cost_updated', 'description', 'comments', 'tags')

    def clean(self):
        super().clean()
        device_type = self.cleaned_data.get('device_type')
        module_type = self.cleaned_data.get('module_type')
        if bool(device_type) == bool(module_type):
            raise forms.ValidationError('Provide either device_type or module_type — exactly one.')
        region = self.cleaned_data.get('region')
        site = self.cleaned_data.get('site')
        if bool(region) == bool(site):
            raise forms.ValidationError('Provide either region or site — exactly one.')

        # Same-scope rows revise the existing price rather than colliding with
        # the unique constraint; construct_instance builds onto this instance,
        # so the save becomes an UPDATE.
        existing = ReplacementPrice.objects.filter(
            device_type=device_type, module_type=module_type,
            region=region, site=site,
        ).first()
        if existing is not None and not self.instance.pk:
            self.instance = existing


class WorksheetPickerForm(forms.Form):
    """Choosing which replacement model the worksheet prices."""

    device_type = DynamicModelChoiceField(
        queryset=DeviceType.objects.all(), required=False, selector=True,
        label='Replacement model',
        help_text='The model being purchased. The worksheet lists every site '
                  'holding hardware that this model replaces.',
    )
