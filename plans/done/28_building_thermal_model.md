# Step 28 — Building thermal model for space heating and combi heat pump

## Architectural note: extending the degree-days demand model

Plans 26 and 27 implement space heating using the **degree-days demand** approach:
an external system computes the total thermal energy the building needs over the
horizon and publishes it as a scalar `heat_needed_kwh`. The solver then satisfies this
total without caring about which steps heating occurs at, beyond price ordering.

The degree-days approach cannot represent **pre-heating**: the act of heating a building
beyond its current comfort level during cheap price periods so that the building can
**coast** — remain without HP operation — through a later expensive period. Pre-heating
exploits the building's thermal mass (walls, floors, furniture) as free energy storage.
This is often the single most valuable optimisation available for a space-heated building
with time-of-use pricing.

This plan introduces a **building thermal model (BTM)** as an opt-in extension to both
`SpaceHeatingDevice` (plan 26) and `CombiHeatPumpDevice` (plan 27). When the BTM is
configured, the solver tracks indoor temperature as a per-step state variable and
enforces a comfort envelope rather than a total-heat lower bound.

**When to use each model:**

| Scenario | Recommended model |
|---|---|
| Radiator system, no thermal mass | Degree-days (`heat_needed_kwh`) |
| Underfloor heating, high thermal mass | BTM (`building_thermal`) |
| Unknown building properties | Degree-days until BTM params are calibrated |
| Inverter HP, precision control | BTM |
| Legacy on/off HP, basic control | Either; BTM adds minimal benefit |

**The BTM is opt-in per device.** The degree-days model remains the default. Setting
`building_thermal: null` (the default) keeps the existing plan 26/27 behaviour exactly.

---

## MILP formulation

### Variables (new, per step `t` in the horizon)

**`T_indoor[t]`** — Indoor air temperature at the end of step `t`, in degrees Celsius.

This is a continuous variable representing the mean indoor temperature of the entire
building zone. It is a decision variable because the solver chooses, at each step,
whether to run the HP and thereby raise or lower the rate of temperature change.

Bounds: `[comfort_min_c, comfort_max_c]`. These are the hard comfort constraints. No
slack is added: if the solver cannot maintain comfort (because the building is already
outside the range on entry, or the HP is too small), the problem is infeasible and the
solve loop falls back to the retained schedule.

### Parameters (per step `t`, supplied as inputs)

**`T_outdoor[t]`** — Outdoor air temperature for step `t`, in degrees Celsius.
From `inputs.outdoor_temp_forecast_c`, a list of floats published via MQTT. One value
per horizon step. Steps beyond the forecast length are not modelled.

### Dynamics constraint (for each step `t`)

The indoor temperature evolves according to a first-order linear difference equation:

$$T_{indoor}[t] = \alpha \cdot T_{prev} + \beta_{heat} \cdot P_{heat}[t] + \beta_{outdoor} \cdot T_{outdoor}[t]$$

where:

$$\alpha = 1 - \frac{dt \cdot L}{C}$$

$$\beta_{heat} = \frac{dt}{C}$$

$$\beta_{outdoor} = \frac{dt \cdot L}{C}$$

and:
- $C$ = `thermal_capacity_kwh_per_k` — building thermal mass in kWh/K
- $L$ = `heat_loss_coeff_kw_per_k` — heat loss coefficient in kW/K
- $dt$ = step duration in hours (0.25 for 15-minute steps)
- $T_{prev}$ = `current_indoor_temp_c` for `t=0`; `T_indoor[t-1]` for `t > 0`
- $P_{heat}[t]$ = thermal power delivered to the building in kW during step `t`

$P_{heat}[t]$ depends on the control mode:

**On/off mode (SpaceHeatingDevice, CombiHeatPumpDevice SH mode):**
$$P_{heat}[t] = \text{elec\_power\_kw} \cdot \text{cop} \cdot \text{hp\_on}[t]$$

**Power-stage (SOS2) mode (SpaceHeatingDevice only):**
$$P_{heat}[t] = \sum_s w[t][s] \cdot \text{elec\_kw}[s] \cdot \text{cop}[s]$$

**CombiHeatPumpDevice SH mode:**
$$P_{heat}[t] = \text{elec\_power\_kw} \cdot \text{cop\_sh} \cdot \text{sh\_mode}[t]$$

