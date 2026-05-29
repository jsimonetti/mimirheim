"""Configuration schema for the Zonneplan prices helper.

This module defines all Pydantic models that validate the tool's config.yaml.
It has no imports from the Zonneplan API client, fetcher, or publisher modules.

The root model is ZonneplanPricesConfig, which mirrors the structure of
NordpoolConfig so that HelperDaemon's autodiscovery and stats machinery work
without changes to the base class.
"""
from __future__ import annotations

from typing import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from helper_common.config import HomeAssistantConfig, MqttConfig
import helper_common.topics as _topics


# Default formulas: pass the all-in price through unchanged. Export defaults to
# 0.0 because Zonneplan does not publish a per-hour export price; operators must
# configure their own export formula or accept zero export revenue.
_DEFAULT_IMPORT_FORMULA = "price"
_DEFAULT_EXPORT_FORMULA = "price_excl_tax"


def _compile_formula(formula: str) -> Callable[[datetime, float, float], float]:
    """Compile a price formula string into a callable.

    The formula string is a Python expression that may reference three variables:

    - ``price``: the all-in import price in EUR/kWh for that step (float).
    - ``price_excl_tax``: the import price excluding tax in EUR/kWh (float).
    - ``ts``: the step's start time as a ``datetime`` (UTC, aware).

    The compiled callable takes ``(ts, price, price_excl_tax)`` and returns a
    float.

    The formula is evaluated with full Python access — there is no sandbox.
    This is intentional: the config file is operator-controlled and is treated
    as executable code in the same way a Python script is. Do not load config
    from untrusted sources.

    Args:
        formula: A Python expression string.

    Returns:
        A callable ``(ts: datetime, price: float, price_excl_tax: float) -> float``.

    Raises:
        ValueError: If the formula contains a syntax error or does not compile.
    """
    try:
        fn = eval(f"lambda ts, price, price_excl_tax: {formula}")  # noqa: S307
    except SyntaxError as exc:
        raise ValueError(
            f"Price formula has a syntax error: {exc!s}\n  formula: {formula!r}"
        ) from exc
    if not callable(fn):
        raise ValueError(f"Price formula did not produce a callable: {formula!r}")
    return fn


class ZonneplanApiConfig(BaseModel):
    """Zonneplan-specific fetch and pricing parameters.

    Attributes:
        email: Email address used to trigger the Zonneplan login email. Required
            for the in-daemon auth flow. When absent and the token file is also
            absent, the daemon logs an error and refuses to proceed.
        token_file: Path to the JSON file where OAuth tokens are persisted
            between restarts. Must be on a Docker volume to survive container
            restarts. Defaults to ``zonneplan_token.json`` in the working
            directory.
        import_formula: Python expression for the all-in import price in
            EUR/kWh. Available variables: ``price`` (all-in, incl. tax),
            ``price_excl_tax`` (excl. tax), ``ts`` (step start datetime, UTC).
            Defaults to ``"price"`` which passes the all-in price through
            unchanged.
        export_formula: Python expression for the net export price in EUR/kWh.
            Same variables available as ``import_formula``. Defaults to
            ``"0.0"`` (no export revenue modelled) because Zonneplan does not
            publish a per-hour export price.
    """

    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(
        default=None,
        description=(
            "Email address registered with the Zonneplan account. Required for "
            "first-time authentication. When absent and no token file exists, "
            "the daemon logs an error and skips the cycle."
        ),
        json_schema_extra={"ui_label": "Email address", "ui_group": "basic"},
    )
    token_file: str = Field(
        default="zonneplan_token.json",
        description=(
            "Path to the JSON file where OAuth tokens are persisted between "
            "restarts. Must be on a Docker volume to survive container restarts."
        ),
        json_schema_extra={"ui_label": "Token file path", "ui_group": "advanced"},
    )
    import_formula: str = Field(
        default=_DEFAULT_IMPORT_FORMULA,
        description=(
            "Python expression for the all-in import price in EUR/kWh. "
            "Available variables: ``price`` (all-in incl. tax, EUR/kWh), "
            "``price_excl_tax`` (excl. tax, EUR/kWh), ``ts`` (datetime, UTC)."
        ),
        json_schema_extra={"ui_label": "Import price formula", "ui_group": "basic"},
    )
    export_formula: str = Field(
        default=_DEFAULT_EXPORT_FORMULA,
        description=(
            "Python expression for the net export price in EUR/kWh. "
            "Same variables available as import_formula. Defaults to price_excl_tax."
        ),
        json_schema_extra={"ui_label": "Export price formula", "ui_group": "basic"},
    )

    @field_validator("import_formula", "export_formula", mode="after")
    @classmethod
    def _validate_formula(cls, v: str) -> str:
        _compile_formula(v)
        return v


