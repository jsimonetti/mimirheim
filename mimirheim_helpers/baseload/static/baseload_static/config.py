"""Configuration schema for the static baseload tool.

This module defines all Pydantic models that validate the tool's config.yaml.
It has no imports from other baseload_static modules.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from helper_common.config import HomeAssistantConfig, MqttConfig
import helper_common.topics as _topics


class StaticBaseloadConfig(BaseModel):
    """Parameters that define the static load profile.

    Args:
        profile_kw: Power values in kilowatts that form the repeating cycle
            used when no per-weekday override applies. The most common use is
            24 values, one per hour-of-day (00:00–23:00 UTC). Any length from
            1 to 168 is accepted; the profile is tiled via wall-clock UTC hour
            modulo. Required unless ``weekly_profiles_kw`` covers all 7
            weekdays (0=Monday … 6=Sunday).
        weekly_profiles_kw: Optional per-weekday profiles in kilowatts, keyed
            by Python weekday number (0=Monday, 1=Tuesday, … 6=Sunday). Each
            entry must follow the same rules as ``profile_kw``: 1–168 values
            indexed by UTC hour-of-day via modulo. When a step's weekday has
            an entry here it takes precedence over ``profile_kw``. If all 7
            days are provided, ``profile_kw`` may be omitted entirely.
        horizon_hours: Number of hourly steps to publish, starting from the
            current wall-clock hour. Defaults to 48. Must be between 1 and 168.
    """

    model_config = ConfigDict(extra="forbid")

    profile_kw: list[float] | None = Field(default=None, min_length=1, max_length=168, json_schema_extra={"ui_label": "Hourly profile (kW)", "ui_group": "basic"})
    weekly_profiles_kw: dict[int, list[float]] | None = Field(default=None, json_schema_extra={"ui_label": "Weekly profiles (kW)", "ui_group": "advanced"})
    horizon_hours: int = Field(default=48, ge=1, le=168, json_schema_extra={"ui_label": "Horizon (hours)", "ui_group": "advanced"})

    @model_validator(mode="after")
    def _validate_profiles(self) -> "StaticBaseloadConfig":
        """Enforce that at least one profile source is fully defined."""
        if self.profile_kw is None and self.weekly_profiles_kw is None:
            raise ValueError(
                "At least one of profile_kw or weekly_profiles_kw must be set."
            )
        if self.weekly_profiles_kw is not None:
            invalid_keys = set(self.weekly_profiles_kw.keys()) - set(range(7))
            if invalid_keys:
                raise ValueError(
                    f"weekly_profiles_kw keys must be 0\u20136 (0=Monday, 6=Sunday). "
                    f"Invalid keys: {sorted(invalid_keys)}"
                )
            for day, profile in self.weekly_profiles_kw.items():
                if not (1 <= len(profile) <= 168):
                    raise ValueError(
                        f"weekly_profiles_kw[{day}] must have 1\u2013168 elements, "
                        f"got {len(profile)}."
                    )
        if self.profile_kw is None:
            missing = set(range(7)) - set(self.weekly_profiles_kw.keys())
            if missing:
                raise ValueError(
                    f"profile_kw is absent; weekly_profiles_kw must cover all 7 "
                    f"weekdays (0=Monday \u2026 6=Sunday). Missing: {sorted(missing)}"
                )
        return self


class BaseloadConfig(BaseModel):
    """Root configuration for the static baseload daemon.

    Args:
        mqtt: MQTT broker connection settings.
        mimir_topic_prefix: The ``mqtt.topic_prefix`` configured in mimirheim. Used
            to derive default values for ``output_topic`` and
            ``mimir_trigger_topic``. Defaults to ``"mimir"``.
        mimir_static_load_name: The ``static_loads`` device name in the mimirheim config
            that this tool publishes to. Used to derive the default
            ``output_topic`` when it is not set explicitly. Defaults to
            ``"base_load"``.
        trigger_topic: The tool subscribes here; a message fires one publish cycle.
        output_topic: Base load forecast payload is published retained to this topic.
            Defaults to the mimirheim canonical baseload topic derived from
            ``mimir_topic_prefix`` and ``mimir_static_load_name``.
        baseload: Static load profile and horizon configuration.
        signal_mimir: If True, publish to mimir_trigger_topic after publishing the
            forecast.
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
        json_schema_extra={"ui_label": "mimirheim static load name", "ui_group": "advanced"},
    )
    trigger_topic: str = Field(description="MQTT topic that triggers a publish cycle.", json_schema_extra={"ui_label": "Trigger topic", "ui_group": "advanced"})
    output_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the retained baseload forecast payload. "
            "Defaults to '{mimir_topic_prefix}/input/baseload/{mimir_static_load_name}/forecast'."
        ),
        json_schema_extra={"ui_label": "Output topic", "ui_group": "advanced", "ui_placeholder": "{mimir_topic_prefix}/input/baseload/{mimir_static_load_name}/forecast"},
    )
    baseload: StaticBaseloadConfig = Field(description="Static load profile and horizon configuration.", json_schema_extra={"ui_label": "Baseload profile", "ui_group": "basic"})
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
