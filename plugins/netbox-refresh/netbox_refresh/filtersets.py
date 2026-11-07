from datetime import timedelta

import django_filters
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
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from netbox.filtersets import NetBoxModelFilterSet

from netbox_refresh.choices import (
    ChecksumTypeChoices,
    LifecycleSourceChoices,
    LifecycleStatusChoices,
    SoftwareSourceChoices,
)
from netbox_refresh.models import (
    EFFECTIVE_EOL_ALIAS,
    LIFECYCLE_DATE_FIELDS,
    effective_end_of_life_expression,
    DeviceSoftware,
    ModelLifecycle,
    ReplacementPrice,
    SoftwareStandard,
    SoftwareVersion,
)

__all__ = (
    'ModelLifecycleFilterSet',
    'ReplacementPriceFilterSet',
    'SoftwareVersionFilterSet',
    'SoftwareStandardFilterSet',
    'DeviceSoftwareFilterSet',
)


class ModelLifecycleFilterSet(NetBoxModelFilterSet):
    assigned_object_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=ContentType.objects.all()
    )
    manufacturer_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Manufacturer.objects.all(), method='filter_manufacturer',
        label='Manufacturer',
    )
    source = django_filters.MultipleChoiceFilter(choices=LifecycleSourceChoices)
    replacement_device_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceType.objects.all(), label='Replacement device type',
    )
    replacement_module_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=ModuleType.objects.all(), label='Replacement module type',
    )
    has_replacement = django_filters.BooleanFilter(
        method='filter_has_replacement', label='Has a replacement model',
    )
    has_cost = django_filters.BooleanFilter(
        method='filter_has_cost', label='Has a replacement cost',
    )
    status = django_filters.MultipleChoiceFilter(
        choices=LifecycleStatusChoices, method='filter_status', label='Status',
    )

    class Meta:
        model = ModelLifecycle
        # Date ranges drive the refresh report, so every lifecycle date gets
        # gte/lte lookups (end_of_support__gte=..., end_of_support__lte=...).
        fields = {
            'id': ['exact'],
            'last_checked': ['exact', 'gte', 'lte'],
            'announcement_date': ['exact', 'gte', 'lte'],
            'end_of_sale': ['exact', 'gte', 'lte'],
            'end_of_sw_maintenance': ['exact', 'gte', 'lte'],
            'end_of_security_support': ['exact', 'gte', 'lte'],
            'end_of_service_attach': ['exact', 'gte', 'lte'],
            'end_of_service_contract_renewal': ['exact', 'gte', 'lte'],
            'end_of_support': ['exact', 'gte', 'lte'],
            'replacement_cost': ['exact', 'gte', 'lte'],
            'currency': ['exact'],
            'bulletin_number': ['exact', 'icontains'],
        }

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        device_types = DeviceType.objects.filter(
            Q(model__icontains=value) | Q(part_number__icontains=value)
        ).values_list('pk', flat=True)
        module_types = ModuleType.objects.filter(
            Q(model__icontains=value) | Q(part_number__icontains=value)
        ).values_list('pk', flat=True)
        return queryset.filter(
            Q(bulletin_number__icontains=value)
            | Q(description__icontains=value)
            | Q(replacement_notes__icontains=value)
            | Q(assigned_object_type__model='devicetype', assigned_object_id__in=list(device_types))
            | Q(assigned_object_type__model='moduletype', assigned_object_id__in=list(module_types))
        ).distinct()

    def filter_manufacturer(self, queryset, name, value):
        if not value:
            return queryset
        device_types = DeviceType.objects.filter(manufacturer__in=value).values_list('pk', flat=True)
        module_types = ModuleType.objects.filter(manufacturer__in=value).values_list('pk', flat=True)
        return queryset.filter(
            Q(assigned_object_type__model='devicetype', assigned_object_id__in=list(device_types))
            | Q(assigned_object_type__model='moduletype', assigned_object_id__in=list(module_types))
        )

    def filter_has_replacement(self, queryset, name, value):
        query = Q(replacement_device_type__isnull=False) | Q(replacement_module_type__isnull=False)
        return queryset.filter(query) if value else queryset.exclude(query)

    def filter_has_cost(self, queryset, name, value):
        if value:
            return queryset.filter(replacement_cost__isnull=False)
        return queryset.filter(replacement_cost__isnull=True)

    def filter_status(self, queryset, name, value):
        """The SQL form of ModelLifecycle.status, so the list can be cut by it.

        Mirrors the property's precedence exactly — past end of life, then
        past end of sale, then anything announced, then checked-but-nothing,
        then never checked. Keep the two in step: a status that filters one
        way and renders another is worse than no filter.
        """
        if not value:
            return queryset
        today = timezone.localdate()
        queryset = queryset.annotate(
            **{EFFECTIVE_EOL_ALIAS: effective_end_of_life_expression()}
        )
        # Every negation below runs over nullable columns, and SQL's
        # NOT (NULL <= today) is NULL — which drops the row — rather than
        # true. Spelling out IS NOT NULL inside each positive term makes its
        # negation read "null or in the future", which is what is meant.
        past_eol = (Q(**{'%s__isnull' % EFFECTIVE_EOL_ALIAS: False})
                    & Q(**{'%s__lte' % EFFECTIVE_EOL_ALIAS: today}))
        past_eos = ~past_eol & Q(end_of_sale__isnull=False) & Q(end_of_sale__lte=today)
        any_date = Q()
        for field_name in LIFECYCLE_DATE_FIELDS:
            any_date |= Q(**{'%s__isnull' % field_name: False})
        announced = ~past_eol & ~past_eos & any_date
        checked = Q(last_checked__isnull=False) | Q(last_synced__isnull=False)
        by_status = {
            LifecycleStatusChoices.STATUS_END_OF_SUPPORT: past_eol,
            LifecycleStatusChoices.STATUS_END_OF_SALE: past_eos,
            LifecycleStatusChoices.STATUS_EOS_ANNOUNCED: announced,
            LifecycleStatusChoices.STATUS_NOT_ANNOUNCED: ~any_date & checked,
            LifecycleStatusChoices.STATUS_UNKNOWN: ~any_date & ~checked,
        }
        query = Q()
        for status in value:
            if status in by_status:
                query |= by_status[status]
        return queryset.filter(query)


