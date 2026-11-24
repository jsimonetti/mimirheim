"""Arbitration engine for closed-loop enforcer selection.

This module decides, for each time step in a solved schedule, which single
device (if any) should act as the closed-loop zero-exchange enforcer. Exactly
one active enforcer per step is the rule: multiple devices running closed-loop
firmware simultaneously on the same AC bus leads to controller oscillation.

What this module does:

- Iterates over all steps in a SolveResult.
- For near-zero-exchange steps, scores all eligible candidates and selects one.
- Excludes a battery that cannot regulate in both directions around zero,
  judged on its start- and end-of-step state of charge (a battery idle or
  charging at the top of its range has no absorb headroom), or whose step is
  inside its battery-care hold.
- Excludes a hybrid inverter whose battery is near full or near empty.
- For non-zero-exchange steps, clears all closed-loop flags (no enforcer needed).
- Sets ``DeviceSetpoint.zero_exchange_active`` and
  ``DeviceSetpoint.loadbalance_active`` as appropriate.
- Applies hysteresis (switch_delta) and minimum dwell to prevent rapid switching.

What this module does not do:

- It does not run the MILP solver or call build_and_solve.
- It does not publish to MQTT or perform any I/O.
- It does not import from mimirheim.io.
- It does not retain state across solve cycles. Hysteresis and dwell operate
  within a single call only. Each call to assign_control_authority is
  independent and starts with no retained enforcer.
"""

import logging
from dataclasses import dataclass

from mimirheim.config.schema import BatteryConfig, EvConfig, HybridInverterConfig, MimirheimConfig
from mimirheim.devices.derating import charge_ceiling_line, discharge_ceiling_line
from mimirheim.core.bundle import (
    BatteryCareStatus,
    DeviceSetpoint,
    ScheduleStep,
    SolveBundle,
    SolveResult,
)

logger = logging.getLogger("mimirheim.control_arbitration")

# Type-priority constants used as the final tiebreak level. Higher number wins
# in the descending sort. Battery has the widest regulation bandwidth; PV
# curtailment is always the last resort because it forfeits free generation.
_TYPE_PRIORITY: dict[str, int] = {"battery": 3, "hybrid_inverter": 2, "ev_charger": 2, "pv": 1}

# Length of one schedule step in hours. The horizon uses fixed 15-minute steps.
_STEP_HOURS: float = 0.25

# Smallest SoC tolerance at full and empty, as a fraction of capacity_kwh
# (0.1 %). It absorbs solver rounding on a planned arrival at either bound
# when headroom_margin_kw is 0; see _bound_tolerance_kwh.
_BOUND_TOLERANCE_FRACTION: float = 0.001

# Battery power in kW at or below which a step counts as idle. Solver output
# carries small rounding noise; this stops such noise being read as a planned
# charge or discharge that would lend a full or empty battery headroom.
_IDLE_KW: float = 0.05


@dataclass
class _Candidate:
    """Scoring bundle for one device on one step.

    Attributes:
        name: Device name key in the config maps and schedule devices dict.
        sp_type: Device type string from the DeviceSetpoint.
        efficiency_score: Efficiency at the expected compensation power.
            For PV this is always 0.0.
        headroom_margin: Absorption headroom minus expected compensation.
            Larger means more slack for real-time disturbance tracking.
        wear_penalty: Negated wear cost, so that lower wear cost produces
            a higher value in a descending comparison.
        type_priority: _TYPE_PRIORITY value for this device type.
        name_key: Device name, used as the final deterministic tiebreak.
            max() on strings is lexicographically largest; we accept whichever
            end of the alphabet wins — the test only requires determinism.
    """

    name: str
    sp_type: str
    efficiency_score: float
    headroom_margin: float
    wear_penalty: float
    type_priority: int
    name_key: str

    def score_tuple(self) -> tuple:
        """Return the comparison tuple used for enforcer selection.

        All levels are in descending order: larger tuple value wins.
        Levels are only consulted when higher levels produce a tie.
        """
        return (
            self.efficiency_score,   # level 1: efficiency at expected compensation
            self.headroom_margin,    # level 2: headroom − expected_compensation
            self.wear_penalty,       # level 3: −wear_cost (lower cost → higher value)
            self.type_priority,      # level 4a: battery > ev > pv
            self.name_key,           # level 4b: lexicographic tiebreak
        )


def _max_charge_kw(name: str, sp_type: str, config: MimirheimConfig) -> float:
    """Return the maximum charge power in kW for a closed-loop-capable device.

    For batteries this is the sum of all charge segment upper bounds (stacked
    segment model), or the last breakpoint power (SOS2 curve model). For EVs
    it is always the segment sum. For PV, no charging concept applies and 0.0
    is returned.

    Args:
        name: Device name key in the config maps.
        sp_type: Device type string (e.g. "battery", "ev_charger", "pv").
        config: Static system configuration.

    Returns:
        Maximum charge power in kW, or 0.0 if not applicable.
    """
    if sp_type == "battery":
        bat_cfg: BatteryConfig | None = config.batteries.get(name)
        if bat_cfg is None:
            return 0.0
        return _max_charge_kw_battery(bat_cfg)
    if sp_type == "ev_charger":
        ev_cfg: EvConfig | None = config.ev_chargers.get(name)
        if ev_cfg is None:
            return 0.0
        return sum(s.power_max_kw for s in ev_cfg.charge_segments)
    if sp_type == "hybrid_inverter":
        hi_cfg = config.hybrid_inverters.get(name)
        if hi_cfg is None:
            return 0.0
        # The DeviceSetpoint.kw for a hybrid inverter is net AC power. The
        # maximum AC-side import (charge) power is the DC-bus charge limit
        # divided by the inverter efficiency (AC → DC conversion loss).
        return hi_cfg.max_charge_kw / hi_cfg.inverter_efficiency
    return 0.0


