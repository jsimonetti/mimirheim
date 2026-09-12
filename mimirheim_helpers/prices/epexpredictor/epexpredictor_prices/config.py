"""Configuration schema for the epexpredictor_prices fetcher.

This module defines all Pydantic models that validate the tool's config.yaml.
It has no imports from the epexpredictor_prices fetcher or publisher modules.
"""
from __future__ import annotations

from typing import Callable, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from helper_common.config import HomeAssistantConfig, MqttConfig
import helper_common.topics as _topics

# Default formulas: pass the raw predicted price through unchanged.
_DEFAULT_IMPORT_FORMULA = "price"
_DEFAULT_EXPORT_FORMULA = "price"

# EPEX day-ahead has been quoted on a 15-minute market time unit for most
# areas since October 2025, matching the Nordpool helper's default and
# reasoning.
_DEFAULT_PRICE_INTERVAL = "quarter_hourly"

# Default request horizon. The API will return about a week of hourly steps
# when asked for everything it has (hours=-1), but only the first several
# hours of that are real day-ahead data; the rest is an increasingly
# speculative model prediction. 72 hours mirrors the open-meteo PV helper's
# forecast_days=3 default: enough to meaningfully extend past a same-day
# Nordpool/supplier horizon without requesting a full week of low-confidence
# data by default.
_DEFAULT_HORIZON_HOURS = 72

_AREA_CODES = Literal[
    "DE", "AT", "BE", "NL", "SE1", "SE2", "SE3", "SE4", "DK1", "DK2", "ES", "PT",
]


def _compile_formula(formula: str) -> Callable[[datetime, float], float]:
    """Compile a price formula string into a callable.

    Identical contract to the Nordpool helper's ``_compile_formula``: a
    Python expression referencing ``price`` (predicted EUR/kWh) and ``ts``
    (UTC-aware datetime), evaluated with full Python access. The config file
    is operator-controlled and treated as executable code; do not load config
    from untrusted sources.

    Args:
        formula: A Python expression string.

    Returns:
        A callable ``(ts: datetime, price: float) -> float``.

    Raises:
        ValueError: If the formula contains a syntax error or does not compile.
    """
    try:
        fn = eval(f"lambda ts, price: {formula}")  # noqa: S307
    except SyntaxError as exc:
        raise ValueError(
            f"Price formula has a syntax error: {exc!s}\n  formula: {formula!r}"
        ) from exc
    if not callable(fn):
        raise ValueError(f"Price formula did not produce a callable: {formula!r}")
    return fn


