#!/command/with-contenv bashio
# Initialise the s6 container environment from the HA Supervisor.
#
# This oneshot service runs before all longrun services (enforced via
# dependencies.d/hassio-env in each service directory). It:
#
#   - Reads per-service enable flags from /data/options.json and writes
#     ENABLE_* environment variables to the s6 container environment.
#   - Reads MQTT broker credentials from the Supervisor and writes
#     MQTT_* environment variables.
#   - Creates /share/mimirheim/dumps and /share/mimirheim/reports so that
#     the Samba add-on can expose them without user intervention.
#   - Writes CONFIG_EDITOR_ALLOWED_IP (the container gateway) so the
#     config editor restricts ingress to the HA ingress proxy only.
#
# When SUPERVISOR_TOKEN is absent (plain Docker), the script exits
# immediately. All ENABLE_* and MQTT_* variables remain unset. Services
# fall back to their config-file-presence gates and YAML-defined MQTT
# settings, preserving the existing plain-Docker behaviour exactly.

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
#
# /proc/net/route columns: Iface Destination Gateway Flags ...
# The default route has Destination == 00000000. Gateway is a little-endian
# 32-bit hex string: "0101A8C0" → reversed bytes → 192.168.1.1
# We extract it with awk (no arithmetic needed), then convert each byte pair
# to decimal with printf so no gawk extensions (e.g. strtonum) are required.
HEX_GW=$(awk 'NR>1 && $2=="00000000" { print $3; exit }' /proc/net/route)
if [ -n "${HEX_GW}" ]; then
    o1=$(printf '%d' "0x${HEX_GW:6:2}")
    o2=$(printf '%d' "0x${HEX_GW:4:2}")
    o3=$(printf '%d' "0x${HEX_GW:2:2}")
    o4=$(printf '%d' "0x${HEX_GW:0:2}")
    printf '%s' "${o1}.${o2}.${o3}.${o4}" > /var/run/s6/container_environment/CONFIG_EDITOR_ALLOWED_IP
fi

# MQTT broker credentials from the Supervisor. These override any mqtt:
# values in the YAML config files when running as a HA add-on.
printf '%s' "$(bashio::services mqtt 'host')"     > /var/run/s6/container_environment/MQTT_HOST
printf '%s' "$(bashio::services mqtt 'port')"     > /var/run/s6/container_environment/MQTT_PORT
printf '%s' "$(bashio::services mqtt 'username')" > /var/run/s6/container_environment/MQTT_USERNAME
printf '%s' "$(bashio::services mqtt 'password')" > /var/run/s6/container_environment/MQTT_PASSWORD
# bashio returns 'true' or 'false' as a string.
printf '%s' "$(bashio::services mqtt 'ssl')"      > /var/run/s6/container_environment/MQTT_SSL
