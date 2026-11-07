# First-boot operator guide (one time, run by you on the prod instance)

Everything here happens exactly once. After this, every 30-day redeploy is
automatic (RUNBOOK-redeploy.md). No secrets ever leave this box or enter git.

Prereqs:

- Ubuntu 24 instance with Docker and **Compose v2**, this repo at `/opt/netbox`.
  A stock Ubuntu install often has only the legacy `docker-compose` v1 binary,
  which cannot parse these files — `sudo apt-get install -y docker-compose-v2`.
  Check with `docker compose version` (a space, not a hyphen).
- the data disk attached
- an instance profile granting the S3 media bucket
- the existing Apache + mod_auth_mellon server (separate host)
- **RDS reachable on 5432 from this instance's security group, with the
  `netbox` database and `netbox` role already created.** Nothing here creates
  them; if they are missing the container restart-loops on a database error
  that is only visible in `docker compose logs netbox`.

## How the layout works (read this first)

**None of these paths exist yet — you create them in the steps below.** There
are only two locations, and the split is the whole point of the design:

| Path | What it is | Survives a redeploy? |
|---|---|---|
| `/opt/netbox` | This git repo: compose files, plugins, scripts. Ships with the AMI. | **No** — replaced with each new AMI, and that is fine, it is just code. |
| `/mnt/data_disk/netbox-secrets/` | The files you write by hand: the env files. Lives on the **persistent data disk**. | **Yes** — the disk detaches from the old instance and attaches to the new one. |

The data-disk mount point is `/mnt/data_disk` throughout. If it ever moves, set
it once rather than editing paths everywhere:

```bash
echo 'DATA_MOUNT=/mnt/data_disk' | sudo tee /etc/netbox-deploy.conf
```

`deploy/bootstrap.sh` and the systemd unit both read that file. Bake it into the
AMI or write it from user-data — `/etc` is replaced along with the AMI, so an
edit made by hand on a running instance will not survive a redeploy.

`/mnt/data_disk` is the mount point for the data disk, so `mkdir` there only works after
the disk is mounted (step 1). `/opt/netbox` is wherever the AMI checked the
repo out — if yours is elsewhere, set `REPO_DIR` when running bootstrap.

**The containers do not read from the data disk directly.** On every boot
`deploy/bootstrap.sh` connects the two: it symlinks the env files from the data
disk into `/opt/netbox/env/`, and copies `client-credentials.json` into
Docker Compose then reads everything
from the repo paths as normal. (That one file is copied rather than symlinked
because its directory is bind-mounted into a container, and a symlink inside a
bind mount resolves against the *container's* filesystem, where that path does
not exist.)

So the rule of thumb: **you only ever hand-edit files under
`/mnt/data_disk/netbox-secrets/`.** Never edit the copies under `/opt/netbox` — a
redeploy throws them away.

## 1. Prepare the data disk (label is what bootstrap mounts by)

If the disk is already mounted at `/mnt/data_disk` (fstab or cloud-init), skip
straight to creating the directory — bootstrap detects an existing mount and
leaves it alone.

```bash
# Only if the disk is brand new and not already mounted:
sudo mkfs.ext4 -L NETBOXDATA /dev/nvme1n1   # adjust device; SKIP if it already has data
sudo mkdir -p /mnt/data_disk && sudo mount LABEL=NETBOXDATA /mnt/data_disk

# Always — check FIRST, then create:
mountpoint /mnt/data_disk    # must say "is a mountpoint" before continuing
sudo mkdir -p /mnt/data_disk/netbox-secrets
```

Order matters: if the disk is not mounted, `mkdir` would create the tree on the
root filesystem, everything below would be written there and silently disappear
at the next redeploy — and the non-empty directory would then shadow the real
mount. Bootstrap refuses to start in that situation for the same reason.

## 2. Generate the persistent app secrets

**Run this once, ever.** Re-running regenerates `SECRET_KEY` (logging everyone
out) and `API_TOKEN_PEPPER_1` (invalidating every API token), so it refuses if
the file already exists.

```bash
cd /opt/netbox
[ -e /mnt/data_disk/netbox-secrets/netbox.env ] && { echo "REFUSING: netbox.env already exists"; return 2>/dev/null || exit 1; }
gen() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
DBPW=$(gen 32); RPW=$(gen 24); RCPW=$(gen 24)
sed -e "s/{{SECRET_KEY}}/$(gen 60)/" \
    -e "s/{{API_TOKEN_PEPPER}}/$(gen 50)/" \
    -e "s/{{DB_PASSWORD}}/$DBPW/" \
    -e "s/{{REDIS_PASSWORD}}/$RPW/" \
    -e "s/{{REDIS_CACHE_PASSWORD}}/$RCPW/" \
    env/netbox.env.example | sudo tee /mnt/data_disk/netbox-secrets/netbox.env >/dev/null
sed "s/{{REDIS_PASSWORD}}/$RPW/"  env/redis.env.example       | sudo tee /mnt/data_disk/netbox-secrets/redis.env >/dev/null
sed "s/{{REDIS_CACHE_PASSWORD}}/$RCPW/" env/redis-cache.env.example | sudo tee /mnt/data_disk/netbox-secrets/redis-cache.env >/dev/null
```

