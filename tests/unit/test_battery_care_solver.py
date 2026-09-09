"""Solver-level tests for the battery full-charge policy.

``test_battery_care.py`` covers the arithmetic. These tests build real models
and check what the policy does to a schedule: that the ratchet floor binds,
that the deadline is met, that the energy is bought when it is cheap, and that
a disabled policy leaves the model exactly as it was.

The last of those is the point of the whole exercise. A mechanism that reaches
100% at an arbitrary moment can be written in ten lines outside the solver;
what justifies putting it inside is that the solver pays for it in the cheapest
hours available.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.model_builder import build_and_solve
from mimirheim.core.solver_backend import CBCSolverBackend

_T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_CAPACITY = 10.0


# Pre-hold policy values for the scenarios below that were written against a
# single-step target at the reset threshold. Their step arithmetic depends on
# it: 100% needs one more charging step than 97%, and a two-hour hold changes
# where the probe can pin the run. The hold has its own tests at the end.
_PRE_HOLD: dict = {"target_pct": 97.0, "hold_hours": 0.0}


def _config(*, wear_cost: float = 0.0, **ratchet: object) -> MimirheimConfig:
    """A single battery, no PV, one static load, and a roomy grid connection.

    Unless a test says otherwise the policy plans for the reset threshold and
    holds for a single step. That is the pre-hold behaviour these scenarios
    were written against; their step arithmetic depends on it. The hold and
    the 100% target have their own tests below, which set both explicitly.
    """
    battery: dict = {
        "capacity_kwh": _CAPACITY,
        "min_soc_kwh": 1.0,
        "charge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
        "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
        "wear_cost_eur_per_kwh": wear_cost,
    }
    if ratchet:
        ratchet.setdefault("target_pct", ratchet.get("full_threshold_pct", 97.0))
        ratchet.setdefault("hold_hours", 0.0)
        battery["soc_ratchet"] = ratchet
    return MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {"home": battery},
            "static_loads": {"base": {}},
        }
    )


def _bundle(
    *,
    soc_kwh: float,
    last_full_utc: datetime | None,
    prices: list[float],
    load_kw: float = 0.0,
) -> SolveBundle:
    horizon = len(prices)
    return SolveBundle(
        solve_time_utc=_T0,
        horizon_prices=prices,
        horizon_export_prices=[p - 0.05 for p in prices],
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[0.0] * horizon,
        base_load_forecast=[load_kw] * horizon,
        battery_inputs={"home": {"soc_kwh": soc_kwh, "last_full_utc": last_full_utc}},
    )


def _soc_series(result) -> list[float]:
    return [step.devices["home"].soc_kwh for step in result.schedule]


def _charge_kw(result) -> list[float]:
    """Charge power per step in kW. Positive net_power is production, so flip."""
    return [max(0.0, -step.devices["home"].kw) for step in result.schedule]


# ---------------------------------------------------------------------------
# Disabled: nothing changes
# ---------------------------------------------------------------------------


def test_disabled_policy_leaves_the_schedule_untouched() -> None:
    """The default configuration must reproduce today's behaviour exactly."""
    prices = [0.30] * 8 + [0.05] * 8
    bundle = _bundle(soc_kwh=5.0, last_full_utc=_T0 - timedelta(days=99), prices=prices)

    off = build_and_solve(bundle, _config())
    explicitly_off = build_and_solve(bundle, _config(enabled=False))

    assert off.battery_care == {}
    assert _soc_series(off) == _soc_series(explicitly_off)


# ---------------------------------------------------------------------------
# The ratchet floor
# ---------------------------------------------------------------------------


def test_ratchet_floor_stops_the_solver_discharging_through_it() -> None:
    """The care floor must bind above the configured min_soc_kwh, not alongside it.

    Six missed intervals put the floor at 3 kWh against a configured minimum of
    1 kWh, so the two are distinguishable: the same solve with the policy off
    discharges below 3 kWh, and with it on it does not. A test whose dynamic
    floor equalled min_soc_kwh would pass with the entire ratchet deleted.

    Prices fall across the horizon, so the solver wants to sell down early and
    buy back late. The floor is what removes that option.
    """
    prices = [0.40] * 8 + [0.10] * 8
    bundle = _bundle(
        soc_kwh=6.0,
        last_full_utc=_T0 - timedelta(days=42),
        prices=prices,
        load_kw=1.0,
    )

    on = build_and_solve(bundle, _config(enabled=True, target_interval_days=7.0))
    off = build_and_solve(bundle, _config())

    assert on.battery_care["home"].floor_kwh == pytest.approx(3.0)
    assert min(_soc_series(on)) >= 3.0 - 1e-6
    assert min(_soc_series(off)) < 3.0 - 1e-6


