import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from netbox_refresh.models import (
    DeviceSoftware,
    ModelLifecycle,
    ReplacementPrice,
    SoftwareStandard,
    SoftwareVersion,
)

__all__ = (
    'ModelLifecycleTable',
    'RefreshReportTable',
    'SoftwareVersionTable',
    'SoftwareStandardTable',
    'DeviceSoftwareTable',
    'ComplianceReportTable',
    'VersionRollupTable',
)

# Rendered next to a version that nothing has confirmed recently. The compliance
# state and the freshness of the reading are separate facts, and a stale reading
# should not be able to hide behind a green badge.
STALE_MARKER = (
    '{% if record.is_stale %}<span class="text-warning ms-1" '
    'title="Not confirmed recently"><i class="mdi mdi-clock-alert-outline"></i></span>{% endif %}'
)


class ModelLifecycleTable(NetBoxTable):
    assigned_object = tables.Column(
        linkify=True, orderable=False, verbose_name='Hardware model',
    )
    manufacturer = tables.Column(linkify=True, orderable=False)
    part_number = tables.Column(orderable=False)
    status = columns.TemplateColumn(
        template_code='{% load helpers %}{% badge record.get_status_display '
                      'bg_color=record.get_status_color %}',
        orderable=False,
    )
    replacement = tables.Column(linkify=True, orderable=False)
    installed_count = tables.Column(orderable=False, verbose_name='Installed')
    extended_cost = tables.Column(orderable=False, verbose_name='Total cost')
    source = columns.ChoiceFieldColumn()
    # Not a stored column, so it cannot be ordered on here — the refresh report
    # annotates the same value in SQL when it needs to sort or filter.
    effective_end_of_life = columns.DateColumn(
        orderable=False, verbose_name='Functional end of life',
        accessor='effective_end_of_life',
    )

    class Meta(NetBoxTable.Meta):
        model = ModelLifecycle
        fields = (
            'pk', 'assigned_object', 'manufacturer', 'part_number', 'status',
            'effective_end_of_life',
            'announcement_date', 'end_of_sale', 'end_of_sw_maintenance',
            'end_of_security_support', 'end_of_routine_failure_analysis',
            'end_of_service_attach', 'end_of_service_contract_renewal', 'end_of_support',
            'replacement', 'replacement_cost', 'currency', 'installed_count',
            'extended_cost', 'cost_updated', 'bulletin_number', 'source', 'last_synced',
            'last_checked', 'description',
        )
        default_columns = (
            'assigned_object', 'part_number', 'status', 'end_of_sale',
            'end_of_support', 'effective_end_of_life', 'last_checked',
            'replacement', 'replacement_cost', 'installed_count', 'extended_cost',
        )


class ReplacementPriceTable(NetBoxTable):
    hardware_model = tables.Column(
        linkify=True, orderable=False, verbose_name='Model purchased',
    )
    scope = tables.Column(linkify=True, orderable=False, verbose_name='Applies to')
    cost = tables.Column()
    currency = tables.Column()

    class Meta(NetBoxTable.Meta):
        model = ReplacementPrice
        fields = ('pk', 'id', 'hardware_model', 'scope', 'cost', 'currency',
                  'cost_updated', 'description', 'created', 'last_updated')
        default_columns = ('hardware_model', 'scope', 'cost', 'currency', 'cost_updated')


class RegionCostTable(tables.Table):
    """Report rows are plain dicts assembled by the view, not model instances."""

    region = tables.Column(linkify=lambda record: record['region_url'])
    units = tables.Column(verbose_name='Units')
    total = tables.Column(verbose_name='Replacement cost')
    unpriced = tables.Column(verbose_name='Unpriced units')

    class Meta:
        attrs = {'class': 'table table-hover object-list'}
        empty_text = 'Nothing to price in the selected window.'
        orderable = False


