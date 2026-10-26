"""Solver-level tests for heat stores.

These tests build real models through ``build_and_solve`` and assert on the
published schedule: when the store boosts, how much it draws, and how the
planned temperature evolves. They never inspect variable names or constraint
counts, so they hold for any formulation that produces the specified plan.

The default store is a 200 L DHW heat pump boiler with one boost stage that
lets the heat pump heat 5 K above its normal setpoint:

    C = 0.233 kWh/K, normal_cop = 2.5, normal_setpoint_c = 50, ambient_c = 20,
    drift = 0.4 K/h, stage 1: 0.5 kW at COP 2.5 up to 55 degrees C.

Its full boost stores 0.233 * 5 = 1.165 kWh of heat above the normal setpoint,
which the normal program's maintenance loss of 0.233 * 0.4 * 0.25 = 0.0233 kWh
per step takes 50 steps to use up.
"""

from datetime import datetime, timezone

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle, SolveResult
from mimirheim.core.model_builder import build_and_solve

_T0 = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
_HORIZON = 96
_C = 0.233
_SETPOINT = 50.0
_CAP = 55.0
_STAGE_KW = 0.5


def _store(**overrides: object) -> dict:
    store: dict = {
        "heat_capacity_kwh_per_k": _C,
        "normal_cop": 2.5,
        "normal_setpoint_c": _SETPOINT,
        "ambient_c": 20.0,
        "drift_k_per_hour": 0.4,
        "boost_stages": [{"power_kw": _STAGE_KW, "cop": 2.5, "max_temp_c": _CAP}],
    }
    store.update(overrides)
    return store


def _config(*, pv: bool = False, hybrid: bool = False, **store: object) -> MimirheimConfig:
    raw: dict = {
        "mqtt": {"host": "localhost", "client_id": "test"},
        "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
        "static_loads": {"base": {}},
        "heat_stores": {"boiler": _store(**store)},
    }
    if pv:
        raw["pv_arrays"] = {"roof": {"max_power_kw": 10.0}}
    if hybrid:
        raw["hybrid_inverters"] = {
            "hybrid": {
                "capacity_kwh": 5.0,
                "max_charge_kw": 0.0001,
                "max_discharge_kw": 0.0001,
                "max_pv_kw": 10.0,
            }
        }
    return MimirheimConfig.model_validate(raw)


def _bundle(
    *,
    prices: list[float],
    export_prices: list[float] | None = None,
    temp_c: float = _SETPOINT,
    load_kw: float = 0.5,
    pv_kw: list[float] | None = None,
    hybrid_pv_kw: list[float] | None = None,
    strategy: str = "minimize_cost",
    solve_time_utc: datetime = _T0,
) -> SolveBundle:
    horizon = len(prices)
    pv = pv_kw if pv_kw is not None else [0.0] * horizon
    data: dict = {
        "strategy": strategy,
        "solve_time_utc": solve_time_utc,
        "horizon_prices": prices,
        "horizon_export_prices": (
            export_prices if export_prices is not None else [p - 0.05 for p in prices]
        ),
        "horizon_confidence": [1.0] * horizon,
        "pv_forecast": pv,
        "base_load_forecast": [load_kw] * horizon,
        "heat_store_inputs": {"boiler": {"current_temp_c": temp_c}},
    }
    if pv_kw is not None:
        data["pv_forecasts"] = {"roof": pv}
    if hybrid_pv_kw is not None:
        data["hybrid_inverter_inputs"] = {
            "hybrid": {"soc_kwh": 2.5, "pv_forecast_kw": hybrid_pv_kw}
        }
    return SolveBundle.model_validate(data)


def _stages(result: SolveResult) -> list[int]:
    return [step.devices["boiler"].boost_stage for step in result.schedule]


def _draw_kw(result: SolveResult) -> list[float]:
    """Electrical draw per step in kW. net_power is negative for a consumer."""
    return [-step.devices["boiler"].kw for step in result.schedule]


def _temps(result: SolveResult) -> list[float]:
    return [step.devices["boiler"].temp_c for step in result.schedule]