This expression is **linear** in all solver variables (`hp_on[t]`, `w[t][s]`,
`sh_mode[t]`), so no binary products arise. The BTM does not require any additional
binary or integer variables beyond those already declared by the device.

### Comfort constraints

For each step `t`:
$$T_{indoor}[t] \geq \text{comfort\_min\_c}$$
$$T_{indoor}[t] \leq \text{comfort\_max\_c}$$

The bounds are hard constraints. They are enforced by the variable bounds on
`T_indoor[t]`: the variable is declared with `lb=comfort_min_c, ub=comfort_max_c`.

### Replacement of the degree-days constraint

When the BTM is active, the `sum(heat_terms) >= heat_needed_kwh` constraint from
plans 26 and 27 is **not added**. The comfort envelope alone determines how much heat
the HP must produce. The `heat_needed_kwh` input field is ignored (but still accepted
for backward-compatible bundle assembly).

### Terminal value

No terminal value is added for indoor temperature in this plan. The comfort constraints
guarantee the building is habitable throughout the horizon. Whether the building is
warm or cold at the horizon end is immaterial to the next solve cycle, which
re-evaluates the full horizon from the latest observed indoor temperature.

A terminal value for indoor temperature (penalising temperatures below a preferred
level at `T_indoor[T-1]`) can be added in a subsequent plan if operational experience
shows the solver systematically under-heats the last few steps.

---

## References

- IMPLEMENTATION_DETAILS §7 — SolveBundle and per-device input models
- IMPLEMENTATION_DETAILS §8 — MIP model design: device contract and objective builder
- `mimirheim/devices/thermal_boiler.py` (plan 25) — tank temperature state variable pattern
- `mimirheim/devices/space_heating.py` (plan 26) — on/off and SOS2 modes, min-run structure
- `mimirheim/devices/combi_heat_pump.py` (plan 27) — mutual exclusion, DHW/SH mode variable

---

## Files to create

- `tests/unit/test_building_thermal_model.py`

## Files to modify

- `mimirheim/config/schema.py` — new `BuildingThermalInputsConfig`, `BuildingThermalConfig`;
  add optional `building_thermal` field to `SpaceHeatingConfig` and `CombiHeatPumpConfig`
- `mimirheim/core/bundle.py` — extend `SpaceHeatingInputs` and `CombiHeatPumpInputs` with
  optional `current_indoor_temp_c` and `outdoor_temp_forecast_c` fields
- `mimirheim/devices/space_heating.py` — add BTM branch in `add_variables` and
  `add_constraints`; new `_T_indoor` variable dict
- `mimirheim/devices/combi_heat_pump.py` — add BTM branch in `add_variables` and
  `add_constraints` for the SH mode; new `_T_indoor` variable dict
- `mimirheim/io/input_parser.py` — new `parse_outdoor_temp_forecast` and
  `parse_current_indoor_temp` functions
- `mimirheim/io/mqtt_client.py` — subscribe to outdoor temp forecast and indoor temp topics
  when BTM is configured
- `mimirheim/core/readiness.py` — track BTM sensor topics in `_sensor_topics`; assemble
  BTM inputs into the bundle in `snapshot()`
- `mimirheim/io/ha_discovery.py` — publish input sensors for outdoor temp forecast and
  indoor temp
- `mimirheim/config/example.yaml` — add BTM fields to space_heating_hps and combi_heat_pumps
  examples
- `tests/unit/test_config_schema.py` — BTM config validation tests

---

## Tests first

Create `tests/unit/test_building_thermal_model.py`. Use a real solver throughout.

### Shared fixture

```python
# Physical parameters used across most tests.
# Small, leaky building with a powerful heat pump — dynamics are clearly visible
# within a short (T=8) horizon.
#
# Building:
#   thermal_capacity_kwh_per_k = 5.0   → 1 kWh shifts the building by 0.2°C
#   heat_loss_coeff_kw_per_k   = 0.8   → at 15°C indoor–outdoor diff: 12 kW loss
#
# HP:
#   elec_power_kw = 6.0, cop = 3.5     → 21 kW thermal; per step: 5.25 kWh
#   temperature rise with HP on: 5.25 / 5.0 = 1.05°C per step
#
# Derived step constants  (dt = 0.25 h):
#   alpha            = 1 − 0.25 × 0.8 / 5.0 = 0.96
#   beta_heat        = 1.05  (°C per step when HP on)
#   beta_outdoor     = 0.25 × 0.8 / 5.0 = 0.04  (°C per °C outdoor contribution)
#
BTM_CFG = BuildingThermalConfig(
    thermal_capacity_kwh_per_k=5.0,
    heat_loss_coeff_kw_per_k=0.8,
    comfort_min_c=18.0,
    comfort_max_c=24.0,
    inputs=None,  # omit in unit tests; topics are not needed
)
```

