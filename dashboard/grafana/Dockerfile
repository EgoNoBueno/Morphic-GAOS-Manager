FROM grafana/grafana-oss:10.4.2

# Install BigQuery datasource plugin at image build time (not runtime) so the
# container starts without needing internet access or extra startup time.
# GF_INSTALL_PLUGINS is intentionally NOT used — it downloads at startup and
# fails silently under Cloud Run's startup probe timing.
USER root
RUN grafana-cli --pluginsDir /var/lib/grafana/plugins plugins install grafana-bigquery-datasource
USER grafana

# Explicit database config — SQLite is correct for a single-instance,
# read-only CEO dashboard. Dashboards survive restarts because they are
# baked into the image as provisioning files, not stored in the DB.
ENV GF_DATABASE_TYPE="sqlite3"

# Grafana provisioning path — datasources and dashboard providers live here.
ENV GF_PATHS_PROVISIONING="/etc/grafana/provisioning"

COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/ /etc/grafana/dashboards/

EXPOSE 3000
