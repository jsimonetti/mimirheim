"""zonneplan_prices — Zonneplan electricity price fetcher for mimirheim.

This package fetches hourly electricity prices from the Zonneplan API and
publishes them to an MQTT topic in the format expected by mimirheim.

It does not contain any solver logic and does not import from the mimirheim
package. Communication with mimirheim is exclusively via MQTT.

Zonneplan uses an email-based OTP authentication flow. The daemon handles the
auth flow itself; no separate CLI or container exec is required. The user must
click a link in a login email once per token lifetime.
"""