### `test_btm_dynamics_no_hp`

Solver with T=4, dt=0.25. Set import price very high so the solver never runs the HP.
`current_indoor_temp_c=21.0`, outdoor forecast `[10.0, 10.0, 10.0, 10.0]`. Assert
`T_indoor[t]` at each step matches the analytical formula exactly:

```
T_indoor[t] = alpha * T_indoor[t-1] + beta_outdoor * T_outdoor[t]
```

Use exact arithmetic `pytest.approx` tolerance 1e-4.

### `test_btm_dynamics_with_hp`

T=4. Set import price very negative (free electricity) so the HP runs at every step
(or until `comfort_max_c` is hit). `current_indoor_temp_c=18.0`, outdoor `[0.0]*4`.
Assert `T_indoor[t]` matches the formula with `hp_on[t]=1` for all steps where
temperature is below `comfort_max_c`.

### `test_btm_comfort_min_enforced`

T=8. `current_indoor_temp_c=19.5` (above `comfort_min_c=18.0` but close). Outdoor
forecast `[0.0]*8` (very cold). Import price moderate (0.2 EUR/kWh). Assert
`T_indoor[t] >= 18.0` at every step.

### `test_btm_comfort_max_enforced`

T=8. `current_indoor_temp_c=23.5` (near `comfort_max_c=24.0`). Outdoor forecast
`[22.0]*8` (warm outside; heat loss is near zero). Import price very negative (free).
Assert `T_indoor[t] <= 24.0` at every step, and `hp_on[t]` is 0 at the steps where
temperature would already exceed the ceiling.

### `test_btm_preheat_shifts_hp_to_cheap_steps`

T=8. Price profile: steps 0–3 cheap (0.05 EUR/kWh), steps 4–7 expensive (1.0 EUR/kWh).
Outdoor forecast: steps 0–3 mild (10°C), steps 4–7 cold (−5°C).
`current_indoor_temp_c=20.0`, `comfort_min_c=18.0`, `comfort_max_c=24.0`.

Assert that `sum(hp_on[t] for t in range(0, 4)) > sum(hp_on[t] for t in range(4, 8))`.

Rationale: the solver should pre-heat during cheap steps, building up thermal storage,
and defer as much operation as possible to avoid expensive steps. The test does not
assert that the HP is completely off during expensive steps — the cold outdoor
temperature may still require some operation — but the cheap steps must carry more load.

### `test_btm_outdoor_forecast_shorter_than_horizon_raises`

Create `SpaceHeatingInputs` with `outdoor_temp_forecast_c` of length 4 and a horizon of
T=8 steps. Assert that `add_constraints` raises `ValueError` with a message identifying
the device name and the length mismatch.

### `test_btm_sp_on_off_replaces_heat_needed_constraint`

T=8. BTM active (same fixture parameters). `heat_needed_kwh=0.0` set on
`SpaceHeatingInputs` (simulating that the external system published zero demand).
The BTM ignores `heat_needed_kwh` entirely. Assert that with cold outdoor temperatures
(`[0.0]*8`) and low comfort_min (18.0), the solver still activates the HP to maintain
comfort. The HP must not be forced off by the zero-demand guard that exists in the
degree-days path: when BTM is active, the zero-demand early exit must not be taken.

### `test_btm_sp_sos2_mode_applies_btm`

T=8. Configure `SpaceHeatingConfig` with SOS2 stages (zero sentinel, 3 kW at COP 3.0,
6 kW at COP 3.5) and BTM active. `current_indoor_temp_c=21.0`, outdoor `[5.0]*8`,
moderate price. Assert that `T_indoor[t] >= comfort_min_c` at all steps and that
at some step a non-sentinel stage weight is non-zero (HP is actually doing work at
partial load).

