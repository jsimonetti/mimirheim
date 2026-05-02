"""Configuration schema for the homeassistant baseload tool.

This module defines all Pydantic models that validate the tool's config.yaml.
It has no imports from other baseload_ha modules.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helper_common.config import HomeAssistantConfig, MqttConfig
import helper_common.topics as _topics


class EntityConfig(BaseModel):
    """A single power sensor entity and the unit it reports in.

    Args:
        entity_id: Home Assistant entity ID, e.g. ``sensor.kitchen_power``.
        unit: Unit in which this entity reports power. Must be ``"W"`` or ``"kW"``.
            Each entity may use a different unit; the tool converts all values
            to kilowatts before computing the forecast.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(description="Home Assistant entity ID.", json_schema_extra={"ui_label": "Entity ID", "ui_group": "basic"})
    unit: Literal["W", "kW"] = Field(description="Power unit reported by this entity.", json_schema_extra={"ui_label": "Unit", "ui_group": "basic"})


class HaConfig(BaseModel):
    """Home Assistant connection and entity configuration.

    Args:
        url: Base URL of the HA instance including scheme and port.
        token: Long-Lived Access Token generated in HA.
        sum_entities: Entities whose hourly mean power is summed. Must contain
            at least one entry. Each entry specifies the entity ID and the unit
            that entity reports in (``"W"`` or ``"kW"``). Entities may use
            different units.
        subtract_entities: Entities subtracted from the sum. Typically used to
            remove steered loads (battery, PV, deferred appliances) that mimirheim
            already controls, so they are not double-counted. May be empty.
            Each entry specifies the entity ID and its unit independently.
        lookback_days: Number of previous days of history to average. Must be
            between 1 and 30 inclusive.
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

    url: str = Field(description="Base URL of the HA instance including scheme and port.", json_schema_extra={"ui_label": "HA URL", "ui_group": "basic"})
    token: str = Field(description="Long-Lived Access Token generated in HA.", json_schema_extra={"ui_label": "HA token", "ui_group": "basic"})
    sum_entities: list[EntityConfig] = Field(min_length=1, json_schema_extra={"ui_label": "Sum entities", "ui_group": "basic"})
    subtract_entities: list[EntityConfig] = Field(default_factory=list, json_schema_extra={"ui_label": "Subtract entities", "ui_group": "advanced"})
    lookback_days: int = Field(default=7, ge=1, le=112, json_schema_extra={"ui_label": "Lookback days", "ui_group": "advanced"})
    lookback_decay: float = Field(default=1.0, ge=1.0, json_schema_extra={"ui_label": "Lookback decay", "ui_group": "advanced"})
    horizon_hours: int = Field(default=48, ge=1, le=168, json_schema_extra={"ui_label": "Horizon (hours)", "ui_group": "advanced"})


class BaseloadConfig(BaseModel):
    """Root configuration for the homeassistant baseload daemon.

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
        homeassistant: HA connection and entity configuration.
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
    homeassistant: HaConfig = Field(description="HA connection and entity configuration.", json_schema_extra={"ui_label": "Home Assistant", "ui_group": "basic"})
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