class EpexPredictorApiConfig(BaseModel):
    """EpexPredictor-specific fetch and pricing parameters.

    Attributes:
        area: EPEX bidding zone code, one of the zones EpexPredictor
            supports (DE, AT, BE, NL, SE1-4, DK1-2, ES, PT). Rejected at
            config load time if outside this set, rather than surfacing as a
            422 from the API at the first fetch.
        base_url: API base URL. Defaults to the public
            ``https://epexpredictor.batzill.com`` instance. Override to point
            at a self-hosted instance; the project is open source and its
            own API description explicitly invites self-hosting, mirroring
            the same knob on the open-meteo PV helper's ``open_meteo.base_url``.
        horizon_hours: How many hours ahead to request. Passed straight
            through as the API's ``hours`` parameter. The API may return
            fewer steps near the edge of its own predicted range; the
            configured value is a ceiling, not a guarantee.
        import_formula: Python expression for the all-in import price in
            EUR/kWh. Available variables: ``price`` (predicted EUR/kWh),
            ``ts`` (datetime, UTC). This helper never asks the API to add a
            surcharge or tax itself (see fetcher.py); any markup belongs
            here.
        export_formula: Python expression for the net export price in
            EUR/kWh. Same variables as import_formula.
        price_interval: ``"quarter_hourly"`` (default) or ``"hourly"``.
            Mirrors the Nordpool helper's field of the same name and the
            same rationale: set to ``"hourly"`` only when your supplier
            bills a single dynamic price per whole hour.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "title": "API specific configuration",
            "description": "Configuration for the EpexPredictor API.",
            "x-category": "API",
            "x-titleHidden": True,
        }
    )

    area: _AREA_CODES = Field(
        title="EPEX area",
    )
    base_url: str = Field(
        default="https://epexpredictor.batzill.com",
        title="API base URL",
        description="API base URL. Change for a self-hosted EpexPredictor instance.",
    )
    horizon_hours: int = Field(
        default=_DEFAULT_HORIZON_HOURS,
        ge=1,
        title="Horizon (hours)",
    )
    import_formula: str = Field(default=_DEFAULT_IMPORT_FORMULA, title="Import price formula")
    export_formula: str = Field(default=_DEFAULT_EXPORT_FORMULA, title="Export price formula")
    price_interval: Literal["hourly", "quarter_hourly"] = Field(
        default=_DEFAULT_PRICE_INTERVAL,
        title="Price interval",
    )

    @field_validator("import_formula", "export_formula", mode="after")
    @classmethod
    def _validate_formula(cls, v: str) -> str:
        _compile_formula(v)
        return v


class ConfidenceDecayConfig(BaseModel):
    """Confidence values assigned to forecast steps by how far ahead they are.

    The four band fields have identical shape and identical starting
    defaults to the open-meteo PV helper's ``ConfidenceDecayConfig``, per
    explicit instruction: these numbers describe how much a
    day-or-more-ahead forecast can generally be trusted, not anything
    specific to weather or price. Anchored to fetch time, not to the API's
    own ``knownUntil`` real/predicted boundary (see the plan's Purpose
    section for the reasoning). ``known_until_confidence`` is an addition
    specific to this helper, with no PV equivalent, layered on top of that
    base mechanism.

    Attributes:
        hours_0_to_6: Confidence for steps 0-6 hours ahead. Default 0.90.
        hours_6_to_24: Confidence for steps 6-24 hours ahead. Default 0.75.
        hours_24_to_48: Confidence for steps 24-48 hours ahead. Default 0.55.
        hours_48_plus: Confidence for steps more than 48 hours ahead. Default 0.35.
        known_until_confidence: When set, overrides the decay-band result
            for any step at or before the API's ``knownUntil`` timestamp
            (real EPEX auction data already published) with this fixed
            value. Steps after ``knownUntil`` are never affected and keep
            using the bands above. Null (default): no override, the bands
            apply uniformly to every step including real data, matching the
            fetch-time-anchor design. Set to 1.0 to treat the real portion
            of this feed as confirmed data, the same way the Nordpool
            helper always reports confidence 1.0.
    """

    model_config = ConfigDict(extra="forbid")

    hours_0_to_6: float = Field(default=0.90, ge=0.0, le=1.0, title="Confidence 0-6 h")
    hours_6_to_24: float = Field(default=0.75, ge=0.0, le=1.0, title="Confidence 6-24 h")
    hours_24_to_48: float = Field(default=0.55, ge=0.0, le=1.0, title="Confidence 24-48 h")
    hours_48_plus: float = Field(default=0.35, ge=0.0, le=1.0, title="Confidence 48+ h")
    known_until_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Overrides the confidence of every step at or before the API's "
            "knownUntil timestamp with this fixed value. Null (default) "
            "applies the decay bands uniformly, including to real data."
        ),
        title="Known-until confidence override",
    )


class EpexPredictorPricesConfig(BaseModel):
    """Root configuration for the epexpredictor_prices daemon.

    Mirrors NordpoolConfig in every structural respect so HelperDaemon's
    autodiscovery and stats machinery works unchanged.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "title": "EpexPredictor Prices Helper Configuration",
            "description": "Configuration for the EpexPredictor Prices Helper Daemon.",
        }
    )

    mqtt: MqttConfig
    mimir_topic_prefix: str = Field(
        default="mimir",
        title="Topic prefix",
        description="mimirheim mqtt.topic_prefix. Used to derive default output and trigger topics.",
        json_schema_extra={"x-category": "Topics"},
    )
    trigger_topic: str = Field(
        title="Trigger topic",
        description="MQTT topic that triggers a fetch cycle.",
        json_schema_extra={"x-category": "Topics"},
    )
    output_topic: str | None = Field(
        default=None,
        title="Output topic",
        description=(
            "MQTT topic for the retained price payload. "
            "Defaults to '{mimir_topic_prefix}/input/prices' when not set."
        ),
        json_schema_extra={"x-category": "Topics"},
    )
    epexpredictor: EpexPredictorApiConfig
    confidence_decay: ConfidenceDecayConfig = Field(default_factory=ConfidenceDecayConfig)
    ha_discovery: HomeAssistantConfig | None = Field(
        default=None,
        title="HA discovery",
        description="Optional Home Assistant MQTT discovery settings.",
    )
    stats_topic: str | None = Field(
        default=None,
        title="Stats topic",
        description="MQTT topic where per-cycle run statistics are published.",
    )
    signal_mimir: bool = Field(
        default=False,
        title="Signal mimirheim",
        description="Publish to 'Mimirheim trigger topic' after publishing prices.",
    )
    mimir_trigger_topic: str | None = Field(
        default=None,
        title="Mimirheim trigger topic",
        description="Topic to trigger 'Mimirheim'. Derived from 'Topic prefix' when not set.",
    )


    @model_validator(mode="after")
    def _derive_mimir_topics(self) -> "EpexPredictorPricesConfig":
        p = self.mimir_topic_prefix
        if self.output_topic is None:
            self.output_topic = _topics.prices_topic(p)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(p)
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "EpexPredictorPricesConfig":
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-epexpredictor"
        return self
