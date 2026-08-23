"""Building thermal model (BTM) constraints, shared by the heat pump devices.

Both ``SpaceHeatingDevice`` and ``CombiHeatPumpDevice`` can be configured with a
``building_thermal`` block. When they are, the degree-days total-heat lower
bound is replaced by a first-order thermal model of the building, and the solver
schedules the heat pump to keep indoor temperature inside a comfort band rather
than to deliver a fixed quantity of heat. That lets it pre-heat when electricity
is cheap and coast on the building's stored heat when it is not.

The two devices differ only in how they express the thermal power the heat pump
delivers at each step: one has an on/off binary or an SOS2 stage set, the other a
space-heating mode binary. Everything else about the model is the same, so the
dynamics live here and each device passes in a callable for its own heat power.

This module has no I/O and no state. It imports from ``mimirheim.config`` for the
``BuildingThermalConfig`` type and from ``mimirheim.core.context``; it never
imports from ``mimirheim.io`` or ``mimirheim.devices``.
"""

from collections.abc import Callable
from typing import Any

from mimirheim.config.schema import BuildingThermalConfig
from mimirheim.core.context import ModelContext


def add_building_thermal_constraints(
    ctx: ModelContext,
    *,
    device_name: str,
    btm: BuildingThermalConfig,
    indoor_temp: dict[int, Any],
    current_indoor_temp_c: float | None,
    outdoor_temp_forecast_c: list[float] | None,
    heat_power_kw: Callable[[int], Any],
) -> None:
    """Constrain indoor temperature to the building's first-order thermal dynamics.

    The building is treated as a single lumped thermal mass. For each step t:

    .. code-block::

        T_indoor[t] = alpha * T_prev
                    + (dt / C) * P_heat[t]
                    + beta_outdoor * T_outdoor[t]

    where:

    - ``C`` is ``thermal_capacity_kwh_per_k``, the energy the building stores or
      releases per degree of indoor temperature change.
    - ``L`` is ``heat_loss_coeff_kw_per_k``, the power the building loses per
      degree of indoor-to-outdoor difference.
    - ``alpha = 1 - dt * L / C`` is the share of the previous indoor temperature
      the building still holds after one step. ``BuildingThermalConfig``
      validates that it stays strictly between 0 and 1.
    - ``beta_outdoor = dt * L / C`` is the pull of outdoor temperature on
      indoor, and equals ``1 - alpha``. With the heat pump off and a steady
      outdoor temperature the indoor temperature converges on it.
    - ``dt / C`` converts the heat delivered in one step (kW times hours, so
      kWh) into the temperature rise it causes.
    - ``T_prev`` is the measured indoor temperature at t=0 and the previous
      step's decision variable thereafter.

    Every term is linear: ``C``, ``L`` and ``dt`` are constants, and
    ``heat_power_kw`` returns an expression that is linear in the device's own
    variables.

    The comfort band is not enforced here. It is expressed as bounds on the
    ``indoor_temp`` variables, which the calling device declares.

    Args:
        ctx: The current solve context.
        device_name: Used in error messages so an operator can tell which
            device is missing an input.
        btm: The device's validated building thermal configuration.
        indoor_temp: The device's indoor temperature variables, keyed by step.
            Must cover every step in ``ctx.T``.
        current_indoor_temp_c: Measured indoor temperature now, in degrees
            Celsius. Supplies the initial condition at t=0.
        outdoor_temp_forecast_c: Per-step outdoor temperature forecast in
            degrees Celsius. Must be at least as long as the horizon.
        heat_power_kw: Callable returning the thermal power the device delivers
            at a step, in kW, as a solver expression.

    Raises:
        ValueError: If ``current_indoor_temp_c`` is None, or if
            ``outdoor_temp_forecast_c`` is None or shorter than the horizon.
            Both are required whenever ``building_thermal`` is configured; the
            model has no initial condition and no driving temperature without
            them.
    """
    horizon = len(ctx.T)

    if current_indoor_temp_c is None:
        raise ValueError(
            f"Device {device_name!r}: building_thermal is configured but "
            "current_indoor_temp_c is None."
        )
    if outdoor_temp_forecast_c is None or len(outdoor_temp_forecast_c) < horizon:
        have = len(outdoor_temp_forecast_c) if outdoor_temp_forecast_c else 0
        raise ValueError(
            f"Device {device_name!r}: outdoor_temp_forecast_c has {have} values "
            f"but the horizon requires {horizon}."
        )

    capacity = btm.thermal_capacity_kwh_per_k
    loss = btm.heat_loss_coeff_kw_per_k
    dt = ctx.dt

    alpha = 1.0 - dt * loss / capacity
    beta_outdoor = dt * loss / capacity
    dt_over_capacity = dt / capacity

    for t in ctx.T:
        t_prev = current_indoor_temp_c if t == 0 else indoor_temp[t - 1]
        p_heat = heat_power_kw(t)

        # Rearranged so that solver variables sit on the left and constants on
        # the right, which is the form add_constraint expects. At t=0 the
        # previous temperature is a measured constant and moves to the right;
        # at later steps it is the previous step's variable and stays on the
        # left.
        rhs = beta_outdoor * outdoor_temp_forecast_c[t]
        if t == 0:
            rhs += alpha * t_prev
            ctx.solver.add_constraint(
                indoor_temp[t] - dt_over_capacity * p_heat == rhs
            )
        else:
            ctx.solver.add_constraint(
                indoor_temp[t] - dt_over_capacity * p_heat - alpha * t_prev == rhs
            )