### `test_btm_combi_hp_sh_mode_applies_btm`

T=8. `CombiHeatPumpConfig` with BTM active (same BTM parameters). `current_temp_c`
(DHW tank) set well above `min_temp_c` so DHW mode is not forced. Outdoor `[0.0]*8`,
`current_indoor_temp_c=18.5`, `comfort_min_c=18.0`. Assert that `T_indoor[t] >= 18.0`
at all steps and that `sh_mode[t]` is 1 for at least one step. Assert DHW tank
temperature dynamics are unaffected (follow the plan 27 thermal boiler formula).

### `test_btm_combi_hp_dhw_and_sh_both_satisfied`

T=8. BTM active. DHW tank starts below `min_temp_c` (forced heating), AND outdoor
temperature is cold enough to require SH operation. Assert that both DHW and SH
constraints are satisfied: `T_tank[T-1] >= min_temp_c` and `T_indoor[T-1] >= comfort_min_c`.
Assert mutual exclusion: no step has both `dhw_mode[t]=1` and `sh_mode[t]=1`.

### `test_btm_degree_days_path_unchanged_when_btm_not_set`

T=8. `SpaceHeatingConfig` with `building_thermal=None` (default). `heat_needed_kwh=6.0`.
Confirm the solver behaves exactly as before plan 28: the HP runs enough to satisfy
the total, and `T_indoor` stateVariables are never declared (assert accessing
`device._T_indoor` raises `AttributeError` or returns an empty dict).

Add to `tests/unit/test_config_schema.py`:

- `test_btm_thermal_capacity_must_be_positive` — `thermal_capacity_kwh_per_k=0` raises.
- `test_btm_heat_loss_coeff_must_be_positive` — `heat_loss_coeff_kw_per_k=0` raises.
- `test_btm_comfort_min_must_be_below_max` — `comfort_min_c >= comfort_max_c` raises.
- `test_btm_space_heating_accepts_building_thermal_field` — `SpaceHeatingConfig` with a
  valid `BuildingThermalConfig` validates without error.
- `test_btm_combi_hp_accepts_building_thermal_field` — same for `CombiHeatPumpConfig`.

Run:

```bash
uv run pytest tests/unit/test_building_thermal_model.py tests/unit/test_config_schema.py -k "btm"
```

All tests must fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py` — new models

**`BuildingThermalInputsConfig`** — MQTT input topics for live building state:

```python
class BuildingThermalInputsConfig(BaseModel):
    """MQTT input topic configuration for the building thermal model.

    Both topics are required when the BTM is active. They provide the initial
    condition (current indoor temperature) and the per-step driving parameter
    (outdoor temperature forecast) for the building heat balance equation.

    Attributes:
        topic_current_indoor_temp_c: MQTT topic publishing the current mean
            indoor temperature in degrees Celsius, retained. Published by a
            thermostat, climate entity, or temperature sensor in the home
            automation system.
        topic_outdoor_temp_forecast_c: MQTT topic publishing the per-step
            outdoor temperature forecast as a JSON array of floats, retained.
            One value per 15-minute step, covering at least as many steps as
            the mimirheim horizon. Typically published by a weather integration
            (e.g. Open-Meteo, Met.no) via the home automation system.
    """

    model_config = ConfigDict(extra="forbid")

    topic_current_indoor_temp_c: str = Field(
        description="MQTT topic for current indoor temperature in °C, retained."
    )
    topic_outdoor_temp_forecast_c: str = Field(
        description=(
            "MQTT topic for per-step outdoor temperature forecast, JSON array of floats, retained."
        )
    )