def _max_discharge_kw(bat_cfg: BatteryConfig) -> float:
    """Return the maximum discharge power of a battery in kW.

    Mirror of ``_max_charge_kw`` for the discharge direction: the sum of the
    discharge segment upper bounds, or the last breakpoint of the discharge
    efficiency curve.

    Args:
        bat_cfg: Static battery configuration.

    Returns:
        Maximum discharge power in kW, or 0.0 if neither model is configured.
    """
    if bat_cfg.discharge_efficiency_curve is not None:
        return bat_cfg.discharge_efficiency_curve[-1].power_kw
    if bat_cfg.discharge_segments is not None:
        return sum(s.power_max_kw for s in bat_cfg.discharge_segments)
    return 0.0


def _ceiling_kw(
    line: tuple[float, float] | None,
    max_kw: float,
    low_kw: float,
    soc_kwh: float,
) -> float:
    """Evaluate a derating line at a start-of-step SoC, clamped to ``[low_kw, max_kw]``.

    The line comes from ``mimirheim.devices.derating``, the same line the
    device constrains the solver with (see IMPLEMENTATION_DETAILS.md §8,
    subsection "Power derating near SOC extremes"). Outside the derating band
    the line exceeds ``max_kw`` and the ceiling is ``max_kw``.

    Args:
        line: ``(slope, intercept)``, or None when derating is not configured.
        max_kw: Maximum power in kW.
        low_kw: Lowest ceiling in kW. A battery passes its derating end point
            (its schema keeps it below ``max_kw``), so the ceiling stays there
            beyond capacity; a hybrid inverter passes 0, matching its solver
            constraint, whose end point may lie above ``max_kw``.
        soc_kwh: Start-of-step state of charge in kWh.

    Returns:
        Power limit in kW. Non-negative.
    """
    if line is None:
        return max_kw
    return max(0.0, min(max_kw, max(low_kw, line[0] * soc_kwh + line[1])))


def _charge_limit_kw(bat_cfg: BatteryConfig, soc_kwh: float) -> float:
    """Return a battery's AC charge power limit in kW at the given SoC.

    Args:
        bat_cfg: Static battery configuration.
        soc_kwh: Start-of-step state of charge in kWh.

    Returns:
        Charge power limit in kW, derated near full when configured.
    """
    max_kw = _max_charge_kw_battery(bat_cfg)
    line = charge_ceiling_line(
        max_kw, bat_cfg.capacity_kwh, bat_cfg.reduce_charge_above_soc_kwh, bat_cfg.reduce_charge_min_kw
    )
    return _ceiling_kw(line, max_kw, bat_cfg.reduce_charge_min_kw or 0.0, soc_kwh)


def _discharge_limit_kw(bat_cfg: BatteryConfig, soc_kwh: float) -> float:
    """Return a battery's AC discharge power limit in kW at the given SoC.

    Args:
        bat_cfg: Static battery configuration.
        soc_kwh: Start-of-step state of charge in kWh.

    Returns:
        Discharge power limit in kW, derated near empty when configured.
    """
    max_kw = _max_discharge_kw(bat_cfg)
    line = discharge_ceiling_line(
        max_kw, bat_cfg.min_soc_kwh, bat_cfg.reduce_discharge_below_soc_kwh, bat_cfg.reduce_discharge_min_kw
    )
    return _ceiling_kw(line, max_kw, bat_cfg.reduce_discharge_min_kw or 0.0, soc_kwh)


def _hybrid_charge_limit_ac_kw(hi_cfg: HybridInverterConfig, soc_kwh: float) -> float:
    """Return a hybrid inverter's AC-side charge power limit in kW at the given SoC.

    The DC charge ceiling (derated near full when configured) divided by the
    inverter efficiency, as ``_max_charge_kw`` does for the underated limit.

    Args:
        hi_cfg: Static hybrid inverter configuration.
        soc_kwh: Start-of-step state of charge in kWh.

    Returns:
        AC charge power limit in kW.
    """
    line = charge_ceiling_line(
        hi_cfg.max_charge_kw, hi_cfg.capacity_kwh, hi_cfg.reduce_charge_above_soc_kwh, hi_cfg.reduce_charge_min_kw
    )
    dc_kw = _ceiling_kw(line, hi_cfg.max_charge_kw, 0.0, soc_kwh)
    return dc_kw / hi_cfg.inverter_efficiency


