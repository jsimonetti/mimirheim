# 8. MIP model design: device contract, split variables, piecewise efficiency, and objective builder

## ModelContext

`ModelContext` is a short-lived container created once per solve in `build_and_solve()` and threaded through every model-building call. It holds the three things every device and the objective builder need without being passed as individual arguments:

```python
# mimirheim/core/context.py
from mimirheim.core.solver_backend import SolverBackend

class ModelContext:
    def __init__(self, solver: SolverBackend, horizon: int, dt: float) -> None:
        self.solver = solver     # the live solver instance — devices add variables and constraints here
        self.T = range(horizon)  # time index; len(T) == len(bundle.horizon_prices)
        self.dt = dt             # time step duration in hours (0.25 for quarter-hourly)
```

`ModelContext` does **not** carry `SolveBundle` or `MimirheimConfig`. Those are passed explicitly where needed so that the data flow stays visible at each call site. Devices receive their slice of the bundle via `add_constraints(ctx, inputs)` and read their config from `self.config` (set at construction per §6).

`dt` is always `0.25` (15 minutes). The horizon length $H$ is variable — it equals the number of 15-minute steps between `solve_start` and `horizon_end` as computed by `compute_horizon_steps()`. The time step duration is fixed regardless of horizon length.

## Device method contract

Every device class in `mimirheim/devices/` must implement four methods. The model builder calls them in order without knowing the device type:

```python
class Device(Protocol):
    name: str

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MIP variables owned by this device."""

    def add_constraints(self, ctx: ModelContext, inputs: DeviceInputs) -> None:
        """Add physics constraints. inputs carries the validated MQTT state for this device."""

    def net_power(self, t: int) -> LinExpr:
        """Return the net power expression at step t. Positive = producing, negative = consuming.
        Used by the system power balance constraint."""

    def objective_terms(self, t: int) -> LinExpr:
        """Return any cost or penalty terms this device contributes to the objective at step t.
        For most devices this is zero. Battery and EV return a wear cost."""
```

`DeviceInputs` is a union of the per-device input models from `SolveBundle` — the model builder slices the bundle and passes the relevant piece to each device.

`LinExpr` is the solver expression type produced when device variables are combined arithmetically (e.g. `self.charge_seg[t, 0] + self.charge_seg[t, 1]`). It is whatever type `SolverBackend.add_var()` returns when added together. Device code must only return expressions formed from its own variables — never raw numeric values, and never variables belonging to another device.

## Grid device

`Grid` is architecturally different from other devices. There is exactly one instance per solve (config has a single `grid:` section, not a named map), and its variables are the primary economic variables that `ObjectiveBuilder` references directly.

```python
# mimirheim/devices/grid.py
class Grid:
    def __init__(self, config: GridConfig) -> None: ...

    def add_variables(self, ctx: ModelContext) -> None:
        # declares import_[t], export_[t], and _grid_dir[t] for each t in ctx.T
        # import_[t] and export_[t] bounds come from config.import_limit_kw / export_limit_kw
        # _grid_dir[t] is a binary: 0 = import step, 1 = export step

    def add_constraints(self, ctx: ModelContext, inputs: None) -> None:
        # Big-M constraints couple the direction binary to the flow variables:
        # import_[t] <= import_limit_kw * (1 - _grid_dir[t])   # zero on export steps
        # export_[t] <= export_limit_kw *       _grid_dir[t]    # zero on import steps

    def net_power(self, t: int) -> LinExpr:
        return self.import_[t] - self.export_[t]   # positive = net import

    def objective_terms(self, t: int) -> LinExpr:
        return 0   # economics are handled by ObjectiveBuilder, not Grid itself
```

`Grid` receives `inputs=None` because it has no MQTT state — its physical constraints come entirely from config. `ObjectiveBuilder` holds a reference to the `Grid` instance so it can access `grid.import_[t]` and `grid.export_[t]` directly. No other component references Grid variables.

**Preventing simultaneous import and export** — a physical grid connection cannot carry power in both directions at the same time step. A single binary `_grid_dir[t]` per step encodes the allowed direction. One binary (rather than two) is sufficient because the two Big-M constraints above already enforce mutual exclusion: when `_grid_dir[t] = 0`, export is forced to zero; when `_grid_dir[t] = 1`, import is forced to zero. A separate mutual-exclusion constraint is not needed. This adds T binary variables, exactly half the count of a two-sentinel formulation.

