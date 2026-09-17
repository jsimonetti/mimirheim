"""Configuration schema for the mimirheim-reporter daemon.

This module defines the Pydantic models for the reporter YAML configuration.
It is the single source of truth for field names, types, constraints, and defaults.

What this module does not do:
- It does not import from ``mimirheim`` or any other non-utility package.
- It does not perform any MQTT operations.
- It does not render any HTML files.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helper_common.config import MqttConfig
import helper_common.topics as _topics


class ReporterReportingSection(BaseModel):
    """Reporting-specific paths and retention settings for the reporter daemon.

    This section points the reporter to the dump directory shared with mimirheim
    and to the directory where HTML reports are written.

    Attributes:
        dump_dir: Directory shared with the mimirheim container. The reporter reads
            dump file pairs from here via a bind-mount or named volume. Must
            be readable by the reporter process.
        output_dir: Directory where the reporter writes ``{ts}_report.html``,
            ``plotly.min.js``, ``inventory.js``, and ``index.html``.
        max_reports: Maximum number of ``*_report.html`` files to retain in
            ``output_dir``. Older reports are removed when this limit is
            exceeded. 0 means unlimited.
        notify_topic: MQTT topic to subscribe to for dump-available
            notifications. Defaults to ``'{mimir_topic_prefix}/status/dump_available'``
            when not set.
    """

    model_config = ConfigDict(extra="forbid")

    dump_dir: Path = Field(description="Shared dump directory (read by reporter).")
    output_dir: Path = Field(description="Directory to write HTML reports into.")
    max_reports: int = Field(
        default=100,
        ge=0,
        description="Maximum retained HTML reports. 0 = unlimited."
    )
    notify_topic: str | None = Field(
        default=None,
        description="MQTT topic to subscribe to for dump-available notifications."
    )


class ReporterConfig(BaseModel):
    """Root configuration model for the mimirheim-reporter daemon.

    Attributes:
        mqtt: MQTT broker connection parameters.
        reporting: Reporting paths and retention settings.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(description="MQTT broker connection parameters.")
    mimir_topic_prefix: str = Field(
        default="mimir",
        description="mimirheim mqtt.topic_prefix. Used to derive the default notify_topic."
    )
    reporting: ReporterReportingSection = Field(
        description="Reporting paths and retention settings."
    )

    @model_validator(mode="after")
    def _derive_mimir_topics(self) -> ReporterConfig:
        """Fill in mimirheim-side topics that were not explicitly set.

        Derives ``reporting.notify_topic`` from ``mimir_topic_prefix`` when it
        has not been set explicitly.
        """
        if self.reporting.notify_topic is None:
            self.reporting.notify_topic = _topics.dump_available_topic(
                self.mimir_topic_prefix
            )
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> ReporterConfig:
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-reporter"
        return self
