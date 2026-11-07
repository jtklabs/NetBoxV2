"""Lifecycle records for hardware, software, licensing and support coverage.

The Python package and URL prefix keep their original names so existing
migrations, links, and API consumers continue to work.
"""

from netbox.plugins import PluginConfig


class RefreshConfig(PluginConfig):
    name = 'netbox_refresh'
    verbose_name = 'Lifecycle'
    description = 'Hardware and software lifecycle, licenses, and support coverage'
    version = '0.3.0'
    author = 'Nova Team'
    author_email = 'noreply@example.com'
    base_url = 'refresh'
    min_version = '4.6.0'
    # Pointed at explicitly rather than left to the default ('graphql.schema'),
    # which resolves the attribute `schema` on the package netbox_refresh.graphql
    # — where the submodule of the same name shadows it depending on import
    # order. This form is unambiguous: module netbox_refresh.graphql.schema,
    # attribute `schema`.
    graphql_schema = 'graphql.schema.schema'
    default_settings = {
        # Manufacturer names whose part numbers are Cisco PIDs, used by the
        # EoX sync to decide which device/module types to look up.
        'cisco_manufacturers': ['Cisco'],
        # Cisco Support API credentials. Left empty so they can come from the
        # environment instead of being written into configuration.
        'cisco_client_id': '',
        'cisco_client_secret': '',
        # How often the worker runs the EoX sync on its own, in minutes.
        # Weekly by default — vendors announce EoL in bulletins, not by the
        # hour. 0 disables the automatic schedule (the button still works).
        # Only registered when credentials are configured, so a stack with
        # no Cisco API access never schedules a job that can only fail.
        'cisco_sync_interval_minutes': 60 * 24 * 7,
        # Card placement on device/device-type pages.
        'lifecycle_card_position': 'right_page',
        # Base URL of the internal HTTP file server that holds code images.
        # When set, a version with an image filename but no explicit URL gets a
        # download link derived from this (e.g. 'http://images.internal/net/').
        # Plain http is expected and fine — see SoftwareVersion.download_url.
        'image_base_url': '',
        # A running-version reading older than this many days is shown as stale.
        # Freshness is deliberately separate from the compliance state: a device
        # can be compliant against a six-month-old reading, and the report should
        # say so rather than render it green and confident.
        'stale_after_days': 90,
        # Render dates in tables the way the rest of NetBox renders them, i.e.
        # honouring the locale. Off by default because it patches a NetBox
        # internal; see ready().
        'us_dates_in_tables': False,
    }

    def ready(self):
        super().ready()
        if self.default_settings and _setting_enabled('us_dates_in_tables'):
            _localise_table_dates()
        _register_eox_system_job()


def _register_eox_system_job():
    """Put the Cisco EoX sync on the worker's schedule, if it can run at all.

    NetBox's rqworker enqueues everything in registry['system_jobs'] at
    startup and JobRunner re-enqueues after each run, so registering here is
    all it takes to make the sync recur — the same mechanism NetBox uses for
    its own housekeeping. Done from ready() rather than with the @system_job
    decorator because the interval comes from settings, and because a job
    that cannot authenticate should not be scheduled to fail weekly.
    """
    from django.conf import settings
    from netbox.registry import registry

    configured = settings.PLUGINS_CONFIG.get('netbox_refresh', {})
    interval = configured.get(
        'cisco_sync_interval_minutes',
        RefreshConfig.default_settings['cisco_sync_interval_minutes'],
    )
    try:
        interval = int(interval or 0)
    except (TypeError, ValueError):
        interval = 0
    if interval <= 0:
        return
    if not (configured.get('cisco_client_id') and configured.get('cisco_client_secret')):
        return

    from netbox_refresh.jobs import CiscoEoxSyncJob

    registry['system_jobs'][CiscoEoxSyncJob] = {'interval': interval}


def _setting_enabled(name):
    from django.conf import settings

    configured = settings.PLUGINS_CONFIG.get('netbox_refresh', {})
    return bool(configured.get(name, RefreshConfig.default_settings[name]))


def _localise_table_dates():
    """Make table columns respect the locale like every other date does.

    NetBox's own DateColumn returns value.isoformat() outright, so a list view
    shows 2027-12-31 whatever the locale says, while the detail page for the
    same record shows 12/31/2027. Setting US dates without this gets you half
    an answer, and the list is where anyone actually reads end-of-life dates.

    This patches a NetBox internal, which is worth being uncomfortable about:
    it is a five-line render method that has been stable for a long time, but
    an upgrade could change it. So it is opt-in, it checks the shape it
    expects before touching anything, and it complains rather than failing
    silently or crashing the app on start.
    """
    import logging

    logger = logging.getLogger('netbox_refresh')
    try:
        from django.utils.formats import date_format
        from netbox.tables import columns
    except ImportError as exc:
        logger.warning('us_dates_in_tables: %s — leaving table dates alone', exc)
        return

    patched = []

    date_column = getattr(columns, 'DateColumn', None)
    if date_column is not None and hasattr(date_column, 'render'):
        def render_date(self, value):
            # `value` alone is left as ISO: that is what CSV export and
            # copy-to-clipboard use, where an unambiguous date matters more
            # than a familiar one.
            return date_format(value) if value else None

        date_column.render = render_date
        patched.append('DateColumn')

    # "Last updated" and friends are a different class, and a list showing
    # 12/31/2027 beside 2026-08-11 09:14:22 is not what anybody meant by US
    # dates. Its timezone conversion is kept: it is the reason the column
    # exists, and dropping it would show UTC to everyone.
    datetime_column = getattr(columns, 'DateTimeColumn', None)
    if datetime_column is not None and hasattr(datetime_column, 'render'):
        import zoneinfo

        from django.conf import settings as django_settings

        def render_datetime(self, value):
            if not value:
                return None
            local = value.astimezone(zoneinfo.ZoneInfo(django_settings.TIME_ZONE))
            return date_format(local, 'DATETIME_FORMAT')

        datetime_column.render = render_datetime
        patched.append('DateTimeColumn')

    if not patched:
        logger.warning(
            'us_dates_in_tables: netbox.tables.columns is not the shape this '
            'expects, so table dates are unchanged'
        )
        return
    logger.info('us_dates_in_tables: %s now follow the locale', ', '.join(patched))


config = RefreshConfig
