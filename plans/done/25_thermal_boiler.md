# Step 25 — Thermal boiler device (immersion heater and heat pump DHW)

## Architectural note

The electric immersion heater and the heat pump DHW boiler are the same MILP model.
Both have: a hot water tank with a temperature state variable, a binary on/off control,
and a linear temperature rise per step. The only difference is the coefficient on the
electrical input: for a resistive element that coefficient is 1.0 (COP=1); for a heat
pump compressor it is typically 2.5–4.0.

Separate device classes would duplicate the model entirely. This plan implements a
single `ThermalBoilerDevice` with a `cop: float` field. Setting `cop=1.0` models an
immersion heater; setting `cop=3.0` models a heat pump DHW boiler.

Minimum run length is the key operational difference: a resistive element can cycle
freely (`min_run_steps=0`), while a heat pump compressor must run in consecutive blocks
to avoid wear (`min_run_steps > 0`). This is implemented as a standard minimum up-time
constraint.

## References

- IMPLEMENTATION_DETAILS §7, subsection "Device Protocol"
- IMPLEMENTATION_DETAILS §8, subsection "Wear cost in objective terms"
- `mimirheim/devices/battery.py` — binary mode guard and SOC dynamics as structural reference
- `mimirheim/core/objective.py` — `_terminal_soc_terms` for the terminal value mechanism

---

## Files to create

- `mimirheim/devices/thermal_boiler.py`
- `tests/unit/test_thermal_boiler_constraints.py`

## Files to modify

- `mimirheim/config/schema.py` — new `ThermalBoilerInputsConfig`, `ThermalBoilerConfig`
- `mimirheim/core/bundle.py` — new `ThermalBoilerInputs`, field on `SolveBundle`
- `mimirheim/config/schema.py` — `MimirheimConfig.thermal_boilers`
- `mimirheim/config/schema.py` — `MimirheimConfig.device_names_unique` validator
- `mimirheim/core/model_builder.py` — wire device into solve loop and power balance
- `mimirheim/io/input_parser.py` — parse MQTT inputs for the new device
- `mimirheim/io/mqtt_publisher.py` — publish `heater_on` setpoint
- `tests/unit/test_config_schema.py` — config validation tests

---

## Tests first

Create `tests/unit/test_thermal_boiler_constraints.py`. Use a real solver with `T=4`,
`dt=0.25`. All tests use a helper fixture that builds a `ThermalBoilerConfig` with
`volume_liters=200`, `elec_power_kw=3.0`, `cooling_rate_k_per_hour=2.0`,
`setpoint_c=55.0`, `min_temp_c=40.0`, and a `ThermalBoilerInputs` with
`current_temp_c=45.0`, unless the test specifies otherwise.

- `test_boiler_temp_rises_when_heating` — force `heater_on[t]=1` for all T=4 steps by
  setting import cost to a very large negative number (free electricity). COP=1.0.
  Assert `T_tank[T-1]` equals `current_temp_c + T * heat_rise_per_step - T * cool_per_step`
  where `heat_rise_per_step = elec_power_kw * 1.0 * dt / thermal_cap_kwh_per_k`.
- `test_boiler_temp_drops_when_not_heating` — set import cost very high so the solver
  never heats (initial temp already above min_temp_c). Assert each step's temperature
  decreases by exactly `cooling_rate_k_per_hour * dt`.
- `test_boiler_temp_never_below_min` — start with `current_temp_c` just above
  `min_temp_c` and set import cost very high so heater prefers to stay off. Assert
  `T_tank[t] >= min_temp_c` at every step.
- `test_boiler_temp_never_above_setpoint` — set import cost very low (drives heating).
  Assert `T_tank[t] <= setpoint_c` at every step.
- `test_boiler_schedules_at_cheap_step` — T=2, step 0 expensive (1.0 EUR/kWh), step 1
  cheap (0.01 EUR/kWh). Initial temp is above `min_temp_c` by exactly
  `cooling_rate_k_per_hour * dt` so the solver must heat once to maintain minimum.
  Assert `heater_on[0] == 0` and `heater_on[1] == 1`.
