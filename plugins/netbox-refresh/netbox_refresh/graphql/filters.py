"""GraphQL filters.

Mirrors the REST filtersets rather than reproducing them: strawberry-django
generates the per-field lookups from the model when `lookups=True`, so only
the relations and the fields people actually filter on are spelled out here.

Derived values — compliance_status, is_stale — are deliberately NOT filterable.
They are computed in Python from the standard in force, so there is no column
to filter on, and a filter that silently only worked on some rows would be
worse than not having one. Filter on the underlying facts (exempt,
software_version, collected_at) or use the REST filterset, which does the
derivation server-side.
"""

from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.filters import PrimaryModelFilter
from strawberry_django import DateFilterLookup, DatetimeFilterLookup, StrFilterLookup

from netbox_refresh import models

if TYPE_CHECKING:
    from dcim.graphql.filters import DeviceFilter, DeviceTypeFilter, PlatformFilter, RegionFilter, SiteFilter

__all__ = (
    'ModelLifecycleFilter',
    'SoftwareVersionFilter',
    'SoftwareStandardFilter',
    'DeviceSoftwareFilter',
)


@strawberry_django.filter_type(models.ModelLifecycle, lookups=True)
class ModelLifecycleFilter(PrimaryModelFilter):
    end_of_sale: DateFilterLookup | None = strawberry_django.filter_field()
    end_of_support: DateFilterLookup | None = strawberry_django.filter_field()
    bulletin_number: StrFilterLookup | None = strawberry_django.filter_field()
    source: StrFilterLookup | None = strawberry_django.filter_field()
    replacement_device_type: Annotated['DeviceTypeFilter', strawberry.lazy('dcim.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )


@strawberry_django.filter_type(models.ReplacementPrice, lookups=True)
class ReplacementPriceFilter(PrimaryModelFilter):
    currency: StrFilterLookup | None = strawberry_django.filter_field()
    cost_updated: DateFilterLookup | None = strawberry_django.filter_field()
    device_type: Annotated['DeviceTypeFilter', strawberry.lazy('dcim.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    region: Annotated['RegionFilter', strawberry.lazy('dcim.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    site: Annotated['SiteFilter', strawberry.lazy('dcim.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )


@strawberry_django.filter_type(models.SoftwareVersion, lookups=True)
class SoftwareVersionFilter(PrimaryModelFilter):
    version: StrFilterLookup | None = strawberry_django.filter_field()
    release_date: DateFilterLookup | None = strawberry_django.filter_field()
    image_filename: StrFilterLookup | None = strawberry_django.filter_field()
    checksum: StrFilterLookup | None = strawberry_django.filter_field()
    platform: Annotated['PlatformFilter', strawberry.lazy('dcim.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )


@strawberry_django.filter_type(models.SoftwareStandard, lookups=True)
class SoftwareStandardFilter(PrimaryModelFilter):
    valid_from: DateFilterLookup | None = strawberry_django.filter_field()
    valid_to: DateFilterLookup | None = strawberry_django.filter_field()
    # Filtering on either M2M joins the scope table, so a standard covering
    # three matching device types comes back three times. strawberry-django's
    # answer is the DISTINCT flag every filter input carries — pass
    # `filters: {device_types: {...}, DISTINCT: true}` when filtering on
    # these.
    device_types: Annotated['DeviceTypeFilter', strawberry.lazy('dcim.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    platforms: Annotated['PlatformFilter', strawberry.lazy('dcim.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    approved_versions: Annotated['SoftwareVersionFilter', strawberry.lazy('netbox_refresh.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    preferred_version: Annotated['SoftwareVersionFilter', strawberry.lazy('netbox_refresh.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )


@strawberry_django.filter_type(models.DeviceSoftware, lookups=True)
class DeviceSoftwareFilter(PrimaryModelFilter):
    raw_version: StrFilterLookup | None = strawberry_django.filter_field()
    source: StrFilterLookup | None = strawberry_django.filter_field()
    exempt: bool | None = strawberry_django.filter_field()
    collected_at: DatetimeFilterLookup | None = strawberry_django.filter_field()
    last_checked: DatetimeFilterLookup | None = strawberry_django.filter_field()
    exempt_review_by: DateFilterLookup | None = strawberry_django.filter_field()
    device: Annotated['DeviceFilter', strawberry.lazy('dcim.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    software_version: Annotated['SoftwareVersionFilter', strawberry.lazy('netbox_refresh.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
