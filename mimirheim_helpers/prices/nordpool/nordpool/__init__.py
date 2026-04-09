"""nordpool — Day-ahead electricity price fetcher for mimirheim.

This package fetches confirmed day-ahead electricity prices from the Nordpool
data portal and publishes them to an MQTT topic in the format expected by mimirheim.

It does not contain any solver logic and does not import from the mimirheim package.
Communication with mimirheim is exclusively via MQTT.
"""
