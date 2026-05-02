"""Configuration schema for the homeassistant_db baseload tool.

This module defines all Pydantic models that validate the tool's config.yaml.
It has no imports from other baseload_ha_db modules.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helper_common.config import HomeAssistantConfig, MqttConfig
import helper_common.topics as _topics


class EntityConfig(BaseModel):
    """A single sensor entity with optional unit override and outlier detection settings.

    Args:
        entity_id: Home Assistant entity ID, e.g. ``sensor.kitchen_power``.
        unit: Unit override. When set, this value is used instead of the unit
            recorded in ``statistics_meta``. Omit to auto-detect from the database.
            Accepted power units: ``"W"``, ``"kW"``, ``"MW"``, ``"GW"``.
            Accepted energy units: ``"Wh"``, ``"kWh"``, ``"MWh"``.
        outlier_factor: Threshold multiplier for P99-based outlier detection.
            A reading is dropped when its absolute value exceeds
            ``P99_effective * outlier_factor``. Default is 10.0, which gives an
            order-of-magnitude margin for typical residential sensors. Lower
            values (e.g. 5.0) tighten the threshold for sensors with a known
            physical maximum. Must be strictly greater than 0.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(description="Home Assistant entity ID.", json_schema_extra={"ui_label": "Entity ID", "ui_group": "basic"})
    unit: Optional[Literal["W", "kW", "MW", "GW", "Wh", "kWh", "MWh"]] = Field(
        default=None,
        description=(
            "Unit override. When set, this value is used instead of the unit "
            "recorded in statistics_meta. Omit to auto-detect from the database."
        ),
        json_schema_extra={"ui_label": "Unit override", "ui_group": "advanced"},
    )
    outlier_factor: float = Field(
        default=10.0,
        gt=0.0,
        description=(
            "Threshold multiplier for P99-based outlier detection. "
            "Readings exceeding P99_effective * outlier_factor are dropped. "
            "Default 10.0 gives an order-of-magnitude margin for residential sensors."
        ),
        json_schema_extra={"ui_label": "Outlier factor", "ui_group": "advanced"},
    )


class HaConfig(BaseModel):
    """Home Assistant recorder database connection and entity configuration.

    Args:
        db_url: SQLAlchemy database URL pointing to the HA recorder database.
            SQLite example: ``sqlite:////config/home-assistant_v2.db``.
            PostgreSQL example: ``postgresql+psycopg2://user:pass@host/homeassistant``.
            MariaDB example: ``mysql+pymysql://user:pass@host/homeassistant``.
            The appropriate database driver must be installed alongside this
            tool (e.g. ``psycopg2-binary`` for PostgreSQL).
        sum_entities: Entities whose hourly mean power is summed. Must contain
            at least one entry. Each entry specifies the entity ID and an optional
            unit override (``"W"``, ``"kW"``, ``"MW"``, or ``"GW"``).
            When the unit is omitted, it is auto-detected from the
            ``statistics_meta`` table. Entities may use different units.
        subtract_entities: Entities subtracted from the sum. Typically used to
            remove steered loads (battery, PV, deferred appliances) that mimirheim
            already controls, so they are not double-counted. May be empty.
            Each entry specifies the entity ID and its optional unit override.
        lookback_days: Number of previous days of history to average. Must be
            between 1 and 112 inclusive (16 weeks).
        lookback_decay: Recency weight ratio applied over the lookback window.
            The newest day is weighted ``lookback_decay`` times more than the
            oldest day, with intermediate days interpolated exponentially.
            ``1.0`` (default) disables weighting and produces a plain average.
            Must be >= 1.0.
        horizon_hours: Number of hours of forecast to publish, starting from the
            current wall-clock hour. The 24-hour day profile is tiled to fill
            the window if horizon_hours exceeds 24. Must be between 1 and 168
            inclusive (one week).
    """

    model_config = ConfigDict(extra="forbid")

    db_url: str = Field(description="SQLAlchemy database URL pointing to the HA recorder database.", json_schema_extra={"ui_label": "DB URL", "ui_group": "basic"})
    sum_entities: list[EntityConfig] = Field(min_length=1, json_schema_extra={"ui_label": "Sum entities", "ui_group": "basic"})
    subtract_entities: list[EntityConfig] = Field(default_factory=list, json_schema_extra={"ui_label": "Subtract entities", "ui_group": "advanced"})
    lookback_days: int = Field(default=7, ge=1, le=112, json_schema_extra={"ui_label": "Lookback days", "ui_group": "advanced"})
    lookback_decay: float = Field(default=1.0, ge=1.0, json_schema_extra={"ui_label": "Lookback decay", "ui_group": "advanced"})
    horizon_hours: int = Field(default=48, ge=1, le=168, json_schema_extra={"ui_label": "Horizon (hours)", "ui_group": "advanced"})