## Split charge/discharge variables

Battery and EV use two non-negative variables per time step rather than one signed variable:

```
charge[t]    ∈ [0, max_charge_kw]
discharge[t] ∈ [0, max_discharge_kw]
net_power[t]  = discharge[t] - charge[t]
```

A single signed variable would require one efficiency constant and cannot model asymmetric charge/discharge losses. The split enables per-direction efficiency and piecewise efficiency segments (see below).

**Preventing simultaneous charge and discharge** — without a guard the solver can charge and discharge at the same time to exploit any efficiency spread as free energy. Prevent this with a binary `mode[t]` variable and Big-M constraints:

```
mode[t] ∈ {0, 1}          (1 = charging, 0 = discharging)
charge[t]    ≤ max_charge_kw    × mode[t]
discharge[t] ≤ max_discharge_kw × (1 − mode[t])
```

This adds one binary per time step. The guard is always applied unconditionally — the solver always enforces that a device cannot charge and discharge simultaneously. This is a mathematical necessity, not a hardware setting: without the guard the LP can exploit any efficiency spread as free energy, producing a physically meaningless solution.

## Anti-roundtrip direction binary

With two or more batteries (or two or more EV chargers), each device's own `mode[t]` binary (see above) only rules out that device charging and discharging in the same step — it says nothing about two different devices choosing opposite directions in the same step (one charges while another discharges). Because each device's efficiency and wear cost is paid independently, such a roundtrip always delivers less net stored energy than the alternative (both idle, or only one charging). The objective's efficiency and wear terms already penalise this economically, but under edge-case numeric conditions (e.g. a degenerate LP where several solutions tie on cost) the solver can still produce a roundtripping schedule.

`mimirheim/core/model_builder.py` closes this gap directly: when two or more batteries are present, it creates one shared `bat_system_mode[t]` binary per step (before calling `add_variables`, so no device creates its own per-device copy alongside it) and hands it to every battery via `set_external_mode`. All batteries then share the same charge/discharge direction at each step, ruling out the roundtrip by construction rather than relying on the objective to make it unattractive. The same binary is created for EV chargers when two or more are configured, regardless of V2H capability — for a charge-only EV the discharge bound is trivially non-binding, so sharing the binary is harmless but keeps the activation logic uniform across all multi-device cases.

With only one battery (or one EV) present, this is unnecessary: there is no second device to roundtrip against, so the per-device `mode[t]` created by `add_variables` is used unchanged. The added cost is one binary variable per time step per device group — negligible at residential MILP scale.

## Idle-state binary (`active[t]`)

Battery and EV support an optional minimum operating power floor per direction (`min_charge_kw`, `min_discharge_kw`): hardware such as a DC-coupled inverter with a minimum PWM duty cycle cannot safely run below a threshold, so the floor expresses "run at or above this power, or do not run at all".

`mode[t]` (the charge/discharge direction binary) cannot express this alone when both floors are configured. `mode[t]` is binary and selects a direction, not an activity level: gating the charge floor on `mode[t]` and the discharge floor on `(1 - mode[t])` leaves no value of `mode[t]` under which the device can sit still — idling becomes infeasible and the device is forced to cycle on every step regardless of price.

`active[t]` supplies the missing third state: `active[t] = 0` holds the device at rest; `active[t] = 1` permits it to run in the direction `mode[t]` selects and arms whichever floor applies to that direction. With `active[t]` present, four states are reachable per step: idle, charge at or above `min_charge_kw`, discharge at or above `min_discharge_kw`, and an unfloored direction is still free down to zero. The constraint shape is:

```
charge_ac    ≤ max_charge_kw    × active[t]
discharge_ac ≤ max_discharge_kw × active[t]
charge_ac    ≥ min_charge_kw    × (mode[t] + active[t] - 1)
discharge_ac ≥ min_discharge_kw × (active[t] - mode[t])
```

The first two force both directions to zero when at rest (without them the solver could set `active[t] = 0` and still charge, escaping the floor entirely). The last two are Big-M floors: the right-hand side is only positive in the one mode/active combination each floor applies to, and zero or negative — hence slack — in every other combination.

