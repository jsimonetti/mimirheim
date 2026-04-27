"""Arbitration engine for closed-loop enforcer selection.

This module decides, for each time step in a solved schedule, which single
device (if any) should act as the closed-loop zero-exchange enforcer. Exactly
one active enforcer per step is the rule: multiple devices running closed-loop
firmware simultaneously on the same AC bus leads to controller oscillation.

What this module does:

- Iterates over all steps in a SolveResult.
- For near-zero-exchange steps, scores all eligible candidates and selects one.
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
from dataclasses import dataclass, field

from mimirheim.config.schema import BatteryConfig, EvConfig, MimirheimConfig, PvConfig
from mimirheim.core.bundle import DeviceSetpoint, EvInputs, ScheduleStep, SolveBundle, SolveResult

logger = logging.getLogger("mimirheim.control_arbitration")

# Type-priority constants used as the final tiebreak level. Higher number wins
# in the descending sort. Battery has the widest regulation bandwidth; PV
# curtailment is always the last resort because it forfeits free generation.
_TYPE_PRIORITY: dict[str, int] = {"battery": 3, "hybrid_inverter": 2, "ev_charger": 2, "pv": 1}


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
        if bat_cfg.charge_efficiency_curve is not None:
            return bat_cfg.charge_efficiency_curve[-1].power_kw
        if bat_cfg.charge_segments is not None:
            return sum(s.power_max_kw for s in bat_cfg.charge_segments)
        return 0.0
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


def _absorption_headroom(name: str, sp: DeviceSetpoint, config: MimirheimConfig) -> float:
    """Compute absorption headroom for a closed-loop-capable device at its current setpoint.

    Headroom measures how much additional power the device can absorb from the
    AC bus relative to its current operating point. It is used to filter out
    devices that cannot meaningfully respond to grid disturbances.

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
) -> list[_Candidate]:
    """Build a list of eligible candidates for enforcer selection on one step.

    A device is eligible when all of the following hold:

    1. It is in ``zex_capable`` (has the capability flag and output topic set).
    2. Its ``zero_exchange_active`` field is not None in the schedule (capability
       is present in the schema — this is always True for devices in zex_capable).
    3. For EV devices: ``ev_available[name]`` is True (vehicle is plugged in).
    4. Its absorption headroom is >= ``headroom_margin``.

    Args:
        step: The current schedule step.
        expected_compensation_kw: Power (kW) the enforcer is expected to absorb
            or supply. Used for efficiency scoring.
        zex_capable: Set of device names with zero_exchange/zero_export enabled.
        ev_available: Device name → availability flag for EV chargers.
        config: Static system configuration.
        headroom_margin: Minimum headroom to be eligible (kW).

    Returns:
        Sorted list of Candidate objects, best candidate first.
    """
    candidates: list[_Candidate] = []

    for name, sp in step.devices.items():
        if name not in zex_capable:
            continue

        # EV availability gate.
        if sp.type == "ev_charger" and not ev_available.get(name, False):
            continue

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

    This function replaces ``apply_zero_export_flags`` from Plan 42. It
    implements a four-level scoring cascade to select the best enforcer,
    with hysteresis and minimum dwell to prevent rapid switching.

    **Step classification**: A step is near-zero-exchange when both
    ``grid_import_kw`` and ``grid_export_kw`` are below
    ``config.control.exchange_epsilon_kw``. Only near-zero-exchange steps
    trigger enforcer selection; all other steps clear all capable devices.

    **Candidate eligibility** (all must hold):

    1. Capability flag enabled (``zero_exchange`` for battery/EV,
       ``zero_export`` for PV).
    2. For EVs: vehicle is plugged in (``bundle.ev_inputs[name].available``).
    3. Absorption headroom >= ``config.control.headroom_margin_kw``.

    **Scoring cascade** (four levels, descending; later levels break ties):

    1. Efficiency at the expected compensation power. PV always scores 0.0.
    2. Headroom margin (headroom − expected compensation). More slack is better.
    3. Wear proxy: lower ``wear_cost_eur_per_kwh`` wins.
    4. Type priority (battery=3, ev=2, pv=1) then device name (lexicographic).

    **Hysteresis**: a challenger must exceed the current enforcer's score by
    ``config.control.switch_delta`` to trigger a switch.

    **Minimum dwell**: once selected, a device holds the enforcer role for at
    least ``config.control.min_enforcer_dwell_steps`` consecutive steps,
    unless it becomes ineligible (availability lost, headroom drops below
    margin).

    **Loadbalance**:

    - EV devices with ``capabilities.loadbalance=True`` receive
      ``loadbalance_active=True`` on all steps where the vehicle is plugged in.
    - When a battery is the zero_exchange enforcer on a step, the EV's
      ``loadbalance_active`` is set to False for that step (the battery's
      closed-loop controller and an EVSE loadbalance controller both regulate
      the same grid current; only one may be authoritative).
    - An EV that is itself the zero_exchange enforcer receives
      ``zero_exchange_active=True`` and ``loadbalance_active=False``.
    - ``loadbalance_active=True`` is only set on steps where the EV is NOT
      selected as the zero_exchange enforcer.

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

    new_schedule: list[ScheduleStep] = []

    for step in result.schedule:
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
                if name in zex_capable:
                    updated_devices[name] = DeviceSetpoint(
                        **{**sp.model_dump(), "zero_exchange_active": False}
                    )
                elif name in lb_capable:
                    # loadbalance: active when EV is plugged in.
                    ev_plugged = ev_available.get(name, False)
                    updated_devices[name] = DeviceSetpoint(
                        **{**sp.model_dump(), "loadbalance_active": ev_plugged}
                    )
                else:
                    updated_devices[name] = sp
        else:
            # Near-zero-exchange step: select enforcer.
            candidates = _build_candidates(
                step=step,
                expected_compensation_kw=expected_kw,
                zex_capable=zex_capable,
                ev_available=ev_available,
                config=config,
                headroom_margin=ctrl.headroom_margin_kw,
            )

            # Determine whether the current enforcer remains eligible.
            current_still_eligible = (
                current_enforcer is not None
                and any(c.name == current_enforcer for c in candidates)
            )

            if not current_still_eligible:
                # Current enforcer is gone (unplugged, lost headroom). Select fresh.
                current_enforcer = None
                dwell_remaining = 0
                current_score = -1.0

            # Dwell: if an enforcer is locked in and dwell has not expired, keep it.
            if current_enforcer is not None and dwell_remaining > 0:
                # Check if a challenger beats the current enforcer by switch_delta.
                # Dwell does not prevent an override when the challenger margin is
                # large — but the plan says dwell takes priority over switch_delta.
                # Exact rule: dwell_remaining > 0 → keep current enforcer regardless
                # of challenger score, unless current becomes ineligible.
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
                        # Check switch_delta against the full score tuple, not just level 1.
                        # Use the composite float score as a proxy: sum of weighted levels.
                        # For a stable and simple comparison, we compare the efficiency
                        # score (level 1) difference only — which is the most meaningful
                        # discriminator and what the plan describes.
                        best_eff = best.score_tuple()[0]
                        if best.name != current_enforcer and (best_eff - current_score) > ctrl.switch_delta:
                            # Challenger significantly outscores current enforcer: switch.
                            current_enforcer = best.name
                            current_score = best_eff
                            dwell_remaining = max(0, ctrl.min_enforcer_dwell_steps - 1)
                        enforcer_name = current_enforcer
                else:
                    # No eligible candidates.
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
                if name in zex_capable:
                    flag = (name == enforcer_name)
                    new_sp = DeviceSetpoint(**{**sp.model_dump(), "zero_exchange_active": flag})
                    # If this EV is enforcer (zero_exchange), clear its loadbalance.
                    if name in lb_capable and flag:
                        new_sp = DeviceSetpoint(**{**new_sp.model_dump(), "loadbalance_active": False})
                    updated_devices[name] = new_sp
                elif name in lb_capable:
                    # EV with only loadbalance (no zero_exchange capability).
                    # Active when plugged in AND the battery is not the enforcer.
                    ev_plugged = ev_available.get(name, False)
                    lb_flag = ev_plugged and not enforcer_is_battery
                    updated_devices[name] = DeviceSetpoint(
                        **{**sp.model_dump(), "loadbalance_active": lb_flag}
                    )
                else:
                    updated_devices[name] = sp

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
