"""ML-based PV production forecast helper for mimirheim.

This package contains a daemon that:
- Trains an XGBoost model on historical PV production (from Home Assistant)
  and historical weather observations (from KNMI).
- Publishes hourly production forecasts driven by Meteoserver weather predictions.
- Outputs an MQTT payload in the mimirheim PowerForecastStep format.

It does not import from mimirheim core. All communication with mimirheim is via MQTT.
"""