def _max_charge_kw_battery(bat_cfg: BatteryConfig) -> float:
    """Return the maximum charge power of a battery in kW.

    Args:
        bat_cfg: Static battery configuration.

    Returns:
        Last charge curve breakpoint, or the sum of the charge segment upper
        bounds, or 0.0 if neither model is configured.
    """
    if bat_cfg.charge_efficiency_curve is not None:
        return bat_cfg.charge_efficiency_curve[-1].power_kw
    if bat_cfg.charge_segments is not None:
        return sum(s.power_max_kw for s in bat_cfg.charge_segments)
    return 0.0


def _soc_trajectory(
    name: str,
    measured_kwh: float | None,
    capacity_kwh: float,
    schedule: list[ScheduleStep],
    fill_from_power: bool,
) -> list[tuple[float | None, float | None]]:
    """Return the (start, end) state of charge of one storage device for every step.

    ``DeviceSetpoint.soc_kwh`` is the state of charge at the end of a step, so
    the start of a step is the end of the previous one, and the start of the
    first step is the measured SoC.

    When a step carries no ``soc_kwh`` (a dispatch-suppressed schedule zeroes
    storage and drops the SoC), the end is estimated from the start:

    - Battery (``fill_from_power``): the start minus the planned power over
      one step, clamped to ``[0, capacity_kwh]`` (not to ``min_soc_kwh``, so a
      measured SoC below the minimum is not moved up). Efficiency losses are
      ignored; the result only needs to locate the battery relative to full
      and empty.
    - Hybrid inverter: the start, unchanged. Its net AC power includes its own
      PV, so it says nothing about the battery; carrying the SoC forward keeps
      a full or empty hybrid gated.

    Args:
        name: Device name in the schedule devices.
        measured_kwh: Measured SoC at the start of the first step, or None.
        capacity_kwh: Usable capacity in kWh.
        schedule: The solved schedule.
        fill_from_power: Estimate a missing end SoC from planned power.

    Returns:
        One ``(start_kwh, end_kwh)`` pair per schedule step. Either value is
        None when it cannot be determined (no measured SoC and no published
        SoC so far).
    """
    start = measured_kwh
    trajectory: list[tuple[float | None, float | None]] = []
    for step in schedule:
        sp = step.devices.get(name)
        if sp is not None and sp.soc_kwh is not None:
            end: float | None = sp.soc_kwh
        elif start is not None and fill_from_power:
            # kw > 0 is discharge, so SoC falls by kw * step length.
            kw = sp.kw if sp is not None else 0.0
            end = min(capacity_kwh, max(0.0, start - kw * _STEP_HOURS))
        else:
            end = start
        trajectory.append((start, end))
        start = end
    return trajectory


def _in_care_hold(t: int, care: BatteryCareStatus | None) -> bool:
    """Return True if horizon step ``t`` lies inside the battery-care hold.

    The hold covers ``enforced_step`` through ``enforced_step +
    enforced_hold_steps`` inclusive, by horizon step index ``t``. It applies
    only when a target was actually enforced. Closed-loop regulation drops the
    planned target, so the hold would be drained by the house load.

    Args:
        t: Horizon step index of the schedule step.
        care: Battery-care status of the battery, or None.

    Returns:
        True if the step is part of the enforced hold.
    """
    if care is None or care.enforced_step is None:
        return False
    if care.enforced_target_kwh is None or care.enforced_target_kwh <= 0.0:
        return False
    last = care.enforced_step + (care.enforced_hold_steps or 0)
    return care.enforced_step <= t <= last


def _effective_floor_kwh(
    bat_cfg: BatteryConfig,
    care: BatteryCareStatus | None,
    measured_kwh: float | None,
) -> float:
    """Return the lowest SoC in kWh the plan lets the battery reach.

    That is ``min_soc_kwh``, raised by the battery-care ratchet floor. The
    solver applies the floor as ``soc >= min(floor_kwh, measured SoC)`` (see
    ``Battery._add_care_constraints``), so a battery that starts below the
    floor is held where it is. A closed-loop controller would discharge below
    this level to cover house load and undo the floor.

    Args:
        bat_cfg: Static battery configuration.
        care: Battery-care status of the battery, or None.
        measured_kwh: Measured SoC from ``bundle.battery_inputs``, or None.

    Returns:
        Effective lower SoC bound in kWh.
    """
    if care is None or care.floor_kwh <= 0.0 or measured_kwh is None:
        return bat_cfg.min_soc_kwh
    return max(bat_cfg.min_soc_kwh, min(care.floor_kwh, measured_kwh))


def _bound_tolerance_kwh(capacity_kwh: float, headroom_margin_kw: float) -> float:
    """Return how close to full or empty, in kWh, counts as at the bound.

    The energy to sustain ``headroom_margin_kw`` for one step, and at least
    0.1 % of capacity, so solver rounding does not make a full or empty
    battery eligible when the margin is 0. This is the only SoC tolerance in
    this module. See IMPLEMENTATION_DETAILS.md §9, subsection "Battery
    eligibility".

    Args:
        capacity_kwh: Usable capacity in kWh.
        headroom_margin_kw: ``control.headroom_margin_kw``.

    Returns:
        Tolerance in kWh.
    """
    return max(headroom_margin_kw * _STEP_HOURS, capacity_kwh * _BOUND_TOLERANCE_FRACTION)