**When `active[t]` is not created.** With at most one floor configured, the unfloored direction can always be driven to zero, so idling is already reachable through `mode[t]` alone and no extra binary is needed — this keeps the common case (no floors configured) variable-free.

**Why a shared `mode[t]` changes the requirement.** The reasoning above assumes a device owns its `mode[t]`. Once several devices share one direction binary (the anti-roundtrip binary above), a neighbour that wants to charge sets `mode[t] = 1` for everyone, and a device with only `min_charge_kw` set is then forced to charge whether or not it has headroom — one device at capacity would make the entire charging direction infeasible for the group, the opposite of what sharing is for. So when the mode is shared, either floor alone is enough to require `active[t]`, not just both together.

This applies identically to `Battery` (`mimirheim/devices/battery.py`) and `EvDevice` (`mimirheim/devices/ev.py`).

## Piecewise efficiency (battery and EV)

A single efficiency constant `η` cannot model the real behaviour of batteries and EV chargers, where efficiency varies with operating power. mimirheim uses piecewise linear segments to approximate the efficiency curve while keeping the model fully linear.

Each charge and discharge direction is split into segments, each with its own efficiency:

```python
class EfficiencySegment(BaseModel):
    power_max_kw: float = Field(gt=0)
    efficiency: float = Field(gt=0, le=1.0)
```

In config:

```yaml
batteries:
  battery_main:
    capacity_kwh: 10.0
    charge_segments:
      - { power_max_kw: 1.5, efficiency: 0.90 }
      - { power_max_kw: 1.0, efficiency: 0.95 }   # up to 2.5 kW total
    discharge_segments:
      - { power_max_kw: 2.5, efficiency: 0.95 }
```

For each direction the total power is the sum of the segment variables, each bounded by its segment's `power_max_kw`. The SOC update uses per-segment efficiency:

```
charge_seg[t, i]    ∈ [0, segment_i.power_max_kw]
total_charge[t]      = Σ_i charge_seg[t, i]
energy_stored[t]     = Σ_i segment_i.efficiency × charge_seg[t, i] × dt

discharge_seg[t, i] ∈ [0, segment_i.power_max_kw]
total_discharge[t]   = Σ_i discharge_seg[t, i]
energy_drawn[t]      = Σ_i (1 / segment_i.efficiency) × discharge_seg[t, i] × dt

soc[t] = soc[t−1] + energy_stored[t] − energy_drawn[t]
```

All constraints remain linear. The solver fills lower-efficiency segments first when the efficiency ordering is monotone — no binary variables are needed to enforce segment order for a concave efficiency curve. If the curve is not concave (e.g. efficiency dips in the middle), binary activation variables can be added per segment, but this is not expected for v1 batteries.

The same segment structure applies to EV charging. EV discharging (V2H) uses discharge segments if the hardware supports it; otherwise `discharge_segments` is empty.

**Segment count guidance:** 2–3 segments per direction is sufficient for most residential hardware. More segments increase binary count and solve time without meaningful accuracy gain.

**Single segment = power limit with flat efficiency.** A device with a hardware power cap but no known efficiency curve is expressed as one segment: `{ power_max_kw: 5.0, efficiency: 0.95 }`. The segment variable is bounded `∈ [0, 5.0]`, which is the power constraint. There is no separate `max_charge_kw` field — the sum of all segment `power_max_kw` values is the maximum power for that direction. This also covers infrastructure limits: if a grid connection caps EV charging at 7.4 kW, that is expressed as the segment bound, not a separate constraint elsewhere.

## Wear cost in objective terms

Battery and EV degradation is modelled as a per-kWh throughput cost added to the objective. This prevents the solver from cycling the battery aggressively to exploit small price spreads that do not justify the wear:

```
objective_terms(t)  =  +wear_cost_eur_per_kwh × (total_charge[t] + total_discharge[t]) × dt
```

`wear_cost_eur_per_kwh` is a config field on `BatteryConfig` and `EvConfig`. The term is added to the minimisation objective, so a positive value discourages cycling. Setting it to zero disables wear modelling. A typical value for a lithium battery is 0.02–0.05 €/kWh throughput. The correct value depends on the battery's cycle life warranty and replacement cost, so it is left to the user to configure.

## Vendor capability flags

