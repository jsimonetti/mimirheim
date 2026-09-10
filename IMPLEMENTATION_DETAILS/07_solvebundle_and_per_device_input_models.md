# 7. SolveBundle and per-device input models

**Decision: all runtime MQTT inputs are Pydantic models collected into a single `SolveBundle`**

`MimirheimConfig` captures what the system *is* (static, loaded once at startup). `SolveBundle` captures what we *know right now* — assembled fresh each solve cycle from the latest retained MQTT values by resampling timestamped forecast steps onto the 15-minute solver grid.

Two step models act as the wire format for forecast data arriving on MQTT:

```python
# mimirheim/core/bundle.py
class PriceStep(BaseModel):
    """One period of day-ahead electricity prices."""
    ts: datetime                   # UTC start of this price period
    import_eur_per_kwh: float = Field(ge=0)
    export_eur_per_kwh: float
    confidence: float = Field(default=1.0, ge=0, le=1)

class PowerForecastStep(BaseModel):
    """One point in a PV generation or load forecast."""
    ts: datetime                   # UTC time of this forecast point
    kw: float = Field(ge=0)        # forecast power in kilowatts
    confidence: float = Field(default=1.0, ge=0, le=1)
```

These are stored by `ReadinessState` as they arrive and resampled onto the 15-minute solver grid at snapshot time. The resampled flat arrays are what enters `SolveBundle`:

```python
class BatteryInputs(BaseModel):
    soc_kwh: float = Field(..., ge=0)
    timestamp: datetime

    @model_validator(mode="after")
    def check_freshness(self) -> "BatteryInputs":
        age = datetime.now(UTC) - self.timestamp
        if age > timedelta(minutes=5):
            raise ValueError(f"stale battery reading: {age}")
        return self

class EvInputs(BaseModel):
    soc_kwh: float = Field(..., ge=0)
    available: bool
    window_earliest: datetime | None = None
    window_latest: datetime | None = None
    timestamp: datetime

class DeferrableWindow(BaseModel):
    earliest: datetime
    latest: datetime

class HybridInverterInputs(BaseModel):
    """Current state for a hybrid inverter: battery SOC and the PV generation forecast.

    The pv_forecast_kw list is already resampled onto the 15-minute solver grid at
    snapshot time — the same format as SolveBundle.pv_forecast but scoped to one device.
    """
    soc_kwh: float = Field(..., ge=0)
    pv_forecast_kw: list[float] = Field(..., min_length=1)

class ThermalBoilerInputs(BaseModel):
    """Current state for an electric thermal boiler: current tank temperature.

    The initial tank temperature is required because the solver must track tank
    temperature as a decision variable across the horizon. Without the initial
    state the first-step constraint cannot be formed.
    """
    current_temp_c: float

class SpaceHeatingInputs(BaseModel):
    """Current state for a space heating heat pump.

    When building_thermal is not configured:
      heat_needed_kwh carries the heat demand for the horizon (degree-days model).

    When building_thermal is configured:
      current_indoor_temp_c provides the initial indoor temperature (required).
      outdoor_temp_forecast_c provides the per-step outdoor temperature forecast
      (required; already at 15-minute resolution — no resampling is applied).
      heat_needed_kwh is still accepted but is ignored by the solver.
    """
    heat_needed_kwh: float = Field(..., ge=0)
    current_indoor_temp_c: float | None = None
    outdoor_temp_forecast_c: list[float] | None = None

class CombiHeatPumpInputs(BaseModel):
    """Current state for a combi heat pump covering both DHW and space heating.

    current_temp_c is the DHW tank temperature — the initial condition for the
    tank thermal dynamics model.

    heat_needed_kwh is the space heating demand when building_thermal is not
    configured. When building_thermal is configured, the BTM fields take over
    and heat_needed_kwh is ignored for SH (DHW is unaffected).
    """
    current_temp_c: float
    heat_needed_kwh: float = Field(..., ge=0)
    current_indoor_temp_c: float | None = None
    outdoor_temp_forecast_c: list[float] | None = None

class SolveBundle(BaseModel):
    strategy: str = "minimize_cost"   # from mimir/input/strategy; defaults to "minimize_cost"
    solve_time_utc: datetime
    horizon_prices: list[float] = Field(..., min_length=1)        # EUR/kWh, 15-min grid
    horizon_export_prices: list[float] = Field(..., min_length=1) # EUR/kWh, 15-min grid
    horizon_confidence: list[float] = Field(..., min_length=1)    # [0, 1] per step
    pv_forecast: list[float] = Field(..., min_length=1)           # kW, 15-min grid
    base_load_forecast: list[float] = Field(..., min_length=1)    # kW, 15-min grid
    battery_inputs: dict[str, BatteryInputs] = Field(default_factory=dict)
    ev_inputs: dict[str, EvInputs] = Field(default_factory=dict)
    deferrable_windows: dict[str, DeferrableWindow] = Field(default_factory=dict)
    hybrid_inverter_inputs: dict[str, HybridInverterInputs] = Field(default_factory=dict)
    thermal_boiler_inputs: dict[str, ThermalBoilerInputs] = Field(default_factory=dict)
    space_heating_inputs: dict[str, SpaceHeatingInputs] = Field(default_factory=dict)
    combi_hp_inputs: dict[str, CombiHeatPumpInputs] = Field(default_factory=dict)
```

