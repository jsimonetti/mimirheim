# Step 26 — Space heating heat pump device

## Architectural note: why no building thermal model

This plan does not introduce indoor temperature tracking or building thermal mass. The
simpler **degree-days demand** approach is sufficient for 24-hour scheduling:

1. Compute the total heat the building needs today from meteorological data:
   `heat_needed_kwh = max(0, degree_days × degree_days_factor − heat_already_produced)`
2. Solve for the cheapest time slots within the day to produce that total.

This avoids requiring per-step indoor temperature forecasts, a building thermal
inertia model, or a comfort envelope model. It is the correct scope for a first
implementation. The model is not reduced in usefulness: it correctly shifts heating
loads to cheap price slots, which is the primary value of a space heating optimiser.

**The one thing degree-days cannot do** is pre-heat a building in advance of an
expensive period and coast during that period. That requires a building thermal model
(wall/slab thermal mass, per-step indoor temperature state variable). This is a natural
future step (plan 28) once the simpler device is in production and you know whether the
added complexity justifies itself.

**How `heat_needed_kwh` arrives in mimirheim:**

The degree-days calculation requires average outdoor temperature, which comes from a
weather service. Rather than integrating a weather service into mimirheim, `heat_needed_kwh`
is published to a configured MQTT topic by the user's home automation system
(Home Assistant, Node-RED, or a sidecar script). This keeps mimirheim a pure MQTT consumer.

## References

- IMPLEMENTATION_DETAILS §7, subsection "Device Protocol"
- IMPLEMENTATION_DETAILS §8, subsection "Piecewise efficiency (battery and EV)"
- `mimirheim/devices/thermal_boiler.py` (plan 25) — minimum run length constraint
- `mimirheim/core/objective.py` — `_terminal_soc_terms` (not applicable here; no state)

---

## Files to create

- `mimirheim/devices/space_heating.py`
- `tests/unit/test_space_heating_constraints.py`

## Files to modify

- `mimirheim/config/schema.py` — new `HeatingStage`, `SpaceHeatingInputsConfig`,
  `SpaceHeatingConfig`; `MimirheimConfig.space_heating_hps`
- `mimirheim/core/bundle.py` — new `SpaceHeatingInputs`, field on `SolveBundle`
- `mimirheim/config/schema.py` — `MimirheimConfig.device_names_unique` validator
- `mimirheim/core/model_builder.py` — wire device into solve loop
- `mimirheim/io/input_parser.py` — parse MQTT inputs
- `mimirheim/io/mqtt_publisher.py` — publish setpoints
- `tests/unit/test_config_schema.py` — config validation tests

---

## Tests first

Create `tests/unit/test_space_heating_constraints.py`. Use a real solver with `T=8`,
`dt=0.25`. Base fixture: `SpaceHeatingConfig` with `elec_power_kw=5.0`, `cop=3.5`
(on/off mode, single stage), `min_run_steps=4`, and `SpaceHeatingInputs` with
`heat_needed_kwh=7.0`.

- `test_space_heating_produces_required_heat` — with uniform price, assert that
  `sum(thermal_output[t] for t in T) >= heat_needed_kwh` is satisfied in the solution,
  where `thermal_output[t] = cop * P_el_kw * dt` when the HP is on.
- `test_space_heating_schedules_at_cheap_steps` — T=8 steps. Steps 0–3 cost
  1.0 EUR/kWh; steps 4–7 cost 0.05 EUR/kWh. `heat_needed_kwh` requires 4 steps of
  full-power operation. Assert all 4 active steps fall in [4, 7].
- `test_space_heating_min_run_steps_respected` — price profile that would prefer
  2 separate single-step bursts. Assert that if the HP runs at all, it runs in
  consecutive blocks of at least `min_run_steps` steps.
- `test_space_heating_zero_demand_produces_no_heat` — `heat_needed_kwh=0.0`. Assert
  all `hp_on[t] == 0` and electrical consumption is zero.
- `test_space_heating_net_power_negative_when_on` — at any step where `hp_on[t]=1`,
  `device.net_power(t)` evaluates to approximately `−elec_power_kw`.
- `test_space_heating_power_stages_sos2_respects_heat_total` — with two stages
  (stage 0: sentinel at 0 kW COP 0; stage 1: 3 kW at COP 3.0; stage 2: 5 kW at
  COP 3.5), set `heat_needed_kwh` achievable only at full power. Assert solver reaches
  full power at some step.
- `test_space_heating_power_stages_at_most_two_adjacent_nonzero` — with SOS2 stages,
  at each step at most two adjacent stage weights are nonzero.

Add to `tests/unit/test_config_schema.py`:
- `test_space_heating_stages_must_start_with_zero_power` — first stage `elec_kw != 0`
  raises.
- `test_space_heating_stages_must_be_strictly_increasing_power` — duplicate power
  values raise.