def get_import_fn(config: ZonneplanApiConfig) -> Callable[[datetime, float, float], float]:
    """Return the compiled import price callable for this config.

    Args:
        config: Validated ZonneplanApiConfig instance.

    Returns:
        A callable ``(ts, price, price_excl_tax) -> float``.
    """
    return _compile_formula(config.import_formula)


def get_export_fn(config: ZonneplanApiConfig) -> Callable[[datetime, float, float], float]:
    """Return the compiled export price callable for this config.

    Args:
        config: Validated ZonneplanApiConfig instance.

    Returns:
        A callable ``(ts, price, price_excl_tax) -> float``.
    """
    return _compile_formula(config.export_formula)


class ZonneplanPricesConfig(BaseModel):
    """Root configuration for the zonneplan_prices daemon.

    Mirrors NordpoolConfig in structure so that HelperDaemon's autodiscovery
    and stats machinery works without any changes to the base class.

    Attributes:
        mqtt: MQTT broker connection parameters.
        mimir_topic_prefix: Topic prefix used to construct default topic paths.
            Defaults to ``"mimir"``.
        trigger_topic: Topic the daemon subscribes to. A message here triggers
            one fetch-and-publish cycle.
        output_topic: Topic to publish the price payload to. When None the
            daemon defaults to ``{mimir_topic_prefix}/input/prices``.
        zonneplan: Zonneplan API and pricing parameters.
        ha_discovery: Optional Home Assistant MQTT discovery settings. When
            present, HelperDaemon publishes a button entity on connect and on
            every HA birth message. No extra code is required in the subclass.
        stats_topic: Optional topic for per-cycle run statistics.
        signal_mimir: When True, publish an empty non-retained trigger message
            to ``mimir_trigger_topic`` after each successful price fetch.
        mimir_trigger_topic: Required when ``signal_mimir`` is True.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(
        description="MQTT broker connection settings.",
        json_schema_extra={"ui_label": "MQTT", "ui_group": "basic"},
    )
    mimir_topic_prefix: str = Field(
        default="mimir",
        description="mimirheim mqtt.topic_prefix. Used to derive default output and trigger topics.",
        json_schema_extra={"ui_label": "mimirheim topic prefix", "ui_group": "advanced"},
    )
    trigger_topic: str = Field(
        description="MQTT topic that triggers a fetch cycle.",
        json_schema_extra={"ui_label": "Trigger topic", "ui_group": "advanced"},
    )
    output_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the retained price payload. "
            "Defaults to '{mimir_topic_prefix}/input/prices' when not set."
        ),
        json_schema_extra={"ui_label": "Output topic", "ui_group": "advanced", "ui_placeholder": "{mimir_topic_prefix}/input/prices"},
    )
    zonneplan: ZonneplanApiConfig = Field(
        description="Zonneplan API and pricing formula parameters.",
        json_schema_extra={"ui_label": "Zonneplan API", "ui_group": "basic"},
    )
    ha_discovery: HomeAssistantConfig | None = Field(
        default=None,
        description="Optional Home Assistant MQTT discovery settings.",
        json_schema_extra={"ui_label": "HA discovery", "ui_group": "advanced"},
    )
    stats_topic: str | None = Field(
        default=None,
        description="MQTT topic where per-cycle run statistics are published.",
        json_schema_extra={"ui_label": "Stats topic", "ui_group": "advanced"},
    )
    signal_mimir: bool = Field(
        default=False,
        description="Publish to mimir_trigger_topic after publishing prices.",
        json_schema_extra={"ui_label": "Signal mimirheim", "ui_group": "advanced"},
    )
    mimir_trigger_topic: str | None = Field(
        default=None,
        description="Topic to trigger mimirheim. Derived from mimir_topic_prefix when not set.",
        json_schema_extra={"ui_label": "mimirheim trigger topic", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _derive_hioo_topics(self) -> "ZonneplanPricesConfig":
        """Fill in mimirheim-side topics that were not explicitly set."""
        p = self.mimir_topic_prefix
        if self.output_topic is None:
            self.output_topic = _topics.prices_topic(p)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(p)
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "ZonneplanPricesConfig":
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-zonneplan-prices"
        return self
