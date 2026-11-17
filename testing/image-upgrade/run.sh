#!/usr/bin/env bash
# Verify upgrading both services from an old image with an unchanged legacy .env.
set -euo pipefail
repo_dir=$(cd "$(dirname "$0")/../.." && pwd)
case_dir=$(mktemp -d)
project_name="lifecycle-upgrade-check-$$"
image=""
source_image="netbox-lifecycle:$project_name-source"
old_image="netbox-lifecycle:$project_name-legacy-0.4.4"
compose() {
  COMPOSE_PROJECT_NAME="$project_name" docker compose -p "$project_name" \
    --env-file "$case_dir/.env" -f "$case_dir/docker-compose.yml" \
    -f "$repo_dir/testing/image-upgrade/compose.test.yml" "$@"
}
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker image rm "$image" "$source_image" "$old_image" >/dev/null 2>&1 || true
  rm -rf "$case_dir"
}
trap cleanup EXIT
cp "$repo_dir/Dockerfile" "$repo_dir/docker-compose.yml" "$repo_dir/.dockerignore" "$case_dir/"
cp -R "$repo_dir/plugins" "$repo_dir/configuration" "$case_dir/"
mkdir "$case_dir/env"
for file in "$repo_dir"/env/*.env.example; do
  cp "$file" "$case_dir/env/$(basename "${file%.example}")"
done
cp "$repo_dir/.env.example" "$case_dir/.env"
# Keep the previous installation's plugin pin throughout the entire upgrade.
printf '\nLIFECYCLE_VERSION=0.4.4\nNETBOX_PORT=0\n' >> "$case_dir/.env"
expected=$(sed -n "s/^ *version = '\(.*\)'/\1/p" "$repo_dir/plugins/netbox-refresh/netbox_refresh/__init__.py")
image=$(compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["netbox"]["image"])')
case "$image" in
  *-"$expected") ;;
  *) printf 'Compose selected a different plugin release: %s\n' "$image" >&2; exit 1 ;;
esac
compose build netbox
docker tag "$image" "$source_image"
cat > "$case_dir/Dockerfile.stale" <<'DOCKERFILE'
ARG SOURCE_IMAGE=netbox-lifecycle:fixture-source
FROM ${SOURCE_IMAGE}
USER root
RUN sed -i "s/version = '[0-9.]*'/version = '0.4.4'/" /opt/netbox-plugins/netbox-refresh/netbox_refresh/__init__.py
DOCKERFILE
docker build --build-arg "SOURCE_IMAGE=$source_image" -f "$case_dir/Dockerfile.stale" -t "$old_image" "$case_dir"
# Model the previous release's environment-selected image tag.
cat > "$case_dir/compose.before.yml" <<'YAML'
services:
  netbox:
    image: netbox-lifecycle:${COMPOSE_PROJECT_NAME}-legacy-${LIFECYCLE_VERSION:-0.4.4}
  netbox-worker:
    image: netbox-lifecycle:${COMPOSE_PROJECT_NAME}-legacy-${LIFECYCLE_VERSION:-0.4.4}
YAML
compose -f "$case_dir/compose.before.yml" up -d --no-deps --no-build --pull never netbox netbox-worker
version() {
  compose exec -T "$1" /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell \
    -c 'from netbox_refresh import config; print(config.version)' | tail -n 1
}
test "$(version netbox)" = 0.4.4
test "$(version netbox-worker)" = 0.4.4
old_web=$(compose ps -q netbox)
old_worker=$(compose ps -q netbox-worker)
# No --build flag, version override, environment edit or manual image retagging.
compose up -d --no-deps netbox netbox-worker
test "$(version netbox)" = "$expected"
test "$(version netbox-worker)" = "$expected"
new_web=$(compose ps -q netbox)
new_worker=$(compose ps -q netbox-worker)
test "$old_web" != "$new_web"
test "$old_worker" != "$new_worker"
test "$(docker inspect -f '{{.Image}}' "$new_web")" = "$(docker inspect -f '{{.Image}}' "$new_worker")"
grep -qx 'LIFECYCLE_VERSION=0.4.4' "$case_dir/.env"
printf 'Upgrade passed: both services load Lifecycle %s with the legacy environment unchanged.\n' "$expected"
