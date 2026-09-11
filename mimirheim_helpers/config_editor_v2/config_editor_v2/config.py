"""Bootstrap configuration schema for the config-editor-v2 web service.

This module defines the single Pydantic model that validates
config-editor-v2.yaml, plus `load_config`, which reads and validates that
file. It has no imports from any other config_editor_v2 module and no
imports from config-editor (v1) -- config-editor-v2 is an independent
service with its own bootstrap configuration.

What this module does not do:
- It does not import from mimirheim core or any helper package. The
  registered configuration models those packages define (`registry.py`)
  are a separate concern from this editor's own bootstrap settings.
- It does not perform any HTTP I/O. Starting the server is `__main__.py`'s
  responsibility.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError


class ConfigEditorV2Config(BaseModel):
    """Configuration for the config-editor-v2 web service.

    The service is activated by the existence of config-editor-v2.yaml. An
    empty file enables it on the default port with all other settings at
    their defaults.

    Attributes:
        port: TCP port the service listens on. Defaults to 8099, the same
            default v1's config-editor uses. This is deliberate, not an
            oversight: the port is already a configurable field, exactly
            like v1's own `port` field, so an operator who wants to run
            both editors at once changes one of the two ports themselves.
            Running both enabled with unmodified defaults fails to bind the
            second service to the port.
        config_dir: Path to the directory containing mimirheim YAML config
            files. Default /config. This must be the same directory that is
            bind-mounted into the container.
        log_level: Python logging level name. Default INFO.
        disabled: If True, or if the key is present without a value (null),
            load_config() exits with code 0 without starting the server.
            Exists so that users can disable the service without removing
            the config file.
    """

    model_config = ConfigDict(extra="forbid")

    port: int = Field(
        default=8099,
        ge=1024,
        le=65535,
        description=(
            "TCP port the service listens on. Defaults to 8099, the same "
            "default v1's config-editor uses -- change one of the two ports "
            "if both editors are enabled at once."
        ),
    )
    config_dir: Path = Field(
        default=Path("/config"),
        description=(
            "Path to the config directory. "
            "Must match the container volume mount point."
        ),
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level: DEBUG, INFO, WARNING.",
    )
    disabled: bool | None = Field(
        default=False,
        description=(
            "Set to true, or include as a bare key without a value, to disable "
            "config-editor-v2 without removing the config file. load_config() "
            "will exit with code 0 when this is set."
        ),
    )


def load_config(path: str) -> ConfigEditorV2Config:
    """Loads and validates the YAML configuration file.

    An empty YAML file (or a file containing only comments) is valid and
    produces a ConfigEditorV2Config with all defaults applied.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        The validated ConfigEditorV2Config instance.

    Raises:
        SystemExit: With exit code 0 if the config marks the service as
            disabled.
        SystemExit: With exit code 1 if the file exists but cannot be read,
            or if the configuration fails Pydantic validation.
    """
    try:
        with Path(path).open() as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        # No config file is not an error: the service starts with all
        # defaults, which means it starts enabled. See the module docstring
        # and IMPLEMENTATION_DETAILS.md's "Deployment" section: gating is
        # by file *presence*, but the same defaults apply whether the file
        # exists-and-is-empty or is absent entirely.
        return ConfigEditorV2Config()
    except OSError as exc:
        print(f"ERROR: Cannot read config file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        cfg = ConfigEditorV2Config.model_validate(raw)
    except PydanticValidationError as exc:
        print(f"ERROR: Invalid configuration in {path!r}:\n{exc}", file=sys.stderr)
        sys.exit(1)

    # A bare `disabled` key (YAML null) or `disabled: true` both mean the
    # user wants the service off. Exit cleanly so the process terminates
    # without noise.
    if cfg.disabled is None or cfg.disabled is True:
        sys.exit(0)

    return cfg
