"""config_editor_v3: the Config Editor client of the Config Service protocol.

Discovers Config Owners (mimirheim core, and any helper that adopts the
Config Service protocol) via their retained Descriptors on MQTT
(`registry.py`, `mqtt_client.py`), and renders each one's configuration as a
read-only, server-rendered page (`render.py`, `server.py`, `templates/`).

This package never imports a Config Owner's pydantic validation model
directly, and never reads or writes a Config Owner's configuration file
itself: it only consumes the generic `Descriptor`/`FormSpec` types from
`mimirheim_shared`. Submitting edits (`validate_and_write`) is added in a
later step; this package is read-only for now.
"""