class BaseloadConfig(BaseModel):
    """Root configuration for the homeassistant_db baseload daemon.

    Args:
        mqtt: MQTT broker connection settings.
        mimir_topic_prefix: The ``mqtt.topic_prefix`` configured in mimirheim. Used
            to derive default values for ``output_topic`` and
            ``mimir_trigger_topic``. Defaults to ``"mimir"``.
        mimir_static_load_name: The ``static_loads`` device name in the mimirheim config
            that this tool publishes to. Used to derive the default
            ``output_topic`` when it is not set explicitly. Defaults to
            ``"base_load"``.
        trigger_topic: The tool subscribes here; a message fires one fetch cycle.
        output_topic: Base load forecast payload is published retained to this topic.
            Defaults to the mimirheim canonical baseload topic derived from
            ``mimir_topic_prefix`` and ``mimir_static_load_name``.
        homeassistant: HA recorder database connection and entity configuration.
        signal_mimir: If True, publish to mimir_trigger_topic after publishing the forecast.
        mimir_trigger_topic: Required when signal_mimir is True. Defaults to the
            mimirheim canonical trigger topic derived from ``mimir_topic_prefix``.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(description="MQTT broker connection settings.", json_schema_extra={"ui_label": "MQTT", "ui_group": "basic"})
    mimir_topic_prefix: str = Field(
        default="mimir",
        description="mimirheim mqtt.topic_prefix. Used to derive default output and trigger topics.",
        json_schema_extra={"ui_label": "mimirheim topic prefix", "ui_group": "advanced"},
    )
    mimir_static_load_name: str = Field(
        default="base_load",
        description="mimirheim static_loads device name. Used to derive the default output_topic.",
        json_schema_extra={"ui_label": "mimirheim static load name", "ui_group": "advanced", "ui_source": "static_loads"},
    )
    trigger_topic: str = Field(description="MQTT topic that triggers a fetch cycle.", json_schema_extra={"ui_label": "Trigger topic", "ui_group": "advanced"})
    output_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the retained baseload forecast payload. "
            "Defaults to '{mimir_topic_prefix}/input/baseload/{mimir_static_load_name}/forecast'."
        ),
        json_schema_extra={"ui_label": "Output topic", "ui_group": "advanced", "ui_placeholder": "{mimir_topic_prefix}/input/baseload/{mimir_static_load_name}/forecast"},
    )
    homeassistant: HaConfig = Field(description="HA recorder database connection and entity configuration.", json_schema_extra={"ui_label": "Home Assistant DB", "ui_group": "basic"})
    signal_mimir: bool = Field(default=False, description="Publish to mimir_trigger_topic after publishing the forecast.", json_schema_extra={"ui_label": "Signal mimirheim", "ui_group": "advanced"})
    mimir_trigger_topic: str | None = Field(default=None, description="mimirheim trigger topic. Derives from mimir_topic_prefix when not set.", json_schema_extra={"ui_label": "mimirheim trigger topic", "ui_group": "advanced"})
    ha_discovery: HomeAssistantConfig | None = Field(default=None, description="Optional Home Assistant MQTT discovery settings.", json_schema_extra={"ui_label": "HA discovery", "ui_group": "advanced"})
    stats_topic: str | None = Field(default=None, description="MQTT topic where per-cycle run statistics are published.", json_schema_extra={"ui_label": "Stats topic", "ui_group": "advanced"})

    @model_validator(mode="after")
    def _derive_hioo_topics(self) -> "BaseloadConfig":
        """Fill in mimirheim-side topics that were not explicitly set."""
        p = self.mimir_topic_prefix
        if self.output_topic is None:
            self.output_topic = _topics.baseload_forecast_topic(p, self.mimir_static_load_name)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(p)
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "BaseloadConfig":
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-baseload"
        return self