def _near_bounds(
    start_kwh: float,
    end_kwh: float | None,
    floor_kwh: float,
    capacity_kwh: float,
    tolerance_kwh: float,
) -> tuple[bool, bool]:
    """Return whether the step is near full and whether it is near empty.

    The start- or end-of-step SoC within ``tolerance_kwh`` of ``capacity_kwh``
    (or of ``floor_kwh``) counts. Checking the end catches a planned charge or
    discharge that already uses the remaining room during the step.

    Args:
        start_kwh: Start-of-step SoC in kWh.
        end_kwh: End-of-step SoC in kWh, or None if unknown.
        floor_kwh: Effective lower SoC bound in kWh.
        capacity_kwh: Usable capacity in kWh.
        tolerance_kwh: From ``_bound_tolerance_kwh``.

    Returns:
        ``(near_full, near_empty)``.
    """
    socs = [s for s in (start_kwh, end_kwh) if s is not None]
    near_full = any(s >= capacity_kwh - tolerance_kwh for s in socs)
    near_empty = any(s <= floor_kwh + tolerance_kwh for s in socs)
    return near_full, near_empty


def _battery_headroom(
    kw: float,
    start_kwh: float,
    end_kwh: float | None,
    floor_kwh: float,
    bat_cfg: BatteryConfig,
    headroom_margin_kw: float,
) -> tuple[float, float]:
    """Return the battery's absorb and supply headroom in kW for one step.

    The forecast error an enforcer corrects has no known sign, so both
    directions matter:

    - absorb = charge limit (zero when near full) - planned charge + planned discharge
    - supply = discharge limit (zero when near empty) - planned discharge + planned charge

    A battery at the top that is idle or charging therefore has no absorb
    headroom: the plan holds it there. One that is discharging can still
    absorb by discharging less. See IMPLEMENTATION_DETAILS.md §9, subsection
    "Battery eligibility".

    Args:
        kw: Planned battery power in kW (positive is discharge). Power within
            ``_IDLE_KW`` of zero is solver noise and counts as zero, so it
            cannot lend a full or empty battery headroom.
        start_kwh: Start-of-step SoC in kWh.
        end_kwh: End-of-step SoC in kWh, or None if unknown.
        floor_kwh: Effective lower SoC bound, from ``_effective_floor_kwh``.
        bat_cfg: Static battery configuration.
        headroom_margin_kw: ``control.headroom_margin_kw``.

    Returns:
        ``(absorb_kw, supply_kw)``. Either may be zero.
    """
    if abs(kw) <= _IDLE_KW:
        kw = 0.0
    charge_kw = -kw if kw < 0.0 else 0.0
    discharge_kw = kw if kw > 0.0 else 0.0
    tolerance = _bound_tolerance_kwh(bat_cfg.capacity_kwh, headroom_margin_kw)
    near_full, near_empty = _near_bounds(start_kwh, end_kwh, floor_kwh, bat_cfg.capacity_kwh, tolerance)
    absorb = (0.0 if near_full else _charge_limit_kw(bat_cfg, start_kwh)) - charge_kw + discharge_kw
    supply = (0.0 if near_empty else _discharge_limit_kw(bat_cfg, start_kwh)) - discharge_kw + charge_kw
    return max(0.0, absorb), max(0.0, supply)


def _storage_step_state(
    step: ScheduleStep,
    index: int,
    trajectories: dict[str, list[tuple[float | None, float | None]]],
    floors: dict[str, float],
    battery_care: dict[str, BatteryCareStatus],
    config: MimirheimConfig,
) -> tuple[frozenset[str], dict[str, float]]:
    """Decide which zero-exchange batteries and hybrid inverters may enforce on this step.

    A battery is excluded when the step is inside its battery-care hold, or
    when its absorb or supply headroom is below ``headroom_margin_kw`` (or
    zero, so that a margin of 0 still excludes a full or empty battery).

    A hybrid inverter is excluded when its battery is near full or near empty.
    Its net AC power includes its own PV, so its supply headroom cannot be read
    from the schedule; the gate is conservative instead. Its absorb headroom
    uses the derated charge ceiling.

    When the start SoC is unknown the published end SoC stands in for it; with
    neither, the SoC rules are skipped, which keeps the previous behaviour for
    callers that supply no storage state.

    Args:
        step: The schedule step.
        index: Position of the step in the schedule.
        trajectories: Per-device (start, end) SoC, from ``_soc_trajectory``.
        floors: Per-device lower SoC bound: ``_effective_floor_kwh`` for
            batteries, ``min_soc_kwh`` for hybrid inverters.
        battery_care: ``SolveResult.battery_care``.
        config: Static system configuration.

    Returns:
        The names to drop from the candidates, and the SoC-aware absorb
        headroom of every device whose start or end SoC is known. The
        candidate builder scores those devices on that headroom instead of the
        configured maximum charge power.
    """
    margin = config.control.headroom_margin_kw
    excluded: set[str] = set()
    absorb_by_name: dict[str, float] = {}
    for name, trajectory in trajectories.items():
        sp = step.devices.get(name)
        if sp is None:
            continue
        start_kwh, end_kwh = trajectory[index]
        # Without a measured start SoC, the step's published end SoC is the
        # best available reference for full and empty.
        reference_kwh = start_kwh if start_kwh is not None else end_kwh
        hi_cfg = config.hybrid_inverters.get(name)
        if hi_cfg is not None:
            if reference_kwh is None:
                continue
            tolerance = _bound_tolerance_kwh(hi_cfg.capacity_kwh, margin)
            if any(_near_bounds(reference_kwh, end_kwh, floors[name], hi_cfg.capacity_kwh, tolerance)):
                excluded.add(name)
                continue
            charge_kw = -sp.kw if sp.kw < 0.0 else 0.0
            discharge_kw = sp.kw if sp.kw > 0.0 else 0.0
            absorb_by_name[name] = max(
                0.0, _hybrid_charge_limit_ac_kw(hi_cfg, reference_kwh) - charge_kw + discharge_kw
            )
            continue
        bat_cfg = config.batteries[name]
        if reference_kwh is not None:
            absorb, supply = _battery_headroom(sp.kw, reference_kwh, end_kwh, floors[name], bat_cfg, margin)
            absorb_by_name[name] = absorb
            weakest = min(absorb, supply)
            if weakest < margin or weakest <= 0.0:
                excluded.add(name)
        if _in_care_hold(step.t, battery_care.get(name)):
            excluded.add(name)
    return frozenset(excluded), absorb_by_name


