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

from helper_common.config import MqttConfig, load_helper_config
from pydantic import BaseModel, ConfigDict, Field

from config_editor.formspec import CONFIG_EDITOR_CONFIG_FORM_SPEC

# Stable regardless of user configuration; the Config Editor needs a fixed
# identity for this tool across restarts and reconfiguration -- including
# for its own configuration, since it is a Config Owner of itself the same
# way every other helper is a Config Owner of its own configuration.
CONFIG_OWNER_ID = "config-editor"
CONFIG_OWNER_DISPLAY_NAME = "Config Editor"


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
    """Loads and validates config-editor.yaml via the shared helper startup path.

    Delegates to `helper_common.config.load_helper_config`, the same
    two-phase load (Broker Settings, then the full model) every other
    helper uses (config-owner-startup-resilience ticket 03): Broker
    Settings failure remains immediately fatal, since MQTT is a hard
    requirement regardless of what the Config Editor is for. Any other
    failure -- including a missing file, since the Config Editor is the
    tool a first-time user relies on to create every other file -- is no
    longer fatal; the process connects to MQTT and shows up in the Config
    Editor's own registry as Awaiting Configuration, exactly like every
    other helper, until a corrected configuration is written and a Restart
    Request arrives.

    Args:
        path: Filesystem path to the YAML configuration file.
        logger: Logger passed through to `load_helper_config`.

    Returns:
        The validated ConfigEditorConfig, with allowed_ip already overridden
        from CONFIG_EDITOR_ALLOWED_IP if that environment variable is set.

    Raises:
        SystemExit: With code 0 if the config marks the editor as disabled.
        SystemExit: With code 1 if the `mqtt:` section does not validate on
            its own. With code 0 after Awaiting Configuration mode ends
            (Restart Request or termination signal) if the rest of the
            configuration did not validate.
    """
    cfg = load_helper_config(
        path,
        ConfigEditorConfig,
        logger,
        owner_id=CONFIG_OWNER_ID,
        display_name=CONFIG_OWNER_DISPLAY_NAME,
        form_spec=CONFIG_EDITOR_CONFIG_FORM_SPEC,
    )

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