`BatteryConfig` and `EvConfig` each carry a `capabilities` sub-object with flags that document hardware behaviour. These do **not** affect the solver model — they control how mimirheim post-processes and publishes the schedule.

```yaml
batteries:
  battery_main:
    capabilities:
      staged_power: false    # true = hardware only accepts discrete setpoints (e.g. 0/25/50/100%)
      zero_exchange: false   # true = inverter has a boolean closed-loop zero-exchange register
    outputs:
      exchange_mode: null    # MQTT topic; published when zero_exchange capability is true
```

- **`staged_power: true`** — the hardware cannot accept an arbitrary continuous power value. Before publishing the schedule setpoint, mimirheim rounds it to the nearest supported stage. The solver itself always produces continuous-valued schedules and is not affected by this flag.
- **`zero_exchange: true`** (battery, EV) / **`zero_export: true`** (PV) — the inverter or charger has a boolean closed-loop mode register. When set to `true`, the hardware uses its own current transformers to continuously prevent grid exchange (or export, for PV) until the flag is cleared. mimirheim publishes `"true"` or `"false"` to `outputs.exchange_mode` (or `outputs.zero_export_mode` for PV) once per solve cycle. The hardware performs all real-time enforcement autonomously.
- **EV `loadbalance: true`** — the EVSE supports autonomous excess-PV following. When `loadbalance_active=True` is asserted, the charger measures net grid current and clamps charge power to available PV surplus. mimirheim does not publish a numeric kW setpoint when this mode is active.

Only one device may hold the closed-loop enforcer role per time step. The arbitration engine in `mimirheim/core/control_arbitration.py` selects the enforcer and sets `DeviceSetpoint.zero_exchange_active` accordingly (see the Arbitration engine section below).

## ObjectiveBuilder

A single `ObjectiveBuilder` class translates the `strategy` field from `SolveBundle` into the MIP objective expression for the schedule.

```python
# mimirheim/core/objective.py
class ObjectiveBuilder:
    def add_hard_cap_constraints(self, ctx: ModelContext, grid: Grid, config: MimirheimConfig) -> None: ...
    def build(self, ctx, devices, grid, bundle, config) -> float: ...   # returns the solver budget left
```

`build()` returns the wall-clock budget remaining for the caller's solve, because a lexicographic strategy spends part of it internally.

One other component installs an objective and solves: `_probe_care_targets` in `model_builder.py`, which runs before `build()` on cycles where a battery full-charge target is due. It minimises the gap to each due battery's target — a feasibility question with no economics in it — purely to find a step the target can be pinned to, then hands the model back untouched apart from that constraint. `add_hard_cap_constraints` is public and idempotent so the probe can guarantee the grid caps are in place before it solves.

Internally it branches on `bundle.strategy`:

- **`minimize_cost`** — weighted objective dominated by `confidence[t] × (import_price[t] × import[t] − export_price[t] × export[t])`. Import and export penalties derived from `constraints` block are added.
- **`minimize_consumption`** — lexicographic two-solve: first minimise total grid import (Phase 1), then minimise full net cost subject to the import bound found in Phase 1 (Phase 2). Phase 2 uses the same objective as `minimize_cost`, so export revenue and device wear are still optimised within the import constraint. If Phase 1 finds no solution there is no bound to lock and no variable values to read, so the lock is skipped, Phase 2 solves for cost alone, and `SolveResult.strategy_degraded` is set. The same flag is set when Phase 1 returns a time-limited incumbent: the lock is applied, but to an achievable volume rather than a proven minimum. Either way the schedule is published under the requested strategy name, and the flag is what says the strategy's guarantee was not delivered.
- **`balanced`** — weighted combination of cost and self-sufficiency terms using `balanced_weights` from config.

All three modes add:
- Device wear terms from `device.objective_terms(t)` for each device
- Import/export hard cap enforcement from `constraints.import_limit_kw` / `constraints.export_limit_kw` (added as constraints, not objective terms)
- Confidence weighting: every economic term is multiplied by `bundle.horizon_confidence[t]`

`build_and_solve()` may therefore call the solver more than once: the `minimize_consumption` phase 1, and the full-charge probe on cycles where a target is due. Both are hidden behind the same signature — callers see one call and one `SolveResult` — and both take their time from `solver.time_limit_seconds` rather than adding to it.

## Power balance constraint