def test_floor_above_the_current_soc_does_not_make_the_model_infeasible() -> None:
    """A floor that has climbed past where the battery sits must not break the solve.

    This is the normal state of a ratchet that just stepped: the battery is low,
    which is why it stepped. An unguarded ``soc[t] >= floor`` is infeasible at
    t=0 because the SOC cannot jump, and losing the entire schedule to protect a
    cell-balancing policy is the wrong trade. The floor clamps to the present
    SOC instead, which is what the hardware does: it stops discharging.
    """
    prices = [0.20] * 16
    bundle = _bundle(
        soc_kwh=2.0,
        last_full_utc=_T0 - timedelta(days=200),  # 28 intervals: capped at 80%
        prices=prices,
        load_kw=0.5,
    )
    result = build_and_solve(bundle, _config(enabled=True))

    assert result.solve_status in ("optimal", "feasible")
    assert result.battery_care["home"].floor_kwh == pytest.approx(8.0)
    # Clamped to the current SOC: no discharge below where it started.
    assert min(_soc_series(result)) >= 2.0 - 1e-6


# ---------------------------------------------------------------------------
# The deadline, and the price awareness that justifies it
# ---------------------------------------------------------------------------


def test_deadline_inside_the_horizon_forces_a_full_charge() -> None:
    prices = [0.20] * 16
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=3),
        prices=prices,
    )
    result = build_and_solve(bundle, _config(enabled=True))

    status = result.battery_care["home"]
    assert status.deadline_step == 11  # 3 h ahead at quarter-hourly steps
    assert status.full_target_kwh == pytest.approx(9.7)
    assert _soc_series(result)[11] >= 9.7 - 1e-5


def test_the_balance_charge_is_bought_in_the_cheap_window() -> None:
    """The whole justification for putting this in the optimiser.

    Two things have to be shown together, and the second is what makes the test
    worth having:

    1. The policy causes energy to be bought that would not otherwise be
       bought. The wear cost is set above the terminal-SOC credit, which values
       stored energy at the average import price divided by the step length, so
       storing energy is a losing trade at any price in this horizon and the
       disabled solve charges nothing. Without this, the terminal-SOC credit would fill the battery in
       the cheap hours all by itself and the test would pass with the deadline
       constraint deleted.
    2. That forced energy lands in the cheapest quarter-hours available. The
       first two hours cost 0.40/kWh, the last two 0.05/kWh, and the deadline
       is at the end, so the solver has a free choice and must make the right
       one.
    """
    prices = [0.40] * 8 + [0.05] * 8
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=4),
        prices=prices,
    )

    off = build_and_solve(bundle, _config(wear_cost=1.0))
    on = build_and_solve(bundle, _config(wear_cost=1.0, enabled=True))

    # 1. Nothing is bought without the policy.
    assert sum(_charge_kw(off)) * 0.25 < 0.01, (
        "the disabled solve charged on its own; the test cannot distinguish "
        "the policy from ordinary terminal-SOC economics"
    )

    # 2. The policy's energy is bought, and bought cheaply.
    charge = _charge_kw(on)
    expensive_kwh = sum(charge[:8]) * 0.25
    cheap_kwh = sum(charge[8:]) * 0.25

    assert _soc_series(on)[-1] >= 9.7 - 1e-5
    assert cheap_kwh > 1.5, f"expected the charge in the cheap half, got {cheap_kwh:.3f}"
    assert expensive_kwh < 0.05, (
        f"charged {expensive_kwh:.3f} kWh at 0.40/kWh with a cheap window available"
    )


def test_an_unreachable_target_is_chased_as_far_as_it_goes() -> None:
    """A target the charger cannot reach must neither be dropped nor forbidden.

    1 kWh in the battery, 9.7 wanted, and one hour of a 5 kW charger can add at
    most 5 kWh. Imposing 9.7 anyway would return infeasible and lose the whole
    schedule over a policy about cell balancing. Dropping the target instead
    would leave the battery that is furthest behind with no instruction at all.
    The probe does neither: it requires the highest SOC it proved reachable, so
    the battery charges flat out and the status says what was actually asked.
    """
    prices = [0.20] * 4
    bundle = _bundle(
        soc_kwh=1.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=1),
        prices=prices,
    )
    result = build_and_solve(bundle, _config(enabled=True))

    status = result.battery_care["home"]
    assert result.solve_status in ("optimal", "feasible")
    assert status.deadline_step == 3
    assert status.full_target_kwh == pytest.approx(9.7)
    # 5 kW for a full hour at unit efficiency: 1.0 -> 6.0 kWh, and no further.
    assert _soc_series(result)[-1] == pytest.approx(6.0)
    assert status.enforced_target_kwh == pytest.approx(6.0, abs=1e-3)
    assert status.enforced_target_kwh < status.full_target_kwh


