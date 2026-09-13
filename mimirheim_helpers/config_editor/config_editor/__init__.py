"""config_editor: the Config Editor client of the Config Service protocol.

Discovers Config Owners (mimirheim core, and any helper that adopts the
Config Service protocol) via their retained Descriptors on MQTT
(`registry.py`, `mqtt_client.py`), renders each one's configuration as a
server-rendered page, and submits edits back to it for validation and
writing (`render.py`, `server.py`, `submission.py`, `templates/`).

This package never imports a Config Owner's pydantic validation model
directly, and never reads or writes a Config Owner's configuration file
itself: it only consumes the generic `Descriptor`/`FormSpec` types from
`mimirheim_shared`, and forwards Candidate Values to the owning process's
own `validate_and_write` over MQTT.
"""