# ---------------------------------------------------------------------------
# Flat prices never boost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["minimize_cost", "balanced"])
def test_flat_prices_never_boost(strategy: str) -> None:
    result = build_and_solve(
        _bundle(prices=[0.25] * _HORIZON, strategy=strategy), _config()
    )

    assert result.solve_status in ("optimal", "feasible")
    assert _stages(result) == [0] * _HORIZON
    assert _draw_kw(result) == pytest.approx([0.0] * _HORIZON, abs=1e-6)


# ---------------------------------------------------------------------------
# Price spread
# ---------------------------------------------------------------------------


def _spread_prices() -> list[float]:
    """Eight cheap steps, then expensive for the rest of the horizon."""
    return [0.05] * 8 + [0.40] * (_HORIZON - 8)


def test_price_spread_boosts_before_the_expensive_period() -> None:
    result = build_and_solve(_bundle(prices=_spread_prices()), _config())

    stages = _stages(result)
    assert any(stage == 1 for stage in stages[:8])
    assert all(stage == 0 for stage in stages[8:])
    # The store is filled to its stage cap by the end of the cheap period.
    assert _temps(result)[7] == pytest.approx(_CAP, abs=1e-4)


def test_draw_is_full_stage_power_except_where_the_store_ends_at_its_cap() -> None:
    """A store close to its cap has room for less than one full step.

    At 54 degrees C the store holds 0.932 kWh and has room for 0.233 kWh plus
    one step's losses, less than the 0.3125 kWh one full-power step delivers.
    A boost before the expensive period therefore reaches the cap in a partial
    step.
    """
    result = build_and_solve(_bundle(prices=_spread_prices(), temp_c=54.0), _config())

    partial_steps = 0
    for stage, draw, temp in zip(
        _stages(result), _draw_kw(result), _temps(result), strict=True
    ):
        if stage == 0:
            assert draw == pytest.approx(0.0, abs=1e-6)
        elif draw < _STAGE_KW - 1e-6:
            partial_steps += 1
            assert temp == pytest.approx(_CAP, abs=1e-4)
        else:
            assert draw == pytest.approx(_STAGE_KW, abs=1e-6)
    assert partial_steps >= 1


def test_planned_temperature_starts_from_the_measurement_and_decays() -> None:
    """With no boost, a store above its setpoint cools back towards it."""
    result = build_and_solve(
        _bundle(prices=[0.25] * _HORIZON, temp_c=53.0), _config()
    )

    temps = _temps(result)
    assert temps[0] < 53.0
    assert all(later <= earlier + 1e-9 for earlier, later in zip(temps, temps[1:], strict=False))
    assert temps[-1] >= _SETPOINT - 1e-9


def test_temperature_below_setpoint_counts_as_no_stored_heat() -> None:
    result = build_and_solve(
        _bundle(prices=[0.25] * _HORIZON, temp_c=42.0), _config()
    )

    assert _temps(result) == pytest.approx([_SETPOINT] * _HORIZON, abs=1e-6)


def test_temperature_above_the_cap_is_clamped() -> None:
    """A reading above the highest cap counts as a full store, not more."""
    hot = build_and_solve(_bundle(prices=[0.25] * _HORIZON, temp_c=70.0), _config())
    full = build_and_solve(_bundle(prices=[0.25] * _HORIZON, temp_c=_CAP), _config())

    assert _temps(hot) == pytest.approx(_temps(full), abs=1e-6)
    assert _temps(hot)[0] <= _CAP


# ---------------------------------------------------------------------------
# Negative prices
# ---------------------------------------------------------------------------


def test_negative_prices_fill_the_store_then_hold_at_loss_replacement_draw() -> None:
    negative = 16
    prices = [-0.10] * negative + [0.30] * (_HORIZON - negative)
    result = build_and_solve(
        _bundle(prices=prices, export_prices=[p - 0.05 for p in prices]), _config()
    )

    temps = _temps(result)
    draws = _draw_kw(result)
    first_full = next(t for t, temp in enumerate(temps) if temp >= _CAP - 1e-4)
    assert first_full < negative

    for t in range(first_full + 1, negative):
        # Held at the cap: boosting, but only replacing what the store loses.
        assert temps[t] == pytest.approx(_CAP, abs=1e-4)
        assert _stages(result)[t] == 1
        assert 0.0 < draws[t] < 0.2 * _STAGE_KW


