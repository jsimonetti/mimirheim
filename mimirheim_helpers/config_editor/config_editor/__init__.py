"""mimirheim config editor web service.

This package provides a lightweight in-container web UI for editing
mimirheim.yaml via a browser. It is activated by the presence of
/config/config-editor.yaml; if that file is absent the s6 service sleeps
harmlessly.

What this package does not do:
- It does not import from mimirheim core device or IO modules.
- It does not perform MQTT operations.
- It does not authenticate users.
"""
