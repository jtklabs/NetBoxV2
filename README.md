# NetBox deployment

Env-driven NetBox deployment (dev local / prod on EC2 behind netbox.example.com/netbox).
See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the gated plan and [VERSIONS.md](VERSIONS.md) for pins.

Standalone companion services:

- [Endpoint discovery](services/endpoint-discovery/README.md): read-only remote
  SNMPv3 collectors, an independent PostgreSQL observation database, and a scoped
  portal API for MAC/IP-to-port lookup and history. Includes Ansible deployment.
- [Path monitor](services/path-monitor/README.md): one-second traceroutes to a
  configured destination list, persistent path-change history, multipath-aware
  detection, and a reporting dashboard. Deploy independently of NetBox.
- [Live job tracker](services/job-tracker/README.md): read-only wallboard for
  Discovery device jobs.

## Dev quickstart

On first setup, initialize the environment and set the Python package index.
Then `dev-up.sh` manages the image build, stack, and health check:

```bash
bash scripts/init-dev-env.sh
# Edit .env: set PYTHON_INDEX_URL to your internal Python index /simple URL.
bash scripts/dev-up.sh
```

(Invoked with `bash` so it works from a ZIP download too, where the executable
bit is lost. From a git clone `./scripts/dev-up.sh` is equivalent.)

It is idempotent, so it is also how to restart. It prints the URL and the generated
admin password when NetBox is ready (first boot runs all migrations, ~10 minutes).

Doing it by hand is the same three steps:

```bash
./scripts/init-dev-env.sh   # generates .env + env/*.env with fresh local secrets
# Set PYTHON_INDEX_URL in .env before building.
bash scripts/compose-build.sh
docker compose up -d
```

**`scripts/init-dev-env.sh` is not optional.** A fresh clone ships only
`env/*.example` templates, and `.env` is what selects the compose overlay chain —
without it compose runs the base file alone, which has no database. Re-running the
script is safe: it creates only what is missing and keeps the shared passwords in
`env/netbox.env`, `postgres.env`, `redis.env` and `redis-cache.env` consistent.

### Python package index for image builds

Set this in the **root `.env`** (the file containing `COMPOSE_FILE`):

```dotenv
PYTHON_INDEX_URL=https://packages.example.com/repository/pypi/simple/
```

Use your internal index's actual `/simple/` URL. In production this `.env` is
linked to the data disk, for example `/data/netbox-secrets/.env` (the bootstrap
default is `/mnt/data_disk/netbox-secrets/.env`). This is a build setting,
so putting it in `env/prod.env` or the remote netops script's `.env` does not apply.
The build scripts and bootstrap pass it to both plugin installation steps as a BuildKit
secret. It replaces the default package index; there is no public PyPI fallback.
The index must serve build dependencies such as `setuptools`, even though our
plugins are copied from this repository. A missing or empty setting stops builds.

The URL may include URL-encoded basic-auth credentials when required by the
index. Use single quotes around the `.env` value if it contains a literal `$`.
The value is not written into the image's environment or Docker build arguments.
Existing host `pip.conf` / `PIP_INDEX_URL` settings are not inherited by this build.

After setting or changing the index, rebuild and recreate the app containers:

```bash
bash scripts/compose-build.sh --no-cache netbox
docker compose up -d --force-recreate netbox netbox-worker
```

`--no-cache` forces package installation to run again: BuildKit secret-value
changes alone do not invalidate cached layers. Bootstrap still skips building
when an image is already present. The wrapper resolves dotenv values using
Compose, then explicitly exports only `PYTHON_INDEX_URL` to the builder. This
avoids builders that pass an empty secret when the value exists only in `.env`.
It works with both Compose Bake and the internal builder; no `COMPOSE_BAKE`
override is needed. It does not source the env file as shell code or print the URL.

For an explicit production env path, run from the repository:

```bash
sudo bash scripts/compose-build.sh --env-file /data/netbox-secrets/.env --no-cache netbox
sudo docker compose up -d --force-recreate netbox netbox-worker
```

