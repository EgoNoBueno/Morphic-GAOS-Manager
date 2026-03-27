FROM grafana/grafana-oss:10.4.2

# Install BigQuery datasource plugin at image build time so the container
# starts without needing internet access to the Grafana plugin registry.
ENV GF_INSTALL_PLUGINS="grafana-bigquery-datasource"

# Explicit database config — SQLite is correct for a single-instance,
# read-only CEO dashboard. Dashboards survive restarts because they are
# baked into the image as provisioning files, not stored in the DB.
ENV GF_DATABASE_TYPE="sqlite3"

# Grafana provisioning path — datasources and dashboard providers live here.
ENV GF_PATHS_PROVISIONING="/etc/grafana/provisioning"

COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/ /etc/grafana/dashboards/

EXPOSE 3000