# ---------------------------------------------------------------------------
# The credit for displaced heating
# ---------------------------------------------------------------------------


def test_stored_heat_is_used_at_its_physical_rate_at_negative_export_prices() -> None:
    """Heat already in the store is used as it physically would be.

    For the first 16 steps PV exactly covers the base load and export prices are
    negative, so the heating stored heat displaces would have run on PV worth
    less than nothing and the clamped credit is zero. The plan must still use
    the stored heat in those steps: holding it for the evening, when the credit
    is positive, would pretend the store keeps heat it is losing.
    """
    surplus = 16
    pv = [0.5] * surplus + [0.0] * (_HORIZON - surplus)
    exports = [-0.05] * surplus + [0.10] * (_HORIZON - surplus)
    cfg = _config(pv=True)
    full = build_and_solve(
        _bundle(prices=[0.30] * _HORIZON, export_prices=exports, temp_c=_CAP, pv_kw=pv),
        cfg,
    )
    assert all(stage == 0 for stage in _stages(full))

    # Physical cooldown with no boost: S[t] = (1 - d) * S[t-1] - maint, so the
    # temperature drops by at least drift * dt = 0.1 K per step.
    temps = _temps(full)
    previous = _CAP
    for t in range(surplus):
        assert temps[t] <= previous - 0.1 + 1e-6
        previous = temps[t]


def test_displaced_heating_on_pv_surplus_is_valued_at_export_price() -> None:
    """A midday boost on surplus PV is worth less than one that displaces import.

    Two solves differ only in whether PV covers the base load in the evening,
    when the stored heat is used. When it does, the displaced heating is worth
    the export price, too little to pay for the boost; when it does not, it is
    worth the import price, which does pay.
    """
    cheap = 8
    prices = [0.10] * cheap + [0.40] * (_HORIZON - cheap)
    exports = [0.0] * _HORIZON

    covered = build_and_solve(
        _bundle(prices=prices, export_prices=exports, pv_kw=[5.0] * _HORIZON),
        _config(pv=True),
    )
    uncovered = build_and_solve(
        _bundle(prices=prices, export_prices=exports, pv_kw=[0.0] * _HORIZON),
        _config(pv=True),
    )

    assert all(stage == 0 for stage in _stages(covered))
    assert any(stage == 1 for stage in _stages(uncovered))


def test_hybrid_inverter_pv_counts_towards_covering_base_load() -> None:
    cheap = 8
    prices = [0.10] * cheap + [0.40] * (_HORIZON - cheap)
    exports = [0.0] * _HORIZON

    result = build_and_solve(
        _bundle(
            prices=prices, export_prices=exports, hybrid_pv_kw=[5.0] * _HORIZON
        ),
        _config(hybrid=True),
    )

    assert all(stage == 0 for stage in _stages(result))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_missing_bundle_entry_is_rejected() -> None:
    bundle = _bundle(prices=[0.25] * _HORIZON).model_copy(
        update={"heat_store_inputs": {}}
    )
    with pytest.raises(ValueError, match="boiler"):
        build_and_solve(bundle, _config())


def test_schedule_entries_carry_type_stage_and_temperature() -> None:
    result = build_and_solve(_bundle(prices=_spread_prices()), _config())

    entry = result.schedule[0].devices["boiler"]
    assert entry.type == "heat_store"
    assert entry.boost_stage in (0, 1)
    assert entry.temp_c is not None
    assert entry.kw <= 0.0


# ---------------------------------------------------------------------------
# Multiple boost stages
# ---------------------------------------------------------------------------

# The example boiler from the README: stage 1 is the heat pump above its normal
# setpoint, stage 2 the element, stage 3 both. Stage 3's COP follows from its
# parts, (0.5 * 2.5 + 1.2 * 1.0) / 1.7 = 1.44, and its cap is the heat pump's.
_ELEMENT_CAP = 65.0
_EXAMPLE_STAGES = [
    {"power_kw": 0.5, "cop": 2.5, "max_temp_c": _CAP},
    {"power_kw": 1.2, "cop": 1.0, "max_temp_c": _ELEMENT_CAP},
    {"power_kw": 1.7, "cop": 1.44, "max_temp_c": _CAP},
]
_EXAMPLE_POWER_KW = {n: s["power_kw"] for n, s in enumerate(_EXAMPLE_STAGES, start=1)}
_EXAMPLE_CAP_C = {n: s["max_temp_c"] for n, s in enumerate(_EXAMPLE_STAGES, start=1)}
# More than one step's losses: drift (0.1 K) plus extra decay at the element's
# cap (about 0.05 K). A store this far above a stage's cap at the start of a
# step cannot end the step at or below it.
_ABOVE_CAP_MARGIN_K = 0.2


