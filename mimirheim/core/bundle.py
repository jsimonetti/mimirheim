"""Runtime input and output models for a single mimirheim solve cycle.

This module defines two groups of Pydantic models:

Timestamped forecast step models (carried on MQTT, consumed by ReadinessState):
    PriceStep, PowerForecastStep

Input models (assembled each solve cycle from live MQTT values):
    BatteryInputs, EvInputs, DeferrableWindow, SolveBundle

Output models (produced by build_and_solve and published to MQTT / golden files):
    DeviceSetpoint, ScheduleStep, SolveResult

All models use ``extra="forbid"`` so that typos in field names and unexpected
payload fields are caught at the boundary rather than silently ignored.

This module has no imports from ``mimirheim.io`` or ``mimirheim.config``. It is the shared
vocabulary between the IO layer (which populates the models) and the solver core
(which consumes them). Neither layer needs to know about the other.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


class BatteryInputs(BaseModel):
    """Live battery state received from MQTT, validated at the system boundary.

    Attributes:
        soc_kwh: Current state of charge in kWh. Must be non-negative.
    """

    model_config = ConfigDict(extra="forbid")

    soc_kwh: float = Field(ge=0, description="State of charge in kWh.")


class EvInputs(BaseModel):
    """Live EV charger state received from MQTT, validated at the system boundary.

    Attributes:
        soc_kwh: Current vehicle state of charge in kWh. Must be non-negative.
        available: True if a vehicle is currently plugged in and ready to
            accept charge or discharge commands.
        target_soc_kwh: SOC the vehicle must reach by ``window_latest``, in kWh.
            When None, no minimum-SOC deadline constraint is applied.
        window_earliest: Earliest UTC datetime at which charging may begin.
            None if not relevant or not provided by the vehicle.
        window_latest: Latest UTC datetime by which the vehicle must reach
            ``target_soc_kwh``. None if not relevant or not provided.
    """

    model_config = ConfigDict(extra="forbid")

    soc_kwh: float = Field(ge=0, description="State of charge in kWh.")
    available: bool
    target_soc_kwh: float | None = Field(
        default=None,
        ge=0,
        description="Required SOC at end of charging window, in kWh. None = no deadline constraint.",
    )
    window_earliest: datetime | None = None
    window_latest: datetime | None = None


class PriceStep(BaseModel):
    """A single timestamped electricity price entry from the MQTT prices topic.

    Price data arrives from day-ahead markets at arbitrary resolution (typically
    hourly from Nordpool). ``ReadinessState`` stores a list of these and resamples
    them to the 15-minute solver grid using a step (constant) function: the price
    quoted for a given timestamp applies until the next known timestamp.

    Attributes:
        ts: UTC datetime when this price period begins.
        import_eur_per_kwh: Cost of importing 1 kWh from the grid in EUR.
            May be negative during periods of excess generation when the market
            pays consumers to absorb power (common in day-ahead markets during
            high renewable output).
        export_eur_per_kwh: Revenue for exporting 1 kWh to the grid in EUR.
            May be negative in markets that charge for export.
        confidence: Forecast confidence in [0, 1]. For confirmed day-ahead
            prices this is 1.0; for intraday estimates it may be lower.
            Defaults to 1.0 when not supplied by the data source.
    """

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    import_eur_per_kwh: float = Field(description="Import price in EUR/kWh. May be negative.")
    export_eur_per_kwh: float = Field(description="Export price in EUR/kWh. May be negative.")
    confidence: float = Field(ge=0.0, le=1.0, default=1.0, description="Forecast confidence [0, 1].")


class PowerForecastStep(BaseModel):
    """A single timestamped power forecast entry for PV or static load.

    PV generation and household base-load forecasts arrive from external APIs
    at arbitrary resolution (typically hourly). ``ReadinessState`` stores a list
    of these and resamples them to the 15-minute solver grid using linear
    interpolation between adjacent known points.

    Attributes:
        ts: UTC datetime of this forecast point.
        kw: Forecast power in kW. Must be non-negative (generation for PV,
            consumption for base load — both are strictly positive quantities).
        confidence: Forecast confidence in [0, 1]. Defaults to 1.0 when
            not supplied by the data source.
    """

    model_config = ConfigDict(extra="forbid")

    ts: datetime
    kw: float = Field(ge=0, description="Forecast power in kW.")
    confidence: float = Field(ge=0.0, le=1.0, default=1.0, description="Forecast confidence [0, 1].")


class HybridInverterInputs(BaseModel):
    """Live state for a DC-coupled hybrid inverter, received from MQTT.

    Combines the battery state-of-charge reading with the per-step PV DC power
    forecast for the inverter's MPPT input. Both values are required before the
    solver can build a complete sub-model for the hybrid inverter.

    Attributes:
        soc_kwh: Current battery state of charge in kWh. Must be non-negative.
        pv_forecast_kw: PV DC power forecast in kW for each time step in the
            current horizon. Must contain at least one value. Length must equal
            the horizon length used by the current solve.
    """

    model_config = ConfigDict(extra="forbid")

    soc_kwh: float = Field(ge=0, description="Battery state of charge in kWh.")
    pv_forecast_kw: list[float] = Field(
        min_length=1,
        description="PV DC power forecast in kW per step for this inverter.",
    )


class SpaceHeatingInputs(BaseModel):
    """Live space heating demand received from MQTT, validated at the system boundary.

    Attributes:
        heat_needed_kwh: Total thermal energy in kWh that the heat pump must
            produce this horizon. Computed externally (e.g. from degree-days)
            and published to MQTT by the home automation system. Zero means no
            heating is currently needed — the device will stay off for this
            solve cycle. Ignored when building_thermal is configured on the
            device; the comfort envelope takes over then.
        current_indoor_temp_c: Current mean indoor temperature in degrees
            Celsius. Required when the device has building_thermal configured.
            Provides the initial condition for the BTM dynamics equation at
            t=0 of each solve cycle.
        outdoor_temp_forecast_c: Per-step outdoor temperature forecast in
            degrees Celsius, one value per 15-minute step. Must cover at least
            as many steps as the solve horizon. Required when the device has
            building_thermal configured.
    """

    model_config = ConfigDict(extra="forbid")

    heat_needed_kwh: float = Field(
        ge=0.0,
        description="Total thermal energy in kWh to produce this horizon.",
    )
    current_indoor_temp_c: float | None = Field(
        default=None,
        description="Current indoor temperature in °C. Required for BTM.",
    )
    outdoor_temp_forecast_c: list[float] | None = Field(
        default=None,
        description="Per-step outdoor temperature forecast in °C. Required for BTM.",
    )


class CombiHeatPumpInputs(BaseModel):
    """Live combined DHW and space heating heat pump state from MQTT.

    Attributes:
        current_temp_c: Current DHW water temperature in degrees Celsius, as
            read from the tank sensor. Used as the initial temperature for the
            first step's temperature-dynamics constraint.
        heat_needed_kwh: Total space heating thermal energy in kWh required
            this horizon. Computed externally from degree-days data. Zero means
            no space heating is needed — the HP may still run in DHW mode.
            Ignored when building_thermal is configured on the device.
        current_indoor_temp_c: Current mean indoor temperature in degrees
            Celsius. Required when the device has building_thermal configured.
            Provides the initial condition for the BTM dynamics equation at
            t=0 of each solve cycle.
        outdoor_temp_forecast_c: Per-step outdoor temperature forecast in
            degrees Celsius, one value per 15-minute step. Must cover at least
            as many steps as the solve horizon. Required when the device has
            building_thermal configured.
    """

    model_config = ConfigDict(extra="forbid")

    current_temp_c: float = Field(
        description="Current DHW water temperature in °C."
    )
    heat_needed_kwh: float = Field(
        ge=0.0,
        description="Total space heating thermal energy required this horizon in kWh.",
    )
    current_indoor_temp_c: float | None = Field(
        default=None,
        description="Current indoor temperature in °C. Required for BTM.",
    )
    outdoor_temp_forecast_c: list[float] | None = Field(
        default=None,
        description="Per-step outdoor temperature forecast in °C. Required for BTM.",
    )


class ThermalBoilerInputs(BaseModel):
    """Live thermal boiler state received from MQTT, validated at the system boundary.

    Attributes:
        current_temp_c: Current water temperature in degrees Celsius, as read
            from the tank sensor. The solver uses this as the initial temperature
            for the first step's temperature-dynamics constraint.
    """

    model_config = ConfigDict(extra="forbid")

    current_temp_c: float = Field(
        description="Current water temperature in °C, as read from sensor."
    )


class DeferrableWindow(BaseModel):
    """Time window within which a deferrable load must run.

    Attributes:
        earliest: The earliest UTC datetime at which the load may begin.
        latest: The latest UTC datetime by which the load must have started
            (or completed, depending on device config).
    """

    model_config = ConfigDict(extra="forbid")

    earliest: datetime
    latest: datetime


class SolveBundle(BaseModel):
    """Complete snapshot of all runtime inputs for one mimirheim solve cycle.

    Assembled by ``ReadinessState.snapshot()`` from the latest retained MQTT
    values immediately before each solve. The forecast arrays are already
    resampled to the 15-minute solver grid and clipped to the available horizon
    at assembly time. Passed as a single validated argument to
    ``build_and_solve()``, which is a pure function with no I/O.

    ``model_dump()`` of this object is the canonical format for debug dump
    input files and golden file input files (see IMPLEMENTATION_DETAILS §4, §5).

    Attributes:
        strategy: Active optimisation strategy name. Populated from the
            ``mimir/input/strategy`` MQTT topic. Defaults to ``"minimize_cost"``
            when that topic has not been received.
        solve_time_utc: Start of the 15-minute slot in which this solve
            occurred. Always floored to the slot boundary (e.g. a trigger at
            19:34 UTC yields 19:30 UTC). Used as the origin for all
            step-to-datetime conversions: EV and deferrable load window
            mapping, schedule step timestamps, and chart publisher alignment.
        triggered_at_utc: Wall-clock UTC time when the trigger message was
            received and ``snapshot()`` was called. Unlike ``solve_time_utc``
            this is not floored; it is the actual moment the solve was
            initiated. Used for dump file naming and human-visible labels (so
            two solves that fall in the same 15-minute slot can be
            distinguished). May be ``None`` when a bundle is constructed
            outside the normal IO loop (tests, benchmarks).
        horizon_prices: Import price in EUR/kWh for each fifteen-minute step
            in the horizon. Length is dynamic (at least 1) — exactly the number
            of steps available from aligned data coverage across all forecasts.
        horizon_export_prices: Export price (feed-in tariff) in EUR/kWh for
            each step. May be zero or negative in markets with export charges.
        horizon_confidence: Forecast confidence in [0, 1] per step, sourced
            from price step confidence. The solver scales objective terms by
            this value; low-confidence steps are conservatively weighted.
        pv_forecast: PV generation forecast in kW per step. Positive = power
            flowing from the PV array into the home. Sum of all configured
            PV arrays, resampled via linear interpolation.
        base_load_forecast: Forecast of non-controllable (static) household
            load in kW per step. Sum of all configured static loads, resampled
            via linear interpolation.
        battery_inputs: Keyed by battery device name (matching config). Empty
            if no batteries are configured or their MQTT state is stale.
        ev_inputs: Keyed by EV device name. Empty if no EV is plugged in.
        deferrable_windows: Keyed by deferrable load device name. Empty if
            no deferrable loads have active scheduling windows.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(
        default="minimize_cost",
        description="Active optimisation strategy.",
    )
    solve_time_utc: datetime = Field(
        description="15-minute slot boundary at which this solve cycle started (floor of trigger time).",
    )
    triggered_at_utc: datetime | None = Field(
        default=None,
        description="Wall-clock time when the trigger was received. Not floored. Used for dump naming and display labels.",
    )
    horizon_prices: list[float] = Field(
        min_length=1,
        description="Import price in EUR/kWh per 15-minute step.",
    )
    horizon_export_prices: list[float] = Field(
        min_length=1,
        description="Export price in EUR/kWh per 15-minute step.",
    )
    horizon_confidence: list[float] = Field(
        min_length=1,
        description="Forecast confidence in [0, 1] per step.",
    )
    pv_forecast: list[float] = Field(
        min_length=1,
        description="PV generation forecast in kW per step.",
    )
    base_load_forecast: list[float] = Field(
        min_length=1,
        description="Static household load forecast in kW per step.",
    )
    battery_inputs: dict[str, BatteryInputs] = Field(
        default_factory=dict,
        description="Live battery state keyed by device name.",
    )
    ev_inputs: dict[str, EvInputs] = Field(
        default_factory=dict,
        description="Live EV state keyed by device name.",
    )
    hybrid_inverter_inputs: dict[str, HybridInverterInputs] = Field(
        default_factory=dict,
        description="Live hybrid inverter state keyed by device name.",
    )
    thermal_boiler_inputs: dict[str, ThermalBoilerInputs] = Field(
        default_factory=dict,
        description="Live thermal boiler state keyed by device name.",
    )
    space_heating_inputs: dict[str, SpaceHeatingInputs] = Field(
        default_factory=dict,
        description="Live space heating demand keyed by device name.",
    )
    combi_hp_inputs: dict[str, CombiHeatPumpInputs] = Field(
        default_factory=dict,
        description="Live combined heat pump state keyed by device name.",
    )
    deferrable_windows: dict[str, DeferrableWindow] = Field(
        default_factory=dict,
        description="Active scheduling windows keyed by device name.",
    )
    deferrable_start_times: dict[str, datetime] = Field(
        default_factory=dict,
        description=(
            "Actual start datetimes for deferrable loads that are currently running, "
            "keyed by device name. Published to topic_committed_start_time by the external "
            "automation when the load physically begins."
        ),
    )


