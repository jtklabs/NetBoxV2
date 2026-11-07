####
## Settings that netbox-docker does not map from environment variables.
## Everything here is env-gated, so the same file serves local and production.
####

from os import environ

## Serving under a subpath, e.g. https://netbox.example.com/netbox behind the
## SSO Apache. Set BASE_PATH=netbox/ in env/netbox.env. The proxy must map
## /netbox/static/ to the container's /static/ and pass every other /netbox/
## route through unchanged (apache/netbox.conf does both).
if environ.get("BASE_PATH"):
    BASE_PATH = environ["BASE_PATH"]
