"""Configuration schema for the weather-dependent base load helper.

This module defines every Pydantic model that validates the helper's
``config.yaml``. It has no imports from other ``baseload_weather`` modules and
performs no I/O. Presentation metadata for the Config Service lives in
``baseload_weather.formspec`` (ADR-0002), not here.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import helper_common.topics as _topics
from helper_common.config import HomeAssistantConfig, MqttConfig

DEFAULT_CLIENT_ID = "mimir-baseload-weather"
"""Default MQTT client identifier.

Distinct from the homeassistant_db baseload helper's default so both can run
against the same broker with default settings.
"""


class EnergyEntityConfig(BaseModel):
    """A cumulative energy counter entity contributing to the load.

    Args:
        entity_id: Home Assistant entity ID of an energy counter with
            long-term statistics (``state_class`` total or total_increasing).
        unit: Unit override. When unset, the unit recorded in
            ``statistics_meta`` is used. Only energy units are accepted
            because hourly counter deltas give exact hourly energy.
        outlier_factor: Threshold multiplier for P99-based outlier detection.
            An hourly delta above ``P99 * outlier_factor`` is dropped, and its
            hour is then excluded from training. Must be greater than 0.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(description="Home Assistant entity ID of an energy counter.")
    unit: Optional[Literal["Wh", "kWh", "MWh"]] = Field(
        default=None,
        description="Unit override. When unset, the unit recorded in statistics_meta is used.",
    )
    outlier_factor: float = Field(
        default=10.0,
        gt=0.0,
        description="Threshold multiplier for P99-based outlier detection of hourly deltas.",
    )


class TemperatureEntityConfig(BaseModel):
    """The outdoor temperature entity used as the model's history input.

    Args:
        entity_id: Home Assistant entity ID of an outdoor temperature sensor
            with long-term statistics (``state_class: measurement``).
        unit: Unit override. When unset, the unit recorded in
            ``statistics_meta`` is used. Readings are normalised to degrees
            Celsius before modelling.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(description="Home Assistant entity ID of an outdoor temperature sensor.")
    unit: Optional[Literal["°C", "°F"]] = Field(
        default=None,
        description="Unit override. When unset, the unit recorded in statistics_meta is used.",
    )


class RecorderConfig(BaseModel):
    """Home Assistant recorder database connection and entity selection.

    Args:
        db_url: SQLAlchemy database URL of the Home Assistant recorder
            database. SQLite URLs are opened read-only.
        sum_entities: Energy counters whose hourly energy is summed into the
            load. At least one is required.
        subtract_entities: Energy counters subtracted from the sum, typically
            loads mimirheim already schedules. May be empty.
        temperature_entity: Outdoor temperature sensor providing the history
            the regression is fitted on.
    """

    model_config = ConfigDict(extra="forbid")

    db_url: str = Field(description="SQLAlchemy database URL of the Home Assistant recorder database.")
    sum_entities: list[EnergyEntityConfig] = Field(
        min_length=1, description="Energy counters whose hourly energy is summed into the load."
    )
    subtract_entities: list[EnergyEntityConfig] = Field(
        default_factory=list, description="Energy counters subtracted from the sum."
    )
    temperature_entity: TemperatureEntityConfig = Field(
        description="Outdoor temperature sensor providing the regression history."
    )


class WeatherForecastConfig(BaseModel):
    """Open-Meteo temperature forecast settings.

    Args:
        latitude: Site latitude in decimal degrees.
        longitude: Site longitude in decimal degrees.
        base_url: Open-Meteo API base URL. Override for a self-hosted
            instance or a commercial endpoint.
        timeout_s: HTTP timeout per request in seconds.
    """

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0, description="Site latitude in decimal degrees.")
    longitude: float = Field(ge=-180.0, le=180.0, description="Site longitude in decimal degrees.")
    base_url: str = Field(
        default="https://api.open-meteo.com", description="Open-Meteo API base URL."
    )
    timeout_s: float = Field(
        default=30.0, ge=1.0, le=120.0, description="HTTP timeout per request in seconds."
    )


class ModelConfig(BaseModel):
    """Regression model settings.

    Args:
        timezone: IANA time zone of the site. Hour of day, weekday/weekend
            and ``exclude_dates`` are evaluated in this zone, so household
            routines stay aligned across daylight saving changes.
        lookback_days: Days of history the model is fitted on.
        lookback_decay: Recency weight ratio of the newest to the oldest day
            in the lookback window. ``1.0`` weights all days equally.
        smoothing_hours: Time constant in hours of the exponential moving
            average applied to outdoor temperature. Models the thermal mass
            of the building and its emitters.
        t_base_min_c: Lower end of the balance-temperature search range in
            degrees Celsius.
        t_base_max_c: Upper end of the balance-temperature search range in
            degrees Celsius. Must be greater than ``t_base_min_c``.
        min_samples: Minimum effective number of training hours a fit level
            needs before it is used.
        min_hdd_variance: Minimum weighted variance, in K squared, of the
            heating-degree regressor a fit level needs before a slope is
            estimated.
        exclude_dates: Local dates whose load readings are excluded from
            training, for example days with a known metering fault.
    """

    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(description="IANA time zone of the site, for example 'Europe/Amsterdam'.")
    lookback_days: int = Field(default=56, ge=7, le=400, description="Days of history the model is fitted on.")
    lookback_decay: float = Field(
        default=1.0, ge=1.0, description="Recency weight ratio of the newest to the oldest day."
    )
    smoothing_hours: float = Field(
        default=12.0, ge=1.0, le=72.0, description="Time constant of the outdoor temperature moving average."
    )
    t_base_min_c: float = Field(
        default=10.0, allow_inf_nan=False, description="Lower end of the balance-temperature search range."
    )
    t_base_max_c: float = Field(
        default=20.0, allow_inf_nan=False, description="Upper end of the balance-temperature search range."
    )
    min_samples: int = Field(default=10, ge=3, description="Minimum effective training hours per fit level.")
    min_hdd_variance: float = Field(
        default=1.0, gt=0.0, description="Minimum weighted variance of the heating-degree regressor."
    )
    exclude_dates: list[date] = Field(
        default_factory=list, description="Local dates whose load readings are excluded from training."
    )

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        """Reject time zone names the zoneinfo database does not know."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA time zone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _t_base_range_increasing(self) -> "ModelConfig":
        """Require a non-empty balance-temperature search range."""
        if self.t_base_max_c <= self.t_base_min_c:
            raise ValueError("t_base_max_c must be greater than t_base_min_c")
        return self


