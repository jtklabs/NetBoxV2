# NetBox 4.7 with the Lifecycle plugin (plugins/netbox-refresh).
# The plugin needs nothing beyond NetBox's own dependencies, so it is copied in
# and put on PYTHONPATH rather than installed; its static files go where
# NetBox's collected static files already live.
ARG NETBOX_IMAGE=docker.io/netboxcommunity/netbox:v4.7.4-5.1.1
FROM ${NETBOX_IMAGE}
COPY plugins/netbox-refresh/netbox_refresh /opt/netbox-plugins/netbox-refresh/netbox_refresh
COPY plugins/netbox-refresh/netbox_refresh/static/netbox_refresh /opt/netbox/netbox/static/netbox_refresh
ENV PYTHONPATH=/opt/netbox-plugins/netbox-refresh
