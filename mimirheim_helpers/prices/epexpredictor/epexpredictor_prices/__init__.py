"""epexpredictor_prices — EPEX day-ahead spot price prediction fetcher for mimirheim.

This package fetches EPEX day-ahead spot price predictions from the
EpexPredictor API (https://epexpredictor.batzill.com) and publishes them to
an MQTT topic in the format expected by mimirheim, extending the price
horizon beyond what a true day-ahead source (Nordpool, a supplier feed)
covers, at progressively lower confidence.

It does not contain any solver logic and does not import from the mimirheim
package. Communication with mimirheim is exclusively via MQTT.
"""