- `test_space_heating_on_off_and_stages_mutually_exclusive` — providing both
  `stages` and `elec_power_kw` raises.

Run `uv run pytest tests/unit/test_space_heating_constraints.py tests/unit/test_config_schema.py -k "space_heating"` — all tests must fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py` — new models

**`HeatingStage`** — one point on the HP's electrical-to-thermal power curve:

```python
class HeatingStage(BaseModel):
    """A single operating point on a heat pump's power curve.

    Used in the SOS2 power-stage model for space heating heat pumps. Each
    stage corresponds to a compressor operating mode (e.g. minimum, half,
    full power).

    The first stage must always be at elec_kw=0.0 with cop=0.0. It acts as a
    sentinel for the SOS2 weight that represents the HP being off. Without this
    sentinel, the SOS2 constraint cannot model the off state.

    Attributes:
        elec_kw: Electrical power consumed at this operating point, in kW.
            Must be 0.0 for the first stage. Strictly increasing across stages.
        cop: Coefficient of performance at this operating point. cop=3.5 means
            1 kW of electricity produces 3.5 kW of heat. Must be 0.0 for the
            first (zero-power sentinel) stage.
    """

    model_config = ConfigDict(extra="forbid")

    elec_kw: float = Field(ge=0.0)
    cop: float = Field(ge=0.0)
```

**`SpaceHeatingInputsConfig`** — MQTT input topic configuration:

```python
class SpaceHeatingInputsConfig(BaseModel):
    """MQTT input topics for live space heating state values."""

    model_config = ConfigDict(extra="forbid")

    topic_heat_needed_kwh: str = Field(
        description=(
            "MQTT topic publishing the total thermal energy in kWh that must be "
            "produced this horizon, retained. Computed externally from degree-days "
            "and heat already produced today. Set to 0.0 when no heating is needed."
        )
    )
    topic_heat_produced_today_kwh: str | None = Field(
        default=None,
        description=(
            "Optional MQTT topic for accumulated thermal energy already produced today "
            "in kWh. When provided, the external system publishes this value and "
            "heat_needed_kwh is expected to already subtract it. Kept for reference "
            "only; not read by the solver."
        ),
    )
```

**`SpaceHeatingConfig`:**

```python
class SpaceHeatingConfig(BaseModel):
    """Configuration for a space heating heat pump.

    Two control modes are available. Set exactly one:

    On/off mode: Provide ``elec_power_kw`` and ``cop``. The solver uses a
    single binary variable per step. The HP either runs at full power or is
    off. Simple and works for non-inverter-driven (fixed-speed) compressors.

    Power-stage mode (modulating): Provide ``stages`` — a list of HeatingStage
    objects including a mandatory zero-power sentinel as stage 0. The solver uses
    SOS2 weight variables per step to model partial-load operation. Suitable for
    inverter-driven heat pumps that modulate power continuously.

    Attributes:
        elec_power_kw: Rated electrical power for on/off mode, in kW. Mutually
            exclusive with stages.
        cop: Coefficient of performance for on/off mode. Mutually exclusive
            with stages.
        stages: List of operating points for power-stage (SOS2) mode. Stage 0
            must have elec_kw=0.0, cop=0.0 (the off sentinel). Mutually
            exclusive with elec_power_kw and cop.
        min_run_steps: Minimum consecutive 15-minute steps the HP must run
            once started. Use 4 (one hour) for most heat pump compressors.
            Set to 0 or 1 to disable the minimum run constraint.
        wear_cost_eur_per_kwh: Cycling cost per kWh electrical consumption.
        inputs: MQTT input topic configuration.
    """

    model_config = ConfigDict(extra="forbid")

    elec_power_kw: float | None = Field(default=None, gt=0)
    cop: float | None = Field(default=None, gt=0)
    stages: list[HeatingStage] | None = Field(default=None, min_length=2)
    min_run_steps: int = Field(ge=0, default=4)
    wear_cost_eur_per_kwh: float = Field(ge=0, default=0.0)
    inputs: SpaceHeatingInputsConfig

    @model_validator(mode="after")
    def _validate_mode(self) -> "SpaceHeatingConfig":
        on_off = self.elec_power_kw is not None or self.cop is not None
        staged = self.stages is not None
        if on_off and staged:
            raise ValueError(
                "Provide either (elec_power_kw + cop) for on/off mode or stages "
                "for power-stage mode, not both."
            )
        if not on_off and not staged:
            raise ValueError("Provide either (elec_power_kw + cop) or stages.")
        if on_off and (self.elec_power_kw is None or self.cop is None):
            raise ValueError("On/off mode requires both elec_power_kw and cop.")
        if staged:
            powers = [s.elec_kw for s in self.stages]
            if powers[0] != 0.0:
                raise ValueError("First stage must have elec_kw=0.0 (off sentinel).")
            if len(powers) != len(set(powers)):
                raise ValueError("Stage elec_kw values must be strictly increasing.")
        return self
