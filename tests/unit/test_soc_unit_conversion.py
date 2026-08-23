"""Unit tests for percent-to-kWh state-of-charge conversion.

``SocTopicConfig.unit`` may be ``"percent"`` or ``"kwh"``. Percent is the
default because most residential inverters report SOC that way. The conversion
to kWh must happen exactly once on the path from MQTT payload to
``SolveBundle``, and the conversion site must be the same for every device
class.

These tests exercise the whole path rather than either half of it, because
each half looks correct in isolation: the parser converts, and the readiness
snapshot converts. Only running both together reveals a duplicate.
"""

from datetime import UTC, datetime, timedelta

from unittest.mock import MagicMock

from mimirheim.config.schema import (
    BatteryConfig,
    BatteryInputsConfig,
    EfficiencySegment,
    EvConfig,
    EvInputsConfig,
    GridConfig,
    HybridInverterConfig,
    MimirheimConfig,
    MqttConfig,
    OutputsConfig,
    SocTopicConfig,
)
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.mqtt_client import MqttClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(power_max_kw: float = 5.0) -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=power_max_kw, efficiency=0.95)


def _make_config(unit: str) -> MimirheimConfig:
    """Config with one battery, one EV and one hybrid inverter, all 10 kWh.

    A common capacity across all three devices makes the expected kWh value
    identical, so a single percentage maps to one number in every assertion.
    """
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        outputs=OutputsConfig(
            schedule="mimir/schedule",
            current="mimir/current",
            last_solve="mimir/status",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        batteries={
            "bat": BatteryConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg()],
                discharge_segments=[_seg()],
                inputs=BatteryInputsConfig(
                    soc=SocTopicConfig(topic="home/bat/soc", unit=unit)
                ),
            )
        },
        ev_chargers={
            "ev": EvConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg(7.4)],
                inputs=EvInputsConfig(
                    soc=SocTopicConfig(topic="home/ev/soc", unit=unit),
                    plugged_in_topic="home/ev/plugged",
                ),
            )
        },
        hybrid_inverters={
            "hi": HybridInverterConfig(
                capacity_kwh=10.0,
                max_charge_kw=5.0,
                max_discharge_kw=5.0,
                max_pv_kw=6.0,
                topic_pv_forecast="home/hi/pv_forecast",
                inputs=BatteryInputsConfig(
                    soc=SocTopicConfig(topic="home/hi/soc", unit=unit)
                ),
            )
        },
    )


def _price_payload() -> bytes:
    """24 hours of hourly prices as a raw JSON payload."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    steps = [
        {
            "ts": (now + timedelta(hours=i)).isoformat(),
            "import_eur_per_kwh": 0.20,
            "export_eur_per_kwh": 0.05,
            "confidence": 1.0,
        }
        for i in range(25)
    ]
    import json

    return json.dumps(steps).encode()


def _power_forecast_payload() -> bytes:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    steps = [
        {"ts": (now + timedelta(hours=i)).isoformat(), "kw": 0.0, "confidence": 1.0}
        for i in range(25)
    ]
    import json

    return json.dumps(steps).encode()


def _snapshot_with_soc_payload(unit: str, soc_payload: bytes):
    """Feed one SOC payload through the real parser chain and snapshot.

    Builds the topic handler map exactly as the running daemon does, routes
    the payload through the handler registered for each SOC topic, stores the
    result in ReadinessState, and returns the assembled SolveBundle.
    """
    config = _make_config(unit)
    readiness = ReadinessState(config)
    client = MqttClient(
        config,
        readiness,
        publisher=MagicMock(),
        paho_client=MagicMock(),
        solve_queue=None,
    )
    handlers = client._topic_handlers

    readiness.update("mimir/input/prices", handlers["mimir/input/prices"](_price_payload()))
    readiness.update(
        "home/hi/pv_forecast", handlers["home/hi/pv_forecast"](_power_forecast_payload())
    )
    readiness.update("home/ev/plugged", handlers["home/ev/plugged"](b"true"))
    for topic in ("home/bat/soc", "home/ev/soc", "home/hi/soc"):
        readiness.update(topic, handlers[topic](soc_payload))

    assert readiness.is_ready(), readiness.not_ready_reason()
    return readiness.snapshot()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_percent_soc_converted_once_for_every_device_class() -> None:
    """A 50 % reading on a 10 kWh device must reach the solver as 5.0 kWh.

    Converting twice yields 0.5 kWh, which understates the stored energy by a
    factor of the capacity and makes the solver charge a battery that is
    already half full.
    """
    bundle = _snapshot_with_soc_payload("percent", b"50")

    assert bundle.battery_inputs["bat"].soc_kwh == 5.0
    assert bundle.ev_inputs["ev"].soc_kwh == 5.0
    assert bundle.hybrid_inverter_inputs["hi"].soc_kwh == 5.0


def test_kwh_soc_passed_through_unchanged_for_every_device_class() -> None:
    """With unit='kwh' the published value must reach the solver untouched."""
    bundle = _snapshot_with_soc_payload("kwh", b"5.0")

    assert bundle.battery_inputs["bat"].soc_kwh == 5.0
    assert bundle.ev_inputs["ev"].soc_kwh == 5.0
    assert bundle.hybrid_inverter_inputs["hi"].soc_kwh == 5.0


def test_percent_soc_conversion_is_consistent_across_device_classes() -> None:
    """All three device classes must agree at a non-round percentage.

    A single conversion site per device class is only useful if every class
    uses the same one. This catches a class that converts in a different place
    or with a different formula.
    """
    bundle = _snapshot_with_soc_payload("percent", b"37.5")

    expected = 3.75
    assert bundle.battery_inputs["bat"].soc_kwh == expected
    assert bundle.ev_inputs["ev"].soc_kwh == expected
    assert bundle.hybrid_inverter_inputs["hi"].soc_kwh == expected
