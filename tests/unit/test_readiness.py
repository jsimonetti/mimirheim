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
    InputsConfig,
    MimirheimConfig,
    MqttConfig,
    OutputsConfig,
    SocTopicConfig,
)
from mimirheim.core.bundle import PriceStep, SolveBundle
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


def _make_config_with_price_topics(topics: list[str]) -> MimirheimConfig:
    """Same minimal config as _make_config(), with explicit price topics."""
    config = _make_config()
    config.inputs = InputsConfig(prices=topics)
    return config


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


# ---------------------------------------------------------------------------
# Multi-source price merge
# ---------------------------------------------------------------------------


def test_readiness_multi_topic_price_one_never_published_merged_coverage_sufficient() -> None:
    """Two price topics configured; only one has ever published; solve still proceeds."""
    config = _make_config_with_price_topics(["a/prices", "b/prices"])
    state = ReadinessState(config)
    state.update("a/prices", _make_price_steps(24))
    # "b/prices" never published at all.
    state.update(_BAT_TOPIC, 5.0)
    assert state.is_ready()
    result = state.snapshot()
    assert isinstance(result, SolveBundle)


def test_readiness_multi_topic_price_both_published_merged_coverage_still_short() -> None:
    """Two price topics configured and published, but unioned coverage is still short."""
    config = _make_config_with_price_topics(["a/prices", "b/prices"])
    state = ReadinessState(config)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    # Each source alone covers only 15 minutes of future data; even unioned,
    # coverage tops out at 30 minutes — below the 1-hour (4-step) minimum.
    state.update(
        "a/prices",
        [PriceStep(ts=now, import_eur_per_kwh=0.20, export_eur_per_kwh=0.05)],
    )
    state.update(
        "b/prices",
        [PriceStep(ts=now + timedelta(minutes=15), import_eur_per_kwh=0.22, export_eur_per_kwh=0.05)],
    )
    state.update(_BAT_TOPIC, 5.0)
    assert not state.is_ready()
    assert "horizon" in state.not_ready_reason().lower()


def test_readiness_battery_grid_only_driven_by_price_coverage_alone() -> None:
    """A config with no PV arrays and no static loads is not zeroed by an empty PV/load intersection."""
    config = _make_config_with_price_topics(["mimir/input/prices"])
    assert not config.pv_arrays
    assert not config.static_loads
    state = ReadinessState(config)
    state.update("mimir/input/prices", _make_price_steps())
    state.update(_BAT_TOPIC, 5.0)
    assert state.is_ready()


def test_readiness_snapshot_merges_overlapping_price_sources_by_confidence() -> None:
    """snapshot() merges two overlapping price sources into the expected per-step winner."""
    config = _make_config_with_price_topics(["a/prices", "b/prices"])
    state = ReadinessState(config)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    source_a = [
        PriceStep(ts=now + timedelta(hours=i), import_eur_per_kwh=0.20, export_eur_per_kwh=0.05, confidence=1.0)
        for i in range(25)
    ]
    source_b = [
        PriceStep(ts=now + timedelta(hours=i), import_eur_per_kwh=0.99, export_eur_per_kwh=0.05, confidence=0.4)
        for i in range(25)
    ]
    state.update("a/prices", source_a)
    state.update("b/prices", source_b)
    state.update(_BAT_TOPIC, 5.0)
    assert state.is_ready()
    result = state.snapshot()
    # Source A has confidence 1.0 at every overlapping step, so it wins throughout.
    assert all(p == pytest.approx(0.20) for p in result.horizon_prices)
    assert all(c == pytest.approx(1.0) for c in result.horizon_confidence)


# ---------------------------------------------------------------------------
# Battery full-charge policy: observation and restart persistence
# ---------------------------------------------------------------------------


def _ratchet_config(**ratchet: object) -> MimirheimConfig:
    """One battery with the full-charge policy enabled and explicit topics."""
    from mimirheim.config.schema import BatteryOutputsConfig, SocRatchetConfig

    # hold_hours 0 keeps the pre-hold tests a single-reading reset; the hold
    # wiring tests pass their own value.
    settings: dict = {"enabled": True, "full_threshold_pct": 97.0, "hold_hours": 0.0}
    settings.update(ratchet)
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
                soc_ratchet=SocRatchetConfig(**settings),
                inputs=BatteryInputsConfig(
                    soc=SocTopicConfig(topic="mimir/input/battery/bat/soc", unit="kwh"),
                ),
                outputs=BatteryOutputsConfig(
                    soc_ratchet="mimir/status/battery/bat/soc_ratchet",
                ),
            )
        },
    )