The power balance is the central constraint that couples all devices. It is assembled inside `build_and_solve()` after all devices have added their variables — not inside any device or in a separate class:

```python
for t in ctx.T:
    ctx.solver.add_constraint(
        sum(d.net_power(t) for d in devices) + grid.net_power(t) == 0
    )
```

This enforces that at every time step, total production equals total consumption. PV and discharging batteries contribute positive `net_power`; loads and charging devices contribute negative. The grid variable absorbs any imbalance within its configured limits. If no feasible balance exists (e.g. load exceeds all generation plus import limit), the solve returns `infeasible`.

## Thermal boiler and DHW tank dynamics

`ThermalBoilerDevice` and the DHW portion of `CombiHeatPumpDevice` share the same first-order lumped-capacitance tank model. Tank temperature at each step is a decision variable bounded by the configured safety limits:

```
T_tank[t] ∈ [min_temp_c, max_temp_c]
```

The tank dynamics are modelled as a discrete-time energy balance. Let $V$ be the tank volume (litres), $c_p = 0.001163$ kWh/(litre·K) be the specific heat of water, and $L$ be the heat loss coefficient (kW/K):

$$T_{tank}[t] = T_{tank}[t-1] + \frac{\Delta t}{V \cdot c_p} \left( P_{heat}[t] - L \cdot (T_{tank}[t-1] - T_{amb}) \right)$$

This is linearised by moving all $T_{tank}$ terms to the left:

$$T_{tank}[t] - \alpha \cdot T_{tank}[t-1] = \frac{\Delta t}{V \cdot c_p} \cdot P_{heat}[t] + \beta_{loss}$$

where $\alpha = 1 - \Delta t \cdot L / (V \cdot c_p)$ is the heat retention factor and $\beta_{loss}$ encodes the ambient heat loss term. The initial condition uses `current_temp_c` from the MQTT sensor reading.

The element/boiler is controlled by a binary variable `on[t]`. When on, `P_heat[t] = element_power_kw * cop`. A minimum run-time constraint (if `min_run_steps > 0`) links consecutive `on[t]` values via a Big-M constraint that requires at least `min_run_steps` of operation once switched on, preventing unrealistic short cycling.

**Minimum run-time via a start sentinel** (used whenever `min_run_steps > 1`). A binary `start[t]` detects the off-to-on transition and propagates the run length forward:

```
start[t] >= on[t] - on[t-1]                 (forces start[t]=1 on transition to on)
start[t] <= on[t]                            (start cannot be 1 while off)
on[t+τ]  >= start[t]   for τ in 1..min_run_steps-1, where t+τ < horizon
```

`start[t]` is constrained only from one side (`>=` on transition, `<=` while on), not pinned by equality, so the solver is free to leave it at 0 even while the device runs continuously — a spurious `start=1` would only tighten the run-length constraint further, which a cost-minimising objective never has a reason to do, so the solver naturally drives it to the correct value without an explicit equality. `start[t]` is not created for `t=0` (no prior step to compare against) or when `min_run_steps <= 1` (nothing to enforce).

**Additional refinements in `CombiHeatPumpDevice._add_min_run_constraints` and `SpaceHeatingDevice._add_min_run_constraints`** (both apply the sentinel mechanism to a single on/off indicator — `hp_on[t]` for the combi device):

- *Preventing fresh starts too close to the horizon end.* Without an extra guard, the solver could start a fresh run in the last step or two of the horizon: with no future steps left to constrain, the sentinel inequalities are trivially satisfied by a single-step run, defeating the minimum-run-length guarantee. For every step `t` where `t + min_run_steps > horizon` (a start here could not complete the required run inside the horizon), the constraint set adds `on[t] <= on[t-1]` (no fresh start allowed — the device may still continue a run already in progress) or, at `t=0` specifically, `on[0] == 0` (there is no prior step to continue from, so a run cannot even be in progress).

- *Propagating a `t=0` start explicitly.* `start[t]` is only defined for `t >= 1` (it compares against `t-1`), so the sentinel loop alone does not constrain what happens if the device is already on at `t=0` (e.g. still running from the previous solve cycle, short of the minimum run length). A direct constraint `on[tau] >= on[0]` for `tau` in `1 .. min_run_steps-1` propagates a `t=0` start forward, mirroring what the sentinel enforces for a start at any other step.

