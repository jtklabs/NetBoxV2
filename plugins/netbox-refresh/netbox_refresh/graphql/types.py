"""GraphQL object types for the Lifecycle plugin.

NetBox discovers a plugin's GraphQL schema and merges it into the single
top-level Query, so these types appear in the same `/graphql/` endpoint and
the same GraphiQL explorer as the core ones. Object permissions still apply:
PrimaryObjectType brings BaseObjectType.get_queryset, which restricts the
queryset for the requesting user exactly as the REST API does.

Two things are exposed here that are properties rather than columns —
download_url and compliance_status — because a GraphQL consumer asking "what
should this device be running and where do I get it" should not have to
re-implement the rules. They resolve per object, so they cannot be filtered or
ordered on; see filters.py.
"""

from datetime import date
from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from netbox.graphql.types import ContentTypeType, PrimaryObjectType

from netbox_refresh import models
from netbox_refresh.graphql.filters import (
    DeviceSoftwareFilter,
    ModelLifecycleFilter,
    ReplacementPriceFilter,
    SoftwareStandardFilter,
    SoftwareVersionFilter,
)

if TYPE_CHECKING:
    from dcim.graphql.types import (
        DeviceType, DeviceTypeType, ModuleTypeType, PlatformType, RegionType, SiteType,
    )

__all__ = (
    'ModelLifecycleType',
    'ReplacementPriceType',
    'SoftwareVersionType',
    'SoftwareStandardType',
    'DeviceSoftwareType',
)


@strawberry_django.type(
    models.ModelLifecycle,
    fields='__all__',
    filters=ModelLifecycleFilter,
    pagination=True,
)
class ModelLifecycleType(PrimaryObjectType):
    assigned_object_type: Annotated['ContentTypeType', strawberry.lazy('netbox.graphql.types')] | None
    replacement_device_type: Annotated['DeviceTypeType', strawberry.lazy('dcim.graphql.types')] | None
    replacement_module_type: Annotated['ModuleTypeType', strawberry.lazy('dcim.graphql.types')] | None

    # The generic FK, resolved to whichever hardware model it points at.
    assigned_object: Annotated[
        Annotated['DeviceTypeType', strawberry.lazy('dcim.graphql.types')]
        | Annotated['ModuleTypeType', strawberry.lazy('dcim.graphql.types')],
        strawberry.union('ModelLifecycleAssignmentType'),
    ] | None

    @strawberry_django.field
    def status(self) -> str:
        """Derived from the dates — see ModelLifecycle.status."""
        return self.status

    @strawberry_django.field
    def effective_end_of_life(self) -> date | None:
        return self.effective_end_of_life


@strawberry_django.type(
    models.ReplacementPrice,
    fields='__all__',
    filters=ReplacementPriceFilter,
    pagination=True,
)
class ReplacementPriceType(PrimaryObjectType):
    device_type: Annotated['DeviceTypeType', strawberry.lazy('dcim.graphql.types')] | None
    module_type: Annotated['ModuleTypeType', strawberry.lazy('dcim.graphql.types')] | None
    region: Annotated['RegionType', strawberry.lazy('dcim.graphql.types')] | None
    site: Annotated['SiteType', strawberry.lazy('dcim.graphql.types')] | None


@strawberry_django.type(
    models.SoftwareVersion,
    fields='__all__',
    filters=SoftwareVersionFilter,
    pagination=True,
)
class SoftwareVersionType(PrimaryObjectType):
    platform: Annotated['PlatformType', strawberry.lazy('dcim.graphql.types')]

    @strawberry_django.field
    def download_url(self) -> str:
        """Explicit image_url, else derived from the filename + image_base_url.

        Empty string, not null, when no image location is recorded — matches
        the model property rather than inventing a different contract here.
        """
        return self.download_url

    @strawberry_django.field
    def installed_count(self) -> int:
        return self.installed_count


@strawberry_django.type(
    models.SoftwareStandard,
    fields='__all__',
    filters=SoftwareStandardFilter,
    pagination=True,
)
class SoftwareStandardType(PrimaryObjectType):
    approved_versions: list[Annotated['SoftwareVersionType', strawberry.lazy('netbox_refresh.graphql.types')]]
    preferred_version: Annotated['SoftwareVersionType', strawberry.lazy('netbox_refresh.graphql.types')] | None

    # Device tags select a mode-specific standard before the general scopes.
    device_types: list[Annotated['DeviceTypeType', strawberry.lazy('dcim.graphql.types')]]
    platforms: list[Annotated['PlatformType', strawberry.lazy('dcim.graphql.types')]]
    device_tags: list[Annotated['TagType', strawberry.lazy('extras.graphql.types')]]

    @strawberry_django.field
    def is_active(self) -> bool:
        return self.is_active


@strawberry_django.type(
    models.DeviceSoftware,
    fields='__all__',
    filters=DeviceSoftwareFilter,
    pagination=True,
)
class DeviceSoftwareType(PrimaryObjectType):
    device: Annotated['DeviceType', strawberry.lazy('dcim.graphql.types')]
    software_version: Annotated['SoftwareVersionType', strawberry.lazy('netbox_refresh.graphql.types')] | None

    @strawberry_django.field
    def compliance_status(self) -> str:
        """One of compliant / non-compliant / unknown / no-standard / exempt /
        exempt-expired. Resolved per object against the standard in force
        today, so asking for it across a large list costs a query per row —
        prefer the REST compliance report for whole-fleet reporting."""
        return self.compliance_status

    @strawberry_django.field
    def standard(self) -> Annotated['SoftwareStandardType', strawberry.lazy('netbox_refresh.graphql.types')] | None:
        return self.standard

    @strawberry_django.field
    def is_stale(self) -> bool:
        return self.is_stale