def test_measured_full_charge_is_recorded_when_the_reading_arrives() -> None:
    """The observation happens on arrival, not at the next solve.

    SOC readings come in every minute or two and solves run every fifteen. A
    peak observed only at solve time is a balance charge the policy would
    demand all over again.
    """
    state = ReadinessState(_ratchet_config())
    before = datetime.now(UTC)

    state.update("mimir/input/battery/bat/soc", 9.8)

    assert state._last_full_utc["bat"] >= before


def test_a_reading_below_the_threshold_records_nothing() -> None:
    state = ReadinessState(_ratchet_config())
    state.update("mimir/input/battery/bat/soc", 9.6)
    assert "bat" not in state._last_full_utc


def test_retained_status_seeds_the_timestamp_across_a_restart() -> None:
    """The retained payload mimirheim published is what survives the restart."""
    state = ReadinessState(_ratchet_config())
    last_full = datetime(2026, 5, 30, 8, 0, tzinfo=UTC)

    state.update("mimir/status/battery/bat/soc_ratchet", (last_full, None))

    assert state._last_full_utc["bat"] == last_full


def test_a_replayed_retained_message_cannot_overwrite_a_fresh_observation() -> None:
    """Retained messages arrive on every reconnect, not only at startup.

    Letting one overwrite a full charge observed since startup would move the
    timestamp backwards and re-arm a policy that had just been satisfied.
    """
    state = ReadinessState(_ratchet_config())
    state.update("mimir/input/battery/bat/soc", 9.9)
    observed = state._last_full_utc["bat"]

    state.update(
        "mimir/status/battery/bat/soc_ratchet",
        (datetime(2026, 1, 1, tzinfo=UTC), None),
    )

    assert state._last_full_utc["bat"] == observed


def test_the_status_topic_does_not_gate_readiness() -> None:
    """A fresh installation has no retained status; solving must not wait for one."""
    state = ReadinessState(_ratchet_config())
    assert "mimir/status/battery/bat/soc_ratchet" not in state._sensor_topics


def test_a_disabled_policy_observes_nothing() -> None:
    state = ReadinessState(_ratchet_config(enabled=False))
    state.update("mimir/input/battery/bat/soc", 10.0)
    assert state._last_full_utc == {}


def test_a_persisted_baseline_survives_an_soc_first_startup() -> None:
    """Retained status and live SOC race on every connect, and SOC usually wins.

    MQTT handlers are registered SOC-first and the broker replays retained
    messages in its own order. A plain first-writer-wins would therefore
    discard the persisted baseline on a normal restart and reset the interval
    the battery has already been waiting through — which for a battery never
    yet seen full is the only thing keeping the policy alive.
    """
    state = ReadinessState(_ratchet_config())
    original = datetime(2026, 5, 1, tzinfo=UTC)

    state.update("mimir/input/battery/bat/soc", 5.0)  # sets a "now" baseline
    state.update("mimir/status/battery/bat/soc_ratchet", (None, original))

    assert state._care_since_utc["bat"] == original


def test_the_baseline_never_moves_forward() -> None:
    """A later retained baseline cannot postpone an interval already running."""
    state = ReadinessState(_ratchet_config())
    original = datetime(2026, 5, 1, tzinfo=UTC)

    state.update("mimir/status/battery/bat/soc_ratchet", (None, original))
    state.update(
        "mimir/status/battery/bat/soc_ratchet",
        (None, datetime(2026, 5, 20, tzinfo=UTC)),
    )

    assert state._care_since_utc["bat"] == original


def test_a_later_retained_full_charge_wins() -> None:
    """Two instances or a re-publish can carry a fresher timestamp than ours."""
    state = ReadinessState(_ratchet_config())
    state.update(
        "mimir/status/battery/bat/soc_ratchet",
        (datetime(2026, 5, 1, tzinfo=UTC), None),
    )
    later = datetime(2026, 5, 30, tzinfo=UTC)

    state.update("mimir/status/battery/bat/soc_ratchet", (later, None))

    assert state._last_full_utc["bat"] == later