```

**`BuildingThermalConfig`** — static building physics parameters:

```python
class BuildingThermalConfig(BaseModel):
    """Static parameters for the building thermal model.

    These parameters describe the thermal behaviour of the building zone being
    controlled. They must be calibrated or estimated from the building's
    construction, insulation, and historical heating data.

    The building is modelled as a single lumped thermal mass (a "single-node"
    model). This is a first-order approximation suitable for well-mixed spaces
    such as open-plan living areas or underfloor-heated buildings where the
    temperature is reasonably uniform.

    Attributes:
        thermal_capacity_kwh_per_k: Effective thermal mass of the building in
            kWh per degree Kelvin. Represents how much energy is stored or
            released when the indoor temperature changes by 1°C. Typical
            values range from 3–5 kWh/K for a small well-insulated apartment
            to 15–40 kWh/K for a large passive house with concrete floors.
            Can be estimated from the time the building takes to cool by 1°C
            when the HP is off in calm weather.
        heat_loss_coeff_kw_per_k: Building heat loss coefficient in kW per
            degree Kelvin of indoor–outdoor temperature difference. At a 15°C
            delta, a coefficient of 0.5 kW/K means 7.5 kW of heat is needed
            to maintain temperature. Also known as the specific heat loss rate
            or "Wärmeleitzahl" in German standards. Typical range: 0.05 kW/K
            (very well insulated) to 1.5 kW/K (draughty older building).
        comfort_min_c: Minimum acceptable indoor temperature in degrees
            Celsius. The solver will not allow T_indoor to drop below this
            value at any step. Typical value: 19–21°C.
        comfort_max_c: Maximum acceptable indoor temperature in degrees
            Celsius. Pre-heating is bounded by this ceiling. Typical value:
            22–24°C.
        inputs: MQTT input topic configuration. None is allowed in unit
            tests where live data is injected directly via SpaceHeatingInputs.
    """

    model_config = ConfigDict(extra="forbid")

    thermal_capacity_kwh_per_k: float = Field(
        gt=0,
        description="Building thermal mass in kWh/K.",
    )
    heat_loss_coeff_kw_per_k: float = Field(
        gt=0,
        description="Building heat loss coefficient in kW/K.",
    )
    comfort_min_c: float = Field(
        default=19.0,
        description="Minimum acceptable indoor temperature in °C.",
    )
    comfort_max_c: float = Field(
        default=24.0,
        description="Maximum acceptable indoor temperature in °C.",
    )
    inputs: BuildingThermalInputsConfig | None = Field(
        default=None,
        description="MQTT input topics. None is allowed in unit tests.",
    )

    @model_validator(mode="after")
    def _validate_comfort_range(self) -> "BuildingThermalConfig":
        """Validate that comfort_min_c is strictly less than comfort_max_c."""
        if self.comfort_min_c >= self.comfort_max_c:
            raise ValueError(
                f"comfort_min_c ({self.comfort_min_c}) must be strictly less than "
                f"comfort_max_c ({self.comfort_max_c})."
            )
        return self
```

Add the optional `building_thermal` field to both existing config models:

```python
# In SpaceHeatingConfig:
building_thermal: BuildingThermalConfig | None = Field(
    default=None,
    description=(
        "Optional building thermal model parameters. When set, the solver tracks "
        "indoor temperature as a state variable and enforces the comfort envelope. "
        "When None (default), the degree-days demand model is used instead."
    ),
)

# In CombiHeatPumpConfig:
building_thermal: BuildingThermalConfig | None = Field(
    default=None,
    description=(
        "Optional building thermal model for the SH mode. When set, SH operation "
        "is governed by the indoor comfort envelope rather than heat_needed_kwh. "
        "The DHW tank model is unaffected."
    ),
)
```

### `mimirheim/core/bundle.py` — extend existing input models

Add optional BTM fields to both `SpaceHeatingInputs` and `CombiHeatPumpInputs`:

```python
# In SpaceHeatingInputs:
current_indoor_temp_c: float | None = Field(
    default=None,
    description=(
        "Current indoor temperature in °C. Required when building_thermal is configured "
        "on the device; ignored otherwise."
    ),
)
outdoor_temp_forecast_c: list[float] | None = Field(
    default=None,
    description=(
        "Per-step outdoor temperature forecast in °C. One value per horizon step. "
        "Required when building_thermal is configured; ignored otherwise."
    ),
)

# In CombiHeatPumpInputs (same two fields, same docstrings).
```

The `heat_needed_kwh` field must remain on both models; it is still used in the
degree-days path and is accepted (but ignored) in the BTM path for backward
compatibility.

### `mimirheim/devices/space_heating.py` — BTM branch

Add a `_T_indoor: dict[int, Any]` instance variable (populated only in BTM mode).

In `add_variables`:

```python
def add_variables(self, ctx: ModelContext) -> None:
    """..."""
    # ... existing on/off and SOS2 variable declarations ...

    # BTM indoor temperature variables (declared only when BTM is configured).
    #
    # T_indoor[t] represents the mean indoor temperature at the end of step t.
    # The bounds enforce the comfort envelope as a hard constraint.
    if self.config.building_thermal is not None:
        btm = self.config.building_thermal
        for t in ctx.T:
            self._T_indoor[t] = ctx.solver.add_var(
                lb=btm.comfort_min_c,
                ub=btm.comfort_max_c,
                name=f"{self.name}_T_indoor_{t}",
            )