```

Add `space_heating_hps: dict[str, SpaceHeatingConfig] = Field(default_factory=dict)` to
`MimirheimConfig`, extend `device_names_unique` to include `*self.space_heating_hps`.

### `mimirheim/core/bundle.py` — new input model

```python
class SpaceHeatingInputs(BaseModel):
    """Live space heating demand received from MQTT."""

    model_config = ConfigDict(extra="forbid")

    heat_needed_kwh: float = Field(
        ge=0.0,
        description=(
            "Total thermal energy in kWh to produce this horizon. Zero means no "
            "heating is currently needed."
        ),
    )
```

Add to `SolveBundle`:
```python
space_heating_inputs: dict[str, SpaceHeatingInputs] = Field(
    default_factory=dict,
    description="Keyed by space heating device name. Empty if none configured.",
)
```

### `mimirheim/devices/space_heating.py`

The device implements the Device Protocol.

**On/off mode variables (`add_variables`, `mode == "on_off"`):**

For each `t`:
```python
# hp_on[t]: binary, 1 when the HP is running at rated power.
self._hp_on[t] = ctx.solver.add_var(var_type="B", name=f"sh_hp_on_{t}")
```

If `config.min_run_steps > 1`, also add `start[t]` binaries (same pattern as
ThermalBoilerDevice plan 25).

**Power-stage (SOS2) mode variables (`mode == "staged"`):**

For each `t`:
```python
S = len(config.stages)
# w[t, s]: SOS2 weight for stage s at step t. At most two adjacent weights
# may be nonzero (enforced by the SOS2 constraint below).
self._w[t] = [ctx.solver.add_var(lb=0.0, ub=1.0, name=f"sh_w_{t}_{s}") for s in range(S)]
ctx.solver.add_sos2(self._w[t], [s.elec_kw for s in config.stages])
```

Also add `hp_on[t]` binary (= 1 when any non-zero stage is active), needed for the
minimum run constraint.

**Constraints (`add_constraints`, receives `SpaceHeatingInputs`):**

If `inputs.heat_needed_kwh == 0.0`, pin all controls to zero:
```python
for t in ctx.T:
    ctx.solver.add_constraint(self._hp_on[t] == 0)
```
Return early — no other constraints needed.

**On/off mode:**

Total heat constraint:
```python
# The sum of thermal output over the horizon must be at least heat_needed_kwh.
# This is a lower-bound constraint, not equality: the solver may produce
# slightly more heat than needed if the price profile makes it favourable,
# but it will never produce less.
total_heat = xsum(config.elec_power_kw * config.cop * ctx.dt * self._hp_on[t] for t in ctx.T)
ctx.solver.add_constraint(total_heat >= inputs.heat_needed_kwh)
```

Then add the minimum run constraint (same pattern as ThermalBoilerDevice).

**Power-stage mode:**

For each `t`:
```python
# Convex-combination constraint: weights sum to 1.
ctx.solver.add_constraint(xsum(self._w[t][s] for s in range(S)) == 1)

# hp_on[t] = 1 when any non-zero-power stage is active (i.e. any stage
# other than the sentinel stage 0). This is used by the minimum run
# constraint.
ctx.solver.add_constraint(
    self._hp_on[t] == xsum(self._w[t][s] for s in range(1, S))
)
```

Total heat constraint:
```python
thermal_output_vars = [
    xsum(self._w[t][s] * config.stages[s].elec_kw * config.stages[s].cop * ctx.dt
         for s in range(S))
    for t in ctx.T
]
ctx.solver.add_constraint(xsum(thermal_output_vars) >= inputs.heat_needed_kwh)
```

**`net_power(t)`:**

On/off mode:
```python
return -config.elec_power_kw * self._hp_on[t]
```

Staged mode:
```python
# Electrical consumption is the convex combination of stage powers.
return -xsum(self._w[t][s] * config.stages[s].elec_kw for s in range(S))
```

**`objective_terms(t)`:**

On/off:
```python
if config.wear_cost_eur_per_kwh > 0:
    return config.wear_cost_eur_per_kwh * config.elec_power_kw * self._hp_on[t] * ctx.dt
return 0
```

Staged:
```python
elec_consumption = xsum(self._w[t][s] * config.stages[s].elec_kw for s in range(S))
if config.wear_cost_eur_per_kwh > 0:
    return config.wear_cost_eur_per_kwh * elec_consumption * ctx.dt
return 0
```

**No `terminal_soc_var`:** space heating has no tank state variable. The device does
not participate in the terminal value mechanism.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_space_heating_constraints.py tests/unit/test_config_schema.py
```

All tests green.

```bash
uv run pytest tests/scenarios/
```

No golden file changes — existing scenarios have no space heating configured.

---

## Done

```bash
mv plans/26_space_heating.md plans/done/
```
