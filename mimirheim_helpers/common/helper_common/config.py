"""Shared Pydantic config models and utilities for mimirheim input helper daemons.

All helper tools import ``MqttConfig`` from here rather than defining their
own copy. This ensures every tool has TLS support and consistent field
validation without duplicating the model.

This module also provides ``apply_mqtt_env_overrides``, which is called by
every helper config loader to inject MQTT broker credentials from the HA
Supervisor environment before Pydantic validation runs.

This module has no imports from any specific helper tool.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field


class MqttConfig(BaseModel):
    """MQTT broker connection parameters, shared by all mimirheim input helper daemons.

    Attributes:
        host: Broker hostname or IP address.
        port: Broker TCP port. Default is 1883 (unencrypted); use 8883 with TLS.
        client_id: MQTT client identifier. Must be unique on the broker.
        username: Optional broker username. Omit for anonymous access.
        password: Optional broker password.
        tls_allow_insecure: Enable TLS without verifying the broker certificate.
            Useful for self-signed certificates on private networks. Do not use
            against a broker reachable from an untrusted network.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = Field(description="Broker hostname or IP address.", json_schema_extra={"ui_label": "Broker host", "ui_group": "basic"})
    port: int = Field(default=1883, ge=1, le=65535, description="Broker TCP port.", json_schema_extra={"ui_label": "Broker port", "ui_group": "advanced"})
    client_id: str = Field(description="MQTT client identifier.", json_schema_extra={"ui_label": "Client ID", "ui_group": "basic"})
    username: str | None = Field(default=None, description="Broker username.", json_schema_extra={"ui_label": "Username", "ui_group": "advanced"})
    password: str | None = Field(default=None, description="Broker password.", json_schema_extra={"ui_label": "Password", "ui_group": "advanced"})
    tls_allow_insecure: bool = Field(
        default=False,
        description="Enable TLS without verifying the broker certificate.",
        json_schema_extra={"ui_label": "Allow insecure TLS", "ui_group": "advanced"},
    )


class HomeAssistantConfig(BaseModel):
    """Home Assistant MQTT discovery settings for a helper tool.

    When ``enabled`` is True, the daemon publishes a retained discovery payload
    to ``{discovery_prefix}/button/{tool_name}/config`` on every broker connect
    and whenever HA's birth message (``homeassistant/status = online``) is
    received. This creates a button entity in HA that triggers the tool on press.

    Attributes:
        enabled: Enable or disable HA discovery for this tool. Defaults to False
            for backward compatibility — existing configs that do not have a
            ``homeassistant:`` section are unaffected.
        discovery_prefix: HA discovery topic prefix. Defaults to ``homeassistant``.
        device_name: Human-readable display name for the HA device card. If
            omitted, the daemon uses a formatted version of the tool name.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable HA MQTT discovery.", json_schema_extra={"ui_label": "Enable HA discovery", "ui_group": "advanced"})
    discovery_prefix: str = Field(
        default="homeassistant",
        description="HA MQTT discovery topic prefix.",
        json_schema_extra={"ui_label": "Discovery prefix", "ui_group": "advanced"},
    )
    device_name: str = Field(
        default="",
        description="Display name for the HA device. Defaults to tool name.",
        json_schema_extra={"ui_label": "HA device name", "ui_group": "advanced"},
    )


def apply_mqtt_env_overrides(raw: dict) -> dict:
    """Override the mqtt: section from environment variables if present.

    When running as a HA add-on the Supervisor injects MQTT broker credentials
    as environment variables (written by container/etc/cont-init.d/01-mqtt-env.sh
    before any s6 service starts). These take precedence over whatever appears in
    the YAML config file so users do not need to copy broker credentials into
    their config.

    When the environment variables are absent (plain Docker, no Supervisor) this
    function is a no-op and the YAML values are used as-is.

    Args:
        raw: The raw dict parsed from the YAML config file. Modified in-place
            and returned.

    Returns:
        The same dict with any MQTT env var overrides applied.
    """
    overrides: dict = {}
    if host := os.environ.get("MQTT_HOST"):
        overrides["host"] = host
    if port_str := os.environ.get("MQTT_PORT"):
        overrides["port"] = int(port_str)
    if username := os.environ.get("MQTT_USERNAME"):
        overrides["username"] = username
    if password := os.environ.get("MQTT_PASSWORD"):
        overrides["password"] = password
    # MQTT_SSL is 'true' or 'false' (a string) as returned by bashio.
    # tls_allow_insecure is the inverse of ssl: when SSL is disabled the
    # connection uses plain TCP, not an insecure TLS tunnel.
    if ssl_str := os.environ.get("MQTT_SSL"):
        overrides["tls_allow_insecure"] = ssl_str.lower() != "true"
    if overrides:
        raw.setdefault("mqtt", {})
        raw["mqtt"].update(overrides)
    return raw
