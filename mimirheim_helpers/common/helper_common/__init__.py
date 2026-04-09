"""Shared MQTT infrastructure for mimirheim input helper daemons.

Provides:
- ``MqttConfig``: canonical broker connection config model (shared by all helpers).
- ``HomeAssistantConfig``: optional HA discovery config model.
- ``HelperDaemon``: abstract base class that handles paho setup, TLS, auth,
  trigger subscription, debouncing, HA birth message handling, and discovery.
- ``publish_trigger_discovery``: publish a single HA button entity for a tool.
"""