`scripts/prod-build.sh` also reads the root `.env` (symlinks work), or accepts an
exported `PYTHON_INDEX_URL` for AMI builds without env files. It does not require
the runtime `env/prod.env`. As with Compose, an exported setting takes precedence
over the file, even when empty: unset an empty `PYTHON_INDEX_URL` before building.
When using an exported value with `sudo`, preserve it with
`sudo --preserve-env=PYTHON_INDEX_URL ./scripts/prod-build.sh`.

### Deployed from a ZIP rather than a clone?

A ZIP download drops the executable bit on every script, so `./scripts/dev-up.sh`
fails with "permission denied". Start with the entry point invoked through bash —
it restores the modes for everything else:

```bash
bash scripts/init-dev-env.sh
```

**Before running it on a host that already has a stack, keep your existing
`.env` and `env/*.env`.** They are gitignored, so a ZIP does not contain them,
and if they are missing the script generates *new* secrets — including a new
database password, which will not match the existing PostgreSQL volume, and a
new `SECRET_KEY`, which invalidates every session. Copy the old files back
before starting anything. Extracting a ZIP over the existing directory keeps
them; extracting into a fresh directory does not.

A ZIP also has no `.git`, so there is no way to pull later updates — prefer
`git clone` where your policies allow it.

NetBox: http://127.0.0.1:8080 — local superuser `admin`, password in your generated `.env` (`SUPERUSER_PASSWORD`). No secrets are committed to this repo.

### Reaching dev from another machine

Pass the hostname or IP people will use:

```bash
./scripts/dev-up.sh netbox-dev.example.com
```

That is the whole procedure — no variables to edit and no certificate step. It
generates the TLS certificate, writes every setting it needs into `.env`,
enables the reverse proxy and starts the stack, then prints the URL. Re-run it
any time to restart, or with a different name to move it. `./scripts/dev-up.sh
--local` goes back to loopback-only.

NetBox is served at **`/netbox`, the same path production uses**, so the subpath
and the static-file mapping are exercised every day rather than only in a drill.
The certificate is self-signed, so browsers warn once — the point is keeping
credentials off plaintext HTTP, not proving identity.

Two things worth knowing:

- Setting `DEV_PROXY_SSO_USER` in `.env` additionally simulates the production
  Mellon header handoff, including group sync, so SSO behaviour can be tested
  on the same path. Leave it empty for normal local logins.
- The stack binds to all interfaces so one command works on both a Linux server
  and Docker Desktop. On Linux you can narrow `BIND_ADDRESS` in `.env` to a
  single interface address afterwards and it is respected.

## Prod image

```bash
./scripts/prod-build.sh
```

Builds the production image and verifies every plugin loads inside it, so a broken
plugin fails at bake time rather than during a redeploy. This is the step to add to
the monthly AMI bake — see [docs/FIRST-BOOT.md](docs/FIRST-BOOT.md).


## Layout