def _absorption_headroom(name: str, sp: DeviceSetpoint, config: MimirheimConfig) -> float:
    """Compute absorption headroom for a closed-loop-capable device at its current setpoint.

    Headroom measures how much additional power the device can absorb from the
    AC bus relative to its current operating point. It is used to filter out
    devices that cannot meaningfully respond to grid disturbances.

    For a battery or hybrid inverter with a known SoC this value is not used:
    the SoC-aware absorb headroom from ``_storage_step_state`` replaces it. The
    formulas below apply only when no SoC is known.

    For batteries and EVs:
        headroom = max_charge_kw - actual_charge_kw + actual_discharge_kw
      The device can absorb more by ramping charge up toward max_charge_kw, or
      by ramping discharge down toward zero. Both contribute to total range.

    For PV:
        headroom = actual_production_kw
      The inverter's zero-export firmware clamps production downward; the full
      amount currently produced can be absorbed by curtailing to zero.

    Args:
        name: Device name key in the config maps.
        sp: The device's scheduled setpoint for this time step.
        config: Static system configuration.

    Returns:
        Absorption headroom in kW. Non-negative.
    """
    if sp.type == "pv":
        return max(0.0, sp.kw)
    # kw > 0 means the device is discharging (producing to AC bus).
    # kw < 0 means the device is charging (consuming from AC bus).
    charge_kw = -sp.kw if sp.kw < 0.0 else 0.0
    discharge_kw = sp.kw if sp.kw > 0.0 else 0.0
    max_chg = _max_charge_kw(name, sp.type, config)
    return max(0.0, max_chg - charge_kw + discharge_kw)


def _efficiency_at_power(name: str, sp_type: str, power_kw: float, config: MimirheimConfig) -> float:
    """Return the expected efficiency for a device operating at the given power level.

    For PV, always returns 0.0. PV curtailment forfeits free generation and is
    the last resort in enforcer selection regardless of power level.

    For batteries with the stacked-segment model: identifies the segment that
    ``power_kw`` falls into by cumulative ``power_max_kw`` threshold. If
    ``power_kw`` is zero or the power is at the boundary between segments, the
    first segment matching the level is used. If ``power_kw`` exceeds total
    capacity, the last (least efficient) segment is used.

    For batteries with the SOS2 piecewise-linear model: linearly interpolates
    efficiency between the two adjacent breakpoints that bracket ``power_kw``.

    For EVs: same as the battery stacked-segment logic (EVs only have one model).

    Args:
        name: Device name key in the config maps.
        sp_type: Device type string (e.g. "battery", "ev_charger", "pv").
        power_kw: Expected compensation power in kW. Non-negative.
        config: Static system configuration.

    Returns:
        Efficiency score in the range (0.0, 1.0]. Returns 0.0 for PV.
    """
    if sp_type == "pv":
        return 0.0

    if sp_type == "battery":
        bat_cfg = config.batteries.get(name)
        if bat_cfg is None:
            return 0.0
        if bat_cfg.charge_efficiency_curve is not None:
            # SOS2 model: linear interpolation between adjacent breakpoints.
            curve = bat_cfg.charge_efficiency_curve
            if power_kw <= curve[0].power_kw:
                return curve[0].efficiency
            if power_kw >= curve[-1].power_kw:
                return curve[-1].efficiency
            for i in range(len(curve) - 1):
                p0, p1 = curve[i].power_kw, curve[i + 1].power_kw
                if p0 <= power_kw <= p1:
                    frac = (power_kw - p0) / (p1 - p0)
                    return curve[i].efficiency + frac * (curve[i + 1].efficiency - curve[i].efficiency)
            return curve[-1].efficiency
        if bat_cfg.charge_segments is not None:
            # Stacked-segment model: find the segment that power_kw falls into.
            cumulative = 0.0
            for seg in bat_cfg.charge_segments:
                cumulative += seg.power_max_kw
                if power_kw <= cumulative:
                    return seg.efficiency
            # Exceeds total capacity: use the last segment's efficiency.
            return bat_cfg.charge_segments[-1].efficiency
        return 0.0

    if sp_type == "ev_charger":
        ev_cfg = config.ev_chargers.get(name)
        if ev_cfg is None:
            return 0.0
        cumulative = 0.0
        for seg in ev_cfg.charge_segments:
            cumulative += seg.power_max_kw
            if power_kw <= cumulative:
                return seg.efficiency
        return ev_cfg.charge_segments[-1].efficiency

    if sp_type == "hybrid_inverter":
        hi_cfg = config.hybrid_inverters.get(name)
        if hi_cfg is None:
            return 0.0
        # A hybrid inverter operates at a single inverter efficiency regardless
        # of power level (no per-segment curve). Return the flat efficiency.
        return hi_cfg.inverter_efficiency

    return 0.0


