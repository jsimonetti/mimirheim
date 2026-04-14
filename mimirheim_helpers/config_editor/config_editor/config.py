"""Configuration schema for the mimirheim config editor web service.

This module defines the single Pydantic model that validates
config-editor.yaml. It has no imports from other config_editor modules.

What this module does not do:
- It does not import from mimirheim core or any helper package.
- It does not perform any I/O or HTTP operations.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError


class ConfigEditorConfig(BaseModel):
    """Configuration for the mimirheim config editor web service.

    The editor is activated by the existence of config-editor.yaml.
    An empty file enables the editor on the default port with all other
    settings at their defaults.

    Attributes:
        port: TCP port the editor listens on. Default 8099.
        config_dir: Path to the directory containing mimirheim YAML config
            files. Default /config. This must be the same directory that is
            bind-mounted into the container.
        log_level: Python logging level name. Default INFO.
        disabled: If True, or if the key is present without a value (null),
            load_config() exits with code 0 without starting the server.
            Exists so that users can disable the editor without removing
            the config file.
    """

    model_config = ConfigDict(extra="forbid")

    port: int = Field(
        default=8099,
        ge=1024,
        le=65535,
        description="TCP port the editor listens on.",
        json_schema_extra={"ui_label": "Port", "ui_group": "advanced"},
    )
    config_dir: Path = Field(
        default=Path("/config"),
        description=(
            "Path to the config directory. "
            "Must match the container volume mount point."
        ),
        json_schema_extra={"ui_label": "Config directory", "ui_group": "advanced"},
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level: DEBUG, INFO, WARNING.",
        json_schema_extra={"ui_label": "Log level", "ui_group": "advanced"},
    )
    allowed_ip: str | None = Field(
        default=None,
        description=(
            "If set, the server only accepts HTTP connections from this IP address. "
            "All other connections receive 403 Forbidden. When None (the default), "
            "all IPs are accepted. Set automatically from the CONFIG_EDITOR_ALLOWED_IP "
            "environment variable when running as a HA add-on."
        ),
        json_schema_extra={"ui_label": "Allowed IP", "ui_group": "advanced"},
    )
    disabled: bool | None = Field(
        default=False,
        description=(
            "Set to true, or include as a bare key without a value, to disable the "
            "config editor without removing the config file. load_config() will exit "
            "with code 0 when this is set."
        ),
    )


def load_config(path: str) -> ConfigEditorConfig:
    """Load and validate the YAML configuration file.

    An empty YAML file (or a file containing only comments) is valid and
    produces a ConfigEditorConfig with all defaults applied.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        The validated ConfigEditorConfig instance.

    Raises:
        SystemExit: With exit code 0 if the config marks the editor as disabled.
        SystemExit: With exit code 1 if the file exists but cannot be read, or
            if the configuration fails Pydantic validation.
    """
    try:
        with Path(path).open() as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        # No config file is not an error: the editor starts with all defaults.
        return ConfigEditorConfig()
    except OSError as exc:
        print(f"ERROR: Cannot read config file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        cfg = ConfigEditorConfig.model_validate(raw)
    except PydanticValidationError as exc:
        print(f"ERROR: Invalid configuration in {path!r}:\n{exc}", file=sys.stderr)
        sys.exit(1)

    # A bare `disabled` key (YAML null) or `disabled: true` both mean the user
    # wants the editor off. Exit cleanly so the process terminates without noise.
    if cfg.disabled is None or cfg.disabled is True:
        sys.exit(0)

    # Override allowed_ip from the environment variable injected by
    # cont-init.d/00-options-env.sh when running as a HA add-on. The variable
    # contains the container's default gateway IP, which is the address from
    # which the HA ingress proxy forwards requests.
    cfg.allowed_ip = os.environ.get("CONFIG_EDITOR_ALLOWED_IP") or None
    return cfg
