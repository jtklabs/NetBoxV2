#!/usr/bin/env bash
# Check that apache/netbox.conf strips every client-supplied spelling of the
# identity headers NetBox trusts, such as X_Remote_User for X-Remote-User.
# Needs only Docker. Exits non-zero if any spelling reaches the backend.
#
#   testing/apache-header-spoof/run.sh
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
image="netbox-apache-header-spoof:test"

docker build --quiet --tag "$image" "$here" >/dev/null
docker run --rm \
  --volume "$repo/apache/netbox.conf:/src/netbox.conf:ro" \
  --volume "$repo/env/netbox.env.example:/src/netbox.env.example:ro" \
  "$image"
