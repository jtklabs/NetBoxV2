"""Check that apache/netbox.conf strips client-supplied identity headers.

Runs inside the image built by run.sh. The repo's apache/netbox.conf and
env/prod.env.example are mounted under /src.

NetBox trusts a handful of WSGI keys for remote auth (HTTP_X_REMOTE_USER and
friends). The WSGI server maps both "-" and "_" in a header name to "_", so
X-Remote-User, X_Remote_User and X-Remote_User all land on the same key. This
check sends every one of those spellings through Apache and fails if any of
them reaches the backend.

The trusted keys come from the REMOTE_AUTH_* settings in env/prod.env.example
and from every RequestHeader set/unset line in apache/netbox.conf, so a new
identity header fails here until all its spellings are unset.
"""

from __future__ import annotations

import http.client
import http.server
import itertools
import json
import re
import socket
import subprocess
import sys
import threading
import time

CONF_SOURCE = "/src/netbox.conf"
ENV_SOURCE = "/src/prod.env.example"
CONF_TARGET = "/etc/apache2/conf-available/netbox.conf"
BACKEND_PORT = 8080
# /netbox/api skips the SAML login, so a spoofed header there goes straight to
# NetBox. The rest of /netbox redirects to the IdP first, and the same
# vhost-scope unsets cover it once the user has logged in.
PATHS = ("/netbox/api/", "/netbox/api/users/config/")


def wsgi_key(header: str) -> str:
    return "HTTP_" + header.upper().replace("-", "_")


def spellings(key: str) -> list[str]:
    """Every header name that the WSGI server maps to `key`."""
    parts = key.removeprefix("HTTP_").split("_")
    names = []
    for separators in itertools.product("-_", repeat=len(parts) - 1):
        name = parts[0]
        for separator, part in zip(separators, parts[1:]):
            name += separator + part.capitalize()
        names.append(name)
    return names


def prepare_conf() -> set[str]:
    """Install the repo conf as production would run it; return the identity keys."""
    with open(CONF_SOURCE) as handle:
        lines = handle.read().splitlines()
    rendered, in_mapping = [], False
    for line in lines:
        if line.startswith("Define NETBOX_BACKEND "):
            line = f"Define NETBOX_BACKEND http://127.0.0.1:{BACKEND_PORT}"
        # Production fills in and uncomments the Mellon mapping block.
        if line == "# <Location /netbox>":
            in_mapping = True
        if in_mapping:
            line = re.sub(r"^# ?", "", line)
            if line == "</Location>":
                in_mapping = False
        rendered.append(line)
    if in_mapping or "<Location /netbox>\n    MellonMergeEnvVars" not in "\n".join(rendered):
        sys.exit("FAIL: could not find the Mellon mapping block; did apache/netbox.conf change shape?")
    with open(CONF_TARGET, "w") as handle:
        handle.write("\n".join(rendered) + "\n")

    keys = set()
    for line in lines:
        match = re.match(r"^\s*#?\s*RequestHeader\s+(?:set|unset)\s+(\S+)", line)
        if match and not match.group(1).lower().startswith("x-forwarded-"):
            keys.add(wsgi_key(match.group(1)))
    with open(ENV_SOURCE) as handle:
        for line in handle:
            match = re.match(
                r"^REMOTE_AUTH_(?:HEADER|USER_EMAIL|USER_FIRST_NAME|USER_LAST_NAME|GROUP_HEADER)=(HTTP_\w+)",
                line,
            )
            if match:
                keys.add(match.group(1))
    return keys


class Backend(http.server.BaseHTTPRequestHandler):
    """Stands in for NetBox: echoes the header names and values it received."""

    def do_GET(self) -> None:
        body = json.dumps({"backend": True, "headers": list(self.headers.items())}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def wait_for_port(port: int) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.2)
    sys.exit(f"FAIL: nothing listening on port {port}")


def send(path: str, headers: list[tuple[str, str]]) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", 80, timeout=10)
    connection.putrequest("GET", path)
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    if response.status != 200:
        sys.exit(f"FAIL: {path} returned HTTP {response.status} instead of reaching the backend")
    return json.loads(body)


def main() -> int:
    keys = prepare_conf()
    print("identity keys NetBox or Apache treat as trusted:")
    for key in sorted(keys):
        print(f"  {key}")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", BACKEND_PORT), Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    subprocess.run(["apache2ctl", "configtest"], check=True)
    subprocess.run(["apache2ctl", "start"], check=True)
    wait_for_port(80)

    spoofed = [(name, "spoofed") for key in sorted(keys) for name in spellings(key)]
    controls = [("X-Probe", "present"), ("X_Probe_Underscore", "present")]
    failures = 0
    for path in PATHS:
        received = send(path, spoofed + controls)["headers"]
        names = {name.lower() for name, _ in received}
        if "x-probe" not in names:
            print(f"FAIL {path}: control header X-Probe did not reach the backend")
            failures += 1
        leaked = sorted(name for name, _ in received if wsgi_key(name) in keys)
        print(
            f"{path}: sent {len(spoofed)} spoofed spellings; "
            f"underscore control {'forwarded' if 'x_probe_underscore' in names else 'dropped'} by Apache"
        )
        for name in leaked:
            print(f"FAIL {path}: {name} reached the backend as {wsgi_key(name)}")
        failures += len(leaked)

    print("all identity header spellings stripped" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
