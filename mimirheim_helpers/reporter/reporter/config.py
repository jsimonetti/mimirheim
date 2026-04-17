"""Configuration schema for the mimirheim-reporter daemon.

This module defines the Pydantic models for the reporter YAML configuration.
It is the single source of truth for field names, types, constraints, and defaults.

What this module does not do:
- It does not import from ``mimirheim`` or any other non-utility package.
- It does not perform any MQTT operations.
- It does not render any HTML files.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from helper_common.config import MqttConfig, apply_mqtt_env_overrides
import helper_common.topics as _topics


class ChartPublishingConfig(BaseModel):
    """Configuration for MQTT chart data publishing.

    Controls whether the reporter publishes apex-charts-compatible series
    data and summary statistics after each report render. Both topics are
    independent; either or both may be set.

    Attributes:
        chart_topic: MQTT topic for the time-series chart data payload.
            When None, no chart data is published.
        summary_topic: MQTT topic for the scalar economic summary payload.
            When None, no summary is published.
        max_payload_bytes: Maximum allowed serialised payload size in bytes.
            Payloads that exceed this limit are dropped with a warning rather
            than published, protecting brokers with low message-size settings.
            0 means unlimited.
    """

    model_config = ConfigDict(extra="forbid")

    chart_topic: str | None = Field(default=None, description="MQTT topic for the time-series chart data payload.", json_schema_extra={"ui_label": "Chart topic", "ui_group": "advanced"})
    summary_topic: str | None = Field(default=None, description="MQTT topic for the scalar economic summary payload.", json_schema_extra={"ui_label": "Summary topic", "ui_group": "advanced"})
    max_payload_bytes: int = Field(default=65536, ge=0, description="Maximum serialised payload size in bytes. 0 = unlimited.", json_schema_extra={"ui_label": "Max payload bytes", "ui_group": "advanced"})


class ReporterDiscoveryConfig(BaseModel):
    """HA MQTT discovery settings for the reporter daemon.

    When enabled, discovery payloads are published using the HA MQTT device
    JSON format (homeassistant/device/{device_id}/config). Requires HA 2024.2
    or later.

    Attributes:
        enabled: Enable HA MQTT discovery for reporter sensors.
        discovery_prefix: HA MQTT discovery topic prefix.
        device_id: HA device identifier. Defaults to mqtt.client_id when None.
        device_name: Human-readable HA device display name.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, json_schema_extra={"ui_label": "Enable HA discovery", "ui_group": "advanced"})
    discovery_prefix: str = Field(default="homeassistant", json_schema_extra={"ui_label": "Discovery prefix", "ui_group": "advanced"})
    device_id: str | None = Field(default=None, json_schema_extra={"ui_label": "Device ID", "ui_group": "advanced"})
    device_name: str = Field(default="mimirheim Reporter", json_schema_extra={"ui_label": "Device name", "ui_group": "advanced"})


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

    dump_dir: Path = Field(description="Shared dump directory (read by reporter).", json_schema_extra={"ui_label": "Dump directory", "ui_group": "basic"})
    output_dir: Path = Field(description="Directory to write HTML reports into.", json_schema_extra={"ui_label": "Output directory", "ui_group": "basic"})
    max_reports: int = Field(
        default=100,
        ge=0,
        description="Maximum retained HTML reports. 0 = unlimited.",
        json_schema_extra={"ui_label": "Max reports", "ui_group": "advanced"},
    )
    notify_topic: str | None = Field(
        default=None,
        description="MQTT topic to subscribe to for dump-available notifications.",
        json_schema_extra={"ui_label": "Notify topic", "ui_group": "advanced"},
    )


class ReporterConfig(BaseModel):
    """Root configuration model for the mimirheim-reporter daemon.

    Attributes:
        mqtt: MQTT broker connection parameters.
        reporting: Reporting paths and retention settings.
        chart_publishing: MQTT publishing of apex-charts-compatible chart and
            summary data. All fields default to None (disabled).
        ha_discovery: HA MQTT discovery for chart and summary sensors. None
            means discovery is disabled.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(description="MQTT broker connection parameters.", json_schema_extra={"ui_label": "MQTT", "ui_group": "basic"})
    mimir_topic_prefix: str = Field(
        default="mimir",
        description="mimirheim mqtt.topic_prefix. Used to derive the default notify_topic.",
        json_schema_extra={"ui_label": "mimirheim topic prefix", "ui_group": "advanced"},
    )
    reporting: ReporterReportingSection = Field(
        description="Reporting paths and retention settings.",
        json_schema_extra={"ui_label": "Reporting", "ui_group": "basic"},
    )
    chart_publishing: ChartPublishingConfig = Field(
        default_factory=ChartPublishingConfig,
        description="MQTT publishing of apex-charts-compatible chart and summary data.",
        json_schema_extra={"ui_label": "Chart publishing", "ui_group": "advanced"},
    )
    ha_discovery: ReporterDiscoveryConfig | None = Field(
        default=None,
        description="HA MQTT discovery for chart and summary sensors.",
        json_schema_extra={"ui_label": "HA discovery", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _derive_hioo_topics(self) -> "ReporterConfig":
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
    def _set_client_id_default(self) -> "ReporterConfig":
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-reporter"
        return self


def load_config(path: str) -> ReporterConfig:
    """Load and validate the YAML configuration file.

    Reads the YAML file at ``path``, parses it, and validates it against
    ``ReporterConfig``. On failure, prints a human-readable error and exits.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        The validated ``ReporterConfig`` instance.

    Raises:
        SystemExit: With exit code 1 if the file cannot be read or the
            configuration fails Pydantic validation.
    """
    try:
        with Path(path).open() as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        print(f"ERROR: Cannot read config file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    apply_mqtt_env_overrides(raw)

    try:
        return ReporterConfig.model_validate(raw)
    except PydanticValidationError as exc:
        print(f"ERROR: Invalid configuration in {path!r}:\n{exc}", file=sys.stderr)
        sys.exit(1)
