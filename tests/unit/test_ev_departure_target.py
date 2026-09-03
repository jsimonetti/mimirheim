"""Tests for the EV departure target MQTT input and the deliverability clamp.

The README documents ``target_soc_kwh`` and ``window_latest`` as runtime
fields of the EV state MQTT payload, and ``EvDevice.add_constraints``
enforces them as a hard SOC constraint — but no input path exists:
``parse_ev_inputs`` accepts only a bare number and ``readiness.snapshot``
builds ``EvInputs`` from SOC and plug state alone, so the departure
constraint can never activate from MQTT.

These tests pin the completed feature:

1. The EV state payload may be a JSON object carrying the departure fields;
   a bare numeric payload remains valid.
2. ``readiness.snapshot`` threads the fields into ``EvInputs``.
3. A target beyond what the charger can physically deliver by the deadline
   is clamped to the deliverable energy during bundle assembly (the solver
   then charges flat out) instead of making the entire solve infeasible and
   losing the schedule for every device. The clamp lives in readiness, not
   in the device model: build_and_solve is pure and must not log.
"""

from datetime import datetime, timedelta, timezone

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import EvInputs, PowerForecastStep, PriceStep, SolveBundle
from mimirheim.core.model_builder import build_and_solve
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.input_parser import parse_ev_inputs

# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


def test_parse_bare_number_remains_valid() -> None:
    """A plain numeric payload parses as SOC with no departure fields."""
    st = parse_ev_inputs(b"20.5")
    assert st.soc == pytest.approx(20.5)
    assert st.target_soc_kwh is None
    assert st.window_latest is None
    assert st.window_earliest is None


def test_parse_json_payload_with_departure_fields() -> None:
    """A JSON object payload carries SOC, target, and window."""
    st = parse_ev_inputs(
        b'{"soc": 55.0, "target_soc_kwh": 51.8,'
        b' "window_latest": "2026-08-30T05:00:00Z"}'
    )
    assert st.soc == pytest.approx(55.0)
    assert st.target_soc_kwh == pytest.approx(51.8)
    assert st.window_latest == datetime(2026, 8, 30, 5, tzinfo=timezone.utc)
    assert st.window_earliest is None


def test_parse_json_payload_soc_only() -> None:
    """Departure fields are optional in the JSON form."""
    st = parse_ev_inputs(b'{"soc": 40}')
    assert st.soc == pytest.approx(40.0)
    assert st.target_soc_kwh is None
    assert st.window_latest is None


def test_parse_json_without_soc_is_rejected() -> None:
    """soc is mandatory: a payload with only a target is not a valid state."""
    with pytest.raises(ValueError):
        parse_ev_inputs(b'{"target_soc_kwh": 50}')


def test_parse_garbage_is_rejected() -> None:
    with pytest.raises(ValueError):
        parse_ev_inputs(b"not a number")


def test_parse_unknown_field_is_rejected() -> None:
    """A typo like target_soc_kw must not silently drop the constraint."""
    with pytest.raises(ValueError, match="unknown field"):
        parse_ev_inputs(b'{"soc": 40, "target_soc_kw": 50}')


# ---------------------------------------------------------------------------
# Readiness threading
# ---------------------------------------------------------------------------

_SOLVE_START = datetime.now(timezone.utc)


def _ev_config() -> MimirheimConfig:
    return MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 10.0, "export_limit_kw": 10.0},
            "ev_chargers": {
                "car": {
                    "capacity_kwh": 60.0,
                    "charge_segments": [{"power_max_kw": 8.0, "efficiency": 1.0}],
                    "discharge_segments": [],
                    "inputs": {"soc": {"unit": "kwh"}},
                }
            },
            "static_loads": {"base": {}},
        }
    )