def test_a_reachable_target_is_enforced_in_full() -> None:
    """The status has to be readable as "the policy is on track"."""
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=3),
        prices=[0.20] * 16,
    )
    result = build_and_solve(bundle, _config(enabled=True))

    status = result.battery_care["home"]
    assert status.enforced_target_kwh == pytest.approx(status.full_target_kwh)


def test_no_deadline_means_nothing_is_enforced() -> None:
    """A battery nowhere near due must not have a target imposed on it."""
    bundle = _bundle(
        soc_kwh=5.0, last_full_utc=_T0 - timedelta(hours=6), prices=[0.20] * 8
    )
    result = build_and_solve(bundle, _config(enabled=True))

    status = result.battery_care["home"]
    assert status.deadline_step is None
    assert status.enforced_target_kwh is None
    assert status.enforced_step is None


def test_status_is_reported_even_when_the_policy_asks_for_nothing() -> None:
    """An enabled policy always publishes, so the floor is never invisible."""
    prices = [0.20] * 8
    bundle = _bundle(
        soc_kwh=5.0, last_full_utc=_T0 - timedelta(hours=6), prices=prices
    )
    result = build_and_solve(bundle, _config(enabled=True))

    status = result.battery_care["home"]
    assert status.floor_kwh == 0.0
    assert status.deadline_step is None
    assert status.hours_since_full == pytest.approx(6.0)
    assert status.last_full_utc == _T0 - timedelta(hours=6)


def test_an_overdue_charge_is_not_deferred_to_the_end_of_the_horizon() -> None:
    """The deadline must bite now, not at whatever the horizon's far end is.

    mimirheim executes only the first step of each schedule and re-solves every
    quarter of an hour. Anchoring an overdue charge at the last step means each
    new solve pushes it one step further out, so it is always planned and never
    performed. Here the cheap hours are at the end, so a receding deadline
    would put the whole charge there; the assertion is that it does not.
    """
    prices = [0.40] * 8 + [0.05] * 8
    bundle = _bundle(
        soc_kwh=9.0,
        last_full_utc=_T0 - timedelta(days=30),
        prices=prices,
    )
    result = build_and_solve(bundle, _config(wear_cost=1.0, enabled=True))

    status = result.battery_care["home"]
    # 0.7 kWh at 5 kW takes one quarter-hour step.
    assert status.deadline_step == 0
    assert _soc_series(result)[0] >= 9.7 - 1e-5