## Space heating heat pump model

`SpaceHeatingDevice` supports two modes configured via `SpaceHeatingConfig.mode`:

- **On/off** — `hp_on[t] ∈ {0, 1}`. Heat output at step $t$ is `hp_on[t] * elec_power_kw * cop`. An optional `min_run_steps` constraint prevents frequent cycling.
- **SOS2 (modulating)** — An SOS2 set allows the heat pump to operate at any power level between `min_power_fraction * elec_power_kw` and `elec_power_kw`, or be completely off. SOS2 encodes a piecewise linear function without additional binaries; only two adjacent breakpoint weights are non-zero at any feasible solution.

**Degree-days path (no BTM):** When `building_thermal` is not configured, a single constraint requires the total heat delivered over the horizon to meet the demand `heat_needed_kwh`:

```
Σ_t hp_heat[t] * dt >= heat_needed_kwh
```

The device is excluded entirely (no variables added) when `heat_needed_kwh == 0.0` and no BTM is configured.

**BTM path:** See the Building thermal model section below.

## Combi heat pump model

`CombiHeatPumpDevice` operates in one of three states per time step: DHW mode, SH mode, or idle. A mutual exclusion constraint ensures at most one mode is active:

```
dhw_mode[t] + sh_mode[t] <= 1,    dhw_mode[t], sh_mode[t] ∈ {0, 1}
```

The DHW mode feeds the hot-water tank (same dynamics as `ThermalBoilerDevice`). The SH mode delivers space heating power `sh_mode[t] * elec_power_kw * cop_sh` kW of thermal output.

When `building_thermal` is not configured, the SH mode power must satisfy the aggregate demand `heat_needed_kwh` over the horizon (same as the space heating HP degree-days constraint). When `building_thermal` is configured, the BTM replaces the degree-days constraint for the SH portion.

## Building thermal model (BTM)

The BTM is an optional feature on `SpaceHeatingDevice` and `CombiHeatPumpDevice`. It replaces the degree-days demand constraint with explicit indoor temperature tracking. The primary motivation is enabling pre-heating: storing heat in the building fabric during cheap-electricity periods and reducing HP operation during expensive periods, without violating indoor comfort.

**Physical model.** The building is treated as a single lumped thermal mass $C$ (kWh/K) exchanging heat with the outdoor environment via a heat loss coefficient $L$ (kW/K). The first-order discrete ODE is:

$$T_{in}[t] = T_{in}[t-1] + \frac{\Delta t}{C} \left( P_{heat}[t] - L \cdot (T_{in}[t-1] - T_{out}[t]) \right)$$

Rearranging for the solver (all decision variables on the left):

$$T_{in}[t] - \alpha \cdot T_{in}[t-1] - \frac{\Delta t}{C} \cdot P_{heat}[t] = \beta_{out} \cdot T_{out}[t]$$

where $\alpha = 1 - \Delta t \cdot L / C$ and $\beta_{out} = \Delta t \cdot L / C$.

For $t = 0$, $T_{in}[t-1]$ is the measured `current_indoor_temp_c` (a constant, not a variable):

$$T_{in}[0] - \frac{\Delta t}{C} \cdot P_{heat}[0] = \alpha \cdot T_{in,0} + \beta_{out} \cdot T_{out}[0]$$

**Comfort bounds.** The variable `T_indoor[t]` is bounded at declaration:

```
T_indoor[t] ∈ [comfort_min_c, comfort_max_c]
```

These are hard bounds in the solver, not soft constraints. If the comfort band cannot physically be maintained (e.g. the outdoor forecast is extremely cold and `elec_power_kw` is insufficient), the solve returns infeasible. The operator must either widen the comfort band or increase HP capacity.

**Linearity.** The BTM introduces no bilinear or non-linear terms. `P_heat[t]` is itself a linear expression in existing HP variables (binary × affine, or SOS2), and the ODE coefficients $\alpha$, $\beta_{out}$, $\Delta t / C$ are all scalar constants computed before the model is built. The BTM adds exactly $H$ continuous variables and $H$ equality constraints.

**Outdoor forecast format.** The `outdoor_temp_forecast_c` list is already at 15-minute resolution — one value per solver step. No resampling is applied. If the list is shorter than the active horizon, `add_constraints()` raises `ValueError` with the device name, which the solve loop catches and logs before skipping the solve.

