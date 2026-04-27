"""Post-solve transformations applied to a SolveResult before publication.

This module sits between ``model_builder.build_and_solve`` (which is a pure
MILP solver) and ``io.mqtt_publisher`` (which publishes the result). It applies
lightweight arithmetic decisions that are derived from the solved schedule but
do not require re-solving the MILP.

Current transformations, applied in order by the solve loop in ``__main__``:

1. ``apply_gain_threshold`` — if the benefit of the optimised schedule over the
   naive baseline is below ``config.objectives.min_dispatch_gain_eur``, replace
   the schedule with an idle one (all storage idle, grid covers base load).

Closed-loop enforcer assignment (setting ``DeviceSetpoint.zero_exchange_active``)
is performed by ``mimirheim.core.control_arbitration.assign_control_authority``,
not by this module.

What this module does not do:

- It does not run the MILP solver or modify solver variables.
- It does not publish to MQTT or log.
- It does not read configuration from files or environment variables directly;
  all inputs are passed as function arguments.
- It does not import from ``mimirheim.io``.
"""

import logging
from datetime import datetime

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import DeviceSetpoint, ScheduleStep, SolveBundle, SolveResult

logger = logging.getLogger("mimirheim.post_process")

# Device types whose kw setpoint is set to zero in an idle schedule. PV and
# static_load setpoints are derived from forecasts and are independent of the
# dispatch decision; they are preserved unchanged.
_CONTROLLABLE_TYPES: frozenset[str] = frozenset({"battery", "ev_charger", "deferrable_load", "hybrid_inverter"})

# Strategies for which the gain threshold is meaningful. The threshold compares
# EUR costs, so it is only applicable when the objective involves EUR. The
# minimize_consumption strategy minimises kWh from the grid, not EUR, so a
# cost-based threshold would have no semantic relationship to its objective.
_THRESHOLD_STRATEGIES: frozenset[str] = frozenset({"minimize_cost", "balanced"})


def apply_gain_threshold(
    result: SolveResult, bundle: SolveBundle, config: MimirheimConfig
) -> SolveResult:
    """Replace the schedule with an idle one if the gain is below the configured threshold.

    The gain is defined as ``naive_cost_eur - optimised_cost_eur``: how much
    cheaper the optimised schedule is compared to doing nothing (grid covers
    base load minus PV, storage stays idle).

    When the gain is below ``config.objectives.min_dispatch_gain_eur``, cycling
    storage would cause wear for a benefit too small to justify it. In that
    case this function returns a new ``SolveResult`` whose schedule is idle
    (all controllable device setpoints zero) and whose ``dispatch_suppressed``
    flag is ``True``. The ``naive_cost_eur`` and ``optimised_cost_eur`` fields
    are preserved from the original solve so the status topic still reports the
    quantitative reason for suppression.

    Suppression is bypassed unconditionally when:

    - ``config.objectives.min_dispatch_gain_eur`` is 0.0 (the default).
    - The solve was infeasible (nothing to suppress).
    - The active strategy is not ``minimize_cost`` or ``balanced``.
    - Any EV with an active charge deadline is connected. Deadline charging is
      mandatory; idling the EV charger would strand the vehicle.
    - Any deferrable load has an active scheduling window. The appliance must
      run within the window; idling it would miss the deadline entirely.
    - The gain is negative (the optimised schedule is already worse than naive,
      which indicates mandatory work such as EV charging is driving up costs;
      idling would make things worse, not better).

    Args:
        result: The output of ``build_and_solve``. Not mutated.
        bundle: The solve inputs used to produce ``result``.
        config: Static system configuration providing the threshold value.

    Returns:
        ``result`` unchanged if suppression does not apply, or a new
        ``SolveResult`` with an idle schedule and ``dispatch_suppressed=True``
        if suppression is triggered.
    """
    threshold = config.objectives.min_dispatch_gain_eur

    if threshold <= 0.0:
        return result

    if result.solve_status == "infeasible":
        return result

    if bundle.strategy not in _THRESHOLD_STRATEGIES:
        return result

    if _has_active_deadline(bundle):
        return result

    gain = result.naive_cost_eur - result.optimised_cost_eur

    # Only suppress when gain is a small positive number. Negative gain means
    # the solver had a good reason to spend more than the naive baseline (e.g.
    # mandatory work — this should not have reached here due to the deadline
    # guard above, but be defensive). Gain equal to or above the threshold
    # means dispatch is worthwhile.
    if gain < 0.0 or gain >= threshold:
        return result

    logger.info(
        "Gain %.4f EUR is below threshold %.4f EUR; publishing idle schedule.",
        gain,
        threshold,
    )
    return _build_idle_result(result)