def _is_near_zero_exchange(
    step: ScheduleStep, epsilon: float
) -> bool:
    """Return True if both grid import and export are below the exchange epsilon.

    The two quantities are mutually exclusive in the solver's power balance
    (exactly one is nonzero per step, or both are zero at the deadband
    boundary). Using both guards against floating-point representation where
    one is slightly above zero due to solver tolerance.

    Args:
        step: The schedule step to test.
        epsilon: Exchange threshold in kW from ControlConfig.exchange_epsilon_kw.

    Returns:
        True if the step is near-zero-exchange.
    """
    return step.grid_import_kw <= epsilon and step.grid_export_kw <= epsilon


def _build_candidates(
    step: ScheduleStep,
    expected_compensation_kw: float,
    zex_capable: frozenset[str],
    ev_available: dict[str, bool],
    config: MimirheimConfig,
    headroom_margin: float,
    excluded: frozenset[str] = frozenset(),
    headroom_override: dict[str, float] | None = None,
) -> list[_Candidate]:
    """Build a list of eligible candidates for enforcer selection on one step.

    A device is eligible when all of the following hold:

    1. It is in ``zex_capable`` (has the capability flag and output topic set).
    2. Its ``zero_exchange_active`` field is not None in the schedule (capability
       is present in the schema — this is always True for devices in zex_capable).
    3. For EV devices: ``ev_available[name]`` is True (vehicle is plugged in).
    4. Its absorption headroom is >= ``headroom_margin``.
    5. It is not in ``excluded`` (batteries that are full, empty or held, and
       hybrid inverters near full or empty, on this step; see
       ``_storage_step_state``).

    Args:
        step: The current schedule step.
        expected_compensation_kw: Power (kW) the enforcer is expected to absorb
            or supply. Used for efficiency scoring.
        zex_capable: Set of device names with zero_exchange/zero_export enabled.
        ev_available: Device name → availability flag for EV chargers.
        config: Static system configuration.
        headroom_margin: Minimum headroom to be eligible (kW).
        excluded: Device names to skip on this step.
        headroom_override: Absorption headroom in kW to use instead of
            ``_absorption_headroom`` for the named devices (SoC-aware battery
            headroom).

    Returns:
        Sorted list of Candidate objects, best candidate first.
    """
    candidates: list[_Candidate] = []

    for name, sp in step.devices.items():
        if name not in zex_capable or name in excluded:
            continue

        # EV availability gate.
        if sp.type == "ev_charger" and not ev_available.get(name, False):
            continue

        if headroom_override is not None and name in headroom_override:
            headroom = headroom_override[name]
        else:
            headroom = _absorption_headroom(name, sp, config)
        if headroom < headroom_margin:
            continue

        margin = headroom - expected_compensation_kw
        eff = _efficiency_at_power(name, sp.type, expected_compensation_kw, config)

        # Wear cost: lower cost is better. Negate so higher value wins in max().
        wear_cost = 0.0
        if sp.type == "battery":
            bat_cfg = config.batteries.get(name)
            if bat_cfg is not None:
                wear_cost = bat_cfg.wear_cost_eur_per_kwh
        elif sp.type == "hybrid_inverter":
            hi_cfg = config.hybrid_inverters.get(name)
            if hi_cfg is not None:
                wear_cost = hi_cfg.wear_cost_eur_per_kwh
        # PV and EV have no wear_cost config field; treat as 0.0.

        candidates.append(
            _Candidate(
                name=name,
                sp_type=sp.type,
                efficiency_score=eff,
                headroom_margin=margin,
                wear_penalty=-wear_cost,
                type_priority=_TYPE_PRIORITY.get(sp.type, 0),
                name_key=name,
            )
        )

    return sorted(candidates, key=lambda c: c.score_tuple(), reverse=True)