- `test_boiler_cop_amplifies_thermal_rise` — same config but `cop=3.0`. Assert the
  temperature rise per active step is 3× the `cop=1.0` case.
- `test_boiler_net_power_negative_when_on` — solve and assert that at any step where
  `heater_on[t] == 1`, `device.net_power(t)` evaluates to approximately `−elec_power_kw`.
- `test_boiler_min_run_steps_consecutive` — `min_run_steps=4`. Set up a scenario where
  it is cheapest to heat for a single step (one cheap step; rest expensive). Assert that
  if the solver heats at all, it heats for at least 4 consecutive steps.
- `test_boiler_min_run_zero_allows_single_step` — `min_run_steps=0`. Same price profile.
  Assert the solver can choose to heat for exactly 1 step.
- `test_boiler_terminal_value_prevents_unnecessary_drain` — T=8. Set a uniform low price
  (no cost saving from not heating). Start at full setpoint. Without a terminal value
  term, the solver would drain the tank to `min_temp_c`; with the terminal value term,
  it should prefer leaving the tank warm. Assert `T_tank[7]` is significantly above
  `min_temp_c` when the terminal value is active.

Add to `tests/unit/test_config_schema.py`:
- `test_thermal_boiler_cop_must_be_positive` — `cop <= 0` raises.
- `test_thermal_boiler_min_temp_below_setpoint` — `min_temp_c >= setpoint_c` raises.
- `test_thermal_boiler_volume_must_be_positive` — `volume_liters <= 0` raises.
- `test_thermal_boiler_defaults_valid` — a minimal config with only required fields
  validates without error.

Run `uv run pytest tests/unit/test_thermal_boiler_constraints.py tests/unit/test_config_schema.py -k "boiler"` — all tests must fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py` — new models

**`ThermalBoilerInputsConfig`** — MQTT input topic config (same structural role as
`BatteryInputsConfig`):

```python
class ThermalBoilerInputsConfig(BaseModel):
    """MQTT input topic configuration for a thermal boiler device."""

    model_config = ConfigDict(extra="forbid")

    topic_current_temp: str = Field(
        description="MQTT topic publishing the current water temperature in °C, retained."
    )
```

**`ThermalBoilerConfig`** — static device parameters:

```python
class ThermalBoilerConfig(BaseModel):
    """Configuration for a thermal boiler: electric immersion heater or heat pump DHW.

    The same model covers both device classes. A resistive immersion heater has
    cop=1.0 and min_run_steps=0. A heat pump DHW boiler has cop >= 2.0 and
    typically min_run_steps >= 4 (one hour at 15-minute resolution).

    Attributes:
        volume_liters: Water volume of the tank in litres. Used to compute
            thermal capacity. At 15-minute resolution, 200 L rises about 1.3°C
            per kWh input.
        elec_power_kw: Rated electrical power of the heating element or HP
            compressor, in kW.
        cop: Coefficient of performance. cop=1.0 for resistive elements
            (1 kWh electric = 1 kWh thermal). cop=3.0 means 1 kWh electric
            produces 3 kWh of heat. Must be > 0.
        setpoint_c: Target hot water temperature in °C. The solver will not
            heat above this temperature.
        min_temp_c: Minimum allowable water temperature in °C. The solver
            will heat before the temperature drops below this level. For
            legionella safety, recommended minimum is 45°C.
        cooling_rate_k_per_hour: Rate at which the tank temperature drops in
            K/hour when the heater is off. Combines standby heat losses and
            expected hot water draw. A 200 L tank with good insulation loses
            approximately 1–3 K/hour during normal use.
        min_run_steps: Minimum number of consecutive 15-minute steps the
            heater must run once started. Use 0 or 1 for resistive elements.
            Use 4 (one hour) for heat pump compressors to prevent short-cycling.
        wear_cost_eur_per_kwh: Optional cycling cost per kWh of electrical
            consumption, in EUR. Adds a degradation penalty to the objective
            for heat pump compressors. Set to 0.0 for resistive elements.
        inputs: MQTT input topic configuration.
    """

    model_config = ConfigDict(extra="forbid")

    volume_liters: float = Field(gt=0)
    elec_power_kw: float = Field(gt=0)
    cop: float = Field(gt=0, default=1.0)
    setpoint_c: float
    min_temp_c: float = Field(default=40.0)
    cooling_rate_k_per_hour: float = Field(ge=0)
    min_run_steps: int = Field(ge=0, default=0)
    wear_cost_eur_per_kwh: float = Field(ge=0, default=0.0)
    inputs: ThermalBoilerInputsConfig | None = None

    @model_validator(mode="after")
    def _validate_temp_range(self) -> "ThermalBoilerConfig":
        if self.min_temp_c >= self.setpoint_c:
            raise ValueError(
                f"min_temp_c ({self.min_temp_c}) must be strictly less than "
                f"setpoint_c ({self.setpoint_c})"
            )
        return self
