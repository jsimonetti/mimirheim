"""Configuration schema for the mimirheim Config Editor (v3) service.

Defines the single Pydantic model that validates config-editor-v3.yaml: the
MQTT broker connection used to discover Config Owners via the Config Service
protocol (see `mimirheim_shared/docs/adr/0001`), and the TCP port this
process's own HTTP server listens on.

This module has no imports from mimirheim core, `mimirheim_shared`, or any
specific Config Owner.
"""

from __future__ import annotations

from helper_common.config import MqttConfig
from pydantic import BaseModel, ConfigDict, Field


class ConfigEditorV3Config(BaseModel):
    """Configuration for the mimirheim Config Editor (v3) service.

    Attributes:
        mqtt: Broker connection parameters used to discover Config Owners.
        port: TCP port the editor's HTTP server listens on.
        log_level: Python logging level name.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig
    port: int = Field(default=8100, ge=1024, le=65535, description="TCP port the editor listens on.")
    log_level: str = Field(default="INFO", description="Logging level: DEBUG, INFO, WARNING.")