def test_a_target_missed_on_efficiency_is_met_one_step_late() -> None:
    """Charge efficiency can make a deadline unmeetable; the policy still lands.

    9.0 kWh in the battery, 9.7 wanted by the end of the first quarter-hour,
    and 2 kW into a 90% efficient charger stores 0.45 kWh in that step, not the
    0.5 the AC figure suggests. Pinning the target to step 0 would be infeasible
    on the arithmetic alone. The probe finds the first step that genuinely
    reaches it and pins the target there, so the charge lands one step late
    rather than taking the schedule down.
    """
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {
                "home": {
                    "capacity_kwh": _CAPACITY,
                    "min_soc_kwh": 1.0,
                    "charge_segments": [{"power_max_kw": 2.0, "efficiency": 0.9}],
                    "discharge_segments": [{"power_max_kw": 2.0, "efficiency": 0.9}],
                    "soc_ratchet": {"enabled": True, **_PRE_HOLD},
                }
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = _bundle(
        soc_kwh=9.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(minutes=15),
        prices=[0.20] * 4,
    )

    result = build_and_solve(bundle, config)

    status = result.battery_care["home"]
    assert result.solve_status in ("optimal", "feasible")
    assert status.deadline_step == 0
    # 2 kW × 0.25 h × 0.9 = 0.45 kWh a step, so 9.0 -> 9.45 -> 9.9: step 1.
    assert status.enforced_step == 1
    assert status.enforced_target_kwh == pytest.approx(9.7)
    assert _soc_series(result)[1] >= 9.7 - 1e-5


def test_an_overdue_charge_still_completes_under_charge_derating() -> None:
    """Derating slows the top of the range; it must not stall the policy.

    Charge derating falls linearly from max_charge_kw at the threshold to
    reduce_charge_min_kw at capacity, so a battery at 9.0 kWh with a threshold
    of 8.0 kWh, a 5 kW maximum and a 1 kW minimum can still take 3 kW. That is
    0.75 kWh in a quarter hour, so an overdue battery reaches 9.7 kWh in the
    very first step — which is the step that actually gets executed.
    """
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {
                "home": {
                    "capacity_kwh": _CAPACITY,
                    "min_soc_kwh": 1.0,
                    "charge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
                    "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
                    "reduce_charge_above_soc_kwh": 8.0,
                    "reduce_charge_min_kw": 1.0,
                    "soc_ratchet": {"enabled": True, **_PRE_HOLD},
                }
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = _bundle(
        soc_kwh=9.0,
        last_full_utc=_T0 - timedelta(days=30),
        prices=[0.20] * 8,
    )

    result = build_and_solve(bundle, config)

    status = result.battery_care["home"]
    assert result.solve_status in ("optimal", "feasible")
    assert status.deadline_step == 0
    assert status.enforced_step == 0
    assert _soc_series(result)[0] >= 9.7 - 1e-5


def test_a_poor_sos2_curve_delays_the_charge_without_breaking_the_solve() -> None:
    """A curve that is efficient at low power and poor at full power.

    The schema orders SOS2 breakpoints by power, not by efficiency, and the
    solver interpolates the product of the two, so the last kilowatt of AC power
    buys only 0.05 kW of DC. The deadline is one step away and 0.7 kWh short, so
    it cannot be met on time under any charging choice. The solve still has to
    succeed, and the target still has to be reached shortly after.
    """
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {
                "home": {
                    "capacity_kwh": _CAPACITY,
                    "min_soc_kwh": 1.0,
                    "charge_efficiency_curve": [
                        {"power_kw": 0.0, "efficiency": 0.99},
                        {"power_kw": 1.0, "efficiency": 0.95},
                        {"power_kw": 2.0, "efficiency": 0.50},
                    ],
                    "discharge_efficiency_curve": [
                        {"power_kw": 0.0, "efficiency": 0.99},
                        {"power_kw": 2.0, "efficiency": 0.95},
                    ],
                    "soc_ratchet": {"enabled": True, **_PRE_HOLD},
                }
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = _bundle(
        soc_kwh=9.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(minutes=15),
        prices=[0.20] * 4,
    )

    result = build_and_solve(bundle, config)

    status = result.battery_care["home"]
    assert result.solve_status in ("optimal", "feasible")
    assert status.deadline_step == 0
    # No power reaches the target in one step: 2 kW interpolates to 1.0 kW DC,
    # a quarter kWh, against the 0.7 kWh the deadline asks for. The probe pins
    # the target to the first step that does reach it.
    assert status.enforced_step is not None and status.enforced_step > 0
    assert status.enforced_target_kwh == pytest.approx(9.7)
    assert _soc_series(result)[status.enforced_step] >= 9.7 - 1e-5


def test_the_retained_payload_round_trips_the_policy_baseline() -> None:
    """Solve, publish, parse, reload: the whole persistence path in one test.

    The individual pieces were each tested against hand-written payloads, which
    is exactly how a field can go missing from the middle of the chain and
    still leave every test green.
    """
    import json
    from unittest.mock import MagicMock

    import paho.mqtt.client as mqtt

    from mimirheim.io.input_parser import parse_battery_care
    from mimirheim.io.mqtt_publisher import MqttPublisher

    config = _config(enabled=True)
    care_since = _T0 - timedelta(days=20)
    bundle = _bundle(soc_kwh=5.0, last_full_utc=None, prices=[0.20] * 8)
    bundle = bundle.model_copy(
        update={
            "battery_inputs": {
                "home": bundle.battery_inputs["home"].model_copy(
                    update={"care_since_utc": care_since}
                )
            }
        }
    )

    result = build_and_solve(bundle, config)

    client = MagicMock()
    client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    MqttPublisher(client=client, config=config).publish_result(result)

    topic = config.batteries["home"].outputs.soc_ratchet
    call = next(c for c in client.publish.call_args_list if c.args[0] == topic)
    last_full, parsed_since = parse_battery_care(call.args[1])

    assert last_full is None
    assert parsed_since == care_since
    assert json.loads(call.args[1])["floor_kwh"] == pytest.approx(
        result.battery_care["home"].floor_kwh
    )


def test_a_minimum_charge_power_near_capacity_does_not_stall_the_policy() -> None:
    """The configuration a reachability estimate could not get right.

    8.8 kWh in a 10 kWh battery, a 2 kW minimum charge power, and derating that
    falls from 3 kW here to 1.5 kW at capacity. Reaching the target means taking
    a large step first and a minimum-power step after, which is not the
    trajectory a greedy walk or an even spread produces — so an estimator asked
    "can this be reached in k steps" answered no for every k, and the overdue
    deadline was dropped on every solve, forever. Pricing the gap asks the
    solver instead, which is the only component that can answer.
    """
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {
                "home": {
                    "capacity_kwh": _CAPACITY,
                    "min_soc_kwh": 1.0,
                    "charge_segments": [{"power_max_kw": 3.0, "efficiency": 1.0}],
                    "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 1.0}],
                    "min_charge_kw": 2.0,
                    "min_discharge_kw": 2.0,
                    "reduce_charge_above_soc_kwh": 8.8,
                    "reduce_charge_min_kw": 1.5,
                    "soc_ratchet": {"enabled": True, **_PRE_HOLD},
                }
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = _bundle(
        soc_kwh=8.8, last_full_utc=_T0 - timedelta(days=30), prices=[0.20] * 8
    )

    result = build_and_solve(bundle, config)

    status = result.battery_care["home"]
    assert result.solve_status in ("optimal", "feasible")
    assert status.deadline_step == 0
    assert status.enforced_target_kwh == pytest.approx(9.7)
    assert max(_soc_series(result)) >= 9.7 - 1e-5


def test_the_target_is_met_by_charging_less_hard_when_that_is_what_fits() -> None:
    """Storing less can be the only way to fit under capacity.

    An SOS2 curve interpolates the product of power and efficiency, so it may
    store *less* DC at a higher AC power. Here 9.8 kWh of 10 are used, the
    minimum charge power is 1 kW, and 1 kW stores 0.25 kWh — which overshoots
    capacity. Around 1.75 kW stores 0.1 kWh and fits exactly, reaching the
    target. Finding that outside the solver means searching a non-monotonic
    curve for the least-storing legal power; inside it, it is just a feasible
    point.
    """
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {
                "home": {
                    "capacity_kwh": _CAPACITY,
                    "min_soc_kwh": 1.0,
                    "min_charge_kw": 1.0,
                    "min_discharge_kw": 1.0,
                    "charge_efficiency_curve": [
                        {"power_kw": 0.0, "efficiency": 1.0},
                        {"power_kw": 1.0, "efficiency": 1.0},
                        {"power_kw": 2.0, "efficiency": 0.1},
                    ],
                    "discharge_efficiency_curve": [
                        {"power_kw": 0.0, "efficiency": 0.99},
                        {"power_kw": 2.0, "efficiency": 0.95},
                    ],
                    "soc_ratchet": {"enabled": True, "full_threshold_pct": 99.0, "target_pct": 99.0, "hold_hours": 0.0},
                }
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = _bundle(
        soc_kwh=9.8, last_full_utc=_T0 - timedelta(days=30), prices=[0.20] * 4
    )

    result = build_and_solve(bundle, config)

    status = result.battery_care["home"]
    assert result.solve_status in ("optimal", "feasible")
    assert status.full_target_kwh == pytest.approx(9.9)
    assert status.enforced_target_kwh == pytest.approx(9.9)
    assert max(_soc_series(result)) >= 9.9 - 1e-5


def test_a_balance_charge_survives_the_dispatch_gain_threshold() -> None:
    """Mandatory work must not be vetoed by the small-gain suppression.

    ``apply_gain_threshold`` replaces a barely-profitable schedule with an idle
    one to avoid cycling the battery for a few cents. The idle schedule zeroes
    battery power, so applying it on a cycle where the solver was required to
    reach the full-charge target would strip the balance charge out of the
    published schedule while the retained status still reported the target as
    enforced: the policy would look satisfied on every topic and never run.
    """
    from mimirheim.core.post_process import apply_gain_threshold

    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "objectives": {"min_dispatch_gain_eur": 100.0},
            "batteries": {
                "home": {
                    "capacity_kwh": _CAPACITY,
                    "min_soc_kwh": 1.0,
                    "charge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
                    "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
                    "soc_ratchet": {"enabled": True},
                }
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=3),
        prices=[0.20] * 16,
    )

    result = build_and_solve(bundle, config)
    assert result.battery_care["home"].enforced_target_kwh is not None

    published = apply_gain_threshold(result, bundle, config)

    assert not published.dispatch_suppressed
    assert max(step.devices["home"].soc_kwh for step in published.schedule) >= 9.7 - 1e-5


def test_the_probe_is_skipped_when_nothing_is_due() -> None:
    """No deadline, no probe: the ordinary cycle must not pay for the feature.

    The probe costs a solve. It runs only when a target is actually due, which
    is a few cycles a week at the default interval, so the other several
    hundred keep the whole budget.
    """
    bundle = _bundle(
        soc_kwh=5.0, last_full_utc=_T0 - timedelta(hours=6), prices=[0.20] * 8
    )

    solves: list[float] = []
    original = CBCSolverBackend.solve

    def _record(self, time_limit_seconds):
        solves.append(time_limit_seconds)
        return original(self, time_limit_seconds=time_limit_seconds)

    with patch.object(CBCSolverBackend, "solve", _record):
        result = build_and_solve(bundle, _config(enabled=True))

    assert len(solves) == 1, "a probe solve ran with no full-charge target due"
    assert result.battery_care["home"].enforced_target_kwh is None


def test_the_probe_leaves_the_budget_to_the_real_solve() -> None:
    """Whatever the probe spends comes off the cycle, not on top of it."""
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=3),
        prices=[0.20] * 16,
    )
    config = _config(enabled=True)
    config.solver.time_limit_seconds = 6.0

    budgets: list[float] = []
    original = CBCSolverBackend.solve

    def _record(self, time_limit_seconds):
        budgets.append(time_limit_seconds)
        return original(self, time_limit_seconds=time_limit_seconds)

    with patch.object(CBCSolverBackend, "solve", _record):
        build_and_solve(bundle, config)

    # One probe solve capped at a third of the cycle, one real solve with what
    # the probe did not spend. The probe's allowance is a ceiling, not a cost,
    # so what matters is that the real solve's budget was reduced by the time
    # actually consumed and the two cannot overrun the cycle between them.
    assert len(budgets) == 2
    assert budgets[0] == pytest.approx(2.0)
    assert budgets[1] < 6.0, "the probe's time was not deducted"
    assert budgets[1] > 5.0, "a fast probe should not eat the solve's budget"


def test_the_probe_respects_the_grid_import_cap() -> None:
    """The witness must be drawn from the model the final solve will use.

    ObjectiveBuilder adds constraints.max_import_kw at the top of build(),
    which runs *after* the probe. If the probe solves without those caps its
    trajectory can rely on import the final model forbids, and the target
    derived from it is then unreachable — turning a guard against infeasibility
    into a cause of it.

    Here the battery needs 1.7 kWh by step 3 but the grid is capped at 1 kW, so
    only 0.25 kWh a step is available. An uncapped probe would see a witness
    reaching 9.7 immediately and pin the target to step 0.
    """
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "constraints": {"max_import_kw": 1.0},
            "batteries": {
                "home": {
                    "capacity_kwh": _CAPACITY,
                    "min_soc_kwh": 1.0,
                    "charge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
                    "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
                    "soc_ratchet": {"enabled": True, **_PRE_HOLD},
                }
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = _bundle(
        soc_kwh=8.0, last_full_utc=_T0 - timedelta(days=30), prices=[0.20] * 16
    )

    result = build_and_solve(bundle, config)

    status = result.battery_care["home"]
    assert result.solve_status in ("optimal", "feasible"), (
        "the probe pinned a target the capped model cannot reach"
    )
    assert status.deadline_step == 0
    # 1 kW through the cap is 0.25 kWh a step; 8.0 -> 9.7 needs seven of them.
    assert status.enforced_step == 6
    assert status.enforced_target_kwh == pytest.approx(9.7)


def test_a_probe_that_cannot_settle_the_limit_enforces_nothing() -> None:
    """An unproven probe is a lower bound on the hardware, not a ceiling.

    When the probe stops on its time limit its best SOC says only "at least
    this much", so reducing the target to it would quietly rewrite a reachable
    full charge down to whatever the solver happened to have found. Nothing is
    imposed instead, and the status shows a due target with none enforced.
    """
    bundle = _bundle(
        soc_kwh=1.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=1),
        prices=[0.20] * 4,
    )

    original = CBCSolverBackend.solve
    calls: list[int] = []

    def _first_solve_is_unproven(self, time_limit_seconds):
        calls.append(1)
        status = original(self, time_limit_seconds=time_limit_seconds)
        # Only the probe (the first solve) is degraded to "feasible".
        return "feasible" if len(calls) == 1 else status

    with patch.object(CBCSolverBackend, "solve", _first_solve_is_unproven):
        result = build_and_solve(bundle, _config(enabled=True))

    status = result.battery_care["home"]
    assert result.solve_status in ("optimal", "feasible")
    assert status.full_target_kwh == pytest.approx(9.7)
    assert status.enforced_target_kwh is None, (
        "an unproven probe was treated as proof of the hardware limit"
    )
    assert status.enforced_step is None


def test_a_lossy_battery_is_not_skipped_for_an_efficient_one() -> None:
    """Maximising stored energy would fill the efficient battery and stop.

    ``max Σ soc`` rewards every kWh equally wherever it lands, so it prefers
    pushing the unit-efficiency battery past its own threshold to lifting the
    60%-efficient one at all. Summing each battery's gap to its own target
    removes that preference: a kWh only scores while that battery is short.

    This pins the specific failure, not a general guarantee. The gap sum still
    does not answer "can this battery reach its target at some step" — see
    _probe_care_targets on why a reduced target is a floor rather than a
    measured limit.
    """
    common = {
        "min_soc_kwh": 0.5,
        "soc_ratchet": {"enabled": True, **_PRE_HOLD},
    }
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {
                "efficient": {
                    "capacity_kwh": 10.0,
                    "charge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
                    "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
                    **common,
                },
                "lossy": {
                    "capacity_kwh": 10.0,
                    "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.6}],
                    "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.6}],
                    **common,
                },
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = SolveBundle(
        solve_time_utc=_T0,
        horizon_prices=[0.20] * 16,
        horizon_export_prices=[0.15] * 16,
        horizon_confidence=[1.0] * 16,
        pv_forecast=[0.0] * 16,
        base_load_forecast=[0.0] * 16,
        battery_inputs={
            "efficient": {
                "soc_kwh": 8.0,
                "last_full_utc": _T0 - timedelta(days=30),
            },
            "lossy": {"soc_kwh": 8.0, "last_full_utc": _T0 - timedelta(days=30)},
        },
    )

    result = build_and_solve(bundle, config)

    assert result.solve_status in ("optimal", "feasible")
    for name in ("efficient", "lossy"):
        status = result.battery_care[name]
        assert status.enforced_target_kwh == pytest.approx(9.7), (
            f"{name} was left short by the joint probe"
        )


def test_a_full_neighbour_cannot_block_an_overdue_battery_from_charging() -> None:
    """A shared direction binary must not make charging unreachable.

    Two or more batteries share one charge/discharge direction binary, to stop
    energy circulating between them. A battery configured with min_charge_kw
    but no min_discharge_kw gets no activity binary, because on its own it can
    always idle by taking the discharge direction and driving it to zero. That
    reasoning does not survive sharing: mode 1 then forces *that* battery to
    charge too, so a neighbour sitting at capacity makes the whole charging
    direction infeasible and no battery can charge in any step.

    Here 'full' is at capacity with a 1 kW charge floor and 'due' is overdue
    and nearly empty. Without an activity binary the probe witnesses a
    trajectory where 'due' never charges, and it witnesses the same thing on
    every rolling solve, so the policy never fires at all.
    """
    common = {
        "capacity_kwh": 10.0,
        "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 1.0}],
        "charge_segments": [{"power_max_kw": 3.0, "efficiency": 1.0}],
        "min_soc_kwh": 0.5,
    }
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 0.0},
            "batteries": {
                "full": {**common, "min_charge_kw": 1.0},
                "due": {**common, "soc_ratchet": {"enabled": True, **_PRE_HOLD}},
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = SolveBundle(
        solve_time_utc=_T0,
        horizon_prices=[0.20] * 16,
        horizon_export_prices=[0.15] * 16,
        horizon_confidence=[1.0] * 16,
        pv_forecast=[0.0] * 16,
        base_load_forecast=[0.0] * 16,
        battery_inputs={
            "full": {"soc_kwh": 10.0, "last_full_utc": _T0},
            "due": {"soc_kwh": 1.0, "last_full_utc": _T0 - timedelta(days=30)},
        },
    )

    result = build_and_solve(bundle, config)

    assert result.solve_status in ("optimal", "feasible")
    status = result.battery_care["due"]
    assert status.enforced_target_kwh == pytest.approx(9.7), (
        "the full neighbour's charge floor blocked the shared charging "
        "direction, so the overdue battery could never charge"
    )


# ---------------------------------------------------------------------------
# Hold at the top, and plan for 100%
# ---------------------------------------------------------------------------


def test_the_full_charge_is_held_for_the_configured_time() -> None:
    """Reaching the target once is a touch; the cells need time at the top.

    Two hours at quarter-hourly steps is eight intervals at the target. soc[t]
    is the SOC at the end of step t, so that is nine consecutive boundaries at
    or above the target, the first being the one the charge arrives on. Eight
    boundaries would be seven intervals: 1.75 h for a configured 2 h.
    """
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=3),
        prices=[0.20] * 24,
    )
    result = build_and_solve(bundle, _config(enabled=True, target_pct=100.0, hold_hours=2.0))

    status = result.battery_care["home"]
    soc = _soc_series(result)
    assert status.hold_steps == 8
    assert status.enforced_hold_steps == 8
    start = status.enforced_step
    assert all(soc[t] >= 10.0 - 1e-6 for t in range(start, start + 9))


def test_a_hold_that_runs_past_the_horizon_is_clipped_not_dropped() -> None:
    """What fits is enforced; the next rolling solve continues it.

    Deadline at step 11 of a 16-step horizon leaves five boundaries, which is
    four intervals at the top. Refusing the hold because eight do not fit
    would leave an overdue battery unenforced on every short horizon.
    """
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=3),
        prices=[0.20] * 16,
    )
    result = build_and_solve(bundle, _config(enabled=True, target_pct=100.0, hold_hours=2.0))

    status = result.battery_care["home"]
    assert status.enforced_step == 11
    assert status.enforced_hold_steps == 4
    assert all(s >= 10.0 - 1e-6 for s in _soc_series(result)[11:16])