class WeatherBaseloadConfig(BaseModel):
    """Root configuration of the weather-dependent base load helper.

    Args:
        mqtt: MQTT broker connection settings. ``client_id`` defaults to
            ``mimir-baseload-weather``.
        mimir_topic_prefix: The ``mqtt.topic_prefix`` configured in
            mimirheim. Used to derive default topics.
        mimir_static_load_name: The ``static_loads`` device name in the
            mimirheim config this helper publishes to.
        trigger_topic: A message on this topic fires one cycle. Defaults to
            the trigger shared by all base load helpers.
        output_topic: Retained forecast topic. Defaults to the mimirheim
            base load forecast topic of ``mimir_static_load_name``.
        signal_mimir: Publish to ``mimir_trigger_topic`` after a forecast.
        mimir_trigger_topic: mimirheim trigger topic. Derived from
            ``mimir_topic_prefix`` when not set.
        stats_topic: Retained per-cycle run statistics published by the
            shared helper daemon.
        model_stats_topic: Retained model fit statistics published by this
            helper after each successful cycle.
        ha_discovery: Optional Home Assistant MQTT discovery settings.
        horizon_hours: Hours of forecast to publish after the current hour.
        homeassistant: Recorder database connection and entities.
        weather_forecast: Open-Meteo temperature forecast settings.
        model: Regression model settings.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(description="MQTT broker connection settings.")
    mimir_topic_prefix: str = Field(
        default="mimir", description="mimirheim mqtt.topic_prefix. Used to derive default topics."
    )
    mimir_static_load_name: str = Field(
        default="base_load", description="mimirheim static_loads device name this helper publishes to."
    )
    trigger_topic: str | None = Field(
        default=None,
        description="MQTT topic that triggers a cycle. Defaults to '{mimir_topic_prefix}/input/tools/baseload/trigger'.",
    )
    output_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the retained forecast payload. "
            "Defaults to '{mimir_topic_prefix}/input/baseload/{mimir_static_load_name}/forecast'."
        ),
    )
    signal_mimir: bool = Field(
        default=False, description="Publish to mimir_trigger_topic after publishing the forecast."
    )
    mimir_trigger_topic: str | None = Field(
        default=None, description="mimirheim trigger topic. Derives from mimir_topic_prefix when not set."
    )
    stats_topic: str | None = Field(
        default=None, description="MQTT topic where per-cycle run statistics are published."
    )
    model_stats_topic: str | None = Field(
        default=None, description="MQTT topic where model fit statistics are published."
    )
    ha_discovery: HomeAssistantConfig | None = Field(
        default=None, description="Optional Home Assistant MQTT discovery settings."
    )
    horizon_hours: int = Field(
        default=96, ge=1, le=168, description="Hours of forecast to publish after the current hour."
    )
    homeassistant: RecorderConfig = Field(description="Recorder database connection and entities.")
    weather_forecast: WeatherForecastConfig = Field(description="Open-Meteo temperature forecast settings.")
    model: ModelConfig = Field(description="Regression model settings.")

    @model_validator(mode="after")
    def _derive_mimir_topics(self) -> "WeatherBaseloadConfig":
        """Fill in mimirheim-side topics that were not explicitly set."""
        p = self.mimir_topic_prefix
        if self.trigger_topic is None:
            self.trigger_topic = _topics.helper_trigger_topic(p, "baseload")
        if self.output_topic is None:
            self.output_topic = _topics.baseload_forecast_topic(p, self.mimir_static_load_name)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(p)
        return self

    @model_validator(mode="after")
    def _require_trigger_topic_when_signalling(self) -> "WeatherBaseloadConfig":
        """Reject an empty mimirheim trigger topic when signalling is enabled."""
        if self.signal_mimir and not self.mimir_trigger_topic:
            raise ValueError("mimir_trigger_topic must not be empty when signal_mimir is true")
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "WeatherBaseloadConfig":
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = DEFAULT_CLIENT_ID
        return self
