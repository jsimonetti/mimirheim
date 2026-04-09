"""Configuration schema for the nordpool price fetcher.

This module defines all Pydantic models that validate the tool's config.yaml.
It has no imports from the nordpool fetcher or publisher modules.
"""
from __future__ import annotations

from typing import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from helper_common.config import HomeAssistantConfig, MqttConfig
import helper_common.topics as _topics

# Default formulas: pass the raw spot price through unchanged.
# Users override these with their own supplier's pricing formula.
_DEFAULT_IMPORT_FORMULA = "price"
_DEFAULT_EXPORT_FORMULA = "price"


def _compile_formula(formula: str) -> Callable[[datetime, float], float]:
    """Compile a price formula string into a callable.

    The formula string is a Python expression that may reference two
    variables:

    - ``price``: the raw Nordpool spot price in EUR/kWh for that step.
    - ``ts``: the step's start time as a ``datetime`` (UTC, aware).

    The compiled callable takes ``(ts, price)`` and returns a float.

    The formula is evaluated with full Python access — there is no sandbox.
    This is intentional: the config file is operator-controlled and is treated
    as executable code, in the same way a Python script is. Do not load config
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


class NordpoolApiConfig(BaseModel):
    """Nordpool-specific fetch and pricing parameters.

    The import and export prices are computed by evaluating user-supplied
    Python expression strings. Each expression may reference:

    - ``price``: the raw Nordpool spot price in EUR/kWh for that step.
    - ``ts``: the step start time as a UTC-aware ``datetime`` object.

    This allows any supplier pricing scheme to be expressed without code
    changes. For example, the Dutch Tibber all-in import price is::

        ((price + 0.09161) * 1.21) + 0.0248

    By default both formulas pass the raw spot price through unchanged.

    Attributes:
        area: Nordpool area code (e.g. ``"NO2"``, ``"NL"``, ``"SE3"``).
        import_formula: Python expression that computes the all-in import
            price in EUR/kWh from the raw spot price. Applied to every step.
        export_formula: Python expression that computes the net export price
            in EUR/kWh from the raw spot price. Applied to every step.
    """

    model_config = ConfigDict(extra="forbid")

    area: str = Field(description="Nordpool price area code (e.g. 'NO2', 'NL', 'SE3').", json_schema_extra={"ui_label": "Nordpool area", "ui_group": "basic"})
    import_formula: str = Field(
        default=_DEFAULT_IMPORT_FORMULA,
        description=(
            "Python expression for the all-in import price in EUR/kWh. "
            "Available variables: ``price`` (raw spot, EUR/kWh), ``ts`` (datetime, UTC)."
        ),
        json_schema_extra={"ui_label": "Import price formula", "ui_group": "basic"},
    )
    export_formula: str = Field(
        default=_DEFAULT_EXPORT_FORMULA,
        description=(
            "Python expression for the net export price in EUR/kWh. "
            "Available variables: ``price`` (raw spot, EUR/kWh), ``ts`` (datetime, UTC)."
        ),
        json_schema_extra={"ui_label": "Export price formula", "ui_group": "basic"},
    )

    @field_validator("import_formula", "export_formula", mode="after")
    @classmethod
    def _validate_formula(cls, v: str) -> str:
        # Compile the formula to catch syntax errors at load time rather than
        # at the first fetch cycle. The compiled callable is not stored here —
        # fetcher.py compiles from the string at runtime.
        _compile_formula(v)
        return v


class NordpoolConfig(BaseModel):
    """Root configuration for the nordpool price fetcher daemon.

    Args:
        mqtt: MQTT broker connection settings.
        mimir_topic_prefix: The ``mqtt.topic_prefix`` configured in mimirheim. Used
            to derive default values for ``output_topic`` and
            ``mimir_trigger_topic``. Defaults to ``"mimir"`` to match the mimirheim
            default. Override when your mimirheim instance uses a different prefix.
        trigger_topic: The tool subscribes here; a message fires one fetch cycle.
        output_topic: Price payload is published retained to this topic. Defaults
            to the mimirheim canonical price topic derived from ``mimir_topic_prefix``.
        nordpool: Nordpool API and pricing formula parameters.
        signal_mimir: If True, publish to mimir_trigger_topic after publishing prices.
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
    trigger_topic: str = Field(description="MQTT topic that triggers a fetch cycle.", json_schema_extra={"ui_label": "Trigger topic", "ui_group": "advanced"})
    output_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the retained price payload. "
            "Defaults to '{mimir_topic_prefix}/input/prices' when not set."
        ),
        json_schema_extra={"ui_label": "Output topic", "ui_group": "advanced"},
    )
    nordpool: NordpoolApiConfig = Field(description="Nordpool API and pricing formula parameters.", json_schema_extra={"ui_label": "Nordpool API", "ui_group": "basic"})
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
    signal_mimir: bool = Field(default=False, description="Publish to mimir_trigger_topic after publishing prices.", json_schema_extra={"ui_label": "Signal mimirheim", "ui_group": "advanced"})
    mimir_trigger_topic: str | None = Field(default=None, description="Topic to trigger mimirheim. Derived from mimir_topic_prefix when not set.", json_schema_extra={"ui_label": "mimirheim trigger topic", "ui_group": "advanced"})

    @model_validator(mode="after")
    def _derive_hioo_topics(self) -> "NordpoolConfig":
        """Fill in mimirheim-side topics that were not explicitly set."""
        p = self.mimir_topic_prefix
        if self.output_topic is None:
            self.output_topic = _topics.prices_topic(p)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(p)
        return self
