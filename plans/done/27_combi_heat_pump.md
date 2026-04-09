# Step 27 — Combined DHW and space heating heat pump device

## Prerequisites

This plan requires plans 25 and 26 to be complete. It reuses the tank temperature
model from `ThermalBoilerDevice` and the degree-days heat demand model from
`SpaceHeatingDevice`, but combines them in a single device with a mutual exclusion
constraint between DHW and space heating modes.

## Architectural note: why a separate device class

A combined heat pump is **not** simply a `ThermalBoilerDevice` plus a
`SpaceHeatingDevice` placed side by side in the config. The two devices would compete
independently for grid power with no awareness of each other. The mutual exclusion
constraint — "at most one of DHW or space heating at any step" — cannot be expressed
across two independent device instances. A single device is required.

A combi HP also typically has **different COPs for the two modes**. Heating tap water
to 55°C requires a larger temperature lift than heating the floor circuit at 35°C,
which typically gives a higher COP in space heating mode. These must be separate
parameters.

## References

- IMPLEMENTATION_DETAILS §7, subsection "Device Protocol"
- `mimirheim/devices/thermal_boiler.py` (plan 25) — tank temperature model
- `mimirheim/devices/space_heating.py` (plan 26) — degree-days heat demand and min run
- `mimirheim/config/schema.py` — `HeatingStage` (from plan 26)

---

## Files to create

- `mimirheim/devices/combi_heat_pump.py`
- `tests/unit/test_combi_heat_pump_constraints.py`

## Files to modify

- `mimirheim/config/schema.py` — new `CombiHeatPumpInputsConfig`, `CombiHeatPumpConfig`;
  `MimirheimConfig.combi_heat_pumps`
- `mimirheim/core/bundle.py` — new `CombiHeatPumpInputs`, field on `SolveBundle`
- `mimirheim/config/schema.py` — `MimirheimConfig.device_names_unique` validator
- `mimirheim/core/model_builder.py` — wire device into solve loop
- `mimirheim/io/input_parser.py` — parse MQTT inputs
- `mimirheim/io/mqtt_publisher.py` — publish mode setpoint and temperature setpoint
- `tests/unit/test_config_schema.py` — config validation tests

---

## Tests first

Create `tests/unit/test_combi_heat_pump_constraints.py`. Use a real solver with `T=8`,
`dt=0.25`. Base fixture: `CombiHeatPumpConfig` with `elec_power_kw=6.0`,
`cop_dhw=2.8`, `cop_sh=3.8`, `volume_liters=200`, `setpoint_c=55.0`,
`min_temp_c=40.0`, `cooling_rate_k_per_hour=2.0`, `min_run_steps=4`, and
`CombiHeatPumpInputs` with `current_temp_c=45.0`, `heat_needed_kwh=5.0`.

- `test_combi_mutual_exclusion_no_simultaneous_modes` — at every step, assert that
  `dhw_mode[t] + sh_mode[t] <= 1` is satisfied. Specifically: provide conditions
  where both modes are needed and confirm the solver never sets both to 1 at the same
  step.
- `test_combi_dhw_tank_tracks_temperature` — set `heat_needed_kwh=0.0` (no space
  heating needed) and force the solver to heat the DHW tank. Assert `T_tank` increases
  when `dhw_mode[t]=1` at the same rate as a standalone `ThermalBoilerDevice` with
  `cop=cop_dhw`.
- `test_combi_sh_produces_required_heat` — set `current_temp_c` high enough that no
  DHW heating is needed in the horizon (tank already at setpoint). Assert that the
  solver schedules enough SH operation to satisfy `heat_needed_kwh`.
- `test_combi_dhw_and_sh_both_needed_within_horizon` — configure a scenario where the
  DHW tank needs ~2 steps of heating (to stay above `min_temp_c`) and `heat_needed_kwh`
  requires ~4 SH steps. Assert that the final solution satisfies both constraints with
  no overlap in active modes.
- `test_combi_min_run_steps_respected` — set a price profile that would prefer
  isolated single steps. Assert the HP runs in consecutive blocks of at least
  `min_run_steps` once started, regardless of mode.
- `test_combi_net_power_negative_when_running` — at any step where either mode is
  active, `device.net_power(t)` evaluates to approximately `−elec_power_kw`.
- `test_combi_terminal_value_prevents_tank_drain` — uniform price, T=8. Without
  terminal value the solver drains the DHW tank to `min_temp_c`. Assert the terminal
  value term prevents this (tank temperature at T-1 is meaningfully above `min_temp_c`).