(The generated `DB_PASSWORD` in netbox.env is a placeholder — prod.env
overrides DB_* with the real RDS values in the next step. The SECRET_KEY and
pepper in this file must NEVER change afterward.)

## 3. Fill in prod.env and .env

**Two files, and the split is not cosmetic.** `.env` holds settings Docker
Compose itself reads while parsing (which image, which overlays, which address
to publish on). `prod.env` holds the environment handed *to the container*
(database, S3, SSO). Compose does **not** read `env_file:` entries for `${...}`
substitution, so a compose-level setting placed in `prod.env` is silently
ignored — `BIND_ADDRESS` is the one that bites, because NetBox then stays on
loopback and the Apache server cannot reach it.

```bash
sudo cp env/prod.env.example /mnt/data_disk/netbox-secrets/prod.env
sudo vi /mnt/data_disk/netbox-secrets/prod.env
#   RDS endpoint + credentials       (DB_HOST, DB_USER, DB_PASSWORD)
#   S3 bucket + region               (S3_MEDIA_BUCKET, AWS_REGION)
#   your real hostname               (ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS)
#   your AD group for admins         (REMOTE_AUTH_SUPERUSER_GROUPS)
#   Cisco API creds, if using EoX    (CISCO_CLIENT_ID, CISCO_CLIENT_SECRET)

sudo tee /mnt/data_disk/netbox-secrets/.env >/dev/null <<'EOF'
COMPOSE_FILE=docker-compose.yml:compose/prod.yml
# Required for local image builds; replace with your internal Python index.
PYTHON_INDEX_URL=https://packages.example.com/repository/pypi/simple/
# The address NetBox publishes on. Apache is on another host, so this must NOT
# be loopback. Use 0.0.0.0, NOT the instance's private IP: this file lives on
# the data disk and is reused by every future instance, whose IP will differ —
# a hardcoded IP fails the next redeploy with "cannot assign requested address".
# The security group (8080 open only to the Apache server) is the real control.
BIND_ADDRESS=0.0.0.0
# (VERSION is deliberately absent: compose/prod.yml pins the image tag, so
# VERSION here would have no effect. The tag lives in Dockerfile-Plugins.)
# PROD_IMAGE stays unset: the image is built during the AMI bake
# (scripts/prod-build.sh), or by bootstrap at first boot as a fallback.
EOF
```

Lock the secrets down — everything above was written with the default umask:

```bash
sudo chmod 600 /mnt/data_disk/netbox-secrets/.env \
               /mnt/data_disk/netbox-secrets/netbox.env \
               /mnt/data_disk/netbox-secrets/prod.env
# Settings only — comments are skipped, so this should print nothing:
sudo sh -c 'grep -vn "^[[:space:]]*#" /mnt/data_disk/netbox-secrets/prod.env /mnt/data_disk/netbox-secrets/.env | grep "example.com\|CHANGE_ME"' \
  && echo "^^ placeholders still present — fix these before continuing"
```

(Bootstrap links the redis env files into place on every boot, and generates
them from `netbox.env` if they are missing, so their passwords always match —
no manual `ln` and no chance of a mismatch.)

### Run compose as root in prod

The data-disk secrets are mode 600 and owned by root, and `/opt/netbox/env/*`
are symlinks to them. `docker compose` as your own user therefore fails to read
them — and depending on the subcommand it can fail quietly, leaving you looking
at a container that did not pick up your change. Use `sudo docker compose ...`,
or go through `sudo systemctl restart netbox-compose`.

### If you see "env file env/redis.env not found"

Compose is being run before `deploy/bootstrap.sh` has linked the data-disk
files into the repo. Run bootstrap — that is what wires the two together:

```bash
sudo /opt/netbox/deploy/bootstrap.sh
```

or start it the normal way, `sudo systemctl start netbox-compose`. Running
`docker compose` directly in `/opt/netbox` only works *after* bootstrap has run
at least once since the repo was replaced.

## 4. SAML — nothing to do here

Mellon already runs on the existing Apache server with its SP keys and IdP
metadata in place, and that server already injects the identity headers
(`X-Remote-User`, `X-User-Email`, `X-UserFirstName`, `X-User-LastName`,
`X-User-Groups`). This instance runs no Apache and no Mellon.

## 5. Apache — on the OTHER server, not this one

