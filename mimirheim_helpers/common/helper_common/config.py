"""Shared Pydantic config models and utilities for mimirheim input helper daemons.

All helper tools import ``MqttConfig`` from here rather than defining their
own copy. This ensures every tool has TLS support and consistent field
validation without duplicating the model.

This module also provides ``apply_mqtt_env_overrides``, which injects MQTT
broker credentials from the HA Supervisor environment before Pydantic
validation runs, and ``load_helper_config``, the shared YAML-load-and-validate
entry point that every helper's ``__main__`` calls.

This module has no imports from any specific helper tool.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field

_ConfigT = TypeVar("_ConfigT", bound=BaseModel)


class MqttConfig(BaseModel):
    """MQTT broker connection parameters, shared by all mimirheim input helper daemons.

    Attributes:
        host: Broker hostname or IP address.
        port: Broker TCP port. Default is 1883 (unencrypted); use 8883 with TLS.
        client_id: MQTT client identifier. Must be unique on the broker.
        username: Optional broker username. Omit for anonymous access.
        password: Optional broker password.
        tls: Enable TLS for the broker connection. Set to True when the broker
            listens on an encrypted port (typically 8883). When False, a
            plaintext connection is made regardless of the port number.
        tls_allow_insecure: When True and tls is also True, skip broker
            certificate verification. Useful for self-signed certificates on
            private networks. Has no effect when tls is False. Do not use
            against a broker reachable from an untrusted network.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"x-format": "categories-vertical", "x-categoryOrder": ["Basic", "Advanced"]})

    host: str = Field(description="Broker hostname or IP address.", title="Broker host")
    port: int = Field(default=1883, ge=1, le=65535, description="Broker TCP port.", title="Broker port", json_schema_extra={"x-category": "Advanced"})
    client_id: str | None = Field(default=None, description="MQTT client identifier. Defaults to a tool-specific value when not set.", title="Client ID")
    username: str | None = Field(default=None, description="Broker username.", title="Username", json_schema_extra={"x-category": "Advanced"})
    password: str | None = Field(default=None, description="Broker password.", title="Password", json_schema_extra={"x-category": "Advanced"})
    tls: bool = Field(
        default=False,
        description="Enable TLS for the broker connection. Set to true when the broker listens on an encrypted port (typically 8883).",
        title="Enable TLS", json_schema_extra={"x-category": "Advanced"},
    )
    tls_allow_insecure: bool = Field(
        default=False,
        description="Skip broker certificate verification when TLS is enabled. Has no effect when tls is false.",
        title="Allow insecure TLS", json_schema_extra={"x-category": "Advanced"},
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

    model_config = ConfigDict(extra="forbid", json_schema_extra={"x-format": "categories-vertical", "x-categoryOrder": ["Basic", "Advanced"]})

    enabled: bool = Field(default=False, description="Enable HA MQTT discovery.", title="Enable HA discovery", json_schema_extra={"x-category": "Advanced"})
    discovery_prefix: str = Field(
        default="homeassistant",
        description="HA MQTT discovery topic prefix.",
        title="Discovery prefix", json_schema_extra={"x-category": "Advanced"},
    )
    device_name: str = Field(
        default="",
        description="Display name for the HA device. Defaults to tool name.",
        title="HA device name", json_schema_extra={"x-category": "Advanced"},
    )
    forecast_sensor: bool = Field(
        default=True,
        description="Publish an additional HA sensor entity for the helper's forecast output topic.",
        title="Enable forecast sensor", json_schema_extra={"x-category": "Advanced"},
    )


def _parse_port(port_str: str) -> int:
    """Parse the MQTT_PORT environment variable into a usable TCP port.

    Args:
        port_str: The raw environment variable value.

    Returns:
        The port as an int.

    Raises:
        ValueError: If the value is not an integer, or not in 1..65535. Naming
            the variable and quoting the value matters here: this runs before
            Pydantic sees the config, so without it the operator gets a bare
            "invalid literal for int()" with no clue which setting is wrong.
    """
    try:
        port = int(port_str)
    except ValueError:
        raise ValueError(
            f"MQTT_PORT is not a number: {port_str!r}."
        ) from None
    if not 1 <= port <= 65535:
        raise ValueError(
            f"MQTT_PORT is not a valid TCP port: {port_str!r}. "
            "It must be between 1 and 65535."
        )
    return port


def mqtt_env_overrides() -> dict:
    """Return the ``mqtt`` field overrides supplied by the environment.

    When running as a HA add-on the Supervisor injects MQTT broker credentials
    as environment variables (written by
    container/etc/cont-init.d/01-mqtt-env.sh before any s6 service starts).
    This maps them onto ``MqttConfig`` field names.

    The mapping lives here so there is one definition of it. The config editor
    needs the same values to show which fields the Supervisor controls, and
    previously carried its own copy -- which had already drifted, silently
    ignoring an unparseable MQTT_PORT where this one crashed on it.

    Returns:
        Dict mapping mqtt field names to their env-supplied values. Empty when
        no MQTT env vars are set (plain Docker, no Supervisor).

    Raises:
        ValueError: If ``MQTT_PORT`` is set but is not a valid TCP port.
    """
    overrides: dict = {}
    if host := os.environ.get("MQTT_HOST"):
        overrides["host"] = host
    if port_str := os.environ.get("MQTT_PORT"):
        overrides["port"] = _parse_port(port_str)
    if username := os.environ.get("MQTT_USERNAME"):
        overrides["username"] = username
    if password := os.environ.get("MQTT_PASSWORD"):
        overrides["password"] = password
    # MQTT_SSL is 'true' or 'false' (a string) as returned by bashio.
    if ssl_str := os.environ.get("MQTT_SSL"):
        overrides["tls"] = ssl_str.lower() == "true"
    return overrides


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

    Raises:
        ValueError: If ``raw`` is not a mapping (an empty or comment-only
            config file), or if ``MQTT_PORT`` is set to something that is not a
            valid TCP port.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            "The configuration file contains no configuration. It is empty, "
            "holds only comments, or is not a YAML mapping."
        )

    overrides = mqtt_env_overrides()
    if overrides:
        # A bare `mqtt:` key in YAML parses to None, not to {}. Treat a null
        # section as an absent one so a config that leaves every broker setting
        # to the Supervisor is valid; previously this raised AttributeError on
        # the update() below.
        if raw.get("mqtt") is None:
            raw["mqtt"] = {}
        raw["mqtt"].update(overrides)
    return raw