```

In `add_constraints`, replace the heat-demand section with a dispatch based on whether
BTM is active:

```python
if self.config.building_thermal is not None:
    self._add_btm_constraints(ctx, inputs)
else:
    self._add_degree_days_constraints(ctx, inputs)
```

Extract the existing degree-days constraint into `_add_degree_days_constraints`. Write
`_add_btm_constraints` as a new private method:

```python
def _add_btm_constraints(self, ctx: ModelContext, inputs: SpaceHeatingInputs) -> None:
    """Add building thermal model constraints to the solver.

    Enforces the first-order heat balance:
        T_indoor[t] = alpha * T_prev + beta_heat * P_heat[t] + beta_outdoor * T_outdoor[t]

    where:
        alpha         = 1 - dt * L / C   (thermal decay factor, dimensionless)
        beta_heat     = dt / C            (temperature rise per kWh thermal input, K/kWh)
        beta_outdoor  = dt * L / C        (outdoor coupling coefficient, dimensionless)

    T_indoor is bounded by [comfort_min_c, comfort_max_c] via variable bounds set
    in add_variables; no explicit bound constraints are needed here.

    Args:
        ctx: Active solver context.
        inputs: Validated live inputs including current_indoor_temp_c and
            outdoor_temp_forecast_c.

    Raises:
        ValueError: If outdoor_temp_forecast_c is None or shorter than the horizon.
    """
    btm = self.config.building_thermal
    n_steps = len(ctx.T)

    if inputs.outdoor_temp_forecast_c is None:
        raise ValueError(
            f"SpaceHeating '{self.name}': building_thermal is configured but "
            f"inputs.outdoor_temp_forecast_c is None."
        )
    if len(inputs.outdoor_temp_forecast_c) < n_steps:
        raise ValueError(
            f"SpaceHeating '{self.name}': outdoor_temp_forecast_c has "
            f"{len(inputs.outdoor_temp_forecast_c)} values but horizon has {n_steps} steps."
        )
    if inputs.current_indoor_temp_c is None:
        raise ValueError(
            f"SpaceHeating '{self.name}': building_thermal is configured but "
            f"inputs.current_indoor_temp_c is None."
        )

    C = btm.thermal_capacity_kwh_per_k
    L = btm.heat_loss_coeff_kw_per_k
    alpha = 1.0 - ctx.dt * L / C
    beta_outdoor = ctx.dt * L / C

    for t in ctx.T:
        T_prev = (
            inputs.current_indoor_temp_c if t == 0 else self._T_indoor[t - 1]
        )
        T_out = inputs.outdoor_temp_forecast_c[t]

        # P_heat[t]: thermal power delivered to the building in kW.
        # Expressed as a linear combination of solver variables.
        if self.config.elec_power_kw is not None:
            # On/off mode: P_heat = elec_power_kw * cop * hp_on[t].
            p_heat_term = (
                self.config.elec_power_kw * self.config.cop * self._hp_on[t]
            )
        else:
            # SOS2 mode: P_heat = sum_s(w[t][s] * elec_kw[s] * cop[s]).
            stages = self.config.stages
            p_heat_term = sum(
                self._w[t][s] * stages[s].elec_kw * stages[s].cop
                for s in range(len(stages))
            )

        # Heat delivered to the building this step in kWh.
        q_heat_kwh = p_heat_term * ctx.dt

        # Building heat balance (first-order linear difference equation).
        # T_indoor[t] = alpha * T_prev
        #             + beta_heat * q_heat_kwh    (from HP)
        #             + beta_outdoor * T_outdoor[t]  (outdoor coupling)
        #
        # beta_heat = dt/C is absorbed into q_heat_kwh = P_heat * dt,
        # then divided by C below.
        ctx.solver.add_constraint(
            self._T_indoor[t]
            == alpha * T_prev
            + (1.0 / C) * q_heat_kwh
            + beta_outdoor * T_out
        )