class UnconfiguredModelTable(tables.Table):
    """Hardware models with no lifecycle: plain dict rows from the view."""

    model = tables.Column(linkify=lambda record: record['url'], verbose_name='Hardware model')
    manufacturer = tables.Column(linkify=True)
    part_number = tables.Column()
    kind = tables.Column(verbose_name='Kind')
    installed = tables.Column(verbose_name='Installed')
    lifecycle = columns.TemplateColumn(
        template_code='<span class="text-muted">None configured</span>',
        verbose_name='Lifecycle', orderable=False,
    )
    actions = columns.TemplateColumn(
        template_code='{% if perms.netbox_refresh.add_modellifecycle %}'
                      '<a href="{{ record.add_url }}" class="btn btn-sm btn-primary">'
                      '<i class="mdi mdi-plus-thick" aria-hidden="true"></i> Add lifecycle</a>'
                      '{% endif %}',
        verbose_name='', orderable=False,
    )

    class Meta:
        attrs = {'class': 'table table-hover object-list'}
        empty_text = 'Every hardware model in NetBox has a lifecycle.'
        orderable = False


class SiteCostTable(tables.Table):
    """Report rows are plain dicts assembled by the view, not model instances."""

    site = tables.Column(linkify=lambda record: record['site_url'])
    region = tables.Column(linkify=lambda record: record['region_url'])
    models = tables.Column(verbose_name='Models')
    units = tables.Column(verbose_name='Units')
    total = tables.Column(verbose_name='Replacement cost')
    unpriced = tables.Column(verbose_name='Unpriced units')

    class Meta:
        attrs = {'class': 'table table-hover object-list'}
        empty_text = 'Nothing to price in the selected window.'
        orderable = False


class SiteRefreshTable(tables.Table):
    """One row per site and model: what each site has to replace.

    Report rows are plain dicts assembled by the view, not model instances.
    """

    site = tables.Column(linkify=lambda record: record['site_url'])
    region = tables.Column(linkify=lambda record: record['region_url'])
    model = tables.Column(linkify=lambda record: record['url'], verbose_name='Hardware model')
    manufacturer = tables.Column()
    part_number = tables.Column()
    milestone_date = tables.DateColumn(verbose_name='Milestone')
    installed = tables.Column(verbose_name='Units')
    replacement = tables.Column(linkify=lambda record: record['replacement_url'])
    unit_cost = tables.Column(verbose_name='Unit cost here')
    extended_cost = tables.Column()

    class Meta:
        attrs = {'class': 'table table-hover object-list'}
        empty_text = 'No hardware at any site reaches this milestone in the selected window.'
        orderable = False


class RefreshReportTable(tables.Table):
    """Report rows are plain dicts assembled by the view, not model instances."""

    model = tables.Column(linkify=lambda record: record['url'], verbose_name='Hardware model')
    manufacturer = tables.Column()
    part_number = tables.Column()
    milestone_date = tables.DateColumn(verbose_name='Milestone')
    installed = tables.Column()
    replacement = tables.Column(linkify=lambda record: record['replacement_url'])
    unit_cost = tables.Column()
    extended_cost = tables.Column()

    class Meta:
        attrs = {'class': 'table table-hover object-list'}
        empty_text = 'No hardware models reach this milestone in the selected window.'
        orderable = False


class SoftwareVersionTable(NetBoxTable):
    version = tables.Column(linkify=True)
    platform = tables.Column(linkify=True)
    image = columns.TemplateColumn(
        template_code='{% if record.download_url %}'
                      '<a href="{{ record.download_url }}">'
                      '<i class="mdi mdi-download"></i> {{ record.image_filename|default:"Download" }}</a>'
                      '{% else %}<span class="text-muted">&mdash;</span>{% endif %}',
        orderable=False,
    )
    image_size = columns.TemplateColumn(
        template_code='{% if record.image_size %}{{ record.image_size|filesizeformat }}'
                      '{% else %}<span class="text-muted">&mdash;</span>{% endif %}',
        verbose_name='Size',
    )
    checksum_type = columns.ChoiceFieldColumn()
    installed_count = tables.Column(orderable=False, verbose_name='Devices')

    class Meta(NetBoxTable.Meta):
        model = SoftwareVersion
        fields = (
            'pk', 'id', 'version', 'platform', 'release_date', 'end_of_support', 'image',
            'image_filename', 'image_url', 'image_size', 'checksum_type', 'checksum',
            'installed_count', 'description', 'created', 'last_updated',
        )
        default_columns = (
            'version', 'platform', 'release_date', 'end_of_support', 'image', 'image_size',
            'installed_count',
        )