```

Add `thermal_boilers: dict[str, ThermalBoilerConfig] = Field(default_factory=dict)` to
`MimirheimConfig`, and extend `device_names_unique` to include `*self.thermal_boilers`.

### `mimirheim/core/bundle.py` — new input model

```python
class ThermalBoilerInputs(BaseModel):
    """Live boiler state received from MQTT, validated at the system boundary."""

    model_config = ConfigDict(extra="forbid")

    current_temp_c: float = Field(
        description="Current water temperature in °C, as read from sensor."
    )
```

Add to `SolveBundle`:
```python
thermal_boiler_inputs: dict[str, ThermalBoilerInputs] = Field(
    default_factory=dict,
    description="Keyed by thermal boiler device name. Empty if no boilers configured.",
)
```

### `mimirheim/devices/thermal_boiler.py`

The device implements the Device Protocol.

**Thermal capacity helper (module-level constant):**
```python
# Specific heat capacity of water: 4186 J/kg/K = 4186/3600 Wh/kg/K ≈ 1.163e-3 kWh/L/K
# (1 litre of water = 1 kg approximately)
_WATER_THERMAL_CAP_KWH_PER_LITRE_K = 4186 / 3600 / 1000  # ≈ 0.001163 kWh/L/K
```

**Variables (`add_variables`):**

For each time step `t`:
```python
# T_tank[t]: water temperature in °C at the end of step t.
# Bounds: [min_temp_c - 5, setpoint_c + 5] to allow small numerical slack.
self._T_tank[t] = ctx.solver.add_var(
    lb=config.min_temp_c - 5.0, ub=config.setpoint_c + 5.0, name=f"T_tank_{t}"
)

# heater_on[t]: binary, 1 = heater active during step t.
self._heater_on[t] = ctx.solver.add_var(var_type="B", name=f"heater_on_{t}")
```

If `config.min_run_steps > 1`: for each `t > 0`:
```python
# start[t]: binary sentinel, 1 if the heater turns on at step t
# (i.e. heater_on[t]=1 and heater_on[t-1]=0). Used by the minimum run
# constraint. Not needed when min_run_steps <= 1 because there is no
# minimum consecutive run to enforce.
self._start[t] = ctx.solver.add_var(var_type="B", name=f"heater_start_{t}")
```

**Constraints (`add_constraints`, receives `ThermalBoilerInputs`):**

Pre-compute thermal parameters (not solver variables):
```python
thermal_cap = config.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K  # kWh/K
cool_per_step = config.cooling_rate_k_per_hour * ctx.dt  # K per step
heat_rise_per_step = config.elec_power_kw * config.cop * ctx.dt / thermal_cap  # K per step when on
```

**Temperature dynamics:** for each `t`:
```python
# T_tank[t] = T_tank[t-1] - cooling + heating.
#
# The cooling term is a constant per step: it combines standby losses and
# expected hot water draw (lumped into cooling_rate_k_per_hour by the user).
#
# The heating term is linear: heater_on[t] × heat_rise_per_step.
# heater_on is binary and heat_rise_per_step is a constant, so this is a linear
# expression — no bilinear terms arise.
prior_temp = inputs.current_temp_c if t == 0 else self._T_tank[t - 1]
ctx.solver.add_constraint(
    self._T_tank[t] == prior_temp - cool_per_step + heat_rise_per_step * self._heater_on[t]
)
```

**Temperature bounds:** for each `t`:
```python
ctx.solver.add_constraint(self._T_tank[t] >= config.min_temp_c)
ctx.solver.add_constraint(self._T_tank[t] <= config.setpoint_c)
```

**Minimum run length** (only when `config.min_run_steps > 1`):
```python
# start[t] must be 1 when the heater turns on (transitions from off to on).
# Under a cost-minimisation objective the solver will set start[t] to exactly
# max(0, heater_on[t] - heater_on[t-1]) — never spuriously higher — because
# a higher start value would only tighten the minimum run constraint,
# forcing more heating steps.
for t in range(1, len(ctx.T)):
    ctx.solver.add_constraint(self._start[t] >= self._heater_on[t] - self._heater_on[t - 1])
    ctx.solver.add_constraint(self._start[t] <= self._heater_on[t])

