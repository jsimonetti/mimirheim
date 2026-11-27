"""Shared charge and discharge derating lines for storage devices.

Batteries and hybrid inverters may reduce their charge power near full and
their discharge power near empty. Both use the same two-point linear model:
the power ceiling is a straight line in the start-of-step state of charge.
This module computes that line once so the solver constraints and the control
arbitration cannot drift apart. See IMPLEMENTATION_DETAILS.md §8, subsection
"Power derating near SOC extremes".

What this module does not do:
    - Build solver constraints. Devices do that from the returned line.
    - Read device configs. Callers pass the four numbers that define a line.
    - Derate EV chargers. ``mimirheim.devices.ev.charge_derate_line`` covers
      those.
"""

from __future__ import annotations


def charge_ceiling_line(
    max_kw: float,
    capacity_kwh: float,
    above_kwh: float | None,
    min_kw: float | None,
) -> tuple[float, float] | None:
    """Return the charge power ceiling as ``(slope, intercept)``, or None.

    The ceiling in kW at start-of-step SOC ``s`` is ``slope * s + intercept``.
    It equals ``max_kw`` at ``above_kwh`` and ``min_kw`` at ``capacity_kwh``;
    below ``above_kwh`` it exceeds ``max_kw`` and does not bind.

    Args:
        max_kw: Maximum charge power in kW.
        capacity_kwh: Usable capacity in kWh.
        above_kwh: SOC in kWh above which charge power is derated, or None.
        min_kw: Charge power in kW at ``capacity_kwh``, or None.

    Returns:
        ``(slope, intercept)`` in kW/kWh and kW, or None when derating is not
        configured.
    """
    if above_kwh is None or min_kw is None:
        return None
    slope = (min_kw - max_kw) / (capacity_kwh - above_kwh)
    return slope, max_kw - slope * above_kwh


def discharge_ceiling_line(
    max_kw: float,
    min_soc_kwh: float,
    below_kwh: float | None,
    min_kw: float | None,
) -> tuple[float, float] | None:
    """Return the discharge power ceiling as ``(slope, intercept)``, or None.

    The ceiling in kW at start-of-step SOC ``s`` is ``slope * s + intercept``.
    It equals ``max_kw`` at ``below_kwh`` and ``min_kw`` at ``min_soc_kwh``;
    above ``below_kwh`` it exceeds ``max_kw`` and does not bind.

    Args:
        max_kw: Maximum discharge power in kW.
        min_soc_kwh: Minimum SOC in kWh.
        below_kwh: SOC in kWh below which discharge power is derated, or None.
        min_kw: Discharge power in kW at ``min_soc_kwh``, or None.

    Returns:
        ``(slope, intercept)`` in kW/kWh and kW, or None when derating is not
        configured.
    """
    if below_kwh is None or min_kw is None:
        return None
    slope = (max_kw - min_kw) / (below_kwh - min_soc_kwh)
    return slope, max_kw - slope * below_kwh
