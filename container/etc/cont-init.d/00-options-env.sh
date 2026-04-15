#!/usr/bin/env bashio
# Read per-service enable flags from the HA add-on options and write them
# to the s6 container environment so all service run scripts can check them.
#
# Script ordering: 00 runs before 01-mqtt-env.sh and before any s6 service.
#
# Guards: only runs under the HA Supervisor (SUPERVISOR_TOKEN present).
# When absent (plain Docker), the script exits immediately and all ENABLE_*
# variables remain unset. The s6 run scripts treat an unset variable as
# "not managed by HA" and fall back to config-file-presence gating, which
# preserves the existing plain-Docker behaviour exactly.
#
# This script also:
#   - Creates /share/mimirheim/dumps and /share/mimirheim/reports so that
#     the Samba add-on can expose them without user intervention.
#   - Writes CONFIG_EDITOR_ALLOWED_IP (the container gateway, i.e. the
#     Supervisor host) so the config editor restricts connections to the
#     HA ingress proxy.

if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    exit 0
fi

mkdir -p /var/run/s6/container_environment

# Create shared output directories under /share (Samba-accessible).
mkdir -p /share/mimirheim/dumps
mkdir -p /share/mimirheim/reports

# Read each enable flag from /data/options.json via bashio and publish to
# the s6 container environment.
for SERVICE in nordpool pv_fetcher pv_ml_learner baseload_ha baseload_ha_db \
               baseload_static scheduler reporter config_editor; do
    KEY="enable_${SERVICE}"
    ENV_VAR="ENABLE_$(echo "${SERVICE}" | tr '[:lower:]' '[:upper:]')"
    if bashio::config.true "${KEY}"; then
        printf 'true'  > "/var/run/s6/container_environment/${ENV_VAR}"
    else
        printf 'false' > "/var/run/s6/container_environment/${ENV_VAR}"
    fi
done

# Write the default gateway IP (the Supervisor host) for the config editor's
# IP allowlist. The ingress proxy forwards traffic from this IP, so
# restricting to it prevents direct access from the LAN.
GATEWAY=$(ip route show default | awk '/default/ { print $3 }')
if [ -n "${GATEWAY}" ]; then
    printf '%s' "${GATEWAY}" > /var/run/s6/container_environment/CONFIG_EDITOR_ALLOWED_IP
fi