def load_helper_config(
    path: str,
    model_cls: type[_ConfigT],
    logger: logging.Logger,
) -> _ConfigT:
    """Load, env-override and validate a helper's YAML configuration file.

    Every helper daemon starts the same way: read the YAML, let the HA
    Supervisor environment override the ``mqtt`` section, validate against the
    helper's own Pydantic model, and abort the process if any of that fails.
    This function is that sequence, shared so the five helpers that used to
    carry an identical private copy stay in step.

    Failure is terminal by design. A daemon cannot do useful work with a
    configuration it could not read, and exiting lets the supervisor restart it
    once the operator fixes the file.

    Args:
        path: Filesystem path to the YAML configuration file.
        model_cls: The helper's Pydantic config model.
        logger: The calling helper's logger, so the failure record carries the
            helper's name rather than this module's.

    Returns:
        A validated instance of ``model_cls``.

    Raises:
        SystemExit: With code 1 if the file cannot be read or parsed, or if it
            fails validation. The full traceback is logged first.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text())
        apply_mqtt_env_overrides(raw)
        return model_cls.model_validate(raw)
    except Exception:
        logger.exception("Failed to load configuration from %s", path)
        sys.exit(1)


def map_entry_schema_extra(
    extra: dict[str, Any] | None = None,
) -> Callable[[dict[str, Any], type], None]:
    """Build a Pydantic ``json_schema_extra`` hook for a dynamic-map value model.

    Use as ``model_config = ConfigDict(json_schema_extra=map_entry_schema_extra(...))``
    on a model that is only ever used as the value type of a ``dict[str,
    ThisModel]`` field (e.g. mimirheim's ``batteries``/``pv_arrays``, or a
    helper's own per-device ``arrays`` map) whose *field* schema sets
    ``x-format: "nav-horizontal"`` so the config editor renders one tab per
    named entry instead of a stacked "Add property" list.

    That per-entry tab label is the value schema's own ``title`` if present,
    falling back to the entry's actual map key only when it is not (the
    config editor's Jedison-based frontend has no way to prefer the map key
    over an explicit title). Every Pydantic model gets an auto-generated
    ``title`` equal to its class name, so left alone every tab in a map with
    more than one entry would show that same, indistinguishable class-name
    label instead of the device's own name. Popping ``title`` here is what
    makes the tabs actually distinguish entries.

    Args:
        extra: This model's own ``json_schema_extra`` dict, if it needs one
            (e.g. the usual ``{"x-format": "categories-vertical",
            "x-categoryOrder": [...]}`` boilerplate) -- ``ConfigDict`` accepts
            only one ``json_schema_extra`` value (a dict or a callable, never
            both), so a model that already needs a dict passes it through
            here instead of setting it directly.

    Returns:
        A callable suitable for ``ConfigDict(json_schema_extra=...)``.
    """

    def hook(schema: dict[str, Any], model: type) -> None:
        if extra:
            schema.update(extra)
        schema.pop("title", None)

    return hook
