"""Configuration schema for the mimirheim PV forecast.solar fetcher tool.

This module defines the Pydantic models that represent the pv_fetcher YAML
configuration file. It is the single source of truth for field names, types,
constraints, and defaults.

What this module does not do:
- It does not import from mimirheim or any other tool.
- It does not perform any HTTP or MQTT operations.
- It does not call the forecast_solar library.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from helper_common.config import HomeAssistantConfig, MqttConfig, apply_mqtt_env_overrides
import helper_common.topics as _topics


class ForecastSolarApiConfig(BaseModel):
    """forecast.solar API parameters.

    Attributes:
        api_key: Optional API key for paid forecast.solar tiers. When None
            (the default), the anonymous free-tier endpoint is used, which
            allows 60 requests per hour and returns a one-day horizon.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"x-format": "categories-vertical", "x-categoryOrder": ["Basic", "Advanced"]})

    api_key: str | None = Field(
        default=None,
        description="forecast.solar API key. Null = free anonymous tier.",
        title="API key", json_schema_extra={"x-category": "Advanced"},
    )


class ArrayConfig(BaseModel):
    """Configuration for one physical PV array.

    The entry key in the ``arrays`` map serves two purposes: it is a log label
    and, when ``output_topic`` is not set, it is treated as the mimirheim
    ``pv_arrays`` device name to derive the default output topic.

    Attributes:
        output_topic: MQTT topic to publish the forecast payload to (retained).
            When not set, derived as ``'{mimir_topic_prefix}/input/pv/{key}/forecast'``
            where ``key`` is the ``arrays`` map key for this entry. Set explicitly
            when the array key in this config does not match the mimirheim
            ``pv_arrays`` device name.
        latitude: Site latitude in decimal degrees (positive = north).
        longitude: Site longitude in decimal degrees (positive = east).
        declination: Panel tilt angle in degrees from horizontal. 0 = flat,
            90 = vertical. Must be in [0, 90].
        azimuth: Panel azimuth deviation from south in degrees. 0 = south,
            -90 = east, 90 = west. Must be in [-180, 180].
        peak_power_kwp: Array peak power in kWp. Must be positive.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"x-format": "categories-vertical", "x-categoryOrder": ["Basic", "Advanced"]})

    output_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the forecast payload. Retained. "
            "Defaults to '{mimir_topic_prefix}/input/pv/{array_key}/forecast' when not set."
        ),
        title="Output topic", json_schema_extra={"x-category": "Advanced", "x-enumSource": "#/context/pv_arrays", "x-format": "mimir-topic-placeholder"},
    )
    latitude: float = Field(description="Site latitude in decimal degrees.", title="Latitude")
    longitude: float = Field(description="Site longitude in decimal degrees.", title="Longitude")
    declination: int = Field(
        ge=0, le=90,
        description="Panel tilt in degrees from horizontal. 0 = flat, 90 = vertical.",
        title="Panel tilt",
    )
    azimuth: int = Field(
        ge=-180, le=180,
        description="Panel azimuth: deviation from south in degrees.",
        title="Panel azimuth",
    )
    peak_power_kwp: float = Field(
        gt=0,
        description="Array peak power in kWp.",
        title="Peak power (kWp)",
    )


class ConfidenceDecayConfig(BaseModel):
    """Confidence values assigned to forecast steps by how far ahead they are.

    forecast.solar provides estimates up to several days ahead. Steps further
    in the future are less reliable. These values are applied per step based
    on the distance from the fetch time to the step timestamp.

    Attributes:
        hours_0_to_6: Confidence for steps 0–6 hours ahead. Default 0.90.
        hours_6_to_24: Confidence for steps 6–24 hours ahead. Default 0.75.
        hours_24_to_48: Confidence for steps 24–48 hours ahead. Default 0.55.
        hours_48_plus: Confidence for steps more than 48 hours ahead. Default 0.35.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"x-format": "categories-vertical", "x-categoryOrder": ["Basic", "Advanced"]})

    hours_0_to_6: float = Field(
        default=0.90, ge=0.0, le=1.0,
        description="Confidence for steps 0–6 h ahead.",
        title="Confidence 0–6 h", json_schema_extra={"x-category": "Advanced"},
    )
    hours_6_to_24: float = Field(
        default=0.75, ge=0.0, le=1.0,
        description="Confidence for steps 6–24 h ahead.",
        title="Confidence 6–24 h", json_schema_extra={"x-category": "Advanced"},
    )
    hours_24_to_48: float = Field(
        default=0.55, ge=0.0, le=1.0,
        description="Confidence for steps 24–48 h ahead.",
        title="Confidence 24–48 h", json_schema_extra={"x-category": "Advanced"},
    )
    hours_48_plus: float = Field(
        default=0.35, ge=0.0, le=1.0,
        description="Confidence for steps 48+ h ahead.",
        title="Confidence 48+ h", json_schema_extra={"x-category": "Advanced"},
    )


