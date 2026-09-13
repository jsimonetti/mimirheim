"""Configuration schema for the mimirheim Config Editor service.

Defines the single Pydantic model that validates config-editor.yaml: the
MQTT broker connection used to discover Config Owners via the Config Service
protocol (see `mimirheim_shared/docs/adr/0001`), the TCP port this process's
own HTTP server listens on, and a small set of deployment-only settings
(`allowed_ip`, `disabled`) that have no bearing on the Config Service
protocol itself.

This module has no imports from mimirheim core, `mimirheim_shared`, or any
specific Config Owner.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from helper_common.config import MqttConfig, apply_mqtt_env_overrides
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError


class ConfigEditorConfig(BaseModel):
    """Configuration for the mimirheim Config Editor service.

    Attributes:
        mqtt: Broker connection parameters used to discover Config Owners.
        port: TCP port the editor's HTTP server listens on.
        log_level: Python logging level name.
        allowed_ip: If set, the server only accepts HTTP connections from this
            IP address; all others receive 403 Forbidden. When None (the
            default), every source IP is accepted. The
            CONFIG_EDITOR_ALLOWED_IP environment variable overrides this value
            when set, which is how the HA add-on supplies the ingress
            gateway address.
        disabled: Set to true, or included as a bare key without a value, to
            disable the editor without removing its config file.
            load_config() exits with code 0 when this is set.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig
    port: int = Field(default=8099, ge=1024, le=65535, description="TCP port the editor listens on.")
    log_level: str = Field(default="INFO", description="Logging level: DEBUG, INFO, WARNING.")
    allowed_ip: str | None = Field(
        default=None,
        description=(
            "If set, the server only accepts HTTP connections from this IP address. "
            "All other connections receive 403 Forbidden. When None (the default), "
            "all IPs are accepted. The CONFIG_EDITOR_ALLOWED_IP environment "
            "variable overrides this value when set, which is how the HA add-on "
            "supplies the ingress gateway address."
        ),
    )
    disabled: bool | None = Field(
        default=False,
        description=(
            "Set to true, or include as a bare key without a value, to disable the "
            "config editor without removing the config file. load_config() exits "
            "with code 0 when this is set."
        ),
    )


def load_config(path: str, logger: logging.Logger) -> ConfigEditorConfig:
    """Loads and validates config-editor.yaml, tolerating a missing file.

    Unlike every other helper (`helper_common.load_helper_config`), the Config
    Editor is allowed to start before its own config file exists: it is the
    tool a first-time user relies on to create every other file. A missing
    file is therefore treated as an empty one, not a fatal error. MQTT remains
    a hard requirement regardless: if neither the file nor the HA Supervisor
    environment supplies a broker, validation fails and the process exits.

    Args:
        path: Filesystem path to the YAML configuration file.
        logger: Logger used to record the full traceback on failure.

    Returns:
        The validated ConfigEditorConfig, with allowed_ip already overridden
        from CONFIG_EDITOR_ALLOWED_IP if that environment variable is set.

    Raises:
        SystemExit: With code 0 if the config marks the editor as disabled.
        SystemExit: With code 1 if the file cannot be read or parsed, or if
            the configuration fails Pydantic validation.
    """
    try:
        raw: dict[str, Any] = {}
        if Path(path).exists():
            raw = yaml.safe_load(Path(path).read_text()) or {}
        apply_mqtt_env_overrides(raw)
        cfg = ConfigEditorConfig.model_validate(raw)
    except (OSError, PydanticValidationError, ValueError):
        logger.exception("Failed to load configuration from %s", path)
        sys.exit(1)

    # A bare `disabled` key (YAML null) or `disabled: true` both mean the user
    # wants the editor off. Exit cleanly so the process terminates without noise.
    if cfg.disabled is None or cfg.disabled is True:
        sys.exit(0)

    # Override allowed_ip from the environment variable injected by
    # cont-init.d/00-options-env.sh when running as a HA add-on. The variable
    # contains the container's default gateway IP, which is the address from
    # which the HA ingress proxy forwards requests.
    #
    # Only when it is actually set. Outside the add-on -- plain Docker, bare
    # metal, where the variable does not exist -- a configured allowed_ip
    # would otherwise be replaced with None and the server would accept every
    # source IP. The file is the operator's only restriction there.
    if env_allowed_ip := os.environ.get("CONFIG_EDITOR_ALLOWED_IP"):
        cfg.allowed_ip = env_allowed_ip
    return cfg
