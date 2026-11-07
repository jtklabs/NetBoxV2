# NetBox 4.7 with the Lifecycle plugin

This repository builds and runs our NetBox 4.7 instance:

- **NetBox 4.7.4** on [netbox-docker](https://github.com/netbox-community/netbox-docker) 5.1.1,
  with PostgreSQL 18 and Valkey 9.1.
- **Lifecycle** (`plugins/netbox-refresh`), our plugin for hardware and software
  lifecycle, licenses and support coverage, baked into the NetBox image.
- **`apache/netbox.conf`**, a drop-in include for the existing SSO Apache that
  publishes NetBox at `https://netbox.example.com/netbox`.

NetBox does no discovery of its own. Devices and running versions are written
in through the REST API by NetOps Executor (jtklabs/netops-executor).

| Path | What |
| --- | --- |
| `Dockerfile` | NetBox image with the Lifecycle plugin |
| `docker-compose.yml` | NetBox, worker, PostgreSQL, two Valkey instances |
| `.env.example` | Compose project settings: project name, image tag, published address |
| `env/*.env.example` | NetBox, PostgreSQL and Valkey settings, including the SSO settings |
| `configuration/` | netbox-docker's configuration, plus `extra.py` (`BASE_PATH`) and `plugins.py` (Lifecycle settings) |
| `apache/netbox.conf` | Reverse-proxy include for the SSO Apache |
| `testing/apache-header-spoof/` | Check that the Apache include strips client-supplied identity headers |
| `plugins/netbox-refresh/` | The Lifecycle plugin |

## Versions

| Component | Version |
| --- | --- |
| NetBox | 4.7.4 (`netboxcommunity/netbox:v4.7.4-5.1.1`) |
| netbox-docker files | 5.1.1 |
| Lifecycle (`netbox-refresh`) | 0.4.4 |
| PostgreSQL | 18 (`postgres:18-alpine`) |
| Valkey | 9.1 (`valkey/valkey:9.1-alpine`) |

NetBox 4.7 needs PostgreSQL 15 or later.

## Build and run

```bash
cp .env.example .env
for f in env/*.env.example; do cp "$f" "${f%.example}"; done
# Edit env/*.env: replace every CHANGE_ME. The passwords must match across files
# (see the comments). Generate SECRET_KEY and API_TOKEN_PEPPER_1 as shown there.
docker compose build
docker compose up -d
docker compose ps        # netbox turns healthy once migrations finish
```

The first start runs every migration and takes a few minutes. NetBox then
answers on `BIND_ADDRESS:NETBOX_PORT` from `.env` (default `127.0.0.1:8080`).

For local use without the proxy, comment out the "Behind the SSO Apache"
section of `env/netbox.env` and set `ALLOWED_HOSTS=localhost 127.0.0.1`, then
browse to http://127.0.0.1:8080 and sign in as the local superuser.

## Behind the SSO Apache at /netbox

The existing Apache server signs users in with mod_auth_mellon and proxies to
this host. Three pieces have to agree.

**1. NetBox (`env/netbox.env`)**

- `BASE_PATH=netbox/`: NetBox builds every URL under `/netbox/`.
  `configuration/extra.py` applies it, and the compose healthcheck follows it.
- `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` name the public hostname.
- `CSRF_COOKIE_NAME` and `SESSION_COOKIE_NAME` are distinct from the other
  applications on that hostname.
- `REMOTE_AUTH_*` reads the identity headers Apache sets:

  | Header | Used for |
  | --- | --- |
  | `X-Remote-User` | Username |
  | `X-User-Email` | Email |
  | `X-UserFirstName` | First name |
  | `X-User-LastName` | Last name |
  | `X-User-Groups` | Group sync |

  Users and groups are created on first login. Members of
  `REMOTE_AUTH_SUPERUSER_GROUPS` become superusers.

**2. This host (`.env`)**

- `BIND_ADDRESS` is this host's private address, so the Apache server can reach
  port 8080. It must go in `.env`: Compose reads that file for ports, not
  `env/netbox.env`.
- Allow port 8080 from the Apache server only. NetBox trusts the identity
  headers on that hop, so anything else that reaches the port could sign in as
  anyone.

**3. Apache (`apache/netbox.conf`)**

1. Set `NETBOX_BACKEND` to this host.
2. Fill in and uncomment the Mellon attribute-to-header block. The file explains
   how to find your IdP's attribute names.
3. Copy the file to `/etc/apache2/conf-available/netbox.conf`.
4. Add `Include conf-available/netbox.conf` inside the existing `:443` vhost.
5. Run:

   ```bash
   sudo a2enmod proxy proxy_http headers
   sudo apachectl configtest && sudo systemctl reload apache2
   ```

The include does the following:

- Requires a SAML login for `/netbox`, except `/netbox/api`, which uses NetBox
  API tokens.
- Strips every client-supplied identity header, including the underscore
  spellings such as `X_Remote_User`. The WSGI server reads those as the real
  header, so without this any client could sign in as anyone through
  `/netbox/api`.
- Maps `/netbox/static/` to the container's `/static/`.
- Sends `X-Forwarded-Proto: https`.

`testing/apache-header-spoof/run.sh` checks the stripping with Docker, and CI
runs it when the include or `env/netbox.env.example` changes.

After the Apache include is in place, check it on the Apache server:

```bash
for m in 'Define NETBOX_BACKEND http' 'RequestHeader unset X-Remote-User early' 'RequestHeader unset X_Remote_User early' 'RequestHeader set X-Remote-User' '<Location /netbox/api>' 'ProxyPass        /netbox/'; do grep -qF "$m" /etc/apache2/conf-available/netbox.conf && echo "ok       $m" || echo "MISSING  $m"; done
```

Six `ok` lines mean every required piece is there.

Then check the result: `https://netbox.example.com/netbox/` should redirect to
the IdP and come back signed in. The first SSO login creates the user. Check
that their groups arrived as expected (names or full DNs, and the separator).
Once an admin can sign in through SSO, set `SKIP_SUPERUSER=true`.

API clients call `https://netbox.example.com/netbox/api/...` with a NetBox
token, `Authorization: Bearer nbt_<key>.<secret>`.

## Lifecycle plugin

The Dockerfile copies `plugins/netbox-refresh/netbox_refresh` into the image and
puts it on `PYTHONPATH`. `configuration/plugins.py` enables it and reads its
settings from `env/netbox.env`:

| Setting | What it does |
| --- | --- |
| `CISCO_CLIENT_ID`, `CISCO_CLIENT_SECRET` | Cisco Support API credentials for the EoX sync. Docker secret files `/run/secrets/cisco_client_id` and `/run/secrets/cisco_client_secret` take precedence. |
| `CISCO_SYNC_INTERVAL_MINUTES` | Minutes between syncs: 10080 is weekly, 0 means the Sync button only. |
| `IMAGE_BASE_URL` | Base URL for software image download links. |

NetOps Executor reports running versions to
`/api/plugins/refresh/device-software/report/`. Its token needs add and change
permission on the plugin's Device Software model.

To change the plugin:

1. Edit it.
2. Bump its version in both `plugins/netbox-refresh/pyproject.toml` and
   `netbox_refresh/__init__.py`, and set `LIFECYCLE_VERSION` in `.env.example`
   to match.
3. Run `docker compose build && docker compose up -d`. Migrations run at start.

## Upgrading NetBox

1. Read the NetBox release notes for every version in between.
2. Update the `NETBOX_VERSION` default in `docker-compose.yml`, `.env.example`
   and `Dockerfile`.
3. Compare `configuration/` with the matching netbox-docker release and take any
   new settings.
4. Back up the database:

   ```bash
   docker compose exec postgres pg_dump -U netbox netbox > netbox.sql
   ```

5. Run `docker compose build && docker compose up -d`.

## Moving the existing local 4.7 instance here

The instance now running from `jtklabs/Netbox/.netbox-47` uses Compose project
`netbox47`, the same volume names, and the same plugin. To run it from this
repository instead:

1. Stop it there with `docker compose down`, without `-v`.
2. Copy its `env/*.env` files here.
3. Add the settings in this repository's `env/netbox.env.example`.
4. Keep `COMPOSE_PROJECT_NAME=netbox47` in `.env`.
5. Run `docker compose up -d` here.

The database, media and Valkey volumes are reused as they are.