class ReplacementPriceFilterSet(NetBoxModelFilterSet):
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=DeviceType.objects.all(), label='Device type',
    )
    module_type_id = django_filters.ModelMultipleChoiceFilter(
        queryset=ModuleType.objects.all(), label='Module type',
    )
    region_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Region.objects.all(), label='Region',
    )
    site_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Site.objects.all(), label='Site',
    )

    class Meta:
        model = ReplacementPrice
        fields = ('id', 'currency')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(description__icontains=value)
            | Q(device_type__model__icontains=value)
            | Q(module_type__model__icontains=value)
            | Q(region__name__icontains=value)
            | Q(site__name__icontains=value)
            | Q(currency__iexact=value.strip())
        )


class SoftwareVersionFilterSet(NetBoxModelFilterSet):
    platform_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Platform.objects.all(), label='Platform',
    )
    checksum_type = django_filters.MultipleChoiceFilter(choices=ChecksumTypeChoices)
    has_image = django_filters.BooleanFilter(
        method='filter_has_image', label='Has a downloadable image',
    )

    class Meta:
        model = SoftwareVersion
        fields = {
            'id': ['exact'],
            'version': ['exact', 'icontains'],
            'release_date': ['exact', 'gte', 'lte'],
            'end_of_support': ['exact', 'gte', 'lte'],
            'image_filename': ['exact', 'icontains'],
            'checksum': ['exact'],
        }

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(version__icontains=value)
            | Q(image_filename__icontains=value)
            | Q(platform__name__icontains=value)
            | Q(description__icontains=value)
            | Q(comments__icontains=value)
        ).distinct()

    def filter_has_image(self, queryset, name, value):
        # image_filename counts because a filename plus the configured
        # image_base_url resolves to a working download link.
        query = ~Q(image_url='') | ~Q(image_filename='')
        return queryset.filter(query) if value else queryset.exclude(query)