- `test_combi_cop_difference_affects_mode_preference` — set step 0 price very cheap
  and both DHW and SH needed. When `cop_sh > cop_dhw`, the solver should prefer SH
  at cheap steps (more thermal output per kWh). Assert the mode allocation reflects
  the COP difference.

Add to `tests/unit/test_config_schema.py`:
- `test_combi_cop_dhw_and_cop_sh_both_positive` — either COP <= 0 raises.
- `test_combi_min_temp_below_setpoint` — `min_temp_c >= setpoint_c` raises.

Run `uv run pytest tests/unit/test_combi_heat_pump_constraints.py tests/unit/test_config_schema.py -k "combi"` — all tests must fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py` — new models

**`CombiHeatPumpInputsConfig`:**

```python
class CombiHeatPumpInputsConfig(BaseModel):
    """MQTT input topics for live combined heat pump state values."""

    model_config = ConfigDict(extra="forbid")

    topic_current_temp: str = Field(
        description="MQTT topic for current DHW water temperature in °C, retained."
    )
    topic_heat_needed_kwh: str = Field(
        description="MQTT topic for space heating demand in kWh this horizon, retained."
    )
```

**`CombiHeatPumpConfig`:**

```python
class CombiHeatPumpConfig(BaseModel):
    """Configuration for a combined DHW and space heating heat pump.

    A combi heat pump can operate in two mutually exclusive modes each step:
    DHW mode heats the hot water tank; SH mode delivers heat to the space
    heating circuit. The device cannot do both simultaneously.

    DHW mode typically has a lower COP than SH mode because heating water to
    55°C requires a larger temperature lift than heating a floor circuit to
    35°C. Separate cop_dhw and cop_sh fields capture this difference.

    The tank model in DHW mode is identical to ThermalBoilerDevice (plan 25).
    The space heating model is identical to SpaceHeatingDevice in on/off mode
    (plan 26). Power-stage SOS2 control is not supported for the combined device
    in this plan; add it in a subsequent plan if needed.

    Attributes:
        elec_power_kw: Rated electrical power in kW. Applied in both modes at
            the same compressor power level.
        cop_dhw: COP in DHW (hot water) mode.
        cop_sh: COP in space heating mode.
        volume_liters: DHW tank water volume.
        setpoint_c: DHW target temperature in °C.
        min_temp_c: DHW minimum allowable temperature in °C.
        cooling_rate_k_per_hour: DHW tank cooling rate (standby losses + draw).
        min_run_steps: Minimum consecutive run length across all modes combined.
        wear_cost_eur_per_kwh: Cycling cost per kWh electrical consumption.
        inputs: MQTT input topic configuration.
    """

    model_config = ConfigDict(extra="forbid")

    elec_power_kw: float = Field(gt=0)
    cop_dhw: float = Field(gt=0)
    cop_sh: float = Field(gt=0)
    volume_liters: float = Field(gt=0)
    setpoint_c: float
    min_temp_c: float = Field(default=40.0)
    cooling_rate_k_per_hour: float = Field(ge=0)
    min_run_steps: int = Field(ge=0, default=4)
    wear_cost_eur_per_kwh: float = Field(ge=0, default=0.0)
    inputs: CombiHeatPumpInputsConfig

    @model_validator(mode="after")
    def _validate_temp_range(self) -> "CombiHeatPumpConfig":
        if self.min_temp_c >= self.setpoint_c:
            raise ValueError(
                f"min_temp_c ({self.min_temp_c}) must be strictly less than "
                f"setpoint_c ({self.setpoint_c})"
            )
        return self
```

Add `combi_heat_pumps: dict[str, CombiHeatPumpConfig] = Field(default_factory=dict)` to
`MimirheimConfig`, extend `device_names_unique` to include `*self.combi_heat_pumps`.

### `mimirheim/core/bundle.py` — new input model

```python
class CombiHeatPumpInputs(BaseModel):
    """Live combined heat pump state received from MQTT."""

    model_config = ConfigDict(extra="forbid")

    current_temp_c: float = Field(
        description="Current DHW water temperature in °C."
    )
    heat_needed_kwh: float = Field(
        ge=0.0,
        description="Total space heating thermal energy required this horizon, in kWh.",
    )
```

Add to `SolveBundle`:
```python
combi_hp_inputs: dict[str, CombiHeatPumpInputs] = Field(
    default_factory=dict,
    description="Keyed by combi heat pump device name. Empty if none configured.",
)
```

### `mimirheim/devices/combi_heat_pump.py`

**Variables (`add_variables`):**

For each `t`:
```python
# T_tank[t]: DHW water temperature, bounds [min_temp_c - 5, setpoint_c + 5].
self._T_tank[t] = ctx.solver.add_var(lb=..., ub=..., name=f"chp_T_tank_{t}")

