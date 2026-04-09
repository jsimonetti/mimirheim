"""Unit tests for mimirheim/core/readiness.py.

All tests must fail before the implementation exists (TDD).
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from mimirheim.config.schema import (
    BatteryConfig,
    BatteryInputsConfig,
    EfficiencySegment,
    GridConfig,
    MimirheimConfig,
    MqttConfig,
    OutputsConfig,
    SocTopicConfig,
)
from mimirheim.core.bundle import BatteryInputs, PriceStep, SolveBundle
from mimirheim.core.readiness import ReadinessState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg() -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=5.0, efficiency=0.95)


def _make_config() -> MimirheimConfig:
    """Minimal config with one battery that has MQTT inputs configured."""
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
                    soc=SocTopicConfig(
                        topic="home/bat/soc",
                        unit="kwh",
                    )
                ),
            )
        },
    )


def _make_price_steps(n_hours: int = 24) -> list[PriceStep]:
    """Create hourly price steps from the current hour through n_hours ahead."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return [
        PriceStep(
            ts=now + timedelta(hours=i),
            import_eur_per_kwh=0.20,
            export_eur_per_kwh=0.05,
            confidence=1.0,
        )
        for i in range(n_hours + 1)  # +1 so last step is at now + n_hours
    ]


_PRICES_TOPIC = "mimir/input/prices"
_BAT_TOPIC = "home/bat/soc"


def _feed_all(state: ReadinessState) -> None:
    """Feed all required topics with fresh valid data."""
    state.update(_PRICES_TOPIC, _make_price_steps())
    state.update(_BAT_TOPIC, 5.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_readiness_not_ready_initially() -> None:
    """A freshly constructed ReadinessState has received nothing; is_ready() is False."""
    state = ReadinessState(_make_config())
    assert not state.is_ready()


def test_readiness_ready_when_all_topics_provided() -> None:
    """After updating every expected topic with fresh data, is_ready() returns True."""
    state = ReadinessState(_make_config())
    _feed_all(state)
    assert state.is_ready()


def test_readiness_not_ready_when_one_topic_missing() -> None:
    """Providing only the prices topic (missing battery SOC) keeps is_ready() False."""
    state = ReadinessState(_make_config())
    state.update(_PRICES_TOPIC, _make_price_steps())
    # Battery SOC not yet provided.
    assert not state.is_ready()


def test_readiness_forecast_with_no_future_data_blocks_ready() -> None:
    """Prices that are all in the past result in is_ready() returning False."""
    state = ReadinessState(_make_config())
    # All price steps are 2 hours in the past — no future coverage.
    past_steps = [
        PriceStep(
            ts=datetime.now(UTC) - timedelta(hours=2),
            import_eur_per_kwh=0.20,
            export_eur_per_kwh=0.05,
        )
    ]
    state.update(_PRICES_TOPIC, past_steps)
    state.update(_BAT_TOPIC, 5.0)
    assert not state.is_ready()


def test_not_ready_reason_names_missing_sensor_topic() -> None:
    """not_ready_reason() names the missing sensor topic when it has never been received."""
    state = ReadinessState(_make_config())
    # Only prices provided; battery SOC has never arrived.
    state.update(_PRICES_TOPIC, _make_price_steps())
    reason = state.not_ready_reason()
    assert _BAT_TOPIC in reason


def test_not_ready_reason_reports_short_horizon() -> None:
    """not_ready_reason() reports horizon shortfall when all sensor topics are present."""
    state = ReadinessState(_make_config())
    # Provide sensor topic first so it is not the blocker.
    state.update(_BAT_TOPIC, 5.0)
    # Stale prices: only 1 step in the past, zero future coverage.
    past_steps = [
        PriceStep(
            ts=datetime.now(UTC) - timedelta(hours=2),
            import_eur_per_kwh=0.20,
            export_eur_per_kwh=0.05,
        )
    ]
    state.update(_PRICES_TOPIC, past_steps)
    reason = state.not_ready_reason()
    assert "horizon" in reason.lower()


def test_not_ready_reason_empty_when_ready() -> None:
    """not_ready_reason() returns an empty string when the state is ready."""
    state = ReadinessState(_make_config())
    _feed_all(state)
    assert state.not_ready_reason() == ""


def test_readiness_snapshot_returns_solve_bundle() -> None:
    """When ready, snapshot() returns a valid SolveBundle instance."""
    state = ReadinessState(_make_config())
    _feed_all(state)
    assert state.is_ready()
    result = state.snapshot()
    assert isinstance(result, SolveBundle)


def test_readiness_strategy_defaults_to_minimize_cost() -> None:
    """snapshot().strategy is 'minimize_cost' when no strategy message has been received."""
    state = ReadinessState(_make_config())
    _feed_all(state)
    result = state.snapshot()
    assert result.strategy == "minimize_cost"


def test_readiness_strategy_updated_from_mqtt() -> None:
    """Calling update() on the strategy topic changes snapshot().strategy."""
    state = ReadinessState(_make_config())
    _feed_all(state)
    strategy_topic = "mimir/input/strategy"
    state.update(strategy_topic, "minimize_consumption")
    result = state.snapshot()
    assert result.strategy == "minimize_consumption"


def test_readiness_is_thread_safe() -> None:
    """Concurrent update() calls from multiple threads must not raise exceptions."""
    state = ReadinessState(_make_config())
    errors: list[Exception] = []

    def worker() -> None:
        try:
            state.update(_PRICES_TOPIC, _make_price_steps())
            state.update(_BAT_TOPIC, 5.0)
            state.is_ready()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread exceptions: {errors}"
