"""model_builder — assembles and solves the MILP for one mimirheim cycle.

This module is the entry point for the solver pipeline. The single public
function ``build_and_solve`` takes a ``SolveBundle`` (runtime inputs) and a
``MimirheimConfig`` (static configuration), constructs the full MILP model, solves
it, and returns a ``SolveResult``.

``build_and_solve`` is a **pure function**: it has no side effects, no I/O,
and no shared mutable state. It does not log, write files, or publish to MQTT.
Callers are responsible for all of those actions.

The ``debug_dump`` helper is also defined here but is called by the solve loop
in ``__main__``, not inside ``build_and_solve``.

This module imports from ``mimirheim.core``, ``mimirheim.config``, and ``mimirheim.devices``
but never from ``mimirheim.io``.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import (
    BatteryInputs,
    DeviceSetpoint,
    HybridInverterInputs,
    ScheduleStep,
    SolveBundle,
    SolveResult,
    ThermalBoilerInputs,
)
from mimirheim.core.context import ModelContext
from mimirheim.core.objective import ObjectiveBuilder
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.battery import Battery
from mimirheim.devices.deferrable_load import DeferrableLoad
from mimirheim.devices.ev import EvDevice
from mimirheim.devices.grid import Grid
from mimirheim.devices.hybrid_inverter import HybridInverterDevice
from mimirheim.devices.pv import PvDevice, PvInputs
from mimirheim.devices.static_load import StaticLoad, StaticLoadInputs
from mimirheim.devices.space_heating import SpaceHeatingDevice
from mimirheim.devices.thermal_boiler import ThermalBoilerDevice
from mimirheim.devices.combi_heat_pump import CombiHeatPumpDevice

logger = logging.getLogger("mimirheim.solver")


def _dt_from_horizon(horizon: int) -> float:
    """Return the per-step duration in hours (always 0.25 — mimirheim uses 15-minute steps).

    All ``SolveBundle`` arrays are resampled to a 15-minute grid by
    ``ReadinessState.snapshot()`` before reaching this function. The horizon
    is variable in length (1 to any number of steps) but never in resolution.

    The ``horizon`` argument is accepted for API compatibility but not used.

    Args:
        horizon: Total number of steps in the horizon (unused).

    Returns:
        ``0.25`` — the fixed 15-minute step duration in hours.
    """
    return 0.25


def _eval_net_power(ctx: ModelContext, expr: Any) -> float:
    """Evaluate a device's net_power expression after a solve.

    Some devices (PV, StaticLoad) return a plain Python float from
    ``net_power(t)``; others (Battery, EV, Grid) return a solver expression.
    This helper dispatches correctly for both cases.

    A device returning a Python numeric means it has no solver variables — its
    power is determined purely by the forecast. In that case the value is used
    directly without touching the solver.

    Args:
        ctx: The model context providing access to the solver backend.
        expr: The return value of ``device.net_power(t)``.

    Returns:
        The evaluated power in kW (positive = producing, negative = consuming).
    """
    if isinstance(expr, (int, float)):
        return float(expr)
    return ctx.solver.var_value(expr)


def build_and_solve(bundle: SolveBundle, config: MimirheimConfig) -> SolveResult:
    """Build and solve the MILP optimisation model for the current time horizon.

    This is the central function of mimirheim. It takes a validated snapshot of all
    current inputs (prices, forecasts, device states) and the static system
    configuration, constructs a linear programme, solves it, and returns the
    optimal schedule.

    The function is a pure function with no I/O side effects. It does not read
    from MQTT, write files, or log. Callers are responsible for obtaining the
    inputs and acting on the result.

    Steps:
        1. Derive ``horizon`` and ``dt`` from the length of
           ``bundle.horizon_prices``.
        2. Create a fresh ``CBCSolverBackend`` and ``ModelContext``.
        3. Instantiate all device objects from ``config``.
        4. Call ``add_variables(ctx)`` on all devices.
        5. Call ``add_constraints(ctx, inputs)`` on all devices, passing the
           relevant slice of ``bundle`` to each.
        6. Add the power balance constraint for each time step:
           ``sum(d.net_power(t) for d in all_devices) + grid.net_power(t) == 0``.
        7. Call ``ObjectiveBuilder().build(...)`` to set the objective.
        8. Call ``ctx.solver.solve()``.
        9. If infeasible, return an empty ``SolveResult``.
        10. Extract variable values and assemble ``SolveResult``.

    Args:
        bundle: Validated snapshot of all runtime inputs for this solve cycle.
            Assembled from the latest retained MQTT values by the IO layer.
        config: Validated static system configuration loaded at startup.

    Returns:
        ``SolveResult`` containing the full schedule and solve metadata. If the
        solver finds no feasible solution, ``solve_status`` is ``"infeasible"``
        and ``schedule`` is empty.

    Raises:
        ValueError: If ``bundle`` and ``config`` are internally inconsistent
            (e.g. a device named in ``bundle.battery_inputs`` has no matching
            entry in ``config.batteries``).
    """
    horizon = len(bundle.horizon_prices)
    dt = _dt_from_horizon(horizon)

    solver = CBCSolverBackend(threads=config.solver.threads)
    ctx = ModelContext(solver=solver, horizon=horizon, dt=dt)

    # --- Instantiate all devices ---
    grid = Grid(config=config.grid)

    batteries = [
        Battery(name=name, config=cfg)
        for name, cfg in config.batteries.items()
    ]
    pv_devices = [
        PvDevice(name=name, config=cfg)
        for name, cfg in config.pv_arrays.items()
    ]
    ev_devices = [
        EvDevice(name=name, config=cfg)
        for name, cfg in config.ev_chargers.items()
    ]
    deferrable_loads = [
        DeferrableLoad(name=name, config=cfg)
        for name, cfg in config.deferrable_loads.items()
    ]
    static_loads = [
        StaticLoad(name=name, config=cfg)
        for name, cfg in config.static_loads.items()
    ]
    hybrid_inverters = [
        HybridInverterDevice(name=name, config=cfg)
        for name, cfg in config.hybrid_inverters.items()
    ]
    thermal_boilers = [
        ThermalBoilerDevice(name=name, config=cfg)
        for name, cfg in config.thermal_boilers.items()
    ]
    space_heating_hps = [
        SpaceHeatingDevice(name=name, config=cfg)
        for name, cfg in config.space_heating_hps.items()
    ]
    combi_heat_pumps = [
        CombiHeatPumpDevice(name=name, config=cfg)
        for name, cfg in config.combi_heat_pumps.items()
    ]

    all_devices: list[Any] = [
        *batteries, *pv_devices, *ev_devices, *deferrable_loads, *static_loads,
        *hybrid_inverters, *thermal_boilers, *space_heating_hps, *combi_heat_pumps,
    ]

    # --- Add variables ---
    grid.add_variables(ctx)
    for device in all_devices:
        device.add_variables(ctx)

    # --- Shared system direction binaries (anti-roundtrip) ---
    #
    # When two or more batteries are present, they could individually choose
    # opposite directions (A charges, B discharges) in the same step. Because
    # each battery's efficiency and wear cost must be paid, a roundtrip always
    # delivers less net stored energy than the alternative (A and B both idle,
    # or only B charging). The efficiency terms in the objective already
    # penalise this, but under edge-case numeric conditions the solver may
    # still produce a roundtripping schedule.
    #
    # A shared direction binary `bat_system_mode[t]` forces all batteries to
    # be in the same direction (1=charge, 0=discharge) per step. This adds
    # one binary variable per step — negligible MILP overhead at residential
    # scale — and enforces the physical principle that energy should not loop.
    #
    # When only one battery is present the constraint is unnecessary; the
    # per-device mode from add_variables is used unchanged.
    #
    # The same logic applies to EV chargers regardless of V2H capability.
    # For charge-only EVs the discharge bound is trivially non-binding, so
    # the shared variable is harmless but maintains uniform activation logic.
    if len(batteries) >= 2:
        bat_shared_mode = {
            t: ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
            for t in ctx.T
        }
        for bat in batteries:
            bat.set_external_mode(bat_shared_mode)

    if len(ev_devices) >= 2:
        ev_shared_mode = {
            t: ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
            for t in ctx.T
        }
        for ev in ev_devices:
            ev.set_external_mode(ev_shared_mode)

    # --- Add constraints ---
    grid.add_constraints(ctx, inputs=None)

    for bat in batteries:
        bat_inputs = bundle.battery_inputs.get(bat.name)
        if bat_inputs is None:
            raise ValueError(
                f"Battery {bat.name!r} appears in config but has no entry in "
                f"bundle.battery_inputs."
            )
        bat.add_constraints(ctx, inputs=bat_inputs)

    for pv in pv_devices:
        pv.add_constraints(
            ctx,
            inputs=PvInputs(forecast_kw=bundle.pv_forecast),
        )

    for ev in ev_devices:
        ev_inputs = bundle.ev_inputs.get(ev.name)
        if ev_inputs is None:
            raise ValueError(
                f"EV charger {ev.name!r} appears in config but has no entry in "
                f"bundle.ev_inputs."
            )
        ev.add_constraints(ctx, inputs=ev_inputs, solve_time_utc=bundle.solve_time_utc)

    for dl in deferrable_loads:
        window = bundle.deferrable_windows.get(dl.name)
        start_time = bundle.deferrable_start_times.get(dl.name)
        dl.add_constraints(
            ctx,
            window=window,
            solve_time_utc=bundle.solve_time_utc,
            start_time=start_time,
        )

    for sl in static_loads:
        sl.add_constraints(
            ctx,
            inputs=StaticLoadInputs(forecast_kw=bundle.base_load_forecast),
        )

    for hi in hybrid_inverters:
        hi_inputs = bundle.hybrid_inverter_inputs.get(hi.name)
        if hi_inputs is None:
            raise ValueError(
                f"Hybrid inverter {hi.name!r} appears in config but has no entry in "
                f"bundle.hybrid_inverter_inputs."
            )
        hi.add_constraints(ctx, inputs=hi_inputs)

    for tb in thermal_boilers:
        tb_inputs = bundle.thermal_boiler_inputs.get(tb.name)
        if tb_inputs is None:
            raise ValueError(
                f"Thermal boiler {tb.name!r} appears in config but has no entry in "
                f"bundle.thermal_boiler_inputs."
            )
        tb.add_constraints(ctx, inputs=tb_inputs)

    for sh in space_heating_hps:
        sh_inputs = bundle.space_heating_inputs.get(sh.name)
        if sh_inputs is None:
            raise ValueError(
                f"Space heating HP {sh.name!r} appears in config but has no entry in "
                f"bundle.space_heating_inputs."
            )
        sh.add_constraints(ctx, inputs=sh_inputs)

    for chp in combi_heat_pumps:
        chp_inputs = bundle.combi_hp_inputs.get(chp.name)
        if chp_inputs is None:
            raise ValueError(
                f"Combi heat pump {chp.name!r} appears in config but has no entry in "
                f"bundle.combi_hp_inputs."
            )
        chp.add_constraints(ctx, inputs=chp_inputs)

    # --- Power balance ---
    # At each time step, the sum of all device net_power contributions plus the
    # grid net_power must equal zero. This couples all devices: any surplus
    # production charges batteries or exports; any deficit draws from batteries
    # or imports from the grid.
    for t in ctx.T:
        device_net = sum(d.net_power(t) for d in all_devices)
        grid_net = grid.net_power(t)
        if isinstance(device_net, (int, float)):
            # All devices are forecast-only (no solver variables). The grid
            # must exactly offset the constant device net power.
            ctx.solver.add_constraint(grid_net == -device_net)
        else:
            ctx.solver.add_constraint(device_net + grid_net == 0)

    # --- Objective ---
    ObjectiveBuilder().build(ctx, all_devices, grid, bundle, config)

    # --- Log model size ---
    # Logged at DEBUG so it appears when the operator runs with --log-level DEBUG
    # but does not clutter INFO-level production output.
    # Binary variable count is the key complexity indicator: CBC's
    # branch-and-bound tree has worst-case depth proportional to num_int,
    # while num_cols and num_rows reflect total LP size.
    num_cols, num_rows, num_int, num_nz = ctx.solver.model_stats()
    logger.debug(
        "Model built: %d steps (%.0f h) | %d vars (%d binary) | "
        "%d constraints | %d non-zeros",
        horizon,
        horizon * dt,
        num_cols,
        num_int,
        num_rows,
        num_nz,
    )

    # --- Solve ---
    status = ctx.solver.solve(time_limit_seconds=59.0)

    if status == "infeasible":
        return SolveResult(
            strategy=bundle.strategy,
            objective_value=0.0,
            solve_status="infeasible",
            schedule=[],
        )

    obj_val = ctx.solver.objective_value()

    # --- Extract schedule ---
    # For each time step, the solver has chosen setpoints for every device. The
    # code below reads those values and assembles them into DeviceSetpoint and
    # ScheduleStep objects.
    #
    # Why closed-loop device variables are NOT suppressed for zero-exchange steps
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # The post-process arbitration layer (control_arbitration.py) will later mark
    # certain steps as ``zero_exchange_active=True`` for the selected enforcer
    # device. One might expect the solver variable for that device on that step
    # to be fixed at zero (since the hardware will act autonomously). Do not do
    # this. The reasons are:
    #
    # 1. SOC continuity: the battery or EV state-of-charge variable is threaded
    #    across all steps. If the solver variable for a device is suppressed on
    #    step t, the SOC equation for step t+1 becomes incorrect — the model
    #    thinks the device is idle but the hardware may be charging or discharging.
    #    Wrong SOC estimates on adjacent steps lead to incorrect dispatch on the
    #    steps immediately before and after the closed-loop window.
    #
    # 2. Best prediction: the solver's planned setpoint is the best available
    #    prediction of what the hardware will actually do in closed-loop mode.
    #    The hardware's firmware PID loop will chase zero exchange; the solver
    #    models the same goal via its economic objective. The planned setpoints
    #    are advisory; the hardware enforces the physical constraint autonomously.
    #
    # 3. Self-correction: if the hardware does not precisely track the solver's
    #    planned setpoint (expected — firmware loops are not perfect), the next
    #    solve cycle self-corrects automatically by reading the fresh SOC from
    #    MQTT and re-solving with the updated state.
    #
    # See IMPLEMENTATION_DETAILS.md §9 for the full design discussion.
    schedule: list[ScheduleStep] = []
    for t in ctx.T:
        device_setpoints: dict[str, DeviceSetpoint] = {}

        for bat in batteries:
            device_setpoints[bat.name] = DeviceSetpoint(
                kw=_eval_net_power(ctx, bat.net_power(t)),
                type="battery",
            )
        for pv in pv_devices:
            pv_kw = _eval_net_power(ctx, pv.net_power(t))
            caps = pv.config.capabilities
            # power_limit_kw: the production limit setpoint to send to the
            # inverter register. In staged mode this is the chosen stage kW
            # (the register value), not the effective clipped output. The
            # hardware must receive the stage value so it programs the correct
            # power level; the solar resource then determines actual AC output.
            # In continuous power_limit mode this equals the solver's chosen
            # curtailed output. In fixed mode (no capabilities) it is None.
            if pv.config.production_stages is not None:
                power_limit_kw = pv.chosen_stage_kw(t)
            else:
                power_limit_kw = pv_kw if caps.power_limit else None
            # zero_exchange_active: the discrete closed-loop mode register.
            # Initialised to False for devices that have the capability
            # configured, or None for devices that do not. The arbitration
            # engine (Plan 43) will set the final per-step value.
            zero_exchange_active = False if caps.zero_export else None
            # on_off_active: True when the solver chose to switch on the array
            # at this step; False when it chose to switch it off. None when the
            # device has no on/off capability configured.
            on_off_active = pv.is_on(t) if caps.on_off else None
            # pv_is_curtailed: True when mimirheim is actively limiting PV output
            # below what the solar resource could deliver. Published for all
            # controllable modes (staged, power_limit, on_off). None for fixed
            # mode arrays, where the forecast is always used as-is.
            is_controllable = (
                pv.config.production_stages is not None
                or caps.power_limit
                or caps.on_off
            )
            pv_is_curtailed = pv.is_curtailed(t) if is_controllable else None
            device_setpoints[pv.name] = DeviceSetpoint(
                kw=pv_kw,
                type="pv",
                power_limit_kw=power_limit_kw,
                zero_exchange_active=zero_exchange_active,
                on_off_active=on_off_active,
                pv_is_curtailed=pv_is_curtailed,
            )
        for ev in ev_devices:
            # zero_exchange_active: initialise to False (not in closed-loop mode)
            # for devices that have the capability register, or None for devices
            # that do not. The arbitration engine (Plan 43) will set the final
            # per-step value based on grid exchange and enforcer selection.
            ev_zea = False if ev.config.capabilities.zero_exchange else None
            # loadbalance_active: initialise to False for loadbalance-capable
            # devices, None otherwise.
            ev_lb = False if ev.config.capabilities.loadbalance else None
            device_setpoints[ev.name] = DeviceSetpoint(
                kw=_eval_net_power(ctx, ev.net_power(t)),
                type="ev_charger",
                zero_exchange_active=ev_zea,
                loadbalance_active=ev_lb,
            )
        for dl in deferrable_loads:
            device_setpoints[dl.name] = DeviceSetpoint(
                kw=_eval_net_power(ctx, dl.net_power(t)),
                type="deferrable_load",
            )
        for sl in static_loads:
            device_setpoints[sl.name] = DeviceSetpoint(
                kw=_eval_net_power(ctx, sl.net_power(t)),
                type="static_load",
            )
        for hi in hybrid_inverters:
            device_setpoints[hi.name] = DeviceSetpoint(
                kw=_eval_net_power(ctx, hi.net_power(t)),
                type="hybrid_inverter",
            )
        for tb in thermal_boilers:
            device_setpoints[tb.name] = DeviceSetpoint(
                kw=_eval_net_power(ctx, tb.net_power(t)),
                type="thermal_boiler",
            )
        for sh in space_heating_hps:
            device_setpoints[sh.name] = DeviceSetpoint(
                kw=_eval_net_power(ctx, sh.net_power(t)),
                type="space_heating_hp",
            )
        for chp in combi_heat_pumps:
            device_setpoints[chp.name] = DeviceSetpoint(
                kw=_eval_net_power(ctx, chp.net_power(t)),
                type="combi_heat_pump",
            )

        schedule.append(
            ScheduleStep(
                t=t,
                grid_import_kw=ctx.solver.var_value(grid.import_[t]),
                grid_export_kw=ctx.solver.var_value(grid.export_[t]),
                devices=device_setpoints,
            )
        )

    # Extract the solver-recommended start datetime for each deferrable load
    # that was in binary scheduling state (i.e. dl.start is populated).
    # We scan the completed schedule for the first step where the load's kw is
    # nonzero, then convert the step index to a wall-clock UTC datetime.
    # bundle.solve_time_utc is already the 15-minute slot boundary (floored by
    # ReadinessState.snapshot()), so it is used directly as the step origin.
    _step_td = timedelta(seconds=int(dt * 3600))

    deferrable_recommended_starts: dict[str, datetime] = {}
    for dl in deferrable_loads:
        if not dl.start:
            # Not in scheduling state — running, committed, or no window.
            continue
        for step in schedule:
            kw = step.devices.get(dl.name, DeviceSetpoint(kw=0.0, type="deferrable_load")).kw
            if abs(kw) > 1e-9:
                recommended_dt = bundle.solve_time_utc + _step_td * step.t
                deferrable_recommended_starts[dl.name] = recommended_dt
                break

    return SolveResult(
        strategy=bundle.strategy,
        objective_value=obj_val,
        solve_status=status,
        naive_cost_eur=_compute_naive_cost(bundle, horizon, dt),
        optimised_cost_eur=_compute_optimised_cost(bundle, schedule, dt),
        soc_credit_eur=_compute_soc_credit(bundle, schedule, config, dt),
        schedule=schedule,
        deferrable_recommended_starts=deferrable_recommended_starts,
    )


def _compute_naive_cost(bundle: SolveBundle, horizon: int, dt: float) -> float:
    """Compute the naive baseline cost in EUR over the horizon.

    The naive baseline represents operating without any storage dispatch:
    the household imports whatever PV cannot cover, and exports any PV surplus
    directly to the grid. No battery, EV, or deferrable load optimisation occurs.

    Formula for each step t:

        net_kw = base_load[t] - pv[t]

        if net_kw >= 0:
            cost += net_kw * import_price[t] * dt     # shortfall drawn from grid
        else:
            cost += net_kw * export_price[t] * dt     # surplus fed to grid
                                                       # net_kw is negative, so this
                                                       # subtracts (revenue) when
                                                       # export_price > 0, or adds
                                                       # (penalty) when export_price < 0

    The result may be negative when the average export revenue exceeds the
    import cost across the horizon.

    Args:
        bundle: Solve inputs providing forecasts and prices.
        horizon: Number of time steps in the horizon.
        dt: Step duration in hours (always 0.25 for 15-minute steps).

    Returns:
        Naive cost in EUR. Negative values indicate net export revenue.
    """
    total = 0.0
    for t in range(horizon):
        net_kw = bundle.base_load_forecast[t] - bundle.pv_forecast[t]
        if net_kw >= 0.0:
            # Load exceeds PV: import the shortfall from the grid.
            total += net_kw * bundle.horizon_prices[t] * dt
        else:
            # PV exceeds load: export the surplus to the grid.
            # net_kw is negative; multiplying by export_price and dt gives a
            # negative contribution (revenue) when export_price > 0, or a
            # positive contribution (cost) when export_price < 0.
            total += net_kw * bundle.horizon_export_prices[t] * dt
    return total


def _compute_optimised_cost(
    bundle: SolveBundle, schedule: list[ScheduleStep], dt: float
) -> float:
    """Compute the raw grid cash flow of the optimised schedule in EUR.

    Returns import cost minus export revenue across the horizon, with no
    adjustment for changes in stored energy. This is the number that appears
    on the electricity bill if storage were to remain frozen at its current
    state.

    Use ``soc_credit_eur`` to compare this figure fairly against
    ``naive_cost_eur``: ``effective_cost = optimised_cost_eur - soc_credit_eur``.

    Args:
        bundle: Solve inputs providing prices.
        schedule: Assembled schedule from the solver.
        dt: Step duration in hours (always 0.25 for 15-minute steps).

    Returns:
        Raw grid cash flow in EUR.
    """
    return sum(
        step.grid_import_kw * bundle.horizon_prices[step.t] * dt
        - step.grid_export_kw * bundle.horizon_export_prices[step.t] * dt
        for step in schedule
    )


def _avg_discharge_efficiency(
    segments: list | None,
    curve: list | None,
) -> float:
    """Return the power-weighted average discharge efficiency for a storage device.

    Used to convert a cell-energy SOC delta into its AC-side value when
    computing ``soc_credit_eur``: each kWh stored in the cell delivers
    ``avg_discharge_eff`` kWh of AC on the way out, which displaces that
    much grid import.

    Args:
        segments: List of ``EfficiencySegment`` from the stacked-segment
            model, or None.
        curve: List of ``EfficiencyBreakpoint`` from the SOS2 model, or None.

    Returns:
        A scalar efficiency in (0, 1]. Returns 1.0 when neither model is
        provided (charge-only devices or unconfigured EVs).
    """
    if segments:
        total_kw = sum(s.power_max_kw for s in segments)
        if total_kw == 0.0:
            return sum(s.efficiency for s in segments) / len(segments)
        return sum(s.efficiency * s.power_max_kw for s in segments) / total_kw
    if curve:
        return sum(bp.efficiency for bp in curve) / len(curve)
    return 1.0


def _compute_soc_credit(
    bundle: SolveBundle,
    schedule: list[ScheduleStep],
    config: MimirheimConfig,
    dt: float,
) -> float:
    """Compute the estimated future value of stored energy built up over the horizon.

    When the solver charges storage at cheap prices, the raw grid cash flow
    increases (import cost rises). This credit quantifies what that investment
    is worth: the net SOC increase at end-of-horizon, converted to its AC-side
    equivalent via discharge efficiency, valued at the average import price.

    Formula per storage device:

        delta_cell_kwh = terminal_soc - initial_soc
        soc_credit += avg_import_price * delta_cell_kwh * avg_discharge_eff

    The cell SOC delta is reconstructed from the schedule by accumulating the
    signed AC power at the inverter terminals. Negative AC kW means charging
    (energy flows into the inverter); the cell energy gained equals
    ``|ac_kw| * charge_eff * dt``. For simplicity and consistency with the
    objective function, the reconstruction uses the AC kW directly (treating
    total charge efficiency = 1 at the reconstruction stage) and applies only
    the discharge efficiency when pricing the credit. This is a slight
    overstatement of cell SOC, but the error is small relative to rounding.

    A positive credit means the horizon ends with more stored energy than it
    started with. Subtract from ``optimised_cost_eur`` for a fair comparison
    against ``naive_cost_eur``.

    Args:
        bundle: Solve inputs providing initial SOC per device and import prices.
        schedule: Assembled schedule from the solver.
        config: Static system configuration providing efficiency curves.
        dt: Step duration in hours (always 0.25 for 15-minute steps).

    Returns:
        Estimated future value of the net SOC change in EUR. May be negative
        when the horizon ends with less stored energy than it started with.
    """
    n = len(schedule)
    avg_import_price = sum(bundle.horizon_prices[t] for t in range(n)) / n

    credit = 0.0

    for name, inputs in bundle.battery_inputs.items():
        bat_cfg = config.batteries.get(name)
        discharge_eff = _avg_discharge_efficiency(
            bat_cfg.discharge_segments if bat_cfg else None,
            bat_cfg.discharge_efficiency_curve if bat_cfg else None,
        )
        # Reconstruct terminal SOC from the AC kW in the schedule.
        # Positive kw = discharge (SOC decreases); negative kw = charge.
        soc_delta = -sum(
            step.devices[name].kw * dt
            for step in schedule
            if name in step.devices
        )
        credit += avg_import_price * soc_delta * discharge_eff

    for name, inputs in bundle.ev_inputs.items():
        if not inputs.available:
            continue
        ev_cfg = config.ev_chargers.get(name)
        discharge_eff = _avg_discharge_efficiency(
            ev_cfg.discharge_segments if ev_cfg else None,
            None,  # EvConfig uses only the segments model; no SOS2 curve
        )
        soc_delta = -sum(
            step.devices[name].kw * dt
            for step in schedule
            if name in step.devices
        )
        credit += avg_import_price * soc_delta * discharge_eff

    return credit


def _round4(v: float) -> float:
    """Round a float to 4 decimal places and clamp sub-micro values to zero.

    Solver outputs routinely contain floating-point residuals such as
    -1.3e-12 that represent exact zero in physical terms. Any value whose
    absolute magnitude is smaller than 1e-6 (0.001 W, well below instrument
    noise) is clamped to 0.0. The remaining values are rounded to 4 decimal
    places, giving 0.1 W resolution (0.0001 kW).

    Args:
        v: The raw float from the solver.

    Returns:
        A rounded, noise-free float suitable for human-readable output.
    """
    if abs(v) < 1e-6:
        return 0.0
    return round(v, 4)


def debug_dump(
    bundle: SolveBundle,
    result: SolveResult,
    config: MimirheimConfig,
    dump_dir: Path | None,
    max_dumps: int,
) -> tuple[Path, Path] | None:
    """Write a debug dump of the solve inputs and outputs, if configured.

    This function is called by the solve loop in ``__main__`` after
    ``build_and_solve`` returns. It is **not** called from inside
    ``build_and_solve`` itself — doing so would break the purity guarantee.

    Files are only written when ``dump_dir`` is not None. The caller
    (``__main__``) is responsible for only calling this function when debug
    mode is enabled — ``config.debug.enabled`` controls both the log level
    and whether dumps are produced.

    The input file contains all bundle fields plus a top-level ``config`` key
    holding the static configuration with the ``mqtt`` section omitted.
    The ``mqtt`` section is excluded because it may contain credentials
    (``username``, ``password``) and connection details that have no analytical
    value. The config is embedded so that analysis tools (``analyse_dump.py``)
    have access to device parameters such as ``capacity_kwh`` and
    ``min_soc_kwh`` without requiring a separate config file.

    The output file is a post-processed version of ``SolveResult`` with:

    - Null device fields (``power_limit_kw``, ``zero_exchange_active``, ``loadbalance_active``) omitted.
    - Per-step import and export prices added (from the corresponding bundle
      horizon arrays), so the output file is self-contained for analysis.
    - All float values rounded to 4 decimal places (0.1 W resolution).
    - Sub-1e-6 solver residuals (e.g. ``-1.3e-12``) clamped to 0.0.
    - Step index ``t`` replaced by the UTC datetime string for that step.

    These transformations are applied only to the dump file. The live
    ``SolveResult`` object passed to the MQTT publisher is not modified.

    Args:
        bundle: The solve inputs; written as ``<ts>_input.json``.
        result: The solve outputs; written as ``<ts>_output.json``.
        config: The static mimirheim configuration. The ``mqtt`` section is
            excluded from the dump to avoid writing credentials to disk.
        dump_dir: Directory to write dumps into. None disables all dumping.
        max_dumps: Maximum number of dump pairs to retain. Older files are
            deleted when the limit is exceeded. 0 means unlimited.

    Returns:
        A tuple ``(input_path, output_path)`` for the files written, or
        ``None`` if ``dump_dir`` is ``None`` and no files were written.
    """
    if dump_dir is None:
        return None

    dump_dir.mkdir(parents=True, exist_ok=True)
    # Use the moment the trigger was received (bundle.triggered_at_utc) as the
    # filename timestamp. This is the wall-clock time the solve was initiated,
    # not the post-solve write time. Two solves in the same 15-minute slot
    # therefore get distinct filenames. Falls back to datetime.now() only when
    # triggered_at_utc is absent (e.g. test bundles).
    ts = (bundle.triggered_at_utc or datetime.now(timezone.utc)).strftime(
        "%Y-%m-%dT%H-%M-%SZ"
    )

    # Input file: bundle fields at top level, config embedded under "config".
    # The mqtt section is excluded: it may contain credentials and is not
    # needed for post-hoc analysis.
    input_dict = json.loads(bundle.model_dump_json(exclude_none=True))
    input_dict["config"] = config.model_dump(
        exclude={"mqtt": True}, exclude_none=True
    )
    (dump_dir / f"{ts}_input.json").write_text(
        json.dumps(input_dict, indent=2, default=str)
    )

    # Output file: post-processed for human readability.
    # bundle.solve_time_utc is the 15-minute slot boundary (floored by
    # ReadinessState.snapshot()), so step datetimes are derived directly from it.
    step_seconds = 15 * 60

    steps = []
    for step in result.schedule:
        t_dt = bundle.solve_time_utc + timedelta(seconds=step.t * step_seconds)

        devices: dict = {}
        for name, sp in step.devices.items():
            entry: dict = {
                "kw": _round4(sp.kw),
                "type": sp.type,
            }
            # Omit null-only fields; include only when present.
            if sp.power_limit_kw is not None:
                entry["power_limit_kw"] = _round4(sp.power_limit_kw)
            if sp.zero_exchange_active is not None:
                entry["zero_exchange_active"] = sp.zero_exchange_active
            if sp.on_off_active is not None:
                entry["on_off_active"] = sp.on_off_active
            if sp.loadbalance_active is not None:
                entry["loadbalance_active"] = sp.loadbalance_active
            devices[name] = entry

        steps.append({
            "t": t_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "import_price_eur_per_kwh": round(bundle.horizon_prices[step.t], 6),
            "export_price_eur_per_kwh": round(bundle.horizon_export_prices[step.t], 6),
            "grid_import_kw": _round4(step.grid_import_kw),
            "grid_export_kw": _round4(step.grid_export_kw),
            "devices": devices,
        })

    out_dict = {
        "strategy": result.strategy,
        "solve_status": result.solve_status,
        "objective_value": round(result.objective_value, 6),
        "dispatch_suppressed": result.dispatch_suppressed,
        "naive_cost_eur": round(result.naive_cost_eur, 6),
        "optimised_cost_eur": round(result.optimised_cost_eur, 6),
        "soc_credit_eur": round(result.soc_credit_eur, 6),
        "schedule": steps,
    }

    (dump_dir / f"{ts}_output.json").write_text(
        json.dumps(out_dict, indent=2)
    )

    _rotate_dumps(dump_dir, max_dumps)
    logger.debug("Solve dump written to %s/%s_*.json", dump_dir, ts)
    return (dump_dir / f"{ts}_input.json", dump_dir / f"{ts}_output.json")


def _rotate_dumps(dump_dir: Path, max_dumps: int) -> None:
    """Delete the oldest dump file pairs if the pair count exceeds max_dumps.

    Each solve produces two files (``*_input.json`` and ``*_output.json``).
    ``max_dumps`` is the maximum number of *pairs* to retain. Files are grouped
    by the timestamp prefix that precedes ``_input`` or ``_output``, so a pair
    is always deleted together.

    Args:
        dump_dir: Directory containing dump files.
        max_dumps: Maximum number of pairs to retain. 0 means unlimited.
    """
    if max_dumps <= 0:
        return
    # Collect unique timestamp prefixes, sorted oldest first.
    prefixes: list[str] = []
    seen: set[str] = set()
    for f in sorted(dump_dir.glob("*.json")):
        for suffix in ("_input.json", "_output.json"):
            if f.name.endswith(suffix):
                prefix = f.name[: -len(suffix)]
                if prefix not in seen:
                    seen.add(prefix)
                    prefixes.append(prefix)
                break
    for old_prefix in prefixes[:-max_dumps]:
        for suffix in ("_input.json", "_output.json"):
            (dump_dir / f"{old_prefix}{suffix}").unlink(missing_ok=True)