def test_readiness_threads_departure_fields_into_bundle() -> None:
    """snapshot() carries target_soc_kwh and window_latest into EvInputs."""
    config = _ev_config()
    state = ReadinessState(config)

    now = datetime.now(timezone.utc)
    steps = [now + timedelta(hours=h) for h in range(9)]
    state.update(
        "mimir/input/prices",
        [
            PriceStep(ts=ts, import_eur_per_kwh=0.25, export_eur_per_kwh=0.10)
            for ts in steps
        ],
    )
    state.update(
        "mimir/input/baseload/base/forecast",
        [PowerForecastStep(ts=ts, kw=0.5) for ts in steps],
    )
    window = now + timedelta(hours=8)
    state.update(
        "mimir/input/ev/car/soc",
        parse_ev_inputs(
            (
                '{"soc": 12.0, "target_soc_kwh": 40.0,'
                f' "window_latest": "{window.isoformat()}"}}'
            ).encode()
        ),
    )
    state.update("mimir/input/ev/car/plugged_in", True)

    assert state.is_ready(), state.not_ready_reason()
    bundle = state.snapshot()
    ev = bundle.ev_inputs["car"]
    assert ev.soc_kwh == pytest.approx(12.0)
    assert ev.available is True
    assert ev.target_soc_kwh == pytest.approx(40.0)
    assert ev.window_latest is not None
    assert abs((ev.window_latest - window).total_seconds()) < 1


# ---------------------------------------------------------------------------
# Deliverability clamp
# ---------------------------------------------------------------------------


def _solve_bundle(ev: EvInputs, horizon: int = 4) -> SolveBundle:
    return SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.10] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[0.0] * horizon,
        base_load_forecast=[0.5] * horizon,
        ev_inputs={"car": ev},
    )


def _snapshot_with_target(target_kwh: float) -> "SolveBundle":
    """Feed readiness a full input set with the given departure target."""
    config = _ev_config()
    state = ReadinessState(config)
    now = datetime.now(timezone.utc)
    steps = [now + timedelta(hours=h) for h in range(3)]
    state.update(
        "mimir/input/prices",
        [
            PriceStep(ts=ts, import_eur_per_kwh=0.25, export_eur_per_kwh=0.10)
            for ts in steps
        ],
    )
    state.update(
        "mimir/input/baseload/base/forecast",
        [PowerForecastStep(ts=ts, kw=0.5) for ts in steps],
    )
    window = now + timedelta(hours=1)
    state.update(
        "mimir/input/ev/car/soc",
        parse_ev_inputs(
            (
                f'{{"soc": 0.0, "target_soc_kwh": {target_kwh},'
                f' "window_latest": "{window.isoformat()}"}}'
            ).encode()
        ),
    )
    state.update("mimir/input/ev/car/plugged_in", True)
    assert state.is_ready(), state.not_ready_reason()
    return state.snapshot()


def test_unreachable_target_is_clamped_during_assembly() -> None:
    """A physically impossible target degrades to flat-out charging.

    8 kW charger, 1-hour window: at most ~10 kWh can be delivered from
    empty. A 40 kWh target cannot be met; without the clamp the hard
    constraint makes the whole solve infeasible and no device gets a
    schedule. readiness clamps the target during bundle assembly (with a
    warning), so build_and_solve stays pure and the solve stays feasible.
    """
    bundle = _snapshot_with_target(40.0)
    ev = bundle.ev_inputs["car"]
    assert ev.target_soc_kwh is not None
    assert ev.target_soc_kwh < 40.0

    result = build_and_solve(bundle, _ev_config())
    assert result.solve_status in ("optimal", "feasible")
    # Flat-out charging: 8 kW in every step up to the window.
    window_steps = 4  # 1-hour window at 15-minute steps
    for step in result.schedule[:window_steps]:
        assert step.devices["car"].kw == pytest.approx(-8.0, abs=1e-6)


def test_reachable_target_is_not_clamped() -> None:
    """A deliverable target passes through assembly unchanged."""
    bundle = _snapshot_with_target(4.0)
    assert bundle.ev_inputs["car"].target_soc_kwh == pytest.approx(4.0)


def test_reachable_target_is_enforced_unchanged() -> None:
    """A deliverable target stays a hard constraint (no relaxation)."""
    ev = EvInputs(
        soc_kwh=0.0,
        available=True,
        target_soc_kwh=4.0,
        window_latest=datetime(2026, 6, 1, 12, 45, tzinfo=timezone.utc),
    )
    result = build_and_solve(_solve_bundle(ev), _ev_config())
    assert result.solve_status in ("optimal", "feasible")
    delivered = -sum(s.devices["car"].kw for s in result.schedule) * 0.25
    assert delivered >= 4.0 - 1e-6


