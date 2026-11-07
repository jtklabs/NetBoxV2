# Scope

- This repository is the NetBox 4.7 instance and the Lifecycle plugin (`plugins/netbox-refresh`) only.
- Do not add discovery, polling or device-automation code. NetOps Executor (jtklabs/netops-executor) writes into NetBox
  through the REST API.
- Do not copy components from the old jtklabs/Netbox repository without asking.

# Changes

- Lifecycle plugin changes: bump its version in both `plugins/netbox-refresh/pyproject.toml` and
  `plugins/netbox-refresh/netbox_refresh/__init__.py`, and update `LIFECYCLE_VERSION` in `.env.example` to match.
- Test against NetBox 4.7: `docker compose build && docker compose up -d`, then check the plugin's pages and API.
- Changes to `apache/netbox.conf` or the `REMOTE_AUTH_*` settings: run `testing/apache-header-spoof/run.sh`.
- Never commit `.env` or `env/*.env`. Only the `*.example` files hold settings, and they hold placeholders.
- Use descriptive branch names and do not add assistant attribution or co-author trailers.