Apache proxies across the network to this instance, so two things must line up.

**On this instance**, set `BIND_ADDRESS` in `/mnt/data_disk/netbox-secrets/.env`
(the `.env`, not `prod.env` — see step 3) to its **private** address, then allow
port 8080 **only** from the Apache server in the security group. That hop is
plain HTTP and NetBox trusts the identity headers Apache sets, so anything able
to reach 8080 directly could impersonate any user; the security group is what
prevents that.

**On the Apache server**, copy `apache/netbox.conf` from this repo, set
`NETBOX_BACKEND` at the top to this instance's private address, **fill in and
uncomment the "Mellon attributes → identity headers" block** (the attribute
names are specific to your IdP — the comments in the file say how to find
them), and include it in the existing `netbox.example.com` :443 vhost:

```bash
sudo a2enmod proxy proxy_http headers      # mellon is already enabled
sudo cp netbox.conf /etc/apache2/conf-available/netbox.conf
# add "Include conf-available/netbox.conf" inside the vhost, then:
sudo apachectl configtest && sudo systemctl reload apache2
```

Verification comes after step 6 starts the stack — there is nothing listening
yet at this point.

**If a provisioning/config-management system owns that Apache server**, put the
fully-edited `netbox.conf` into ITS source of truth — a provisioning run that
"restores" a template version silently drops the hand-applied pieces. Each loss
has a distinct signature: mapping block gone → SSO users created with no
account; `/netbox/api` block gone → API tokens bounce to the IdP; proxy section
gone → 503 everywhere; spoof-protection unsets gone → **no symptom at all**,
just a missing security control. `configtest` cannot catch any of this (absent
blocks are valid syntax) — after any provisioning cycle, run:

```bash
for m in 'Define NETBOX_BACKEND http' 'RequestHeader unset X-Remote-User early' 'RequestHeader unset X_Remote_User early' 'RequestHeader set X-Remote-User' '<Location /netbox/api>' 'ProxyPass        /netbox/'; do grep -qF "$m" /etc/apache2/conf-available/netbox.conf && echo "ok       $m" || echo "MISSING  $m"; done
```

Six `ok` lines means every load-bearing piece survived. The second one matters:
the WSGI server behind NetBox reads `X_Remote_User` as `X-Remote-User`, so an
installed copy that unsets only the hyphen spellings lets anyone send
`X_Remote_User` and `X_User_Groups` to `/netbox/api` and log in as any user.
Copy the whole spoof-protection block from the repo, not just that line.
`testing/apache-header-spoof/run.sh` checks the repo copy for every spelling.

### API clients get redirected to the IdP instead of answered

`<Location /netbox>` puts everything behind Mellon — including `/netbox/api/`,
which non-browser clients call with NetBox tokens, not SAML. The shipped
`apache/netbox.conf` carries a `<Location /netbox/api>` block that opts the API
out of Mellon (NetBox's own auth becomes the only gate; the spoof-protection
unsets still strip identity headers there). If your installed copy predates it,
add that block — it must appear AFTER the `<Location /netbox>` auth block,
because later Location sections override earlier ones.

Token formats (NetBox 4.6 — the UI shows the full string once, at creation):

```bash
curl -H 'Authorization: Bearer nbt_<key>.<secret>' https://netbox.example.com/netbox/api/dcim/devices/
```

Legacy v1 tokens use `Authorization: Token <40-char-key>`. A 403 with
`{"detail":"Invalid v1 token"}` means the header reached NetBox but the value
is in the wrong format for its version — it is not an Apache problem.

### A POST fails "CSRF token from POST incorrect" (GETs all fine)

Classic same-hostname cookie collision: the front-end app on this vhost also
uses cookies named `csrftoken`/`sessionid`, and the two applications overwrite
each other's. The fix is shipped in `prod.env.example` (`CSRF_COOKIE_NAME` /
`SESSION_COOKIE_NAME`) — confirm both are present in the data-disk `prod.env`
and force-recreate. Signature if you want proof first: hard-refresh a NetBox
page and the POST works immediately, then fails again after visiting the other
application.

### Removing a plugin: drop its tables BEFORE the image stops shipping it

`manage.py migrate <plugin_app> zero` is the clean way to drop a plugin's
tables, and it only works while the plugin is **still installed**. Rebuild the
image first and the app label is unknown — `CommandError: No installed app with
label '<plugin>'` — leaving orphaned tables that can then only be removed by
hand:

```bash
docker compose exec postgres psql -U netbox -d netbox -c \
  "DROP TABLE IF EXISTS <plugin>_<table>; DELETE FROM django_migrations WHERE app = '<plugin>';"
```

So the order is: drop the tables, *then* pull, rebuild and restart.

### SSO prompts, but no account is created

