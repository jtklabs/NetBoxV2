"""The plugin's slice of the GraphQL Query type.

NetBox merges every entry of `schema` into its single top-level Query class
(netbox/graphql/schema.py: `*registry['plugins']['graphql_schemas']`), so these
fields sit alongside the core ones at /graphql/ with no separate endpoint.

`schema` must be a LIST — the registry extends with it rather than appending.
RefreshConfig points at this module explicitly (`graphql_schema =
'graphql.schema.schema'`) instead of relying on the default lookup, because
the default resolves the attribute `schema` on the package `netbox_refresh.
graphql`, where it collides with this very module's name.
"""

import strawberry
import strawberry_django

from netbox_refresh.graphql.types import (
    DeviceSoftwareType,
    ModelLifecycleType,
    ReplacementPriceType,
    SoftwareStandardType,
    SoftwareVersionType,
)

__all__ = ('LifecycleQuery', 'schema')


@strawberry.type(name='Query')
class LifecycleQuery:
    model_lifecycle: ModelLifecycleType = strawberry_django.field()
    model_lifecycle_list: list[ModelLifecycleType] = strawberry_django.field()
    replacement_price: ReplacementPriceType = strawberry_django.field()
    replacement_price_list: list[ReplacementPriceType] = strawberry_django.field()

    software_version: SoftwareVersionType = strawberry_django.field()
    software_version_list: list[SoftwareVersionType] = strawberry_django.field()

    software_standard: SoftwareStandardType = strawberry_django.field()
    software_standard_list: list[SoftwareStandardType] = strawberry_django.field()

    device_software: DeviceSoftwareType = strawberry_django.field()
    device_software_list: list[DeviceSoftwareType] = strawberry_django.field()


schema = [LifecycleQuery]
