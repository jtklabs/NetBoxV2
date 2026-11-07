# The Lifecycle plugin (plugins/netbox-refresh) is baked into the image by the
# Dockerfile. Its settings come from env/netbox.env; secrets may instead come
# from Docker secret files under /run/secrets/.

from os import environ


def _secret(name: str, env_var: str) -> str:
    """A credential from /run/secrets/<name>, or else from the environment."""
    try:
        with open("/run/secrets/" + name, encoding="utf-8") as handle:
            value = handle.readline().strip()
            if value:
                return value
    except OSError:
        pass
    return environ.get(env_var, "")


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


PLUGINS = ["netbox_refresh"]

PLUGINS_CONFIG = {
    "netbox_refresh": {
        # Cisco Support API credentials for the EoX sync. With both set, the
        # worker runs the sync on the schedule below; leave them empty to
        # enter lifecycle dates by hand.
        "cisco_client_id": _secret("cisco_client_id", "CISCO_CLIENT_ID"),
        "cisco_client_secret": _secret("cisco_client_secret", "CISCO_CLIENT_SECRET"),
        # Minutes between automatic EoX syncs: 10080 is weekly, 0 means the
        # Sync button only.
        "cisco_sync_interval_minutes": _int(environ.get("CISCO_SYNC_INTERVAL_MINUTES"), 10080),
        # Internal HTTP server holding software images. When set, a software
        # version needs only a filename to get a download link.
        "image_base_url": environ.get("IMAGE_BASE_URL", ""),
        # Show list-view dates as MM/DD/YYYY, matching the detail pages.
        "us_dates_in_tables": True,
    },
}