```

### `mimirheim/devices/combi_heat_pump.py` — BTM branch

Add `_T_indoor: dict[int, Any]` instance variable.

In `add_variables`, declare `_T_indoor[t]` variables when `config.building_thermal`
is set (same bounds as in `SpaceHeatingDevice`).

In `add_constraints`, replace the SH constraint section:

```python
# Existing DHW constraint section is unchanged.
# ...

# SH constraint section: degree-days or BTM.
if self.config.building_thermal is not None:
    self._add_btm_sh_constraints(ctx, inputs)
elif inputs.heat_needed_kwh > 0.0:
    ctx.solver.add_constraint(
        sum(self.config.elec_power_kw * self.config.cop_sh * ctx.dt
            * self._sh_mode[t] for t in ctx.T)
        >= inputs.heat_needed_kwh
    )
```

Write `_add_btm_sh_constraints` using the same formula as `SpaceHeatingDevice`, with
`P_heat[t] = elec_power_kw * cop_sh * sh_mode[t]`.

### `mimirheim/io/input_parser.py` — new parsing functions

```python
def parse_current_indoor_temp(payload: Union[bytes, str]) -> float:
    """Parse an indoor temperature reading from an MQTT payload.

    Accepts:
    - A plain numeric string: ``"20.5"``
    - A JSON object with a ``"temp_c"`` key: ``{"temp_c": 20.5}``
    - A JSON object with a ``"value"`` key (Home Assistant state format):
      ``{"value": 20.5}``

    Args:
        payload: Raw MQTT payload bytes or string.

    Returns:
        Indoor temperature in degrees Celsius as a float.

    Raises:
        ValueError: If the payload cannot be parsed or the value is not finite.
    """
    ...


def parse_outdoor_temp_forecast(payload: Union[bytes, str]) -> list[float]:
    """Parse a per-step outdoor temperature forecast from an MQTT payload.

    The payload must be a JSON array of numbers, one entry per 15-minute step:
    ``[5.0, 5.2, 4.8, 4.5, ...]``

    Values may be integers or floats. All values must be finite.

    Args:
        payload: Raw MQTT payload bytes or string.

    Returns:
        List of outdoor temperatures in degrees Celsius, one per step.

    Raises:
        ValueError: If the payload is not a JSON array, contains non-finite
            values, or is empty.
    """
    ...
```

### `mimirheim/io/mqtt_client.py` — subscribe to BTM topics

In `_build_topic_handlers`, add handlers for each device that has a `building_thermal`
section with an `inputs` sub-section:

```python
# BTM topics for space heating heat pumps.
for sh_cfg in config.space_heating_hps.values():
    if sh_cfg.building_thermal is not None and sh_cfg.building_thermal.inputs is not None:
        btm_in = sh_cfg.building_thermal.inputs
        handlers[btm_in.topic_current_indoor_temp_c] = parse_current_indoor_temp
        handlers[btm_in.topic_outdoor_temp_forecast_c] = parse_outdoor_temp_forecast

# BTM topics for combi heat pumps.
for chp_cfg in config.combi_heat_pumps.values():
    if chp_cfg.building_thermal is not None and chp_cfg.building_thermal.inputs is not None:
        btm_in = chp_cfg.building_thermal.inputs
        handlers[btm_in.topic_current_indoor_temp_c] = parse_current_indoor_temp
        handlers[btm_in.topic_outdoor_temp_forecast_c] = parse_outdoor_temp_forecast
```

### `mimirheim/core/readiness.py` — track BTM topics and assemble bundle

In `__init__`, add BTM topics to `_sensor_topics`:

```python
# BTM sensor topics (presence-required, same as existing SOC sensor topics).
# Both the indoor temp reading and the outdoor temp forecast must have been
# received at least once before solving. The outdoor forecast is treated as a
# sensor topic (presence-only) rather than a coverage-based forecast topic
# because it is a simple list of floats keyed by array index, not a
# timestamped series like the PV or price forecasts.
for sh_cfg in config.space_heating_hps.values():
    if sh_cfg.building_thermal is not None and sh_cfg.building_thermal.inputs is not None:
        btm_in = sh_cfg.building_thermal.inputs
        self._sensor_topics.add(btm_in.topic_current_indoor_temp_c)
        self._sensor_topics.add(btm_in.topic_outdoor_temp_forecast_c)