def test_a_hold_the_hardware_cannot_sustain_is_enforced_as_far_as_it_goes() -> None:
    """A load with no grid to serve it must come out of the battery.

    The battery starts full and overdue, so the target is due at step 0 and
    the witness is at the top from the start. From step 2 there is a 4 kW load
    and no import, so the battery has to carry it and cannot stay full. The
    battery arrived full, so its starting SOC is the first boundary; with the
    witness at the top at the end of steps 0 and 1 that is two intervals, and
    that is what is enforced: a short hold rather than none, and rather than
    an infeasible eight.
    """
    battery: dict = {
        "capacity_kwh": _CAPACITY,
        "min_soc_kwh": 1.0,
        "charge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
        "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
        "soc_ratchet": {"enabled": True, "target_pct": 100.0, "hold_hours": 2.0},
    }
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 0.0, "export_limit_kw": 20.0},
            "batteries": {"home": battery},
            "static_loads": {"base": {}},
        }
    )
    horizon = 8
    bundle = SolveBundle(
        solve_time_utc=_T0,
        horizon_prices=[0.20] * horizon,
        horizon_export_prices=[0.15] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[0.0] * horizon,
        base_load_forecast=[0.0, 0.0] + [4.0] * (horizon - 2),
        battery_inputs={
            "home": {"soc_kwh": _CAPACITY, "last_full_utc": _T0 - timedelta(days=8)}
        },
    )
    result = build_and_solve(bundle, config)

    status = result.battery_care["home"]
    soc = _soc_series(result)
    assert status.enforced_step == 0
    assert status.enforced_hold_steps == 2
    assert soc[0] >= 10.0 - 1e-6 and soc[1] >= 10.0 - 1e-6
    assert soc[2] < 10.0