def test_at_most_one_stage_is_on_per_step_and_numbers_match_list_positions() -> None:
    """Each boosting step draws the power of the stage at its list position.

    Draw is full stage power except in a step that ends at that stage's cap,
    where it is only what fits.
    """
    result = build_and_solve(
        _bundle(prices=_spread_prices()), _config(boost_stages=_EXAMPLE_STAGES)
    )

    boosted = 0
    for stage, draw, temp in zip(
        _stages(result), _draw_kw(result), _temps(result), strict=True
    ):
        assert stage in (0, 1, 2, 3)
        if stage == 0:
            assert draw == pytest.approx(0.0, abs=1e-6)
            continue
        boosted += 1
        if temp < _EXAMPLE_CAP_C[stage] - 1e-4:
            assert draw == pytest.approx(_EXAMPLE_POWER_KW[stage], abs=1e-6)
        else:
            assert 0.0 <= draw <= _EXAMPLE_POWER_KW[stage] + 1e-6
    assert boosted > 0


def test_lower_cap_stages_heat_up_to_their_cap_and_only_the_element_above_it() -> None:
    result = build_and_solve(
        _bundle(prices=_spread_prices(), temp_c=_SETPOINT),
        _config(boost_stages=_EXAMPLE_STAGES),
    )

    stages = _stages(result)
    temps = _temps(result)
    # Below the heat pump's cap the more efficient heat pump stages are used;
    # above it only the element can heat.
    assert any(stage in (1, 3) for stage in stages)
    assert max(temps) > _CAP + 0.5
    for t, stage in enumerate(stages):
        previous = _SETPOINT if t == 0 else temps[t - 1]
        if stage in (1, 3):
            # A lower-cap stage never ends a step above its cap.
            assert temps[t] <= _CAP + 1e-4
        if stage != 0 and previous > _CAP + _ABOVE_CAP_MARGIN_K:
            assert stage == 2


def test_stage_with_cap_below_the_stored_heat_is_never_planned() -> None:
    """At 60 degrees C, the 55 degree stages cannot run until the store cools to 55."""
    measured = 60.0
    result = build_and_solve(
        _bundle(prices=_spread_prices(), temp_c=measured),
        _config(boost_stages=_EXAMPLE_STAGES),
    )

    temps = _temps(result)
    for t, stage in enumerate(_stages(result)):
        if stage in (1, 3):
            previous = measured if t == 0 else temps[t - 1]
            assert previous <= _CAP + _ABOVE_CAP_MARGIN_K
            assert temps[t] <= _CAP + 1e-4
    # The eight cheap steps of _spread_prices are still used to top up the
    # store, through the element.
    assert 2 in _stages(result)[:8]


def test_full_store_at_negative_prices_holds_through_the_highest_cap_stage() -> None:
    """Being paid to consume makes boosting compulsory in practice.

    The store starts at the element's cap, above the other stages' caps. The
    plan must stay feasible and hold the store at the cap through the element
    alone, drawing only loss replacement.
    """
    negative = 16
    prices = [-0.10] * negative + [0.30] * (_HORIZON - negative)
    result = build_and_solve(
        _bundle(
            prices=prices,
            export_prices=[p - 0.05 for p in prices],
            temp_c=_ELEMENT_CAP,
        ),
        _config(boost_stages=_EXAMPLE_STAGES),
    )

    assert result.solve_status in ("optimal", "feasible")
    stages = _stages(result)
    draws = _draw_kw(result)
    temps = _temps(result)
    for t in range(negative):
        assert stages[t] == 2
        assert temps[t] == pytest.approx(_ELEMENT_CAP, abs=1e-4)
        # Loss replacement is a small fraction of the element's power.
        assert 0.0 < draws[t] < 0.2 * _EXAMPLE_POWER_KW[2]