class SoftwareStandardFilterSet(NetBoxModelFilterSet):
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        field_name='device_types', queryset=DeviceType.objects.all(),
        label='Device type', distinct=True,
    )
    platform_id = django_filters.ModelMultipleChoiceFilter(
        field_name='platforms', queryset=Platform.objects.all(), label='Platform',
        distinct=True,
    )
    approved_version_id = django_filters.ModelMultipleChoiceFilter(
        field_name='approved_versions', queryset=SoftwareVersion.objects.all(),
        label='Approves version',
    )
    preferred_version_id = django_filters.ModelMultipleChoiceFilter(
        queryset=SoftwareVersion.objects.all(), label='Preferred version',
    )
    is_active = django_filters.BooleanFilter(
        method='filter_is_active', label='In force today',
    )

    class Meta:
        model = SoftwareStandard
        fields = {
            'id': ['exact'],
            'valid_from': ['exact', 'gte', 'lte'],
            'valid_to': ['exact', 'gte', 'lte'],
        }

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(description__icontains=value)
            | Q(comments__icontains=value)
            | Q(approved_versions__version__icontains=value)
            | Q(device_types__model__icontains=value)
            | Q(platforms__name__icontains=value)
        ).distinct()

    def filter_is_active(self, queryset, name, value):
        today = timezone.localdate()
        query = Q(valid_from__lte=today) & (Q(valid_to__isnull=True) | Q(valid_to__gte=today))
        return queryset.filter(query) if value else queryset.exclude(query)


class DeviceSoftwareFilterSet(NetBoxModelFilterSet):
    device_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), label='Device',
    )
    site_id = django_filters.ModelMultipleChoiceFilter(
        field_name='device__site', queryset=Site.objects.all(), label='Site',
    )
    platform_id = django_filters.ModelMultipleChoiceFilter(
        field_name='device__platform', queryset=Platform.objects.all(),
        label='Device platform',
    )
    device_type_id = django_filters.ModelMultipleChoiceFilter(
        field_name='device__device_type', queryset=DeviceType.objects.all(),
        label='Device type',
    )
    role_id = django_filters.ModelMultipleChoiceFilter(
        field_name='device__role', queryset=DeviceRole.objects.all(), label='Device role',
    )
    software_version_id = django_filters.ModelMultipleChoiceFilter(
        queryset=SoftwareVersion.objects.all(), label='Version',
    )
    source = django_filters.MultipleChoiceFilter(choices=SoftwareSourceChoices)
    has_version = django_filters.BooleanFilter(
        method='filter_has_version', label='Version known',
    )
    is_stale = django_filters.BooleanFilter(
        method='filter_is_stale', label='Reading is stale',
    )

    class Meta:
        model = DeviceSoftware
        fields = {
            'id': ['exact'],
            'raw_version': ['exact', 'icontains'],
            'exempt': ['exact'],
            'collected_at': ['gte', 'lte'],
            'exempt_review_by': ['exact', 'gte', 'lte'],
        }

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(device__name__icontains=value)
            | Q(raw_version__icontains=value)
            | Q(software_version__version__icontains=value)
            | Q(exempt_reason__icontains=value)
            | Q(description__icontains=value)
        ).distinct()

    def filter_has_version(self, queryset, name, value):
        if value:
            return queryset.filter(software_version__isnull=False)
        return queryset.filter(software_version__isnull=True)

    def filter_is_stale(self, queryset, name, value):
        """Stale = we have a version, but nothing has confirmed it recently.

        as_of is a property (collected_at, else last_checked, else last_updated),
        so it is rebuilt here with Coalesce to keep the filter in the database
        instead of walking every row in Python.
        """
        days = settings.PLUGINS_CONFIG.get('netbox_refresh', {}).get('stale_after_days', 90)
        threshold = timezone.now() - timedelta(days=days)
        queryset = queryset.annotate(
            _as_of=Coalesce('collected_at', 'last_checked', 'last_updated')
        )
        if value:
            return queryset.filter(software_version__isnull=False, _as_of__lt=threshold)
        return queryset.filter(Q(software_version__isnull=True) | Q(_as_of__gte=threshold))