def test_a_battery_that_arrived_full_counts_its_starting_state() -> None:
    """The measured starting SOC is a boundary; soc[0..7] then closes 8 intervals.

    Same shape as the test above, with the load arriving at step 8 instead of
    step 2. Counting boundaries from soc[0] alone would demand soc[0..8], see
    the load cut the run at eight boundaries, and report seven intervals for a
    hold the battery physically delivered in full.
    """
    battery: dict = {
        "capacity_kwh": _CAPACITY,
        "min_soc_kwh": 1.0,
        "charge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
        "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 1.0}],
        "soc_ratchet": {"enabled": True, "target_pct": 100.0, "hold_hours": 2.0},
    }
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 0.0, "export_limit_kw": 20.0},
            "batteries": {"home": battery},
            "static_loads": {"base": {}},
        }
    )
    horizon = 14
    bundle = SolveBundle(
        solve_time_utc=_T0,
        horizon_prices=[0.20] * horizon,
        horizon_export_prices=[0.15] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[0.0] * horizon,
        base_load_forecast=[0.0] * 8 + [4.0] * (horizon - 8),
        battery_inputs={
            "home": {"soc_kwh": _CAPACITY, "last_full_utc": _T0 - timedelta(days=8)}
        },
    )
    result = build_and_solve(bundle, config)

    status = result.battery_care["home"]
    soc = _soc_series(result)
    assert status.enforced_step == 0
    assert status.enforced_hold_steps == 8
    assert all(s >= 10.0 - 1e-6 for s in soc[:8])
    assert soc[8] < 10.0