# ---------------------------------------------------------------------------
# Deny boost windows
# ---------------------------------------------------------------------------


def test_no_boost_is_planned_inside_a_deny_window() -> None:
    """A deny window over the second half of the cheap period moves the boost.

    Without a window the store boosts as late as possible in the cheap
    period, to lose the least heat before the expensive period. The horizon
    starts at 00:00 UTC; "01:00-02:00" denies steps 4 to 7, so the boost moves
    to steps 0 to 3.
    """
    undenied = _stages(build_and_solve(_bundle(prices=_spread_prices()), _config()))
    assert any(stage == 1 for stage in undenied[4:8])

    result = build_and_solve(
        _bundle(prices=_spread_prices()), _config(deny_boost_hours=["01:00-02:00"])
    )

    stages = _stages(result)
    assert any(stage == 1 for stage in stages[:4])
    assert all(stage == 0 for stage in stages[4:])
    assert all(draw == pytest.approx(0.0, abs=1e-6) for draw in _draw_kw(result)[4:8])


def test_deny_window_crossing_midnight() -> None:
    """A solve starting at 22:00 UTC with "22:00-02:00" denied.

    The window covers steps 0 to 15, across midnight, and so the entire cheap
    period of 8 steps. Steps 16 onwards (02:00) are allowed but expensive.
    """
    start = datetime(2026, 10, 12, 22, 0, tzinfo=timezone.utc)
    prices = [0.05] * 16 + [0.40] * (_HORIZON - 16)
    result = build_and_solve(
        _bundle(prices=prices, solve_time_utc=start),
        _config(deny_boost_hours=["22:00-02:00"]),
    )

    stages = _stages(result)
    assert all(stage == 0 for stage in stages[:16])


def test_deny_window_recurs_daily_and_ends_at_its_end_time() -> None:
    """A 48-hour horizon sees the window on both days; the steps just outside are allowed.

    "01:00-02:00" denies steps 4 to 7 and 100 to 103. Prices are negative in
    every step, so without the window the store would be held at its cap
    throughout and every step would boost.
    """
    horizon = 192
    prices = [-0.10] * horizon
    result = build_and_solve(
        _bundle(prices=prices, export_prices=[p - 0.05 for p in prices]),
        _config(deny_boost_hours=["01:00-02:00"]),
    )

    stages = _stages(result)
    denied = set(range(4, 8)) | set(range(100, 104))
    for t, stage in enumerate(stages):
        if t in denied:
            assert stage == 0
    assert stages[3] == 1
    assert stages[8] == 1
    assert stages[99] == 1
    assert stages[104] == 1


def test_deny_window_applies_to_every_stage() -> None:
    """With three stages and two windows, no stage runs in a denied step.

    Prices are negative throughout, so every allowed step would boost.
    "00:00-01:00" denies steps 0 to 3 and "03:00-03:30" denies steps 12 and 13.
    """
    prices = [-0.10] * _HORIZON
    result = build_and_solve(
        _bundle(prices=prices, export_prices=[p - 0.05 for p in prices]),
        _config(
            boost_stages=_EXAMPLE_STAGES,
            deny_boost_hours=["00:00-01:00", "03:00-03:30"],
        ),
    )

    stages = _stages(result)
    for t in [*range(0, 4), 12, 13]:
        assert stages[t] == 0
    assert any(stage != 0 for stage in stages[4:12])


# ---------------------------------------------------------------------------
# Minimum run within a solve
# ---------------------------------------------------------------------------