def _collect_zex_capable(config: MimirheimConfig) -> frozenset[str]:
    """Return the set of device names that have zero-exchange capability enabled.

    A device is included when its capability flag (``zero_exchange`` for
    batteries and EVs, ``zero_export`` for PV) is True AND the corresponding
    output topic is configured. The output topic requirement is enforced by the
    schema validators; this function trusts those validators.

    Args:
        config: Static system configuration.

    Returns:
        Frozenset of device names eligible for closed-loop enforcer selection.
    """
    capable: set[str] = set()
    for name, bat_cfg in config.batteries.items():
        if bat_cfg.capabilities.zero_exchange:
            capable.add(name)
    for name, ev_cfg in config.ev_chargers.items():
        if ev_cfg.capabilities.zero_exchange:
            capable.add(name)
    for name, pv_cfg in config.pv_arrays.items():
        if pv_cfg.capabilities.zero_export:
            capable.add(name)
    for name, hi_cfg in config.hybrid_inverters.items():
        if hi_cfg.capabilities.zero_exchange:
            capable.add(name)
    return frozenset(capable)


def _collect_loadbalance_capable(config: MimirheimConfig) -> frozenset[str]:
    """Return device names whose EV has loadbalance capability enabled.

    Args:
        config: Static system configuration.

    Returns:
        Frozenset of EV charger names with loadbalance enabled.
    """
    return frozenset(
        name
        for name, ev_cfg in config.ev_chargers.items()
        if ev_cfg.capabilities.loadbalance and ev_cfg.outputs.loadbalance_cmd is not None
    )