def test_deadline_anchors_to_step_ending_at_deadline() -> None:
    """Energy delivered after the deadline must not count towards the target.

    Deadline exactly +1 h at 15-minute steps: the last usable step is 3
    (ending at +1:00). A target of exactly 4 deliverable steps' energy
    (8 kWh at 8 kW) is reachable; the SOC at the end of step 3 must meet it
    without relying on step 4.
    """
    ev = EvInputs(
        soc_kwh=0.0,
        available=True,
        target_soc_kwh=8.0,
        window_latest=datetime(2026, 6, 1, 13, tzinfo=timezone.utc),
    )
    result = build_and_solve(_solve_bundle(ev, horizon=8), _ev_config())
    assert result.solve_status in ("optimal", "feasible")
    delivered_by_deadline = -sum(
        s.devices["car"].kw for s in result.schedule[:4]
    ) * 0.25
    assert delivered_by_deadline >= 8.0 - 1e-6


def test_window_earliest_blocks_charging_before_start() -> None:
    """No charging may be scheduled in steps starting before window_earliest."""
    ev = EvInputs(
        soc_kwh=0.0,
        available=True,
        target_soc_kwh=4.0,
        window_earliest=datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc),
        window_latest=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
    )
    result = build_and_solve(_solve_bundle(ev, horizon=8), _ev_config())
    assert result.solve_status in ("optimal", "feasible")
    # Steps 0 and 1 start before 12:30 and must not charge.
    for step in result.schedule[:2]:
        assert step.devices["car"].kw == pytest.approx(0.0, abs=1e-6)
    delivered = -sum(s.devices["car"].kw for s in result.schedule) * 0.25
    assert delivered >= 4.0 - 1e-6


def test_unaligned_window_earliest_rounds_up() -> None:
    """An earliest time of 12:20 must block the 12:15-12:30 step too."""
    ev = EvInputs(
        soc_kwh=0.0,
        available=True,
        target_soc_kwh=4.0,
        window_earliest=datetime(2026, 6, 1, 12, 20, tzinfo=timezone.utc),
        window_latest=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
    )
    result = build_and_solve(_solve_bundle(ev, horizon=8), _ev_config())
    assert result.solve_status in ("optimal", "feasible")
    # Steps 0 (12:00) and 1 (12:15) start before 12:20 and must not charge.
    for step in result.schedule[:2]:
        assert step.devices["car"].kw == pytest.approx(0.0, abs=1e-6)


def test_clamp_respects_window_earliest() -> None:
    """The deliverability clamp counts only steps inside both endpoints.

    With a 1-hour window whose earliest time leaves a single chargeable
    15-minute step (8 kW -> 2 kWh), a 40 kWh target must be clamped to
    ~2 kWh or the enforced window constraints make the solve infeasible.
    """
    config = _ev_config()
    state = ReadinessState(config)
    now = datetime.now(timezone.utc)
    steps = [now + timedelta(hours=h) for h in range(3)]
    state.update(
        "mimir/input/prices",
        [
            PriceStep(ts=ts, import_eur_per_kwh=0.25, export_eur_per_kwh=0.10)
            for ts in steps
        ],
    )
    state.update(
        "mimir/input/baseload/base/forecast",
        [PowerForecastStep(ts=ts, kw=0.5) for ts in steps],
    )
    earliest = now + timedelta(minutes=45)
    window = now + timedelta(hours=1)
    state.update(
        "mimir/input/ev/car/soc",
        parse_ev_inputs(
            (
                '{"soc": 0.0, "target_soc_kwh": 40.0,'
                f' "window_earliest": "{earliest.isoformat()}",'
                f' "window_latest": "{window.isoformat()}"}}'
            ).encode()
        ),
    )
    state.update("mimir/input/ev/car/plugged_in", True)
    assert state.is_ready(), state.not_ready_reason()
    bundle = state.snapshot()

    result = build_and_solve(bundle, config)
    assert result.solve_status in ("optimal", "feasible")