def _has_active_deadline(bundle: SolveBundle) -> bool:
    """Return True if any controllable device has a mandatory deadline this cycle.

    An EV deadline is active when a vehicle is plugged in, has a target SOC,
    and the deadline has not yet passed. A deferrable load deadline is active
    when the load has a scheduling window registered in the bundle.

    Args:
        bundle: The current solve inputs.

    Returns:
        True if at least one active deadline exists.
    """
    for ev_inputs in bundle.ev_inputs.values():
        if (
            ev_inputs.available
            and ev_inputs.target_soc_kwh is not None
            and ev_inputs.window_latest is not None
            and _is_future(ev_inputs.window_latest, bundle.solve_time_utc)
        ):
            return True

    if bundle.deferrable_windows:
        return True

    return False


def _is_future(dt: datetime, reference: datetime) -> bool:
    """Return True if ``dt`` is strictly after ``reference``, timezone-aware.

    Args:
        dt: The datetime to test.
        reference: The reference point (typically ``bundle.solve_time_utc``).

    Returns:
        True if ``dt > reference``.
    """
    return dt > reference


def _build_idle_result(result: SolveResult) -> SolveResult:
    """Build a SolveResult whose schedule idles all controllable devices.

    All battery, EV, and deferrable load setpoints are set to 0 kW. PV and
    static load setpoints are preserved unchanged (they are forecasts, not
    dispatch decisions). Grid import and export are recomputed from the power
    balance to reflect the reduced dispatch.

    The ``naive_cost_eur`` and ``optimised_cost_eur`` fields from the original
    result are preserved. They represent what the solver found, not what the
    idle schedule costs. This gives operators visibility into the quantitative
    reason for suppression on the status topic.

    Args:
        result: The original solved ``SolveResult``. Not mutated.

    Returns:
        A new ``SolveResult`` with an idle schedule and ``dispatch_suppressed=True``.
    """
    idle_schedule: list[ScheduleStep] = []

    for step in result.schedule:
        idle_devices: dict[str, DeviceSetpoint] = {}
        net_non_controllable = 0.0

        for name, sp in step.devices.items():
            if sp.type in _CONTROLLABLE_TYPES:
                # Zero the controllable device's power. Preserve auxiliary
                # fields (power_limit_kw, zero_exchange_active, loadbalance_active)
                # so that hardware control setpoints are unaffected.
                idle_devices[name] = DeviceSetpoint(
                    kw=0.0,
                    type=sp.type,
                    power_limit_kw=sp.power_limit_kw,
                    zero_exchange_active=sp.zero_exchange_active,
                    loadbalance_active=sp.loadbalance_active,
                )
            else:
                idle_devices[name] = sp
                # Sign convention: kw > 0 means producing (PV), kw < 0 means
                # consuming (static load). Sum gives the net non-controllable
                # power contribution at this step.
                net_non_controllable += sp.kw

        # Power balance with all controllable devices zeroed:
        #   grid_import - grid_export = -net_non_controllable
        # Positive net (PV surplus) → export; negative net (load deficit) → import.
        grid_import_kw = max(0.0, -net_non_controllable)
        grid_export_kw = max(0.0, net_non_controllable)

        idle_schedule.append(ScheduleStep(
            t=step.t,
            grid_import_kw=grid_import_kw,
            grid_export_kw=grid_export_kw,
            devices=idle_devices,
        ))

    # Use model_copy so that any future fields added to SolveResult are
    # carried through automatically. Constructing SolveResult(...) explicitly
    # would silently drop any field not listed here — a bug that has already
    # affected deferrable_recommended_starts once.
    return result.model_copy(update={
        "dispatch_suppressed": True,
        "schedule": idle_schedule,
    })


# ---------------------------------------------------------------------------
# End of module — apply_zero_export_flags and its private helpers have been
# removed. Use mimirheim.core.control_arbitration.assign_control_authority instead.
# ---------------------------------------------------------------------------