class DeviceSetpoint(BaseModel):
    """A single device's power setpoint for one time step.

    Attributes:
        kw: Net power in kW. Positive means the device is producing power
            (e.g. V2H discharge, PV generation). Negative means the device is
            consuming power (e.g. battery charging, EV charging).
        type: Device type string derived from the config section the device
            belongs to (e.g. ``"battery"``, ``"ev_charger"``, ``"pv"``). Used
            by the MQTT publisher to route the setpoint to the correct output
            topic.
        power_limit_kw: For PV devices only. The production limit setpoint in
            kW that mimirheim instructs the inverter to apply. None for all
            non-PV devices and for PV devices whose inverter does not support
            power limiting (``capabilities.power_limit`` is False).
        zero_exchange_active: For battery, EV, and PV devices with closed-loop
            capability. True when mimirheim asserts the device's closed-loop
            zero-exchange or zero-export register for this step. False when the
            register is explicitly de-asserted. None when the device class has
            no closed-loop capability configured.

            For PV devices, this maps to the ``zero_export_mode`` output topic.
            For battery and EV devices, it maps to the ``exchange_mode`` output
            topic.
        on_off_active: For PV devices with ``capabilities.on_off`` enabled.
            True when the solver has decided the array should produce power at
            this step. False when the array should be switched off. None when
            the device has no on/off capability configured.

            Maps to the ``outputs.on_off_mode`` topic: ``"true"`` when on,
            ``"false"`` when off.
        loadbalance_active: For EV chargers with ``capabilities.loadbalance``
            enabled. True when the load-balance mode is asserted for this step.
            False when the mode is explicitly de-asserted. None when the device
            has no load-balance capability configured.
        pv_is_curtailed: For PV devices with any controllable capability
            (staged, ``capabilities.power_limit``, or ``capabilities.on_off``).
            True when mimirheim is actively limiting PV output below the
            available forecast at this step. False when the inverter is free
            to produce as much as the sun provides. None for fixed-mode PV
            arrays (no capability configured) and for all non-PV devices.

            This is a mode-agnostic signal: its meaning is consistent across
            all three controllable modes. In staged mode, True means the
            chosen stage register value is below the forecast. In
            ``power_limit`` mode, True means the solver chose a value
            strictly below the forecast. In ``on_off`` mode, True means the
            array has been switched off.

            Maps to the ``outputs.is_curtailed`` MQTT topic.
    """

    model_config = ConfigDict(extra="forbid")

    kw: float
    type: str
    power_limit_kw: float | None = None
    zero_exchange_active: bool | None = None
    on_off_active: bool | None = None
    loadbalance_active: bool | None = None
    pv_is_curtailed: bool | None = None