def _runs(stages: list[int]) -> list[tuple[int, int]]:
    """Return (first step, length) of each run of consecutive boosting steps, any stage."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for t, stage in enumerate([*stages, 0]):
        if stage != 0 and start is None:
            start = t
        elif stage == 0 and start is not None:
            runs.append((start, t - start))
            start = None
    return runs


def _one_cheap_step_prices() -> list[float]:
    """Expensive throughout except step 10."""
    prices = [0.40] * _HORIZON
    prices[10] = 0.05
    return prices


def test_default_min_run_allows_a_single_step_boost() -> None:
    """At 54 degrees C the store has room for less than one full step.

    With no minimum run the plan tops up in the one cheap step and nowhere
    else. This is the baseline the minimum-run tests below change.
    """
    result = build_and_solve(
        _bundle(prices=_one_cheap_step_prices(), temp_c=54.0), _config()
    )

    assert _runs(_stages(result)) == [(10, 1)]


def test_every_boost_run_lasts_at_least_min_run_steps() -> None:
    """The one cheap step still pays for a run of four steps.

    The run tops up in its first step and holds the store at its cap for the
    other three, drawing only loss replacement rather than full stage power.
    """
    result = build_and_solve(
        _bundle(prices=_one_cheap_step_prices(), temp_c=54.0),
        _config(min_run_steps=4),
    )

    stages = _stages(result)
    temps = _temps(result)
    draws = _draw_kw(result)
    runs = _runs(stages)
    assert runs
    for first, length in runs:
        assert length >= 4 or first + length == _HORIZON
    assert stages[10] == 1
    held = 0
    for first, length in runs:
        for t in range(first, first + length):
            if t > 0 and temps[t - 1] >= _CAP - 1e-4:
                # Held at the cap: boosting, but only replacing losses.
                held += 1
                assert temps[t] == pytest.approx(_CAP, abs=1e-4)
                assert 0.0 < draws[t] < 0.2 * _STAGE_KW
    assert held >= 2


def test_a_run_may_be_cut_short_by_the_horizon_end() -> None:
    """Being paid to consume in the last two steps boosts there alone.

    A run of four steps would have to start in the expensive steps before;
    the minimum run does not reach past the horizon, so it does not have to.
    """
    prices = [0.30] * (_HORIZON - 2) + [-0.10] * 2
    result = build_and_solve(
        _bundle(prices=prices, export_prices=[p - 0.05 for p in prices]),
        _config(min_run_steps=4),
    )

    assert _runs(_stages(result)) == [(_HORIZON - 2, 2)]


def test_a_stage_switch_within_a_run_is_not_a_new_start() -> None:
    """With the example boiler and two cheap steps, the minimum run spans stages.

    Without a minimum run the plan boosts in the two cheap steps only. A
    minimum run of four extends that run into the expensive steps, and the
    plan changes stage within it, with each stage on for fewer than four
    steps. Were a stage switch a new start, every stage would need four steps.
    """
    min_run = 4
    prices = [0.05] * 2 + [0.40] * (_HORIZON - 2)
    unconstrained = _stages(
        build_and_solve(_bundle(prices=prices), _config(boost_stages=_EXAMPLE_STAGES))
    )
    assert all(length < min_run for _, length in _runs(unconstrained))

    result = build_and_solve(
        _bundle(prices=prices),
        _config(boost_stages=_EXAMPLE_STAGES, min_run_steps=min_run),
    )

    stages = _stages(result)
    runs = _runs(stages)
    assert runs
    for first, length in runs:
        assert length >= min_run or first + length == _HORIZON
    first, length = runs[0]
    run = stages[first : first + length]
    assert len(set(run)) > 1
    for stage in set(run):
        segments = _runs([1 if s == stage else 0 for s in run])
        assert max(segment_length for _, segment_length in segments) < min_run


def test_no_boost_starts_that_cannot_complete_before_a_deny_window() -> None:
    """The cheap period is steps 0 to 7; "01:00-02:00" denies steps 4 to 7.

    With a minimum run of four, a run fits in steps 0 to 3. With a minimum
    run of six it does not, and a run may not reach into the window, so
    the cheap steps are not used at all.
    """
    fits = build_and_solve(
        _bundle(prices=_spread_prices()),
        _config(deny_boost_hours=["01:00-02:00"], min_run_steps=4),
    )
    assert _runs(_stages(fits))[0] == (0, 4)

    result = build_and_solve(
        _bundle(prices=_spread_prices()),
        _config(deny_boost_hours=["01:00-02:00"], min_run_steps=6),
    )

    stages = _stages(result)
    assert all(stage == 0 for stage in stages[:8])
    for first, length in _runs(stages):
        assert length >= 6 or first + length == _HORIZON