# dhw_mode[t]: binary, 1 = HP is heating DHW tank at step t.
self._dhw_mode[t] = ctx.solver.add_var(var_type="B", name=f"chp_dhw_{t}")

# sh_mode[t]: binary, 1 = HP is delivering space heating at step t.
self._sh_mode[t] = ctx.solver.add_var(var_type="B", name=f"chp_sh_{t}")

# hp_on[t]: binary, 1 = HP is running in any mode at step t.
# Derived: hp_on[t] = dhw_mode[t] + sh_mode[t]. Used by the minimum run
# constraint to enforce consecutive operation across mode switches.
self._hp_on[t] = ctx.solver.add_var(var_type="B", name=f"chp_on_{t}")
```

If `config.min_run_steps > 1`: add `start[t]` binaries (same pattern as plan 25).

**Constraints (`add_constraints`, receives `CombiHeatPumpInputs`):**

Pre-compute thermal parameters:
```python
thermal_cap = config.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K  # kWh/K
cool_per_step = config.cooling_rate_k_per_hour * ctx.dt  # K/step
dhw_heat_rise = config.elec_power_kw * config.cop_dhw * ctx.dt / thermal_cap  # K/step in DHW mode
```

**Mutual exclusion:**
```python
for t in ctx.T:
    # At each step the HP can be in DHW mode, SH mode, or off. Not both
    # simultaneously: a real heat pump has one refrigerant circuit.
    ctx.solver.add_constraint(self._dhw_mode[t] + self._sh_mode[t] <= 1)

    # hp_on[t] is the logical OR of the two modes. Under minimisation, the
    # solver will never set hp_on[t]=1 unless at least one mode is active.
    ctx.solver.add_constraint(self._hp_on[t] == self._dhw_mode[t] + self._sh_mode[t])
```

**DHW tank dynamics:** for each `t`:
```python
# Same dynamics as ThermalBoilerDevice, but only dhw_mode[t] contributes heat
# (not sh_mode[t]). When the HP is in SH mode the tank still cools at the
# standard rate — the HP's heat is directed to the floor circuit, not the tank.
prior_temp = inputs.current_temp_c if t == 0 else self._T_tank[t - 1]
ctx.solver.add_constraint(
    self._T_tank[t] == prior_temp - cool_per_step + dhw_heat_rise * self._dhw_mode[t]
)
ctx.solver.add_constraint(self._T_tank[t] >= config.min_temp_c)
ctx.solver.add_constraint(self._T_tank[t] <= config.setpoint_c)
```

**Space heating total heat constraint:**
```python
# When SH mode is active, the HP produces cop_sh × elec_power_kw × dt kWh of
# thermal heat per step. The sum over all SH steps must meet the demand.
sh_thermal_output = [
    config.elec_power_kw * config.cop_sh * ctx.dt * self._sh_mode[t]
    for t in ctx.T
]
if inputs.heat_needed_kwh > 0:
    ctx.solver.add_constraint(xsum(sh_thermal_output) >= inputs.heat_needed_kwh)
```

**Minimum run length** (same pattern as plan 25, but applied to `hp_on[t]`):
```python
# The minimum run constraint applies to hp_on[t] — the HP must run for at
# least min_run_steps consecutive steps once started, regardless of whether
# it switches between DHW and SH mode during that run. Mode switches within
# a running block are permitted; only total off→on transitions are constrained.
```

**`net_power(t)`:**
```python
# The HP draws elec_power_kw in both modes. The net power is negative whenever
# the HP is running (consuming from the AC bus).
return -config.elec_power_kw * self._hp_on[t]
```

**`objective_terms(t)`:**
```python
if config.wear_cost_eur_per_kwh > 0:
    return config.wear_cost_eur_per_kwh * config.elec_power_kw * self._hp_on[t] * ctx.dt
return 0
```

**`terminal_soc_var(ctx)`** — DHW terminal value (same mechanism as plan 25):
```python
thermal_cap = config.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K
return (self._T_tank[ctx.T[-1]] - config.min_temp_c) * thermal_cap / config.cop_dhw
```

The COP used here is `cop_dhw` because that is the mode by which the solver would
re-heat the tank after the horizon ends (DHW mode, charging the tank).

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_combi_heat_pump_constraints.py tests/unit/test_config_schema.py
```

All tests green.

```bash
uv run pytest tests/scenarios/ tests/unit/
```

All pre-existing tests remain green. No golden file changes.

---

## Done

```bash
mv plans/27_combi_heat_pump.md plans/done/
```
