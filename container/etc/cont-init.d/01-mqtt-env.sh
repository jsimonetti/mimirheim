#!/usr/bin/env bashio
# Inject MQTT broker credentials from the HA Supervisor into the s6
# container environment so all services inherit them automatically.
#
# When running as a HA add-on, the Supervisor exposes the active MQTT broker's
# credentials via bashio. This script reads them and writes each value to
# /var/run/s6/container_environment/ so that every s6 service run script
# started with #!/usr/bin/with-contenv sh picks them up as environment
# variables.
#
# These variables override any mqtt: values in the YAML config files.
# Users do not need to copy broker credentials into their config files when
# running as a HA add-on.
#
# Guards: only runs when SUPERVISOR_TOKEN is set (i.e. under the HA
# Supervisor). When running as a plain Docker container the variable is
# absent and this script exits immediately, leaving the YAML config files
# as the sole source of MQTT credentials.

if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    exit 0
fi

mkdir -p /var/run/s6/container_environment

printf '%s' "$(bashio::services mqtt 'host')"     > /var/run/s6/container_environment/MQTT_HOST
printf '%s' "$(bashio::services mqtt 'port')"     > /var/run/s6/container_environment/MQTT_PORT
printf '%s' "$(bashio::services mqtt 'username')" > /var/run/s6/container_environment/MQTT_USERNAME
printf '%s' "$(bashio::services mqtt 'password')" > /var/run/s6/container_environment/MQTT_PASSWORD
# bashio returns 'true' or 'false' as a string.
printf '%s' "$(bashio::services mqtt 'ssl')"      > /var/run/s6/container_environment/MQTT_SSL