def test_observations_are_exposed_for_the_publisher() -> None:
    state = ReadinessState(_ratchet_config())
    state.update("mimir/input/battery/bat/soc", 9.9)
    assert "bat" in state.battery_care_observations()


def test_only_live_observations_are_offered_as_publisher_overrides() -> None:
    """A timestamp restored from the broker is history, not an event.

    The publisher clears the derived status fields when an override is newer,
    on the grounds that the battery has just been balanced. A retained value
    arriving late must not trigger that, or a months-overdue battery is
    published as freshly full.
    """
    state = ReadinessState(_ratchet_config())

    state.update(
        "mimir/status/battery/bat/soc_ratchet",
        (datetime(2026, 5, 1, tzinfo=UTC), None),
    )

    assert state._last_full_utc["bat"] == datetime(2026, 5, 1, tzinfo=UTC)
    assert state.battery_care_observations() == {}

    state.update("mimir/input/battery/bat/soc", 9.9)
    assert "bat" in state.battery_care_observations()


def test_baselines_are_exposed_for_the_publisher() -> None:
    state = ReadinessState(_ratchet_config())
    original = datetime(2026, 5, 1, tzinfo=UTC)
    state.update("mimir/status/battery/bat/soc_ratchet", (None, original))
    assert state.battery_care_baselines() == {"bat": original}


def test_a_retained_timestamp_from_the_future_is_ignored() -> None:
    """A future timestamp is a disagreeing clock, not a charge yet to come.

    It has to be dropped at ingest rather than merged, because "later wins"
    would make it permanent: no real measurement could ever beat it, so the
    policy would sit dormant until the fictional time passed, and the retained
    topic would hand the same value back after every restart.
    """
    state = ReadinessState(_ratchet_config())
    future = datetime.now(UTC) + timedelta(days=1)

    state.update("mimir/status/battery/bat/soc_ratchet", (future, None))

    assert state.battery_care_history().get("bat") is None, (
        "a future retained timestamp was accepted and can never be superseded"
    )


def test_a_full_charge_is_recorded_only_after_the_hold_has_been_measured(monkeypatch) -> None:
    """Time at the top has to be observed, not planned, and not a single touch.

    The wiring test for the hold: the first reading above the threshold starts
    the clock and records nothing; a reading two hours later completes it.
    The pure arithmetic is covered in test_battery_care; this checks that the
    readiness tracker threads the run between readings.
    """
    from mimirheim.core import readiness as readiness_module

    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    current = {"now": t0}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return current["now"]

    monkeypatch.setattr(readiness_module, "datetime", _Clock)

    state = ReadinessState(_ratchet_config(hold_hours=2.0))

    state.update("mimir/input/battery/bat/soc", 9.8)
    assert "bat" not in state._last_full_utc, "a touch must not count as a full charge"

    current["now"] = t0 + timedelta(hours=1)
    state.update("mimir/input/battery/bat/soc", 9.9)
    assert "bat" not in state._last_full_utc

    current["now"] = t0 + timedelta(hours=2)
    state.update("mimir/input/battery/bat/soc", 9.8)
    assert state._last_full_utc["bat"] == t0 + timedelta(hours=2)
    assert state.battery_care_observations()["bat"] == t0 + timedelta(hours=2)


def test_a_dip_during_the_hold_starts_it_over(monkeypatch) -> None:
    from mimirheim.core import readiness as readiness_module

    t0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    current = {"now": t0}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return current["now"]

    monkeypatch.setattr(readiness_module, "datetime", _Clock)

    state = ReadinessState(_ratchet_config(hold_hours=2.0))
    state.update("mimir/input/battery/bat/soc", 9.8)
    current["now"] = t0 + timedelta(hours=1)
    state.update("mimir/input/battery/bat/soc", 9.6)  # dipped
    current["now"] = t0 + timedelta(hours=2, minutes=30)
    state.update("mimir/input/battery/bat/soc", 9.8)  # only 1h30 since the dip
    assert "bat" not in state._last_full_utc