## Full-charge target probe (two-phase witness solve)

`_probe_care_targets` in `mimirheim/core/model_builder.py` decides, on the cycles where a battery's periodic full-charge policy (`soc_ratchet`, see README.md's "Periodic full charge" section for the user-facing behaviour) is due, which step and SOC value to pin as a hard constraint. This section covers the two algorithmic choices behind that function that are not part of the user-facing behaviour: why it asks the solver rather than computing reachability directly, and why it minimises a *sum of per-battery gaps* rather than total stored energy.

**Why ask the solver.** Whether a battery can reach a given SOC by a given step depends on charge derating, the minimum charge power, a piecewise (and possibly non-monotonic) efficiency curve, the load, the import limit, and every other device sharing the connection, all at once. This cannot be estimated accurately outside the solver — an earlier version of this feature tried to do so directly and was repeatedly wrong in both directions across several review rounds. Instead, the function solves the model once with an objective that has no economics in it: minimise the total SOC gap between each due battery and its own target across the horizon. The added gap variables do not restrict the feasible set, so this probe is feasible whenever the model itself is. The resulting trajectory is a **witness**: every target imposed afterwards is one the witness already satisfies, which is what guarantees the policy can never make the real solve infeasible.

**Why sum of per-battery gaps, not total stored energy.** An objective of maximising `Σ soc` across all batteries would reward topping up an already-efficient battery past its own target before charging a less efficient one at all — a jointly optimal trajectory could then leave one battery short even when a trajectory satisfying both exists. Summing each battery's own shortfall avoids this: a kWh only counts toward the objective while that specific battery is below its own target, so there is no incentive to over-fill one at another's expense.

**Known under-enforcement cases.** The gap-sum objective can still prefer a trajectory that never reaches a target that is in fact reachable, in two situations: a battery that hovers just below the threshold for many steps can score better than one that touches it once and is then forced back down; and with two batteries, refilling an already-satisfied one can shave more off the total gap than lifting the other to its own threshold. In both cases the function reduces the enforced target to what the witness actually achieved, which is a strictly correct floor (never infeasible), but understates what was achievable. This is a bounded, self-correcting loss — the next cycle's probe retries with fresh state — rather than a proven optimum. The alternative that avoids it (a binary per battery per step asking "can it reach the target at *some* step") is a disjunction, not a sum, and would add one binary variable per battery per step to answer a question that only matters on the rare cycles a charge is due.

The gap variables exist only for this probe solve and are never added to the economic objective, so they cannot be outbid by a priced preference from any strategy. `_CARE_EPS_KWH` is a small tolerance applied when comparing the witness SOC to the target: CBC returns values a few ULPs off the true optimum, so without it a target sitting exactly on the SOC variable's upper bound could fall into the "unreached" branch on every cycle.

## SOC ratchet floor clamp

The `soc_ratchet` policy's dynamic minimum SOC (see README.md's "Periodic full charge (`soc_ratchet`)" section for the user-facing behaviour) is applied in `Battery._add_care_constraints` as `soc[t] >= min(plan.floor_kwh, inputs.soc_kwh)` for every step, rather than `soc[t] >= plan.floor_kwh` directly.

The `min` matters: a floor that has just stepped up will often sit above the current SOC, because the step is precisely what happens when the battery has been sitting low. An unconditional `soc[t] >= floor` would then be infeasible at `t=0` (SOC cannot jump), losing the whole schedule to protect a policy about cell balancing. Clamping to the current SOC instead reproduces the hardware behaviour: an inverter with a floor above the current SOC does not teleport the battery, it simply stops discharging further.

**Known limitation.** The clamp is one constant for the whole horizon, so a battery starting below the floor may charge above it and then discharge back down to where it started — real hardware would hold the floor once reached. Expressing "once above, stay above" needs either a binary per step (solve-time cost on every battery, every cycle) or a forced charge ramp (buys energy without consulting prices). Neither is used; the full-charge target (see "Full-charge target probe" above) is what actually forces the charge, so the gap this leaves is bounded — the battery cannot end up worse off than it started, and the balance charge still happens on schedule.

## Power derating near SOC extremes