class ScheduleStep(BaseModel):
    """The complete power dispatch for a single 15-minute time step.

    Attributes:
        t: Zero-based time step index within the horizon (0–95 for a 96-step
            24-hour horizon).
        grid_import_kw: Power imported from the grid in kW. Non-negative.
        grid_export_kw: Power exported to the grid in kW. Non-negative.
        devices: Per-device setpoints for this step, keyed by device name.
    """

    model_config = ConfigDict(extra="forbid")

    t: int
    grid_import_kw: float
    grid_export_kw: float
    devices: dict[str, DeviceSetpoint]


class SolveResult(BaseModel):
    """Complete output of one mimirheim solve cycle.

    Returned by ``build_and_solve()`` and consumed by both the MQTT publisher
    and the golden file test infrastructure.

    ``model_dump()`` of this object is written as ``golden.json`` in scenario
    test directories and is also the payload of the ``mimir/strategy/schedule``
    MQTT topic (see IMPLEMENTATION_DETAILS §4).

    Attributes:
        strategy: The strategy that was active during this solve.
        objective_value: The objective function value returned by the solver.
            The sign convention follows the solver: lower is better for
            minimisation strategies.
        solve_status: One of ``"optimal"`` (provably optimal), ``"feasible"``
            (time-limited incumbent found but not proven optimal), or
            ``"infeasible"`` (no feasible solution exists). When infeasible,
            ``schedule`` is empty and the previous retained schedule is used.
        naive_cost_eur: Estimated cost in EUR of the naive baseline over the
            horizon: grid covers any shortfall between base load and PV at each
            step, with no storage dispatch. Computed as
            ``sum(max(0, base_load[t] - pv[t]) * import_price[t] * dt)``.
            Zero for infeasible solves.
        optimised_cost_eur: Raw grid cash flow in EUR of the solved schedule:
            import cost minus export revenue, with no adjustment for stored
            energy. Computed as
            ``sum(grid_import[t] * import_price[t] * dt - grid_export[t] * export_price[t] * dt)``.
            Zero for infeasible solves.
        soc_credit_eur: Estimated future value in EUR of the net change in
            stored energy across all batteries and EVs over the horizon.
            Positive when the horizon ends with more energy stored than it
            started with. Computed as
            ``avg_import_price × soc_delta_cell_kwh × avg_discharge_eff``
            per device, summed across all storage devices. Add this to
            ``optimised_cost_eur`` to compare fairly against ``naive_cost_eur``:
            ``effective_cost = optimised_cost_eur - soc_credit_eur``.
            Zero for infeasible solves.
        schedule: Ordered list of ScheduleStep objects, one per time step in
            the horizon. Empty if the solve was infeasible.
        deferrable_recommended_starts: Solver-chosen start datetimes for
            deferrable loads that were in binary scheduling state during this
            solve. Keyed by device name. The value is the UTC datetime of the
            first step where the schedule assigns nonzero power to the load.
            Only present for loads that were actively optimised; absent for
            loads in running, committed, or unscheduled state.
    """

    model_config = ConfigDict(extra="forbid")

    strategy: str
    objective_value: float
    solve_status: str
    dispatch_suppressed: bool = Field(
        default=False,
        description=(
            "True when the gain over the naive baseline was below "
            "config.objectives.min_dispatch_gain_eur and mimirheim published an idle "
            "schedule instead of the optimised one."
        ),
    )
    naive_cost_eur: float = Field(
        default=0.0,
        description=(
            "Estimated cost in EUR of the naive baseline (base load covered by grid, "
            "no storage dispatch). sum(max(0, base_load[t] - pv[t]) * import_price[t] * dt)."
        ),
    )
    optimised_cost_eur: float = Field(
        default=0.0,
        description=(
            "Raw grid cash flow in EUR of the solved schedule: import cost minus export "
            "revenue. sum(grid_import[t] * import_price[t] * dt - grid_export[t] * "
            "export_price[t] * dt). No SOC adjustment applied."
        ),
    )
    soc_credit_eur: float = Field(
        default=0.0,
        description=(
            "Estimated future value in EUR of the net change in stored energy over the "
            "horizon. avg_import_price * soc_delta_cell_kwh * avg_discharge_eff per device. "
            "Subtract from optimised_cost_eur for a fair comparison against naive_cost_eur."
        ),
    )
    schedule: list[ScheduleStep]
    deferrable_recommended_starts: dict[str, datetime] = Field(
        default_factory=dict,
        description=(
            "Solver-chosen start datetimes for deferrable loads in binary scheduling "
            "state, keyed by device name. The datetime is the first step in the schedule "
            "where the load has a nonzero power setpoint."
        ),
    )