All five forecast arrays in `SolveBundle` have the same length, determined at snapshot time by `compute_horizon_steps()` in `mimirheim/core/forecast.py`.

The top-level solver entry point signature is therefore:

```python
def build_and_solve(bundle: SolveBundle, config: MimirheimConfig) -> SolveResult: ...
```

`SolveResult` is the output counterpart to `SolveBundle` — a Pydantic model that carries the complete schedule and is serialised to both MQTT and golden files:

```python
# mimirheim/core/result.py
from pydantic import BaseModel, Field

class DeviceSetpoint(BaseModel):
    kw: float                # net power setpoint; positive = producing, negative = consuming
    type: str                # device type derived from config section (e.g. "battery", "ev_charger")
    soc_kwh: float | None    # terminal SOC in kWh at end of step; None for non-storage devices

class ScheduleStep(BaseModel):
    t: int                   # time step index
    grid_import_kw: float
    grid_export_kw: float
    devices: dict[str, DeviceSetpoint]   # keyed by device name

class SolveResult(BaseModel):
    strategy: str            # "minimize_cost" | "minimize_consumption" | "balanced"
    objective_value: float
    solve_status: str        # "optimal" | "feasible" (time-limited incumbent) | "infeasible"
    schedule: list[ScheduleStep]
```

`SolveResult.model_dump()` is the golden file `golden.json`. The MQTT publisher reads the same object to publish `mimir/strategy/schedule`, `mimir/strategy/current`, and the per-device retained topics.

By the time the solver is called, both halves are already validated.

## Why Pydantic for input models too

MQTT is a system boundary: payloads arrive from external inverters, sensors, and third-party publishers that mimirheim does not control. Validating at this boundary means:

- **Range checks** catch misbehaving hardware before it corrupts a solve (a faulty battery BMS reporting `soc_kwh = -999` gets rejected, not silently optimised around).
- **Staleness checks** are enforced declaratively in `model_validator`, not scattered across the IO layer.
- **Coercion** from raw MQTT bytes/strings to typed Python values happens in one place (the Pydantic model), not ad-hoc throughout the codebase.

## Forecast resampling (`mimirheim/core/forecast.py`)

Raw MQTT forecast payloads arrive at arbitrary resolution (hourly from Nordpool, sub- or super-hourly from PV APIs, irregular from HA history). `ReadinessState.snapshot()` calls the helpers in `mimirheim/core/forecast.py` to resample them onto the 15-minute solver grid before assembling `SolveBundle`.

**Horizon computation:**