`Battery` supports two optional linear derating regions, configured via `reduce_charge_above_soc_kwh`/`reduce_charge_min_kw` and `reduce_discharge_below_soc_kwh`/`reduce_discharge_min_kw`. They model real inverter behaviour: charge power is reduced as the battery approaches full capacity, and discharge power is reduced as it approaches minimum SOC, both approximately linearly in SOC.

Each region is a two-point linear function rearranged into LP form. For charge derating, the two points are `(soc = reduce_charge_above_soc_kwh, power = max_charge_kw)` and `(soc = capacity_kwh, power = reduce_charge_min_kw)`, giving slope `slope_c = (reduce_charge_min_kw - max_charge_kw) / (capacity_kwh - reduce_charge_above_soc_kwh)` (always negative, since min < max and the denominator is positive). Rearranged:

```
charge_total[t] - slope_c * soc_prev <= max_charge_kw - slope_c * reduce_charge_above_soc_kwh
```

Discharge derating is the mirror image, with slope `slope_d = (reduce_discharge_min_kw - max_discharge_kw) / (reduce_discharge_below_soc_kwh - min_soc_kwh)`.

Both constraints are safe to add unconditionally: outside the derated region the right-hand side exceeds the segment-bound maximum power, so the constraint is slack and has no effect. Both are evaluated against the **start-of-step** SOC (`soc_prev`, i.e. `soc[t-1]` or the initial reading at `t=0`), because that is what a real inverter observes when deciding how much power to allow for the step. Using the end-of-step SOC (`soc[t]`) would create a circular dependency between the power decision and the resulting SOC — still linear and solvable, but not physically what the inverter does.

## EV availability gate

`EvDevice.add_constraints` (`mimirheim/devices/ev.py`) forces all charge (and V2H discharge, if configured) variables to zero for every step when `inputs.available` is `False`, and adds no SOC tracking constraint at all for that solve.

Forcing the power variables to zero is the most operationally important constraint on this device: without it, the solver could schedule charge power for a vehicle that is not physically connected. That setpoint would reach the charger hardware and either be ignored (if the charger itself detects no vehicle) or, on hardware that trusts the setpoint, fault.

SOC tracking constraints are omitted entirely, rather than pinned to a fixed value, because the SOC is not a meaningful quantity while the vehicle is away — there is no cell chemistry connected to the model to track. Adding an SOC equality constraint with no valid energy balance behind it (no charger delivering power to source it) would make the model infeasible instead of merely inert.

## Hybrid inverter DC bus power balance

`HybridInverterDevice` (`mimirheim/devices/hybrid_inverter.py`) models a single unit that integrates a PV MPPT input, a battery on the DC bus, and an AC grid connection — the key structural difference from an AC-coupled battery plus a separate PV device. PV can charge the battery directly across the DC bus without any AC round-trip, and both directions of AC/DC conversion share the inverter's own efficiency.

This is expressed as an explicit DC bus balance constraint, in addition to the battery's own charge/discharge Big-M guard (see "Split charge/discharge variables" above, which applies unchanged to `bat_charge_dc[t]`/`bat_discharge_dc[t]`/`mode[t]`):

```
pv_dc[t] + bat_discharge_dc[t] + ac_to_dc[t] * eff_inv
  - bat_charge_dc[t] - dc_to_ac[t] / eff_inv == 0
```

All DC-bus power sources (PV, battery discharge, AC imported and converted to DC) must equal all DC-bus sinks (battery charge, DC consumed to produce the AC export) at every step.

**Inverter direction binary (`inv_mode[t]`).** A single-stage inverter cannot convert AC→DC and DC→AC at the same time. Without a guard, the LP relaxation could do both simultaneously (e.g. to exploit a spread between `ac_to_dc` and `dc_to_ac` bounds), which no physical inverter can do. `inv_mode[t]` gates each direction with the same Big-M pattern as the battery's own `mode[t]`:

```
ac_to_dc[t] ≤ (max_charge_kw / eff_inv)                    × inv_mode[t]
dc_to_ac[t] ≤ (max_discharge_kw + max_pv_kw) × eff_inv × (1 − inv_mode[t])
```

This is a second, independent direction binary from the battery's `mode[t]`: the battery can be charging (`mode[t]=1`) while the inverter is in AC→DC mode supplying that charge from PV and the grid together, or while the inverter is idle on the AC side because PV alone covers the charge — the two binaries are not required to move together.