class SoftwareStandardTable(NetBoxTable):
    device_types = columns.ManyToManyColumn(
        linkify_item=True, verbose_name='Device types',
    )
    platforms = columns.ManyToManyColumn(
        linkify_item=True, verbose_name='Platforms',
    )
    device_tags = columns.ManyToManyColumn(linkify_item=True, verbose_name='Device tags')
    approved_versions = columns.ManyToManyColumn(
        linkify_item=True, verbose_name='Approved versions',
    )
    preferred_version = tables.Column(linkify=True)
    is_active = columns.BooleanColumn(orderable=False, verbose_name='In force')

    class Meta(NetBoxTable.Meta):
        model = SoftwareStandard
        fields = (
            'pk', 'id', 'device_types', 'platforms', 'device_tags', 'approved_versions',
            'preferred_version', 'valid_from', 'valid_to', 'is_active',
            'description', 'created', 'last_updated',
        )
        default_columns = (
            'device_types', 'platforms', 'device_tags', 'approved_versions', 'preferred_version',
            'valid_from', 'valid_to', 'is_active',
        )


class DeviceSoftwareTable(NetBoxTable):
    device = tables.Column(linkify=True)
    software_version = tables.Column(linkify=True, verbose_name='Running version')
    compliance = columns.TemplateColumn(
        template_code='{% load helpers %}{% badge record.get_compliance_status_display '
                      'bg_color=record.get_compliance_status_color %}' + STALE_MARKER,
        orderable=False,
    )
    source = columns.ChoiceFieldColumn()
    site = tables.Column(accessor='device__site', linkify=True, verbose_name='Site')
    exempt = columns.BooleanColumn(verbose_name='Do not upgrade')
    as_of = columns.DateTimeColumn(orderable=False, verbose_name='Confirmed')

    class Meta(NetBoxTable.Meta):
        model = DeviceSoftware
        fields = (
            'pk', 'id', 'device', 'site', 'software_version', 'raw_version',
            'compliance', 'source', 'collected_at', 'last_checked', 'as_of',
            'exempt', 'exempt_reason', 'exempt_approved_by', 'exempt_approved_on',
            'exempt_review_by', 'description', 'created', 'last_updated',
        )
        default_columns = (
            'device', 'site', 'software_version', 'compliance', 'source', 'as_of',
        )


class ComplianceReportTable(tables.Table):
    """Report rows are plain dicts assembled by the view, not model instances.

    Rows come from the Device queryset rather than from DeviceSoftware, so a
    device with no software record at all still appears — as Unknown. Dropping
    those is the failure mode that makes a compliance report worse than useless.
    """

    device = tables.Column(linkify=lambda record: record['device'].get_absolute_url())
    site = tables.Column(accessor='device__site', linkify=True)
    device_type = tables.Column(accessor='device__device_type', linkify=True)
    platform = tables.Column(accessor='device__platform', linkify=True)
    version = tables.Column(verbose_name='Running version')
    status = columns.TemplateColumn(
        template_code='{% load helpers %}{% badge record.status_label '
                      'bg_color=record.status_color %}' + STALE_MARKER,
        verbose_name='Compliance',
    )
    approved = tables.Column(verbose_name='Approved versions')
    source = tables.Column()
    as_of = tables.DateTimeColumn(verbose_name='Confirmed')

    class Meta:
        attrs = {'class': 'table table-hover object-list'}
        empty_text = 'No devices match these filters.'
        orderable = False


class VersionRollupTable(tables.Table):
    """Devices per version per model — the view that drives upgrade planning."""

    device_type = tables.Column(verbose_name='Device type')
    platform = tables.Column()
    version = tables.Column(
        linkify=lambda record: record['version_url'], verbose_name='Running version'
    )
    count = tables.Column(verbose_name='Devices')
    status = columns.TemplateColumn(
        template_code='{% load helpers %}{% badge record.status_label '
                      'bg_color=record.status_color %}',
        verbose_name='Compliance',
    )

    class Meta:
        attrs = {'class': 'table table-hover object-list'}
        empty_text = 'No devices match these filters.'
        orderable = False