Rehearse the whole flow against a real IdP first — `testing/sso-idp/` stands
up a throwaway SAML IdP plus a real mod_auth_mellon Apache running this
repo's `netbox.conf`, so every failure mode below can be reproduced and
fixed without touching prod.

The IdP round-trip working proves only Mellon's *authentication*. Identity
reaches NetBox as HTTP headers, and mod_auth_mellon **does not set headers** —
it exports SAML attributes as Apache environment variables (`MELLON_<attr>`),
which never cross the proxy hop. Without the mapping block in
`apache/netbox.conf`, NetBox receives an authenticated request carrying no
identity at all and just shows its login page. Two checks tell you which side
is broken:

**1. Did the NetBox side get its settings?** (on this instance)

```bash
sudo docker exec netbox-netbox-1 sh -c 'env | grep ^REMOTE_AUTH' 
```

Expect `REMOTE_AUTH_ENABLED=true`, `REMOTE_AUTH_HEADER=HTTP_X_REMOTE_USER` and
the rest of the block from `env/prod.env.example`. Missing entries mean your
data-disk `prod.env` predates them (`git pull` never updates that file — recent
bootstraps log exactly which keys drifted). Add them and
`sudo docker compose up -d --force-recreate netbox`.

**2. Do the headers actually arrive?** (on this instance, while you load the
page in a browser)

```bash
sudo tcpdump -i any -A -s0 'tcp dst port 8080' 2>/dev/null | grep -iE 'x-remote-user|x-user-'
```

Nothing printed while pages load = Apache is not sending them: fill in and
uncomment the mapping block in `apache/netbox.conf`. If your vhost already
builds these headers for another application, check their scope — inside that
app's `<Location>` they do not apply to `/netbox`.

Groups specifically: multi-valued SAML attributes arrive as separate
`MELLON_groups_0`, `MELLON_groups_1`, … variables until
`MellonMergeEnvVars On "|"` collapses them (`|` matches
`REMOTE_AUTH_GROUP_SEPARATOR`). If users appear but with no groups, that line
is the usual culprit — see also `REMOTE_AUTH_AUTO_CREATE_GROUPS` in step 3.

## 6. Start and enable

```bash
sudo cp /opt/netbox/deploy/netbox-compose.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now netbox-compose
sudo tail -f /var/log/netbox-bootstrap.log   # or journalctl -u netbox-compose
```

First boot runs all migrations (several minutes).

## 7. Grant yourself admin

Log in once through SSO. Your account is auto-created with **no rights** — that
is expected. Then grant it from the instance:

```bash
cd /opt/netbox
docker compose exec netbox /opt/netbox/venv/bin/python \
  /opt/netbox/netbox/manage.py shell -c \
  "from users.models import User; u=User.objects.get(username='YOUR_SSO_USERNAME'); u.is_superuser=True; u.is_staff=True; u.save(); print('granted', u.username)"
```

Better still, set `REMOTE_AUTH_SUPERUSER_GROUPS` in prod.env to your admin AD
group (step 3) and group sync grants it automatically on next login.

Two things that do **not** work here, so you do not lose time on them: the
Django admin UI (`/admin/`) was removed in NetBox 4.2 and returns 404, and a
local `createsuperuser` account cannot log in through Apache — remote auth sees
the `X-Remote-User` header on every request and re-authenticates as the SSO
user. A local account is only usable by reaching port 8080 directly.

## 8. Verify

**On the NetBox instance** — backend reachable on the published address. The
Host header matters: `ALLOWED_HOSTS` would reject a request addressed to the
raw IP.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Host: localhost' \
  http://<netbox-private-ip>:8080/netbox/login/     # expect 200
docker compose ps                                   # all healthy
```

**On the Apache server** — the same check proves the security group:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Host: localhost' \
  http://<netbox-private-ip>:8080/netbox/login/     # expect 200
```

**Through Apache** — an unauthenticated request is answered by Mellon in the
auth phase, so expect a redirect to your IdP, *not* a 200. A 200 here would
mean the path is not protected:

```bash
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  https://netbox.example.com/netbox/login/          # expect 302 -> IdP
```

**In a browser**: SSO round-trip completes, device pages render with styles
(proves the static mapping), and a quote document downloads (proves S3).

## Image distribution (decision 2026-07-29: no ECR)

Leave `PROD_IMAGE` unset. Add one step to the monthly AMI bake, after the repo
checkout:

```bash
cd /opt/netbox && ./scripts/prod-build.sh
```

That builds the image under the exact tag `compose/prod.yml` expects and then
verifies every plugin loads inside it — a plugin that would fail on boot is
caught during the bake instead of during a redeploy. Instances then start with
the image already present (fast boot, no Docker Hub dependency at boot time).

If the bake skips this, `deploy/bootstrap.sh` builds at first boot instead
(~5 extra minutes).
