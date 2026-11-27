"""baseload_weather: weather-dependent base load forecast helper for mimirheim.

Reads hourly energy and outdoor temperature history from the Home Assistant
recorder database, fits a per-hour heating-degree regression, combines it
with an Open-Meteo temperature forecast and publishes a static load forecast
to mimirheim over MQTT. It never imports from ``mimirheim/`` and never calls
``build_and_solve()``.
"""