```python
def compute_horizon_steps(solve_start: datetime, *series: list[PriceStep | PowerForecastStep]) -> int:
    """
    Returns the number of 15-minute steps from solve_start to
    min(last_ts across all series that lie at or after solve_start).
    Returns 0 if any series has no data at or after solve_start.
    """
```

`horizon_end` is the *minimum* of the last known timestamp across all forecast series. This prevents the solver from extrapolating: no series is extended beyond its last data point. This function (`compute_horizon_steps`) is used for PV and static-load series, which remain mandatory and intersection-based: every configured series must have future data.

**Price resampling (step function):** The price for a given `ts` applies until the next timestamp in the array. Prices between known steps are constant; the last known price does not extend beyond `horizon_end`.

**Power resampling (step function, hold-previous):** PV generation and static load hold the value of the interval that contains the query time, matching the forecast API's own semantics (`watts[T]` is the average power over `[T, T + duration)`). Linear interpolation would blend two adjacent hourly averages and invent a ramp the data does not describe — most visibly a gradual ramp from zero at sunrise rather than the abrupt step the source data represents. See `resample_power`'s docstring.

**Gap detection:** `find_gaps()` scans a sorted series within `[solve_start, horizon_end]` and returns intervals wider than `readiness.max_gap_hours`. Gaps are reported as warnings and filled by the resampler — they do not block the solve.

### Multi-source price merge

`config.inputs.prices` is a list of MQTT topics, not a single topic. This lets a lower-confidence, longer-horizon predictive source (e.g. a future day-after-day-ahead ML helper) extend the usable planning horizon beyond a shorter, higher-confidence source (e.g. day-ahead market prices) without ever being able to override it while it has real data.

`merge_price_sources(sources: list[list[PriceStep]], solve_start: datetime, n_steps: int)` replaces `resample_prices` as the single entry point; a single-source call is the regression case (`merge_price_sources([one_list], ...)`). For each output step `t`, sources are considered in two tiers:

1. **Real coverage.** A source is a candidate only if it has a hold-previous value at `t` *and* `t <= last_ts(source)` — its own last known timestamp. This is what stops a short-coverage, confidence-1.0 source from being held forward past its own data to permanently outrank a longer-horizon source: without this bound, the merge would never let the longer-horizon source contribute anything, defeating the reason to merge at all.
2. **Leading-edge fallback.** Used only when every source's tier-1 candidacy is empty (`t` is before every source's `first_ts`). Each source with any data at all contributes its own first step, extending the earliest known price backwards — identical to the pre-merge single-source behaviour.

Whichever tier applies, the candidate with the highest `confidence` wins; ties are broken by source list index (config priority order, lowest index wins).

`compute_price_horizon_steps(solve_start, sources)` computes the price horizon by the *maximum* of per-source coverage (union), the opposite of `compute_horizon_steps`'s *minimum* (intersection) used for PV/load. Price sources are optional alternatives to merge, not a mandatory set that must all be present — capping the horizon at the shortest price source would defeat the purpose of configuring a longer one. `ReadinessState._compute_horizon_steps` combines the two: `min(price_steps, other_steps)` when PV or load topics are configured, or `price_steps` alone when they are not (a battery+grid-only system has no PV/load series to intersect against, and calling `compute_horizon_steps()` with zero series would incorrectly return 0 by its own contract).

`SolveBundle.model_dump()` produces the exact JSON written to golden input files and debug dumps, with no manual field listing. `SolveBundle.model_validate(json)` replays any dump as a regression test directly. This was a design goal from §4 and §5; keeping all inputs in one validated model is what makes it work for free.

## Boundary rule for inputs

Input models (`BatteryInputs`, `EvInputs`, etc.) live in `mimirheim/core/bundle.py`. The IO layer (`mimirheim/io/`) constructs them from MQTT messages. Device classes (`mimirheim/devices/`) receive them as arguments to `add_variables()` and `add_constraints()`. Device classes never call into `mimirheim/io/` — inputs are handed down, not fetched.