class PvFetcherConfig(BaseModel):
    """Top-level configuration for the pv_fetcher daemon.

    Attributes:
        mqtt: MQTT broker connection parameters.
        mimir_topic_prefix: The ``mqtt.topic_prefix`` configured in mimirheim. Used
            to derive default ``output_topic`` for each array (when not set)
            and the default ``mimir_trigger_topic``. Defaults to ``"mimir"``.
        trigger_topic: MQTT topic that triggers one fetch-and-publish cycle.
        forecast_solar: forecast.solar API configuration (API key).
        arrays: Named map of PV array configurations. The key is used as
            the mimirheim ``pv_arrays`` device name for topic derivation unless
            the array's ``output_topic`` is explicitly set.
        confidence_decay: Confidence values by forecast horizon. Optional;
            defaults to the values described in ConfidenceDecayConfig.
        signal_mimir: If True, publish an empty message to ``mimir_trigger_topic``
            after all arrays are published. Default False.
        mimir_trigger_topic: mimirheim's trigger topic. Defaults to the mimirheim canonical
            trigger topic derived from ``mimir_topic_prefix``.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"x-format": "categories-vertical", "x-categoryOrder": ["Basic", "Advanced"]})

    mqtt: MqttConfig = Field(description="MQTT broker connection settings.", title="MQTT")
    mimir_topic_prefix: str = Field(
        default="mimir",
        description="mimirheim mqtt.topic_prefix. Used to derive default array output and trigger topics.",
        title="mimirheim topic prefix", json_schema_extra={"x-category": "Advanced"},
    )
    trigger_topic: str = Field(description="MQTT topic that triggers a fetch cycle.", title="Trigger topic", json_schema_extra={"x-category": "Advanced"})
    forecast_solar: ForecastSolarApiConfig = Field(
        default_factory=ForecastSolarApiConfig,
        description="forecast.solar API configuration.",
        title="forecast.solar API",
    )
    arrays: dict[str, ArrayConfig] = Field(
        description="Named map of PV array configurations.",
        title="PV arrays",
    )
    confidence_decay: ConfidenceDecayConfig = Field(
        default_factory=ConfidenceDecayConfig,
        description="Per-band confidence values. Optional; defaults apply.",
        title="Confidence decay", json_schema_extra={"x-category": "Advanced"},
    )
    signal_mimir: bool = Field(
        default=False,
        description="Publish to mimir_trigger_topic after all arrays are published.",
        title="Signal mimirheim", json_schema_extra={"x-category": "Advanced"},
    )
    mimir_trigger_topic: str | None = Field(
        default=None,
        description="mimirheim trigger topic. Defaults to '{mimir_topic_prefix}/input/trigger'.",
        title="mimirheim trigger topic", json_schema_extra={"x-category": "Advanced"},
    )
    ha_discovery: HomeAssistantConfig | None = Field(
        default=None,
        description="HA MQTT discovery configuration.",
        title="HA discovery", json_schema_extra={"x-category": "Advanced"},
    )
    stats_topic: str | None = Field(
        default=None,
        description="MQTT topic where per-cycle run statistics are published.",
        title="Stats topic", json_schema_extra={"x-category": "Advanced"},
    )

    @model_validator(mode="after")
    def _derive_hioo_topics(self) -> "PvFetcherConfig":
        """Fill in mimirheim-side topics that were not explicitly set.

        Derives ``output_topic`` for each array that has not set one explicitly,
        using the array key as the mimirheim ``pv_arrays`` device name. Also derives
        the default ``mimir_trigger_topic``.
        """
        p = self.mimir_topic_prefix
        for key, arr in self.arrays.items():
            if arr.output_topic is None:
                arr.output_topic = _topics.pv_forecast_topic(p, key)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(p)
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "PvFetcherConfig":
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-pv-forecast"
        return self


def load_config(path: str) -> PvFetcherConfig:
    """Load and validate the YAML configuration file.

    Reads the YAML file at ``path``, parses it, and validates it against
    ``PvFetcherConfig``. On failure, prints a human-readable error and exits.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        The validated ``PvFetcherConfig`` instance.

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
        return PvFetcherConfig.model_validate(raw)
    except PydanticValidationError as exc:
        print(f"ERROR: Invalid configuration in {path!r}:\n{exc}", file=sys.stderr)
        sys.exit(1)
