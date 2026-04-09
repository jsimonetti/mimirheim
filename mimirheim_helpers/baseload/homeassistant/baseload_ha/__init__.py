"""baseload_ha — Home Assistant base load forecast tool for mimirheim.

This package queries Home Assistant's long-term statistics API for one or more
power sensors, builds a same-hour-average load profile, and publishes it to
MQTT in the format expected by mimirheim's static_loads input.

It does not contain solver logic and does not import from the mimirheim package.
"""