def test_a_target_of_one_hundred_percent_reaches_the_bound() -> None:
    """The target sits on the SOC variable's upper bound and must still be met.

    A backend that lands a hair under the bound must not be read as having
    missed the target; the tolerance on the way in is what this checks.
    """
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=3),
        prices=[0.20] * 16,
    )
    result = build_and_solve(
        bundle, _config(enabled=True, full_threshold_pct=97.0, target_pct=100.0, hold_hours=0.0)
    )

    status = result.battery_care["home"]
    assert status.full_target_kwh == pytest.approx(_CAPACITY)
    assert status.enforced_target_kwh == pytest.approx(_CAPACITY, abs=1e-5)
    assert _soc_series(result)[status.enforced_step] >= _CAPACITY - 1e-5


def test_a_zero_hold_on_a_battery_already_full_still_reports_a_touch() -> None:
    """soc[0] is pinned, which is physically one interval, but a touch is 0."""
    bundle = _bundle(
        soc_kwh=_CAPACITY,
        last_full_utc=_T0 - timedelta(days=8),
        prices=[0.20] * 8,
    )
    result = build_and_solve(bundle, _config(enabled=True, target_pct=100.0, hold_hours=0.0))
    status = result.battery_care["home"]
    assert status.enforced_step == 0
    assert status.enforced_hold_steps == 0


def test_a_zero_hold_enforces_a_single_step() -> None:
    bundle = _bundle(
        soc_kwh=8.0,
        last_full_utc=_T0 - timedelta(days=7) + timedelta(hours=3),
        prices=[0.20] * 16,
    )
    result = build_and_solve(bundle, _config(enabled=True, hold_hours=0.0))
    status = result.battery_care["home"]
    assert status.hold_steps == 0
    assert status.enforced_hold_steps == 0