def assign_control_authority(
    result: SolveResult,
    bundle: SolveBundle,
    config: MimirheimConfig,
) -> SolveResult:
    """Assign closed-loop enforcer authority to at most one device per step.

    Implements the four-level scoring cascade, hysteresis, minimum dwell, and
    loadbalance-suppression rules documented in full in
    IMPLEMENTATION_DETAILS.md §9, subsections "Enforcer selection" and
    "Loadbalance suppression".

    This function is a pure transformation: it creates new objects and does
    not mutate the input SolveResult, ScheduleStep, or DeviceSetpoint instances.

    Args:
        result: Output of ``apply_gain_threshold`` (or ``build_and_solve``
            directly). Not mutated.
        bundle: Runtime inputs for this solve cycle, including EV availability.
        config: Static system configuration.

    Returns:
        A new SolveResult with updated DeviceSetpoint.zero_exchange_active and
        DeviceSetpoint.loadbalance_active fields, or ``result`` unchanged if
        the schedule is empty (infeasible).
    """
    if not result.schedule:
        return result

    ctrl = config.control
    zex_capable = _collect_zex_capable(config)
    lb_capable = _collect_loadbalance_capable(config)

    # Build a lookup for EV availability from the bundle. Devices not in the
    # bundle are treated as unavailable (no vehicle, no signal).
    ev_available: dict[str, bool] = {
        name: inp.available for name, inp in bundle.ev_inputs.items()
    }

    # Dwell tracking (per-call, not persisted across solves).
    # current_enforcer: name of the device holding the enforcer role, or None.
    # dwell_remaining: how many more steps the current enforcer is locked in.
    current_enforcer: str | None = None
    dwell_remaining: int = 0
    current_score: float = -1.0  # Score of the current enforcer at its selection step.

    # Start- and end-of-step SoC per zero-exchange battery and hybrid
    # inverter, and the lowest SoC the plan lets each reach (for a battery,
    # min_soc_kwh raised by the battery-care ratchet floor), for the rules in
    # _storage_step_state. Constant for the whole call.
    trajectories: dict[str, list[tuple[float | None, float | None]]] = {}
    floors: dict[str, float] = {}
    for name, bat_cfg in config.batteries.items():
        if name not in zex_capable:
            continue
        bat_inputs = bundle.battery_inputs.get(name)
        measured = bat_inputs.soc_kwh if bat_inputs is not None else None
        trajectories[name] = _soc_trajectory(name, measured, bat_cfg.capacity_kwh, result.schedule, True)
        floors[name] = _effective_floor_kwh(bat_cfg, result.battery_care.get(name), measured)
    for name, hi_cfg in config.hybrid_inverters.items():
        if name not in zex_capable:
            continue
        hi_inputs = bundle.hybrid_inverter_inputs.get(name)
        measured = hi_inputs.soc_kwh if hi_inputs is not None else None
        trajectories[name] = _soc_trajectory(name, measured, hi_cfg.capacity_kwh, result.schedule, False)
        floors[name] = hi_cfg.min_soc_kwh

    new_schedule: list[ScheduleStep] = []

    for index, step in enumerate(result.schedule):
        expected_kw = max(step.grid_import_kw, step.grid_export_kw)
        is_near_zero = _is_near_zero_exchange(step, ctrl.exchange_epsilon_kw)

        updated_devices: dict[str, DeviceSetpoint] = {}

        if not is_near_zero:
            # Non-zero-exchange step: no enforcer needed. Clear all flags and
            # reset dwell tracking so selection starts fresh on the next
            # near-zero-exchange step.
            current_enforcer = None
            dwell_remaining = 0
            current_score = -1.0

            for name, sp in step.devices.items():
                # The two capabilities are independent, so both are evaluated
                # for every device. An EV may declare zero_exchange and
                # loadbalance together (the schema calls them orthogonal);
                # treating them as alternatives would leave such an EV without
                # load balancing on every step.
                updates: dict = {}
                if name in zex_capable:
                    updates["zero_exchange_active"] = False
                if name in lb_capable:
                    # No enforcer is active on this step, so nothing competes
                    # with the EVSE for control of grid current. Load balance
                    # whenever a vehicle is connected.
                    updates["loadbalance_active"] = ev_available.get(name, False)
                updated_devices[name] = (
                    DeviceSetpoint(**{**sp.model_dump(), **updates}) if updates else sp
                )
        else:
            # Near-zero-exchange step: select enforcer.
            excluded, storage_absorb = _storage_step_state(
                step, index, trajectories, floors, result.battery_care, config
            )
            candidates = _build_candidates(
                step=step,
                expected_compensation_kw=expected_kw,
                zex_capable=zex_capable,
                ev_available=ev_available,
                config=config,
                headroom_margin=ctrl.headroom_margin_kw,
                excluded=excluded,
                headroom_override=storage_absorb,
            )

            # Determine whether the current enforcer remains eligible.
            current_still_eligible = (
                current_enforcer is not None
                and any(c.name == current_enforcer for c in candidates)
            )

            if not current_still_eligible:
                # Current enforcer is gone (unplugged, lost headroom, full,
                # empty or held). Select fresh.
                current_enforcer = None
                dwell_remaining = 0
                current_score = -1.0

            # Dwell takes priority over switch_delta hysteresis; see
            # IMPLEMENTATION_DETAILS.md §9 for why both exist.
            if current_enforcer is not None and dwell_remaining > 0:
                dwell_remaining -= 1
                enforcer_name = current_enforcer
            else:
                # No dwell lock or dwell expired. Apply switch_delta hysteresis.
                if candidates:
                    best = candidates[0]
                    best_score = best.score_tuple()[0]  # efficiency is level-1 score

                    if current_enforcer is None:
                        # No current enforcer: select the best candidate.
                        enforcer_name = best.name
                        current_enforcer = best.name
                        current_score = best_score
                        dwell_remaining = max(0, ctrl.min_enforcer_dwell_steps - 1)
                    else:
                        # Hysteresis compares level 1 of the score tuple only,
                        # that is the efficiency at the expected compensation
                        # power. The later levels (headroom, wear, type, name)
                        # exist to break ties deterministically within a single
                        # step and are not on a common scale, so a delta across
                        # the whole tuple would not mean anything. Efficiency is
                        # the discriminator that actually justifies moving the
                        # role between devices.
                        best_eff = best.score_tuple()[0]
                        if best.name != current_enforcer and (best_eff - current_score) > ctrl.switch_delta:
                            # Challenger significantly outscores current enforcer: switch.
                            current_enforcer = best.name
                            current_score = best_eff
                            dwell_remaining = max(0, ctrl.min_enforcer_dwell_steps - 1)
                        enforcer_name = current_enforcer
                else:
                    # No eligible candidates: every capable device was dropped
                    # by _build_candidates (EV unplugged, battery full, empty
                    # or held, or headroom below
                    # control.headroom_margin_kw — e.g. already charging flat
                    # out, or PV idle). A normal operating state, not a fault
                    # (hence DEBUG, and only logged when zex_capable is
                    # non-empty — an installation with no closed-loop hardware
                    # has nothing worth reading here). The fallback below
                    # clears every capable device to its explicit setpoint.
                    enforcer_name = None
                    current_enforcer = None
                    dwell_remaining = 0
                    current_score = -1.0

                    if zex_capable:
                        logger.debug(
                            "Step %d: no eligible zero-exchange enforcer candidates; "
                            "all capable devices cleared.",
                            step.t,
                        )

            # Apply the selected enforcer to the step's devices.
            enforcer_is_battery = (
                enforcer_name is not None
                and config.batteries.get(enforcer_name) is not None
            )

            for name, sp in step.devices.items():
                # Both capabilities are evaluated for every device; they are
                # orthogonal and an EV may declare either or both.
                updates: dict = {}
                is_enforcer = name == enforcer_name
                if name in zex_capable:
                    updates["zero_exchange_active"] = is_enforcer
                if name in lb_capable:
                    # Suppressed while this EV or a battery is the enforcer;
                    # see IMPLEMENTATION_DETAILS.md §9, subsection
                    # "Loadbalance suppression".
                    updates["loadbalance_active"] = (
                        ev_available.get(name, False)
                        and not is_enforcer
                        and not enforcer_is_battery
                    )
                updated_devices[name] = (
                    DeviceSetpoint(**{**sp.model_dump(), **updates}) if updates else sp
                )

        new_schedule.append(
            ScheduleStep(
                t=step.t,
                grid_import_kw=step.grid_import_kw,
                grid_export_kw=step.grid_export_kw,
                devices=updated_devices,
            )
        )

    # Use model_copy so that any future fields added to SolveResult are
    # carried through automatically. Constructing SolveResult(...) explicitly
    # would silently drop any field not listed here — a bug that has already
    # affected deferrable_recommended_starts once.
    return result.model_copy(update={"schedule": new_schedule})