for chp_cfg in config.combi_heat_pumps.values():
    if chp_cfg.building_thermal is not None and chp_cfg.building_thermal.inputs is not None:
        btm_in = chp_cfg.building_thermal.inputs
        self._sensor_topics.add(btm_in.topic_current_indoor_temp_c)
        self._sensor_topics.add(btm_in.topic_outdoor_temp_forecast_c)
```

In `snapshot()`, assemble BTM inputs into `SpaceHeatingInputs` and `CombiHeatPumpInputs`:

```python
space_heating_inputs: dict[str, SpaceHeatingInputs] = {}
for name, sh_cfg in self._config.space_heating_hps.items():
    btm_inputs: SpaceHeatingInputs
    if sh_cfg.inputs is not None:
        demand_entry = self._entries.get(sh_cfg.inputs.topic_heat_needed_kwh)
        heat_needed = demand_entry[0] if demand_entry is not None else 0.0
    else:
        heat_needed = 0.0

    indoor_temp: float | None = None
    outdoor_forecast: list[float] | None = None
    if sh_cfg.building_thermal is not None and sh_cfg.building_thermal.inputs is not None:
        btm_in = sh_cfg.building_thermal.inputs
        indoor_entry = self._entries.get(btm_in.topic_current_indoor_temp_c)
        outdoor_entry = self._entries.get(btm_in.topic_outdoor_temp_forecast_c)
        if indoor_entry is not None:
            indoor_temp = indoor_entry[0]
        if outdoor_entry is not None:
            outdoor_forecast = outdoor_entry[0]

    space_heating_inputs[name] = SpaceHeatingInputs(
        heat_needed_kwh=heat_needed,
        current_indoor_temp_c=indoor_temp,
        outdoor_temp_forecast_c=outdoor_forecast,
    )
```

Apply the same pattern for `combi_hp_inputs`. Note that `space_heating_inputs` and
`combi_hp_inputs` are already fields on `SolveBundle` from plans 26 and 27; this just
populates them in `snapshot()` (which they were not, prior to this plan).

### `mimirheim/io/ha_discovery.py` — input sensors for BTM

Add sensors for each device with a BTM `inputs` section:

```python
for name, sh_cfg in config.space_heating_hps.items():
    if sh_cfg.building_thermal is not None and sh_cfg.building_thermal.inputs is not None:
        btm = sh_cfg.building_thermal.inputs
        # Indoor temperature reading (real hardware measurement).
        indoor_id = f"{device_id}_{name}_indoor_temp_c"
        _publish(indoor_id, {
            "name": f"{ha.device_name} {name} indoor temperature",
            "unique_id": indoor_id,
            "state_topic": btm.topic_current_indoor_temp_c,
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "availability": availability,
            "device": device_block,
        })
        # Outdoor temperature forecast (raw JSON array for informational visibility).
        outdoor_id = f"{device_id}_{name}_outdoor_temp_forecast"
        _publish(outdoor_id, {
            "name": f"{ha.device_name} {name} outdoor temp forecast",
            "unique_id": outdoor_id,
            "state_topic": btm.topic_outdoor_temp_forecast_c,
            "availability": availability,
            "device": device_block,
        })
```

Apply the same pattern for each entry in `config.combi_heat_pumps`.

### `mimirheim/config/example.yaml` — BTM configuration example

Update the `space_heating_hps` and `combi_heat_pumps` sections to show the optional
`building_thermal` sub-section. Add a comment block before the example explaining when
to use the BTM vs. the degree-days model.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_building_thermal_model.py tests/unit/test_config_schema.py -k "btm"
```

All new tests green.

```bash
uv run pytest
```

Full suite green. No golden file changes: existing scenarios have no `building_thermal`
configured, so the degree-days code path is exercised unchanged.

Manually verify the example.yaml is valid:

```bash
python -c "
import yaml
from mimirheim.config.schema import MimirheimConfig
with open('mimir/config/example.yaml') as f:
    raw = yaml.safe_load(f)
MimirheimConfig.model_validate(raw)
print('config valid')
"
```

---

## Done

```bash
mv plans/28_building_thermal_model.md plans/done/
```