- `services/job-tracker/` — standalone live device-job wallboard for another server. Reads the NetBox API, updates automatically, and shows progress by saved upgrade wave, tier, and group. See [setup and deployment](services/job-tracker/README.md).
- `docker-compose.yml`, `configuration/`, `env/*.example` — imported from netbox-docker at the tag in [VERSIONS.md](VERSIONS.md) (deviation: the postgres service lives in `compose/dev.yml`; prod uses RDS)
- `compose/` — env overlays: `dev.yml`, `prod.yml`, `dev-proxy.yml` (HTTPS proxy serving dev at `/netbox`, the prod path)
- `plugins/netbox-quotes/` — our quotes/serial-matching plugin (REST **and** GraphQL: `quote_list`, `quote_line_list`, `quote_vendor_list`); `Dockerfile-Plugins` builds the image with it + PyPI plugins
- `plugins/netbox-refresh/` — our lifecycle plugin, shown in the UI as **Lifecycle** (the package keeps its original `netbox_refresh` name and `/api/plugins/refresh/` paths — renaming would cost migrations and API churn for nothing). Hardware and software records are queryable over REST and GraphQL. License and support records are available in the UI and REST API.
  - **Hardware** — EoL dates on device/module types, replacement model links, replacement cost, Cisco EoX sync (`manage.py sync_cisco_eol`), and the refresh cost report at **Lifecycle › Refresh Report**. The report breaks down by hardware model or by site (every model due at each site, priced at that site's rate, with a cost-by-site summary) and exports either breakdown as CSV with the same filters applied (`?export=csv`). The **Model Lifecycles** list also shows every device and module type that has *no* lifecycle yet, reading "None configured", installed units first, with an Add button that opens the form with the model chosen
  - **Software** — a version catalogue (release date, image link/checksum/size), effective-dated **standards** listing the versions explicitly approved for device tags, device types, or platforms, per-device running versions with provenance and freshness, per-device "do not upgrade" exemptions, and the **Compliance Report** / **Version Rollup**. Collectors push readings to `POST /api/plugins/refresh/device-software/report/`. NetBox stores the *link* to a code image, never the image — set `IMAGE_BASE_URL` to your internal image server and a version needs only a filename to get a download link on the version, device and device-type pages
  - **Mode-specific software** — choose **Device tags** and optionally restrict the standard to device types, platforms, or both. A device must match one item in every selected group. A matching tagged standard overrides untagged standards. Among tagged standards, a match on both type and platform wins, then a type match, then a platform match, then a tag-only match. If multiple standards tie at the highest priority, the device shows a conflict and withholds downloads and automatic prestaging.
  - **Coverage** — **License Entitlements** track vendor identifiers, purchased seats, renewals and a vault reference for activation keys. **License Assignments** link seats and device-reported facts to a device. **Support Contracts** link provider records and effective terms to all covered devices. The **Coverage Report** links to upcoming renewals and lists devices without visible active support. Device pages show both license and contract assignments. Manage these records under `/api/plugins/refresh/license-entitlements/`, `license-assignments/`, and `support-contracts/`. Enter purchased terms from the vendor or contract; device reports are a separate observation.
  - **Software lifecycle dates** — enter vendor dates for maintenance, security fixes, and support on the exact Software Version record. Link the vendor notice. The dates are also visible on the device page when the version is linked to its software inventory record.
- `plugins/netbox-compliance/` — our **Config Compliance** plugin: device *configuration* standards defined in NetBox, with a verdict recorded per device per standard, so "how many devices still have `ip http server` on?" is a report rather than a script run against the whole fleet. Three shapes of rule (a line that must be **absent**, a line that must be **present**, an **exact set** such as local accounts where extras are the violation), scoped by platform/role/site/tag and effective-dated. Two capability flags rather than one, because "can a script fix this" and "may a run remove things" differ: the type-7 password standard is detectable and deliberately **audit-only**, and enforce is opt-in per standard. NetBox never holds a running configuration — several of these standards match lines containing secrets, so everything stored is redacted, and remediation is a *template* whose secret comes from the checker's environment at write time. REST **and** GraphQL (`config_standard_list`, `config_compliance_list`); the checker posts to `POST /api/plugins/compliance/config-compliance/report/`; seed the five shipped standards with `manage.py create_config_standards`. Use [raw command standards](docs/RAW-COMMAND-STANDARDS.md) for SSH MAC policy and read-only operational pass/fail checks. **Device Compliance** (Config Compliance → Device Compliance) is the same fleet as one row per device and a column per check: running code (Lifecycle), code staged (the latest staging job for the standard's preferred image), and every config standard in scope, each a tick, a red badge to act on, or a grey not checked/exempt/in progress; filter to devices with a failure and export CSV
- `apache/netbox.conf` — include for the **existing** Apache/Mellon server (a separate host): protects `/netbox`, strips spoofed identity headers, proxies over the private network + static mapping
- `deploy/` + `docs/RUNBOOK-*.md` — 30-day AMI redeploy automation and procedures (incl. `RUNBOOK-noexec-recovery.md` for hardened hosts where containers die at create-task with "permission denied")
- `scripts/prepare-docker-host.sh` — turn a fresh Ubuntu 24 / RHEL 9 box into a Docker host for these stacks: engine + Compose v2, **all Docker networking pinned to CGNAT** (100.64.0.0/10 — the 172.17/12 defaults collide with our networks)
- `testing/sso-idp/` — throwaway SAML IdP + real mod_auth_mellon SP for rehearsing the prod SSO config against a real round trip; see its README
- `scripts/standards.yaml` + `scripts/f5/` — our device standards (SNMP client allow list, syslog collectors) written once, platform-neutral, because the pollers and collectors are the same everywhere; `scripts/f5/f5_standards.py` maps them onto F5 BIG-IPs over iControl REST. Report-only by default (it prints what it would add, remove and leave alone), `--commit` writes, `--clean` enforces an exact match. `scripts/f5/f5_backup.py` is the other half: it pulls a dated UCS **and** SCF off each unit onto the server it runs from, again over iControl REST — md5-verified, `0600`, gitignored, with `--keep N` for retention. See [scripts/f5/README.md](scripts/f5/README.md)
- `scripts/f5-php-mitigation/` — standalone SSH job for F5 K000163821: single IP or CSV input, concurrent workers, `.env` credentials, per-device webhooks, and printed/JSON reports. Read-only checks by default; `--apply` installs the PHP INI mitigation and restarts the selected services. See [setup and receiver contract](scripts/f5-php-mitigation/README.md).
- `scripts/ios/` — the checker for the above: SSH (netmiko) to Cisco IOS/IOS-XE, compare against the standards **held in NetBox**, write the verdict back as a compliance record. Report-only by default; `--update` adds what is missing, `--enforce` also removes what a standard forbids, and neither writes to a device without `--commit`. Non-negotiable guards: never remove the session account, never remove the last privilege-15 local account, cancel removals if an addition could not be built, additions before removals with a re-read in between, and a redacted pre-change capture stored as a rollback reference. Tested against an emulated IOS SSH target, so none of it needs a real switch. See [scripts/ios/README.md](scripts/ios/README.md)
- `scripts/nornir-netops/` — the fleet-push half: takes a **CSV of addresses**, or NetBox (`--netbox`, with per-device source interfaces from interface custom fields), and converges one setting at a time from a per-platform Jinja template across Cisco IOS/IOS-XE and Arista EOS — NTP (with MD5 authentication keys), syslog (destinations, severity, origin-id), banners, ACLs (order enforced), local accounts with password rotation, SNMPv3 users/groups/views/hosts with v2c communities removed, and SNMP packet size. What to converge on comes from a committed `standards.yaml` beside the tool, so the fleet's values are stated once; flags override it. Dry run by default: it connects read-only and prints the exact commands it would send; `--add` adds, `--replace` also removes what is not desired, and neither writes without `--apply`. Device logins and account passwords come from a `.env` or AWS Secrets Manager via an instance role, never a flag, and are scrubbed out of every command list, report and device echo. Same class of guards as `scripts/ios/`: `--replace` never purges the session account, a legacy `password 7` account is negated and rewritten in one push rather than overwritten in place, and after applying the config is read back — if the change did not land the device is reported unverified and startup-config is **not** saved. Read-only checks answer whether a setting is actually working, not merely present (`check-ntp` reads peer associations, reach and sync state across the fleet). Opens a ServiceNow change from a dry run and closes it after applying, never approving one itself. Adding a domain is a feature module plus two templates. See [scripts/nornir-netops/README.md](scripts/nornir-netops/README.md)
- `scripts/clean_inventory.py` — standalone utility (unrelated to the deployment): cleans an inventory CSV by stripping component serials (modules, PSUs, line cards, optics) via the Cisco Product Information API, keeping real devices. Only rows whose name contains parentheses — the `(1)`/`(2)` duplicates — are sent to Cisco; `--check-all` overrides. On those rows a "no record at Cisco" also removes the row (`--keep-unknown` disables). Rows with plain names are never looked up or removed, and a failed lookup never removes anything. `--mode switches` narrows the result to switches only.

## Prod (summary)

Apache + Mellon run on a **separate, already-deployed server** which proxies to
this instance over the private network; the NetBox host runs neither.

One-time setup is [docs/FIRST-BOOT.md](docs/FIRST-BOOT.md): prepare
`/mnt/data_disk/netbox-secrets` on the data disk, set `BIND_ADDRESS=0.0.0.0`,
restrict port 8080 to the Apache server's security group, and add
[apache/netbox.conf](apache/netbox.conf) to that server's vhost. Every redeploy
after that is automatic via user-data/systemd —
[docs/RUNBOOK-redeploy.md](docs/RUNBOOK-redeploy.md).