# If the heater starts at step t, it must remain on for min_run_steps consecutive
# steps. This prevents short-cycling that damages heat pump compressors.
# If min(t + min_run_steps - 1, T - 1) < t + 1 (i.e. near the end of the
# horizon), only the remaining steps are constrained.
for t in range(1, len(ctx.T)):
    for tau in range(1, config.min_run_steps):
        if t + tau < len(ctx.T):
            ctx.solver.add_constraint(self._heater_on[t + tau] >= self._start[t])
```

**`net_power(t)`:**
```python
# The heater draws elec_power_kw from the AC bus when on, zero when off.
# Net power is negative (consuming), consistent with the Device Protocol
# sign convention (positive = producing, negative = consuming).
return -config.elec_power_kw * self._heater_on[t]
```

**`objective_terms(t)`:**
```python
# Cycling cost on electrical consumption, analogous to battery wear cost.
# For resistive elements this should be 0.0. For heat pump compressors,
# a small value (e.g. 0.01 EUR/kWh) discourages unnecessary short cycles
# on top of the minimum run constraint.
if config.wear_cost_eur_per_kwh > 0:
    return config.wear_cost_eur_per_kwh * config.elec_power_kw * self._heater_on[t] * ctx.dt
return 0
```

**`terminal_soc_var(ctx)`** (reuses the existing ObjectiveBuilder terminal value mechanism):
```python
# The "terminal value" of a hot water tank is the cost to re-heat the water
# from min_temp_c back to its current temperature after the horizon ends.
# Equivalently: the electrical kWh saved by having the tank warm at the end.
#
# kWh_thermal_above_min = (T_tank[T-1] - min_temp_c) × thermal_cap
# kWh_electrical_equivalent = kWh_thermal_above_min / cop
# ObjectiveBuilder multiplies by -avg_import_price → negative objective term,
# incentivising the solver to leave the tank warm.
thermal_cap = config.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K
return (self._T_tank[ctx.T[-1]] - config.min_temp_c) * thermal_cap / config.cop
```

### `mimirheim/core/model_builder.py`

Follow the existing battery instantiation pattern: for each entry in
`config.thermal_boilers`, create a `ThermalBoilerDevice`, call `add_variables`,
call `add_constraints` with the matching `ThermalBoilerInputs` from the bundle, and
include the device in the power balance and `devices` list passed to `ObjectiveBuilder`.

### `mimirheim/io/mqtt_publisher.py`

After each solve, publish the `heater_on` setpoint (0 or 1) for each thermal boiler
under the configured output topic. Follow the existing `battery_setpoint` pattern.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_thermal_boiler_constraints.py tests/unit/test_config_schema.py
```

All tests green.

```bash
uv run pytest tests/scenarios/
```

No golden file changes — existing scenarios have no thermal boilers configured.

---

## Done

```bash
mv plans/25_thermal_boiler.md plans/done/
```
