from netbox.search import SearchIndex, register_search

from netbox_refresh.models import (
    DeviceSoftware,
    ModelLifecycle,
    SoftwareStandard,
    SoftwareVersion,
)


@register_search
class ModelLifecycleIndex(SearchIndex):
    model = ModelLifecycle
    fields = (
        ('bulletin_number', 100),
        ('replacement_notes', 2000),
        ('description', 4000),
        ('comments', 5000),
    )
    display_attrs = ('end_of_support', 'source')


@register_search
class SoftwareVersionIndex(SearchIndex):
    model = SoftwareVersion
    fields = (
        ('version', 100),
        ('image_filename', 200),
        ('checksum', 500),
        ('description', 4000),
        ('comments', 5000),
    )
    display_attrs = ('platform', 'release_date')


@register_search
class SoftwareStandardIndex(SearchIndex):
    model = SoftwareStandard
    fields = (
        ('description', 4000),
        ('comments', 5000),
    )
    display_attrs = ('valid_from', 'valid_to')


@register_search
class DeviceSoftwareIndex(SearchIndex):
    model = DeviceSoftware
    fields = (
        # The raw string is indexed high: searching for a version somebody read
        # off a console is the common case, and it may never have been
        # catalogued as a SoftwareVersion.
        ('raw_version', 100),
        ('exempt_reason', 2000),
        ('description', 4000),
        ('comments', 5000),
    )
    display_attrs = ('device', 'source')
