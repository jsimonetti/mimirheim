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
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mimirheim_shared.formspec import FormSpec

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

    model_config = ConfigDict(extra="forbid")

    host: str = Field(description="Broker hostname or IP address.")
    port: int = Field(default=1883, ge=1, le=65535, description="Broker TCP port.")
    client_id: str | None = Field(default=None, description="MQTT client identifier. Defaults to a tool-specific value when not set.")
    username: str | None = Field(default=None, description="Broker username.")
    password: str | None = Field(default=None, description="Broker password.")
    tls: bool = Field(
        default=False,
        description="Enable TLS for the broker connection. Set to true when the broker listens on an encrypted port (typically 8883)."
    )
    tls_allow_insecure: bool = Field(
        default=False,
        description="Skip broker certificate verification when TLS is enabled. Has no effect when tls is false."
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

    enabled: bool = Field(default=False, description="Enable HA MQTT discovery.")
    discovery_prefix: str = Field(
        default="homeassistant",
        description="HA MQTT discovery topic prefix."
    )
    device_name: str = Field(
        default="",
        description="Display name for the HA device. Defaults to tool name."
    )
    forecast_sensor: bool = Field(
        default=True,
        description="Publish an additional HA sensor entity for the helper's forecast output topic."
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


def _read_raw_config(path: str) -> tuple[dict, str | None]:
    """Read and parse a helper's YAML file, never raising.

    A missing file, an unreadable file, and a file that is not valid YAML are
    all reported the same way: as an error string paired with an empty dict,
    rather than an exception. This is what lets ``load_helper_config`` treat
    "no usable file yet" as just another shape of "the configuration does not
    currently validate", the same non-fatal outcome as a file that parses but
    fails the helper's own schema (see ADR-0009 and
    ``mimirheim/io/config_service.py``'s ``_read_raw_config``, the mimirheim
    core counterpart this mirrors).

    Args:
        path: Filesystem path to the YAML configuration file.

    Returns:
        A ``(raw, error)`` pair. ``error`` is ``None`` on success, in which
        case ``raw`` is the parsed mapping (or ``{}`` for an empty or
        comment-only file). On failure ``raw`` is always ``{}`` and ``error``
        is a human-readable description suitable for the Operational State
        topic's ``detail`` field.
    """
    try:
        text = Path(path).read_text()
    except OSError as exc:
        return {}, f"Cannot read config file {path!r}: {exc}"
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {}, f"Cannot parse config file {path!r}: {exc}"
    return raw or {}, None


def load_helper_config(
    path: str,
    model_cls: type[_ConfigT],
    logger: logging.Logger,
    *,
    owner_id: str,
    display_name: str,
    form_spec: FormSpec,
) -> _ConfigT:
    """Load, env-override and validate a helper's YAML configuration file.

    Every helper daemon starts the same way: read the YAML, let the HA
    Supervisor environment override the ``mqtt`` section, validate against the
    helper's own Pydantic model, and either return the result or enter
    Awaiting Configuration mode. This function is that sequence, shared so
    every helper stays in step.

    Validation happens in two stages, mirroring mimirheim core's own startup
    (``mimirheim/__main__.py``, ADR-0009/ADR-0011):

    1. Broker Settings (the ``mqtt:`` section, validated against
       ``MqttConfig`` alone). A daemon with no usable broker settings cannot
       connect to MQTT at all, so it cannot serve the Config Service protocol
       either; this failure remains immediately fatal, exactly as before this
       function supported Awaiting Configuration.
    2. The full configuration model. A failure here — including the file not
       existing or not parsing as YAML — is no longer fatal. Instead this
       function connects using the now-known-valid Broker Settings and serves
       only the Config Service protocol (Descriptor, get_current_values,
       validate_and_write, restart_request) until a Restart Request or
       termination signal, then returns control to the supervisor via
       ``sys.exit(0)`` so it can restart the process against a fixed file.

    Args:
        path: Filesystem path to the YAML configuration file.
        model_cls: The helper's Pydantic config model.
        logger: The calling helper's logger, so the failure record carries the
            helper's name rather than this module's.
        owner_id: This helper's stable Config Owner identifier, used to build
            its Descriptor and every Config Service topic if the full
            configuration does not currently validate.
        display_name: Human-readable name shown by the Config Editor.
        form_spec: The FormSpec paired with ``model_cls``.

    Returns:
        A validated instance of ``model_cls``. Only returns on success; every
        failure path exits the process (see Raises).

    Raises:
        SystemExit: With code 1 if the ``mqtt:`` section does not validate on
            its own (from the file or the environment). With code 0 after
            Awaiting Configuration mode ends (Restart Request or termination
            signal) if the rest of the configuration did not validate.
    """
    raw, load_error = _read_raw_config(path)

    try:
        apply_mqtt_env_overrides(raw)
    except ValueError:
        logger.exception("Failed to load configuration from %s", path)
        sys.exit(1)

    try:
        broker_settings = MqttConfig.model_validate(raw.get("mqtt") or {})
    except ValidationError:
        logger.exception("Invalid Broker Settings (mqtt: section) in %s", path)
        sys.exit(1)

    if load_error is None:
        try:
            return model_cls.model_validate(raw)
        except ValidationError as exc:
            logger.warning(
                "Configuration in %s does not validate; awaiting a corrected "
                "configuration over MQTT: %s",
                path,
                exc,
            )
            detail = str(exc)
    else:
        logger.warning(
            "%s; awaiting a corrected configuration over MQTT.", load_error
        )
        detail = load_error

    # Imported here, not at module level: helper_common.awaiting_configuration
    # imports MqttConfig from this module, so a top-level import would be
    # circular.
    from helper_common.awaiting_configuration import run_awaiting_configuration

    run_awaiting_configuration(
        mqtt_config=broker_settings,
        config_path=Path(path),
        detail=detail,
        owner_id=owner_id,
        display_name=display_name,
        model_cls=model_cls,
        form_spec=form_spec,
        logger=logger,
    )
    sys.exit(0)