def test_deadline_inside_first_interval_clamps_to_current_soc() -> None:
    """A deadline before any step completes cannot be met by any charging.

    The clamp must reduce the target to the current SOC (nothing more is
    deliverable by the deadline) and the solver must not count step 0 —
    which ends after the deadline — towards the target.
    """
    config = _ev_config()
    state = ReadinessState(config)
    now = datetime.now(timezone.utc)
    steps = [now + timedelta(hours=h) for h in range(3)]
    state.update(
        "mimir/input/prices",
        [
            PriceStep(ts=ts, import_eur_per_kwh=0.25, export_eur_per_kwh=0.10)
            for ts in steps
        ],
    )
    state.update(
        "mimir/input/baseload/base/forecast",
        [PowerForecastStep(ts=ts, kw=0.5) for ts in steps],
    )
    # solve_start floors `now` to the 15-minute boundary; a deadline 10
    # minutes past that boundary is inside the first interval regardless of
    # where inside the quarter `now` falls (or already in the past, which
    # must clamp the same way).
    solve_start = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    window = solve_start + timedelta(minutes=10)
    state.update(
        "mimir/input/ev/car/soc",
        parse_ev_inputs(
            (
                '{"soc": 12.0, "target_soc_kwh": 40.0,'
                f' "window_latest": "{window.isoformat()}"}}'
            ).encode()
        ),
    )
    state.update("mimir/input/ev/car/plugged_in", True)
    assert state.is_ready(), state.not_ready_reason()
    bundle = state.snapshot()
    assert bundle.ev_inputs["car"].target_soc_kwh == pytest.approx(12.0)

    result = build_and_solve(bundle, config)
    assert result.solve_status in ("optimal", "feasible")


def test_no_charging_after_the_departure_deadline() -> None:
    """The car is gone after window_latest: no setpoints may follow it."""
    ev = EvInputs(
        soc_kwh=0.0,
        available=True,
        target_soc_kwh=4.0,
        window_latest=datetime(2026, 6, 1, 13, tzinfo=timezone.utc),
    )
    result = build_and_solve(_solve_bundle(ev, horizon=8), _ev_config())
    assert result.solve_status in ("optimal", "feasible")
    # Deadline +1 h maps to step 3; steps 4..7 are after departure.
    for step in result.schedule[4:]:
        assert step.devices["car"].kw == pytest.approx(0.0, abs=1e-6)


def test_expired_deadline_does_not_disable_the_charger() -> None:
    """A stale retained window_latest must not zero the whole horizon.

    window_latest is published retained, so a deadline that has already
    passed is an expired window, not a permanent departure. The vehicle
    must stay dispatchable until a fresh window arrives.

    Forcing the issue structurally: export is capped at zero while PV
    exceeds the base load, so the surplus has nowhere to go unless the EV
    absorbs it. If an expired deadline zeroed the charge variables, the
    solve would be infeasible.
    """
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 10.0, "export_limit_kw": 0.0},
            "ev_chargers": {
                "car": {
                    "capacity_kwh": 60.0,
                    "charge_segments": [{"power_max_kw": 8.0, "efficiency": 1.0}],
                    "discharge_segments": [],
                    "inputs": {"soc": {"unit": "kwh"}},
                }
            },
            "pv_arrays": {"roof": {"max_power_kw": 6.0}},
            "static_loads": {"base": {}},
        }
    )
    ev = EvInputs(
        soc_kwh=0.0,
        available=True,
        window_latest=datetime(2026, 6, 1, 11, tzinfo=timezone.utc),
    )
    bundle = _solve_bundle(ev, horizon=4).model_copy(
        update={
            "pv_forecast": [5.0] * 4,
            "pv_forecasts": {"roof": [5.0] * 4},
        }
    )

    result = build_and_solve(bundle, config)
    assert result.solve_status in ("optimal", "feasible")
    # The 4.5 kW surplus (5.0 PV - 0.5 base load) must land in the EV.
    for step in result.schedule:
        assert step.devices["car"].kw == pytest.approx(-4.5, abs=1e-6)


def test_future_deadline_inside_first_step_blocks_all_dispatch() -> None:
    """Departure before any step completes leaves nothing dispatchable.

    Distinct from an expired retained deadline: the vehicle has not left
    yet at solve time, so every full solver step is post-departure.
    """
    ev = EvInputs(
        soc_kwh=0.0,
        available=True,
        window_latest=datetime(2026, 6, 1, 12, 5, tzinfo=timezone.utc),
    )
    result = build_and_solve(_solve_bundle(ev, horizon=4), _ev_config())
    assert result.solve_status in ("optimal", "feasible")
    for step in result.schedule:
        assert step.devices["car"].kw == pytest.approx(0.0, abs=1e-6)
