<img width="1254" height="1254" alt="image" src="https://github.com/user-attachments/assets/9908dd34-94f7-4ede-938e-a55b9eaa9f08" />



# Mimirheim — Home Energy Optimiser

**mimirheim** is an open-source Python service that computes an optimal energy dispatch schedule for a residential home. Given forecasts of electricity prices, PV generation, and household load, it determines the best schedule for every controllable device — battery, EV, deferrable loads — over a rolling 24-hour planning horizon.

mimirheim is a **pure strategist**. It never controls hardware. It reads all inputs from MQTT and publishes its schedule back to MQTT. Home Assistant, Node-RED, or any other automation platform is responsible for executing that schedule on actual devices.

---

## Contents

1. [Core Principle](#1-core-principle)
2. [Mathematical Model](#2-mathematical-model)
3. [Devices](#3-devices)
4. [Objectives and Strategy](#4-objectives-and-strategy)
5. [Confidence Model](#5-confidence-model)
6. [Input Schema](#6-input-schema)
7. [Output Schema](#7-output-schema)
8. [Running mimirheim](#8-running-mimirheim)
9. [Configuration](#9-configuration)
10. [Readiness and Staleness](#10-readiness-and-staleness)
11. [Home Assistant Integration](#11-home-assistant-integration)
12. [Project Structure](#12-project-structure)
13. [Testing](#13-testing)

---

## 1. Core Principle

```
[HA / Node-RED / scripts]
  │
  ├── publishes:  {prefix}/input/prices          (retained)
  ├── publishes:  {topic_forecast} for each PV   (retained)
  ├── publishes:  {topic_forecast} for each load (retained)
  ├── publishes:  {soc.topic} for each battery   (retained)
  └── publishes:  {prefix}/input/trigger         (not retained; fires each solve cycle)
                        │
                   MQTT broker
                        │
              ┌─────────────────┐
              │   mimirheim service   │
              │                 │
              │  validate       │
              │  → solve MILP   │
              │  → publish      │
              └─────────────────┘
                        │
                   MQTT broker
                        │
  [HA reads schedule, runs automations, controls battery / EV charger / etc.]
```

**mimirheim has no knowledge of any device API, vendor protocol, or home automation platform.** It only speaks MQTT.

### Separation of concerns

| Layer | Owner | Responsibility |
|---|---|---|
| Data provision | HA / Node-RED / scripts | Fetch prices, PV forecasts, device state; publish to MQTT |
| Optimisation | **mimirheim** | Compute optimal schedule; publish to MQTT |
| Execution | HA / Node-RED / scripts | Read schedule from MQTT; control physical devices |

These layers are fully independent. mimirheim can be tested, replaced, or upgraded without touching HA.

---

## 2. Mathematical Model

mimirheim solves a **Mixed-Integer Linear Programme (MILP)** over a discrete time horizon $T = \{0, 1, \ldots, H-1\}$ where each step is one quarter-hour ($\Delta t = 0.25\text{ h}$). The horizon length $H$ is variable and determined at each solve by the available forecast coverage — it equals the number of 15-minute steps between now and the latest timestamp that all forecast series share (see [Section 10](#10-readiness-and-staleness)).

### Power balance (core constraint)

At every time step $t$, total supply equals total demand:

$$p^{import}[t] + \sum_d p^{discharge}_d[t] + p^{pv}[t] = p^{export}[t] + \sum_d p^{charge}_d[t] + p^{base\_load}[t]$$

### Battery state of charge

$$\text{soc}[t] = \text{soc}[t-1] + \Delta t \cdot \left( \sum_i \eta^c_i \cdot p^{charge}_i[t] - \sum_j \frac{1}{\eta^d_j} \cdot p^{discharge}_j[t] \right)$$

Where $i$ and $j$ index the piecewise efficiency segments for charge and discharge respectively. Power (kW) × time (h) = energy (kWh). All SOC quantities are in kWh.

### Piecewise efficiency

Efficiency varies with power level. Each device direction (charge / discharge) is modelled as a list of segments, each with its own efficiency coefficient and power cap. This keeps the model fully linear while approximating the real curve.

### Deferrable load

A deferrable load has a fixed power draw $p^{load}$ and a fixed duration of $K$ consecutive steps. It must complete within a window $[t_{earliest}, t_{latest}]$ supplied at runtime via MQTT. The optimiser chooses start time $s$:

$$p^{load}[t] = p^{load} \cdot \sum_{s: s \le t < s+K} u[s], \quad \sum_s u[s] = 1, \quad u[s] \in \{0,1\}$$

If either window bound is missing or stale, the load is excluded from the current solve.

### Space heating heat pump (degree-days model)

When no building thermal model is configured, the space heating heat pump uses a simple degree-days constraint. A heat demand $Q^{sh}$ (kWh) is supplied at runtime via MQTT. The solver must deliver at least that energy over the horizon:

$$\sum_{t=0}^{H-1} p^{hp}[t] \cdot \text{COP}_{sh} \cdot \Delta t \ge Q^{sh}$$

where $p^{hp}[t]$ is the electrical input power of the heat pump at step $t$. The solver is free to choose which steps the heat pump runs, subject to any minimum run-time constraints. This is a soft constraint from the optimiser's perspective — it shifts HP operation to cheapest-price steps while meeting the total heat demand.

### Building thermal model (BTM)

When `building_thermal` is configured on a heat pump device, the degree-days constraint is replaced by a first-order thermal dynamics model. The building is modelled as a lumped thermal mass with heat loss to the outdoor environment:

$$T_{in}[t] = \alpha \cdot T_{prev} + \frac{\Delta t}{C} \cdot P_{heat}[t] + \beta_{out} \cdot T_{out}[t]$$

where:
- $T_{in}[t]$ — indoor temperature (°C) at step $t$ (decision variable, bounded by comfort limits)
- $T_{prev}$ — indoor temperature at the previous step ($T_{in}[t-1]$ for $t > 0$; measured value for $t = 0$)
- $C$ — building thermal capacity (kWh/K, from config)
- $L$ — heat loss coefficient (kW/K, from config)
- $P_{heat}[t]$ — thermal power delivered to the building at step $t$ (kW)
- $T_{out}[t]$ — outdoor temperature forecast at step $t$ (°C, from MQTT)
- $\alpha = 1 - \Delta t \cdot L / C$ — thermal decay factor
- $\beta_{out} = \Delta t \cdot L / C$ — outdoor coupling factor

The comfort band imposes hard bounds on $T_{in}[t]$:

$$T_{min} \le T_{in}[t] \le T_{max} \quad \forall\, t$$

The solver optimises the HP schedule to minimise energy cost while keeping indoor temperature within the configured comfort range. The BTM enables pre-heating: running the HP when electricity is cheap to store heat in the building fabric for later hours.

### Objective

$$\max \sum_{t=0}^{H-1} c[t] \cdot \left( p^{ex}[t] \cdot x^{ex}[t] - p^{im}[t] \cdot x^{im}[t] \right) - \sum_{t,d} \delta_d \cdot \bigl(p^{charge}_d[t] + p^{discharge}_d[t]\bigr)$$

Where:
- $c[t]$ — per-step price confidence (1.0 for certain prices, <1.0 for forecasts)
- $x^{ex}[t], x^{im}[t]$ — export/import prices in EUR/kWh
- $\delta_d$ — battery/EV wear cost per kWh throughput

Confidence is applied to revenue/cost terms only, never to constraints. Physical feasibility always holds regardless of forecast confidence.

### Solver

**CBC** (COIN-OR Branch and Cut, free, EPL 2.0, embedded via `python-mip`). The solver interface is abstracted behind a `SolverBackend` protocol; any compliant backend can be substituted. A 59-second time limit prevents blocking. CBC is approximately 100× faster than HiGHS on this problem class due to aggressive Gomory cut generation on temperature-coupled binary variables.

---

## 3. Devices

> **Maturity note** — Battery and PV are the most thoroughly tested and deployed device types.
> EV chargers, hybrid inverters, deferrable loads, and all thermal devices (boiler, space
> heating HP, combi heat pump) are implemented and unit-tested but have seen limited
> real-world deployment. If you run mimirheim with any of these devices, feedback and bug reports
> are very welcome. Pull requests with additional integration tests or golden scenarios are
> especially appreciated.

| Device | Config section | Variables |
|---|---|---|
| Battery | `batteries` | `charge[t]`, `discharge[t]`, `soc[t]` |
| PV array | `pv_arrays` | Fixed parameter (forecast, no decision variable) |
| EV charger | `ev_chargers` | `ev_charge_seg[t,i]`, `ev_discharge_seg[t,i]` (V2H if segments provided) |
| Deferrable load | `deferrable_loads` | `u[s]` — binary start time selector |
| Static load | `static_loads` | Fixed parameter (forecast, no decision variable) |
| Grid | `grid` | `import[t]`, `export[t]` |
| Hybrid inverter | `hybrid_inverters` | `charge_seg[t,i]`, `discharge_seg[t,i]`, `soc[t]`, `pv[t]` (clipped forecast) |
| Thermal boiler | `thermal_boilers` | `T_tank[t]`, `on[t]` (binary) |
| Space heating HP | `space_heating_hps` | `hp_on[t]` (on/off) or SOS2 mode variable; optional `T_indoor[t]` (BTM) |
| Combi heat pump | `combi_heat_pumps` | `T_tank[t]`, `dhw_mode[t]`, `sh_mode[t]`, `hp_on[t]`; optional `T_indoor[t]` (BTM) |

Multiple instances of each device type are supported. Each named instance in its config section is independent.

### Static load

At least one `static_loads` entry is required when any generation or storage device is configured. Without it, the power balance is incomplete and the solver will over-export.

### EV charger

The EV charger uses the same piecewise segment structure as the battery. Discharge (V2H) is enabled by providing non-empty `discharge_segments`; leave it empty if the hardware does not support it.

### Hybrid inverter

A hybrid inverter combines battery storage with a directly coupled PV input. The PV forecast is provided via MQTT in the same timestamped format as `pv_arrays`. Battery charge and discharge use the same piecewise efficiency segment structure as the standalone battery.

### Thermal boiler

An electric resistance boiler with a hot-water tank. Tank temperature is tracked as a decision variable across the horizon. The solver may run the boiler element (`on[t] = 1`) at any step to raise the tank temperature, subject to a minimum tank temperature (to satisfy DHW demand) and a maximum temperature (safety limit). The binary `on[t]` variable incurs a minimum run-time constraint to avoid short cycling.

### Space heating heat pump

A heat pump that delivers space heating only (no DHW). Two dispatch modes are supported:

- **On/off** — a binary `hp_on[t]` variable; heat output at full COP when on.
- **SOS2 (modulating)** — a Special Ordered Set of Type 2 variable that allows continuous power modulation between a minimum and maximum operating point.

If `building_thermal` is configured, indoor temperature is tracked as a decision variable and the degree-days heat demand constraint is replaced by the BTM thermal dynamics model (see [§2](#2-mathematical-model)).

### Combi heat pump

A heat pump that covers both domestic hot water (DHW) and space heating (SH) using a single compressor. DHW and SH are mutually exclusive at each time step — the heat pump can operate in exactly one mode per step (or be idle). The DHW mode maintains the hot-water tank temperature; the SH mode delivers space heating.

If `building_thermal` is configured, indoor temperature tracking follows the same BTM model as the space heating HP. The DHW tank dynamics are unaffected by the BTM configuration.

---

## 4. Objectives and Strategy

The active optimisation strategy is read from the MQTT topic `{prefix}/input/strategy`. Publish a retained value to change strategy without restarting mimirheim. The default when the topic has not been received is `minimize_cost`.

| Strategy | Behaviour |
|---|---|
| `minimize_cost` | Maximise revenue and minimise import cost. Default. |
| `minimize_consumption` | Minimise grid import first, maximise revenue within that envelope (two solves). |
| `balanced` | Equal weighting of cost and grid import minimisation; user-configurable weights. |

### Balanced weights

When strategy is `balanced`, relative weights can be tuned in config:

```yaml
objectives:
  balanced_weights:
    cost_weight: 1.0
    self_sufficiency_weight: 1.0
```

Both default to 1.0. Only the ratio matters.

### Exchange-shaping secondary term

Under a net-of-meter (NoM) tariff, import and export prices are symmetric and the `minimize_cost` objective already naturally produces near-zero exchange. When prices are perfectly flat, the solver is indifferent among solutions with equal net cost but different exchange magnitudes.

Setting `exchange_shaping_weight` to a small positive value adds the term `lambda × Σ_t(import_t + export_t)` to the objective. This breaks indifference in favour of lower total exchange without distorting dispatch on any step where there is a real price signal.

```yaml
objectives:
  exchange_shaping_weight: 1e-4   # 0.0 (default) = disabled
```

Choose a value orders of magnitude smaller than typical energy prices so it cannot reverse an economically justified decision. A value of `1e-4` EUR/kWh is appropriate for typical European retail tariffs (0.20–0.35 EUR/kWh).

### Hard constraints

Independent of strategy, hard limits on grid power can be enforced across the full horizon:

```yaml
constraints:
  max_import_kw: 10.0   # null = no cap (default)
  max_export_kw: 0.0    # 0.0 enforces zero export
```

---

## 5. Confidence Model

Confidence is a per-step floating-point value in `[0.0, 1.0]` supplied externally in the prices payload. mimirheim does not compute or decay confidence internally.

| Value | Meaning |
|---|---|
| `1.0` | Guaranteed. Published day-ahead prices. |
| `0.7` | Reasonably likely. Near-term PV forecast. |
| `0.3` | Speculative. ML-predicted prices 36 h ahead. |

A step with `confidence: 0.3` contributes 30% of its face-value revenue signal to the objective. The solver still plans that step — it will still charge the battery if the discounted value justifies it — but makes more conservative decisions.

---

## 6. Input Schema

### Prices — `{prefix}/input/prices`

Published retained. Required. Payload is a JSON array of timestamped price steps:

```json
[
  {
    "ts": "2026-03-30T13:00:00+00:00",
    "import_eur_per_kwh": 0.22,
    "export_eur_per_kwh": 0.18,
    "confidence": 1.0
  },
  {
    "ts": "2026-03-30T14:00:00+00:00",
    "import_eur_per_kwh": 0.21,
    "export_eur_per_kwh": 0.17
  }
]
```

- At least one step covering the current time is required.
- `ts` is an ISO 8601 UTC datetime marking the start of that price period.
- `confidence` is optional per step; defaults to 1.0.
- Steps can be at any resolution (typically hourly from day-ahead markets). mimirheim resamples them to the 15-minute solver grid using a step function: the price for a given `ts` applies until the next timestamp in the array.
- The planning horizon ends at the last `ts` in the array. Steps with `ts` before `solve_start` (now, floored to the nearest 15-minute boundary) are ignored.

### PV forecast — topic from `pv_arrays.*.topic_forecast`

Published retained. Required for each configured PV array. Payload is a JSON array of timestamped power steps:

```json
[
  {"ts": "2026-03-30T13:00:00+00:00", "kw": 0.0, "confidence": 1.0},
  {"ts": "2026-03-30T14:00:00+00:00", "kw": 2.4, "confidence": 0.9},
  {"ts": "2026-03-30T15:00:00+00:00", "kw": 4.1, "confidence": 0.85}
]
```

- `kw` is the forecast output power in kilowatts. Must be non-negative.
- `confidence` is optional per step; defaults to 1.0.
- mimirheim resamples to the 15-minute solver grid using linear interpolation between adjacent known points.

### Static load forecast — topic from `static_loads.*.topic_forecast`

Published retained. Required for each configured static load. Same timestamped format as PV forecast.

```json
[
  {"ts": "2026-03-30T13:00:00+00:00", "kw": 0.45},
  {"ts": "2026-03-30T14:00:00+00:00", "kw": 0.42}
]
```

### Trigger — `{prefix}/input/trigger`

Not retained. Sending any message (including an empty payload) to this topic instructs mimirheim to attempt a solve immediately. mimirheim checks whether all required inputs are present and whether forecast coverage reaches the minimum horizon, then either runs the solve or logs a warning explaining which requirement is not met.

Data topics (prices, forecasts, battery SOC) do not trigger solves. Publish to the trigger topic only after all forecast and sensor data have been refreshed for the current cycle.

### Battery SOC — topic from `batteries.*.inputs.soc.topic`

Published retained. Required for each configured battery that has `inputs.soc` configured. Payload is a plain numeric string:

```
5.2
```

Set `unit: percent` in config to publish a percentage (0–100); mimirheim converts using `capacity_kwh`.

### EV state — topic from `ev_chargers.*.inputs.soc.topic`

Published retained. Required for each configured EV charger that has `inputs` configured. Payload is a plain numeric string:

```
20.0
```

Set `unit: percent` in config to publish a percentage (0–100); mimirheim converts using `capacity_kwh`.

The plug state is published separately on `inputs.plugged_in_topic` as a boolean-like string: `true`/`false`, `on`/`off`, or `1`/`0`. When `false`, the EV device is excluded from the solve.

- `window_earliest` and `window_latest` (ISO 8601 UTC, optional) constrain the charging window.

### Strategy — `{prefix}/input/strategy`

Published retained. Optional. Two payload formats are accepted:

Plain text (for manual publishing with `mosquitto_pub` etc.):

```
minimize_cost
```

JSON object (published by the Home Assistant MQTT select entity via autodiscovery):

```json
{"strategy": "minimize_cost"}
```

Valid values: `minimize_cost`, `minimize_consumption`, `balanced`. Defaults to `minimize_cost` when absent.

### Deferrable window topics — from `deferrable_loads.*.topic_window_*`

Set via the HA MQTT `text` entity that mimirheim registers in the autodiscovery payload. The user types a value
directly into the text field in the HA UI, or an automation publishes to the topic.

Payload must be a plain ISO 8601 datetime string. The timezone offset is optional: a string without an
offset is interpreted as UTC. All three forms below are equivalent:

```
2026-03-30T15:00:00
2026-03-30T15:00:00Z
2026-03-30T15:00:00+00:00
```

A non-UTC offset is preserved as supplied. Use UTC (or no offset) unless the automation explicitly works
in a fixed local timezone.

Both `topic_window_earliest` and `topic_window_latest` must be present for the deferrable load to be included in the binary scheduling problem.

### Deferrable start time — from `deferrable_loads.*.topic_committed_start_time`

Published retained by the HA automation that physically starts the device. Payload must be a plain ISO
8601 datetime string using the same format rules as the window topics: a string without an offset is
interpreted as UTC. Examples:

```
2026-03-30T15:00:00
2026-03-30T15:00:00Z
2026-03-30T15:00:00+00:00
```

This topic is optional. When present and the timestamp indicates the load is currently running (i.e. between `start_time` and `start_time + duration`), mimirheim discards the window and treats the remaining steps as a **fixed power draw** in the power balance. No binary variable is used. This prevents the load from disappearing from the model mid-run as its window becomes invalid.

The four states mimirheim infers from this topic:

| `topic_committed_start_time` value | Condition | mimirheim behaviour |
|---|---|---|
| absent or not configured | — | Binary optimisation within window |
| present | `start_time` in the future | Binary optimisation (treat as absent) |
| present | `solve_start ∈ [start_time, start_time + duration)` | Fixed draw for remaining steps |
| present | `solve_start ≥ start_time + duration` | Run complete; device excluded |

mimirheim never publishes to or clears `topic_committed_start_time`. That is the automation's responsibility.

### Deferrable recommended start — to `deferrable_loads.*.topic_recommended_start_time`

Published retained by mimirheim after each successful solve. Payload is a bare ISO 8601 UTC datetime string:

```
"2026-03-30T15:00:00Z"
```

The value is the UTC datetime of the first nonzero-power step in the schedule for this load. It represents the solver's optimal start time given current prices and the configured window.

This topic is only published when the load is in binary scheduling state (i.e. a window is active and the load has not yet physically started). When the load is running or its committed start time is in the past, nothing is published — the previous retained value remains.

An HA automation can subscribe to this topic and act on it, for example by pre-programming a smart plug to switch on at the recommended time.

### Hybrid inverter SOC — topic from `hybrid_inverters.*.inputs.soc.topic`

Published retained. Required for each configured hybrid inverter that has `inputs.soc` configured. Payload is a plain numeric string:

```
25.6
```

Set `unit: percent` in config to publish a percentage (0–100); mimirheim converts using `capacity_kwh`.

### Hybrid inverter PV forecast — topic from `hybrid_inverters.*.topic_pv_forecast`

Published retained. Required for each configured hybrid inverter. Same timestamped format as `pv_arrays.*.topic_forecast`.

```json
[
  {"ts": "2026-03-30T13:00:00+00:00", "kw": 0.0},
  {"ts": "2026-03-30T14:00:00+00:00", "kw": 1.8}
]
```

### Thermal boiler temperature — topic from `thermal_boilers.*.inputs.topic_current_temp`

Published retained. Required for each configured thermal boiler that has `inputs` configured. Payload is a plain numeric string representing the current tank temperature in degrees Celsius:

```
58.3
```

### Space heating demand — topic from `space_heating_hps.*.inputs.topic_heat_needed_kwh`

Published retained. Required for each configured space heating HP that has `inputs` configured. Payload is a plain numeric string representing the heat energy demand for the horizon in kWh:

```
12.4
```

This topic is not required when `building_thermal` is configured — in that case the solver tracks indoor temperature directly and does not need a pre-computed demand figure.

### Combi heat pump DHW temperature — topic from `combi_heat_pumps.*.inputs.topic_current_temp`

Published retained. Required for each configured combi heat pump that has `inputs` configured. Payload is a plain numeric string representing the current DHW tank temperature in degrees Celsius:

```
51.7
```

### Combi heat pump SH demand — topic from `combi_heat_pumps.*.inputs.topic_heat_needed_kwh`

Published retained. Required for each configured combi heat pump that has `inputs` configured. Payload is a plain numeric string representing the space heating demand for the horizon in kWh:

```
8.2
```

This topic is not required when `building_thermal` is configured on the combi heat pump.

### BTM indoor temperature — topic from `*.building_thermal.inputs.topic_current_indoor_temp_c`

Published retained. Required when `building_thermal.inputs` is configured on a space heating HP or combi heat pump. Payload is a plain numeric string (degrees Celsius) or a JSON object:

```
20.5
```

```json
{"temp_c": 20.5}
```

### BTM outdoor temperature forecast — topic from `*.building_thermal.inputs.topic_outdoor_temp_forecast_c`

Published retained. Required when `building_thermal.inputs` is configured. Payload is a JSON array of outdoor temperature values in degrees Celsius, one value per 15-minute step starting from the current solve time:

```json
[5.2, 5.0, 4.8, 4.7, 4.5, 4.3, 4.1, 4.0]
```

The array must contain at least as many values as the active horizon length. mimirheim raises an error at solve time if the forecast is too short.

---

## 7. Output Schema

### `outputs.schedule`

Published retained after every successful solve. Contains the complete 96-step dispatch schedule:

```json
{
  "strategy": "minimize_cost",
  "objective_value": 1.24,
  "solve_status": "optimal",
  "schedule": [
    {
      "t": 0,
      "grid_import_kw": 0.0,
      "grid_export_kw": 0.0,
      "devices": {
        "home_battery": {"kw": -2.4, "type": "battery"},
        "roof_pv":      {"kw":  3.1, "type": "pv"},
        "base_load":    {"kw":  0.42, "type": "static_load"}
      }
    }
  ]
}
```

`solve_status` is one of `"optimal"` (proven optimal), `"feasible"` (time-limited incumbent), or `"infeasible"` (no feasible solution; schedule is empty and previous retained schedule remains unchanged).

Device `kw` sign convention: **positive = producing / discharging**, **negative = consuming / charging**.

### `outputs.current`

Published retained alongside the schedule. Contains the current-step summary for HA automations that don't need the full schedule:

```json
{
  "t": 0,
  "grid_import_kw": 0.0,
  "grid_export_kw": 0.0,
  "strategy": "minimize_cost",
  "solve_status": "optimal"
}
```

### Per-device setpoint — `{prefix}/device/{device_name}/setpoint`

Published retained for each device in the current step (`t=0`):

```json
{"kw": -2.4, "type": "battery"}
```

HA automations can subscribe directly to `mimirheim/device/home_battery/setpoint` rather than parsing the full schedule JSON.

### `outputs.last_solve`

Published retained after every solve attempt, including failures. Allows monitoring tools to detect problems without reading logs.

On success:
```json
{
  "status": "ok",
  "solve_status": "optimal",
  "generated_at": "2026-03-30T14:15:00+00:00"
}
```

On failure (infeasible, exception, stale inputs):
```json
{
  "status": "error",
  "detail": "Solve returned infeasible — check device configuration.",
  "generated_at": "2026-03-30T14:15:00+00:00"
}
```

### `outputs.availability`

Published retained. `"online"` on broker connect (birth message). `"offline"` on clean shutdown and as MQTT last-will on unclean disconnect. Used by HA `availability_topic` to mark all mimirheim entities unavailable when the service is down.

---

## 8. Running mimirheim

### Requirements

- Python 3.12
- `uv` (dependency management)

### Setup

```bash
uv sync           # create .venv, install all dependencies
```

### Run

```bash
uv run python -m mimirheim --config config.yaml
```

Or directly:

```bash
uv run mimirheim/__main__.py --config config.yaml
```

mimirheim logs to stdout. It connects to the configured MQTT broker and subscribes to all required input topics. It does not solve on a timer — a solve is triggered by a message on `{prefix}/input/trigger`. It runs until SIGTERM or SIGINT.

### Debug dumps

When `debug.dump_dir` is set in config and the `mimirheim.solver` logger is at DEBUG level, mimirheim writes a pair of JSON files (`input_*.json` + `output_*.json`) after each solve. These are in the same format as the golden file test fixtures. Up to `debug.max_dumps` pairs are kept; oldest are removed automatically.

---

## 9. Configuration

mimirheim is configured from a single YAML file passed via `--config`. All fields are validated by Pydantic at startup; an invalid config prints a human-readable error and exits.

### Minimal example

All MQTT topics are derived from `mqtt.topic_prefix` automatically. The only
required fields are the physical device parameters. The `outputs:` block may be
omitted entirely.

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: mimir
  topic_prefix: mimir

grid:
  import_limit_kw: 20.0
  export_limit_kw: 20.0

batteries:
  home_battery:
    capacity_kwh: 13.5
    min_soc_kwh: 1.0
    charge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
    discharge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
    wear_cost_eur_per_kwh: 0.005
    inputs:
      soc:
        unit: kwh          # topic derived to mimir/input/battery/home_battery/soc

pv_arrays:
  roof_pv:
    max_power_kw: 8.0    # topic derived to mimir/input/pv/roof_pv/forecast

static_loads:
  base_load: {}          # topic derived to mimir/input/baseload/base_load/forecast
```

### Full reference

#### `mqtt`

| Field | Type | Default | Description |
|---|---|---|---|
| `host` | string | — | Broker hostname or IP. |
| `port` | int | 1883 | Broker port. |
| `client_id` | string | — | MQTT client identifier. Must be unique on the broker. |
| `topic_prefix` | string | `mimirheim` | Prefix for all fixed mimirheim topics (`{prefix}/input/prices`, etc.) |

#### `outputs` (optional)

All four fields are optional. When a field is absent (or the entire `outputs:` block
is omitted), the topic is derived from `mqtt.topic_prefix`. Explicit values override
the derived default.

| Field | Derived topic (prefix = `mimirheim`) | Description |
|---|---|---|
| `schedule` | `mimir/strategy/schedule` | Full horizon schedule JSON (retained). |
| `current` | `mimir/strategy/current` | Current-step summary (retained). |
| `last_solve` | `mimir/status/last_solve` | Solve status after each attempt (retained). |
| `availability` | `mimir/status/availability` | Birth (`"online"`) and last-will (`"offline"`) (retained). |

#### `grid`

| Field | Type | Description |
|---|---|---|
| `import_limit_kw` | float ≥ 0 | Maximum grid import, kW. Physical connection limit. |
| `export_limit_kw` | float ≥ 0 | Maximum grid export, kW. |

#### `batteries` (dict, keyed by device name)

| Field | Type | Default | Description |
|---|---|---|---|
| `capacity_kwh` | float > 0 | — | Usable capacity in kWh. |
| `min_soc_kwh` | float ≥ 0 | 0.0 | Minimum SOC the solver will discharge to. |
| `charge_segments` | list (min 1) | — | Piecewise efficiency segments for charging. |
| `discharge_segments` | list (min 1) | — | Piecewise efficiency segments for discharging. |
| `wear_cost_eur_per_kwh` | float ≥ 0 | 0.0 | Degradation cost per kWh throughput. |
| `capabilities.staged_power` | bool | false | Hardware only accepts discrete power stages. |
| `capabilities.zero_export_mode` | bool | false | Battery inverter has a boolean zero-export mode register. When true, the inverter autonomously prevents grid export using local CT measurements. mimirheim publishes true/false once per solve cycle; the hardware performs real-time enforcement. |
| `outputs.zero_export_mode` | string or null | null | MQTT topic for the zero-export mode flag. Only published when `capabilities.zero_export_mode` is true. |
| `inputs.soc.topic` | string or null | derived | MQTT topic for SOC readings. Derived as `{prefix}/input/battery/{name}/soc` when absent. |
| `inputs.soc.unit` | `kwh` or `percent` | — | Unit of the published SOC value. |

Each `charge_segments` / `discharge_segments` entry:

| Field | Type | Description |
|---|---|---|
| `power_max_kw` | float > 0 | Power cap for this segment in kW. |
| `efficiency` | float (0, 1] | Round-trip efficiency fraction. |

#### `pv_arrays` (dict, keyed by device name)

| Field | Type | Default | Description |
|---|---|---|---|
| `max_power_kw` | float > 0 | — | Peak array output in kW (used to clip implausible forecast values). |
| `topic_forecast` | string or null | derived | MQTT topic for the timestamped power forecast (`list[{ts, kw, confidence}]`). Derived as `{prefix}/input/pv/{name}/forecast` when absent. |
| `capabilities.power_limit` | bool | false | Inverter accepts a continuous production limit setpoint in kW. When True, mimirheim adds a decision variable per step and may curtail below the forecast. |
| `capabilities.on_off` | bool | false | Inverter supports discrete on/off control. When True, mimirheim treats PV as a binary per step: either the full forecast or zero. Mutually exclusive with `power_limit`. Requires `outputs.on_off_mode`. |
| `capabilities.zero_export_mode` | bool | false | Inverter has a discrete zero-export mode register. When True, mimirheim publishes a boolean command to the configured output topic alongside the power limit. |
| `outputs.power_limit_kw` | string or null | derived | MQTT topic for the production limit setpoint in kW. Only published when `capabilities.power_limit` is true. Derived as `{prefix}/output/pv/{name}/power_limit_kw`. |
| `outputs.zero_export_mode` | string or null | derived | MQTT topic for the zero-export mode boolean command. Only published when `capabilities.zero_export` is true. Derived as `{prefix}/output/pv/{name}/zero_export_mode`. |
| `outputs.on_off_mode` | string or null | derived | MQTT topic for the on/off command. `"true"` = inverter on (producing); `"false"` = inverter off (curtailed by mimirheim). Derived as `{prefix}/output/pv/{name}/on_off_mode`. |

#### `ev_chargers` (dict, keyed by device name)

| Field | Type | Default | Description |
|---|---|---|---|
| `capacity_kwh` | float > 0 | — | Vehicle battery capacity in kWh. |
| `min_soc_kwh` | float ≥ 0 | 0.0 | Minimum SOC. |
| `charge_segments` | list (min 1) | — | Efficiency segments for charging. |
| `discharge_segments` | list | `[]` | Efficiency segments for V2H discharge. Empty = no V2H. |
| `wear_cost_eur_per_kwh` | float ≥ 0 | 0.0 | Degradation cost per kWh throughput. |
| `capabilities.staged_power` | bool | false | Hardware only accepts discrete power stages. |
| `capabilities.zero_export_mode` | bool | false | EV charger has a boolean zero-export mode register. When true, the charger autonomously prevents grid export using local CT measurements. mimirheim publishes true/false once per solve cycle. |
| `outputs.zero_export_mode` | string or null | derived | MQTT topic for the zero-export mode flag. Only published when `capabilities.zero_export_mode` is true. Derived as `{prefix}/output/ev/{name}/exchange_mode`. |
| `inputs.soc.topic` | string or null | derived | MQTT topic for EV SOC/state payload. Derived as `{prefix}/input/ev/{name}/soc`. |
| `inputs.soc.unit` | `kwh` or `percent` | — | Unit of the SOC value. |
| `inputs.plugged_in_topic` | string or null | derived | MQTT topic for plug state (boolean-like payload). Derived as `{prefix}/input/ev/{name}/plugged_in`. |

The departure target is supplied at runtime via the EV state MQTT payload (not config):

| MQTT field | Type | Description |
|---|---|---|
| `target_soc_kwh` | float ≥ 0 or null | Required SOC at departure (hard constraint). Omit or set null when no trip is planned. |
| `window_latest` | ISO 8601 UTC datetime or null | Departure deadline by which `target_soc_kwh` must be reached. |

#### `deferrable_loads` (dict, keyed by device name)

| Field | Type | Default | Description |
|---|---|---|---|
| `power_profile` | list of float > 0 | — | Per-step power draw in kW, one entry per 15-minute step of the run cycle. The array length determines the run duration. Example: `[2.0, 0.8, 0.8, 2.5]` models a 4-step cycle. |
| `topic_window_earliest` | string or null | derived | MQTT topic publishing earliest permitted start (ISO 8601 UTC). Derived as `{prefix}/input/deferrable/{name}/window_earliest`. |
| `topic_window_latest` | string or null | derived | MQTT topic publishing latest permitted end (ISO 8601 UTC). Derived as `{prefix}/input/deferrable/{name}/window_latest`. |
| `topic_committed_start_time` | string or null | derived | Optional MQTT topic where the automation publishes the actual start datetime when the load physically begins (retained, ISO 8601 UTC). Derived as `{prefix}/input/deferrable/{name}/committed_start`. |
| `topic_recommended_start_time` | string or null | derived | MQTT topic to which mimirheim publishes the solver-recommended start datetime (ISO 8601 UTC, retained) after each solve. Only published when the load is in binary scheduling state (not running or committed). Derived as `{prefix}/output/deferrable/{name}/recommended_start`. |

#### `static_loads` (dict, keyed by device name)

| Field | Type | Description |
|---|---|---|
| `topic_forecast` | string or null | MQTT topic for the timestamped load forecast (`list[{ts, kw, confidence}]`). Derived as `{prefix}/input/baseload/{name}/forecast` when absent. |

#### `hybrid_inverters` (dict, keyed by device name)

| Field | Type | Default | Description |
|---|---|---|---|
| `capacity_kwh` | float > 0 | — | Battery capacity in kWh. |
| `min_soc_kwh` | float ≥ 0 | 0.0 | Minimum SOC the solver will discharge to. |
| `charge_segments` | list (min 1) | — | Piecewise efficiency segments for charging. |
| `discharge_segments` | list (min 1) | — | Piecewise efficiency segments for discharging. |
| `wear_cost_eur_per_kwh` | float ≥ 0 | 0.0 | Degradation cost per kWh throughput. |
| `max_pv_kw` | float > 0 | — | Peak PV input power in kW (used to clip the forecast). |
| `topic_pv_forecast` | string or null | derived | MQTT topic for the PV power forecast. Derived as `{prefix}/input/hybrid/{name}/pv_forecast`. |
| `inputs.soc.topic` | string or null | derived | MQTT topic for SOC readings. Derived as `{prefix}/input/hybrid/{name}/soc`. |
| `inputs.soc.unit` | `kwh` or `percent` | — | Unit of the published SOC value. |

#### `thermal_boilers` (dict, keyed by device name)

| Field | Type | Default | Description |
|---|---|---|---|
| `capacity_litres` | float > 0 | — | Tank volume in litres. |
| `min_temp_c` | float | — | Minimum tank temperature required to satisfy DHW demand (°C). |
| `max_temp_c` | float | — | Maximum permissible tank temperature (°C). |
| `heat_loss_coeff_kw_per_k` | float > 0 | — | Tank heat loss coefficient (kW/K). |
| `element_power_kw` | float > 0 | — | Electrical power of the heating element (kW). |
| `cop` | float > 0 | 1.0 | Coefficient of performance (1.0 for pure resistance). |
| `min_run_steps` | int ≥ 0 | 0 | Minimum consecutive steps the element must run once switched on (avoids short cycling). |
| `inputs.topic_current_temp` | string or null | derived | MQTT topic for the current tank temperature (plain float, °C). Derived as `{prefix}/input/thermal_boiler/{name}/temp_c`. |

#### `space_heating_hps` (dict, keyed by device name)

| Field | Type | Default | Description |
|---|---|---|---|
| `elec_power_kw` | float > 0 | — | Electrical input power at full operation (kW). |
| `cop` | float > 0 | — | Coefficient of performance for space heating. |
| `mode` | `on_off` or `sos2` | `on_off` | Dispatch mode. `on_off`: binary on/off; `sos2`: continuous modulation. |
| `min_power_fraction` | float ∈ (0, 1] | 0.3 | Minimum operating point as a fraction of `elec_power_kw` (SOS2 mode only). |
| `min_run_steps` | int ≥ 0 | 0 | Minimum consecutive running steps (on/off mode only). |
| `inputs.topic_heat_needed_kwh` | string or null | derived | MQTT topic for the heat demand over the horizon (plain float, kWh). Required when `building_thermal` is not configured. Derived as `{prefix}/input/space_heating/{name}/heat_needed_kwh`. |
| `inputs.topic_heat_produced_today_kwh` | string or null | derived | MQTT topic for the cumulative heat produced today (plain float, kWh). Informational only; the solver does not read this topic. Derived as `{prefix}/input/space_heating/{name}/heat_produced_today_kwh`. |
| `building_thermal` | object or null | null | Optional building thermal model (BTM). When set, replaces the degree-days demand constraint with first-order temperature dynamics. |

`building_thermal` fields:

| Field | Type | Default | Description |
|---|---|---|---|
| `thermal_capacity_kwh_per_k` | float > 0 | — | Building thermal mass (kWh/K). |
| `heat_loss_coeff_kw_per_k` | float > 0 | — | Building heat loss coefficient (kW/K). |
| `comfort_min_c` | float | 19.0 | Minimum required indoor temperature (°C). |
| `comfort_max_c` | float | 24.0 | Maximum allowed indoor temperature (°C). |
| `inputs.topic_current_indoor_temp_c` | string or null | derived | MQTT topic for the current indoor temperature (plain float or JSON `{temp_c: float}`, °C). Derived as `{prefix}/input/space_heating/{name}/btm/indoor_temp_c`. |
| `inputs.topic_outdoor_temp_forecast_c` | string or null | derived | MQTT topic for the outdoor temperature forecast (JSON array of floats, °C, one per 15-minute step). Derived as `{prefix}/input/space_heating/{name}/btm/outdoor_forecast_c`. |

#### `combi_heat_pumps` (dict, keyed by device name)

| Field | Type | Default | Description |
|---|---|---|---|
| `capacity_litres` | float > 0 | — | DHW tank volume in litres. |
| `min_temp_c` | float | — | Minimum DHW tank temperature (°C). |
| `max_temp_c` | float | — | Maximum DHW tank temperature (°C). |
| `heat_loss_coeff_kw_per_k` | float > 0 | — | DHW tank heat loss coefficient (kW/K). |
| `elec_power_kw` | float > 0 | — | Electrical input power at full operation (kW). |
| `cop_dhw` | float > 0 | — | COP when operating in DHW mode. |
| `cop_sh` | float > 0 | — | COP when operating in SH mode. |
| `min_run_steps` | int ≥ 0 | 0 | Minimum consecutive steps per operating mode. |
| `inputs.topic_current_temp` | string or null | derived | MQTT topic for the current DHW tank temperature (plain float, °C). Derived as `{prefix}/input/combi_hp/{name}/temp_c`. |
| `inputs.topic_heat_needed_kwh` | string or null | derived | MQTT topic for the SH heat demand over the horizon (plain float, kWh). Required when `building_thermal` is not configured. Derived as `{prefix}/input/combi_hp/{name}/sh_heat_needed_kwh`. |
| `building_thermal` | object or null | null | Optional building thermal model. Same structure and semantics as for `space_heating_hps`. Only SH mode is affected; DHW tank dynamics are independent. |

#### `readiness` (optional)

| Field | Type | Default | Description |
|---|---|---|---|
| `min_horizon_hours` | float ≥ 0 | 1.0 | Minimum forecast coverage in hours required before a solve is permitted. A trigger received with less coverage is ignored. |
| `warn_below_hours` | float ≥ 0 | 8.0 | Log a warning when forecast coverage is below this threshold, but proceed with the solve. |
| `max_gap_hours` | float ≥ 0 | 2.0 | Log a warning when any forecast series has a gap wider than this threshold within the active horizon. |

#### `objectives` (optional)

| Field | Type | Default | Description |
|---|---|---|---|
| `balanced_weights.cost_weight` | float ≥ 0 | 1.0 | Weight on revenue/cost terms when strategy is `balanced`. |
| `balanced_weights.self_sufficiency_weight` | float ≥ 0 | 1.0 | Weight on grid import penalty. |
| `min_dispatch_gain_eur` | float ≥ 0 | 0.0 | Minimum projected saving in EUR required to dispatch storage. Below this threshold mimirheim publishes an idle schedule. 0.0 disables. |
| `exchange_shaping_weight` | float ≥ 0 | 0.0 | Weight for the optional secondary term `lambda × Σ_t(import_t + export_t)`. Breaks solver indifference on flat or near-symmetric tariffs by favouring lower total exchange. Must be orders of magnitude smaller than typical prices (e.g. `1e-4`). 0.0 disables. |

#### `constraints` (optional)

| Field | Type | Default | Description |
|---|---|---|---|
| `max_import_kw` | float or null | null | Hard cap on grid import across the horizon. |
| `max_export_kw` | float or null | null | Hard cap on grid export. `0.0` enforces zero export. |

#### `homeassistant` (optional)

See [Section 11](#11-home-assistant-integration).

#### `debug` (optional)

| Field | Type | Default | Description |
|---|---|---|---|
| `dump_dir` | path or null | null | Directory for solve dump files. Null = disabled. |
| `max_dumps` | int ≥ 0 | 50 | Maximum retained dump file pairs. 0 = unlimited. |

### Topic naming convention

All MQTT topics mimirheim reads and writes follow a predictable naming convention
derived from `mqtt.topic_prefix`. No topic string needs to appear in the
configuration for a standard single-broker deployment; every topic field
defaults to `None` and is filled in at startup by the schema validator.

To override the derived topic for any field, set it explicitly in the YAML.
Override only when you need to read a sensor from a non-mimirheim namespace (e.g.
a Home Assistant entity topic) or when sharing a topic between multiple instances.

**Global topics** (prefix = `mimirheim`):

| Config field | Derived topic |
|---|---|
| `outputs.schedule` | `mimir/strategy/schedule` |
| `outputs.current` | `mimir/strategy/current` |
| `outputs.last_solve` | `mimir/status/last_solve` |
| `outputs.availability` | `mimir/status/availability` |
| `inputs.prices` | `mimir/input/prices` |
| `reporting.notify_topic` | `mimir/status/dump_available` |

**Device input topics** (`{p}` = topic prefix):

| Config field | Derived topic |
|---|---|
| `batteries.{name}.inputs.soc.topic` | `{p}/input/battery/{name}/soc` |
| `ev_chargers.{name}.inputs.soc.topic` | `{p}/input/ev/{name}/soc` |
| `ev_chargers.{name}.inputs.plugged_in_topic` | `{p}/input/ev/{name}/plugged_in` |
| `hybrid_inverters.{name}.inputs.soc.topic` | `{p}/input/hybrid/{name}/soc` |
| `hybrid_inverters.{name}.topic_pv_forecast` | `{p}/input/hybrid/{name}/pv_forecast` |
| `pv_arrays.{name}.topic_forecast` | `{p}/input/pv/{name}/forecast` |
| `static_loads.{name}.topic_forecast` | `{p}/input/baseload/{name}/forecast` |
| `deferrable_loads.{name}.topic_window_earliest` | `{p}/input/deferrable/{name}/window_earliest` |
| `deferrable_loads.{name}.topic_window_latest` | `{p}/input/deferrable/{name}/window_latest` |
| `deferrable_loads.{name}.topic_committed_start_time` | `{p}/input/deferrable/{name}/committed_start` |
| `thermal_boilers.{name}.inputs.topic_current_temp` | `{p}/input/thermal_boiler/{name}/temp_c` |
| `space_heating_hps.{name}.inputs.topic_heat_needed_kwh` | `{p}/input/space_heating/{name}/heat_needed_kwh` |
| `space_heating_hps.{name}.inputs.topic_heat_produced_today_kwh` | `{p}/input/space_heating/{name}/heat_produced_today_kwh` |
| `space_heating_hps.{name}.building_thermal.inputs.topic_current_indoor_temp_c` | `{p}/input/space_heating/{name}/btm/indoor_temp_c` |
| `space_heating_hps.{name}.building_thermal.inputs.topic_outdoor_temp_forecast_c` | `{p}/input/space_heating/{name}/btm/outdoor_forecast_c` |
| `combi_heat_pumps.{name}.inputs.topic_current_temp` | `{p}/input/combi_hp/{name}/temp_c` |
| `combi_heat_pumps.{name}.inputs.topic_heat_needed_kwh` | `{p}/input/combi_hp/{name}/sh_heat_needed_kwh` |
| `combi_heat_pumps.{name}.building_thermal.inputs.topic_current_indoor_temp_c` | `{p}/input/combi_hp/{name}/btm/indoor_temp_c` |
| `combi_heat_pumps.{name}.building_thermal.inputs.topic_outdoor_temp_forecast_c` | `{p}/input/combi_hp/{name}/btm/outdoor_forecast_c` |

**Device output topics:**

| Config field | Derived topic |
|---|---|
| `batteries.{name}.outputs.exchange_mode` | `{p}/output/battery/{name}/exchange_mode` |
| `ev_chargers.{name}.outputs.exchange_mode` | `{p}/output/ev/{name}/exchange_mode` |
| `ev_chargers.{name}.outputs.loadbalance_cmd` | `{p}/output/ev/{name}/loadbalance` |
| `pv_arrays.{name}.outputs.power_limit_kw` | `{p}/output/pv/{name}/power_limit_kw` |
| `pv_arrays.{name}.outputs.zero_export_mode` | `{p}/output/pv/{name}/zero_export_mode` |
| `pv_arrays.{name}.outputs.on_off_mode` | `{p}/output/pv/{name}/on_off_mode` |
| `deferrable_loads.{name}.topic_recommended_start_time` | `{p}/output/deferrable/{name}/recommended_start` |

Device output topics are derived for all devices regardless of whether the
corresponding capability is enabled. A disabled capability means the topic is
present in the config but never published.

See IMPLEMENTATION_DETAILS §14 for the derivation mechanics and override pattern.

---

## 10. Readiness and Staleness

mimirheim tracks the freshness of every required input. A solve is attempted only when all required inputs are present, sensor readings are within their staleness window, and forecast data covers at least the minimum horizon.

### Trigger-driven solves

mimirheim does not solve on a fixed timer. A solve is triggered exclusively by a message on `{prefix}/input/trigger`. The trigger is typically published by an external scheduler (a cron job, an HA automation, or the `mimirheim_helpers/scheduler` companion tool) after all data sources have been updated for the current cycle.

If a trigger arrives while inputs are insufficient, mimirheim logs a warning and takes no action. The trigger is not queued or replayed.

### Sensor inputs

Battery SOC, EV state, and thermal device readings are required before mimirheim will solve. mimirheim checks only that a value has been received at least once since startup; there is no staleness window. The most recently retained message on the broker is always used as the current state.

| Input |
|---|
| `{batteries.*.inputs.soc.topic}` |
| `{ev_chargers.*.inputs.soc.topic}` |
| `{ev_chargers.*.inputs.plugged_in_topic}` |
| `{hybrid_inverters.*.inputs.soc.topic}` (when `inputs` is configured) |
| `{hybrid_inverters.*.topic_pv_forecast}` |
| `{thermal_boilers.*.inputs.topic_current_temp}` (when `inputs` is configured) |
| `{space_heating_hps.*.inputs.topic_heat_needed_kwh}` (when `inputs` is configured) |
| `{space_heating_hps.*.building_thermal.inputs.topic_current_indoor_temp_c}` (when BTM `inputs` is configured) |
| `{space_heating_hps.*.building_thermal.inputs.topic_outdoor_temp_forecast_c}` (when BTM `inputs` is configured) |
| `{combi_heat_pumps.*.inputs.topic_current_temp}` (when `inputs` is configured) |
| `{combi_heat_pumps.*.inputs.topic_heat_needed_kwh}` (when `inputs` is configured) |
| `{combi_heat_pumps.*.building_thermal.inputs.topic_current_indoor_temp_c}` (when BTM `inputs` is configured) |
| `{combi_heat_pumps.*.building_thermal.inputs.topic_outdoor_temp_forecast_c}` (when BTM `inputs` is configured) |

### Forecast inputs (coverage-based freshness)

Electricity prices and power forecasts are not checked for age. A Nordpool day-ahead price published at 13:00 is still valid at 21:00. Instead, mimirheim checks how far ahead the data extends.

At trigger time, mimirheim computes:

```
solve_start = now, floored to the nearest 15-minute boundary
horizon_end = min(last_ts for each forecast series that lies at or after solve_start)
n_steps     = (horizon_end − solve_start) in 15-minute steps
```

The solve is blocked if `n_steps` falls below `readiness.min_horizon_hours × 4`. A warning is logged (but the solve proceeds) if `n_steps` falls below `readiness.warn_below_hours × 4`.

| Input | Required |
|---|---|
| `{prefix}/input/prices` | At least `min_horizon_hours` of future coverage |
| `{pv_arrays.*.topic_forecast}` | At least `min_horizon_hours` of future coverage |
| `{static_loads.*.topic_forecast}` | At least `min_horizon_hours` of future coverage |

### Gap warnings

If any forecast series has a gap wider than `readiness.max_gap_hours` within the active horizon, mimirheim logs a warning. Gap detection is informational — it does not block the solve. Gaps are filled by the resampler (step function for prices, linear interpolation for power).

### Optional inputs

| Input | Behaviour when absent |
|---|---|
| `{prefix}/input/strategy` | Strategy defaults to `minimize_cost`; solve is not blocked |
| `{deferrable_loads.*.topic_window_earliest}` | Deferrable load excluded from this solve |
| `{deferrable_loads.*.topic_window_latest}` | Deferrable load excluded from this solve |
| `{space_heating_hps.*.inputs.topic_heat_needed_kwh}` | Space heating HP excluded when BTM is not configured and no demand is present |
| `{combi_heat_pumps.*.inputs.topic_heat_needed_kwh}` | Combi HP SH mode excluded when BTM is not configured and no SH demand is present |

### Retained messages

All forecast and sensor input topics must be published with `retain=True`. On mimirheim restart, the broker delivers the last retained value for each topic immediately on subscribe. Without retention, mimirheim loses state on restart and blocks until fresh messages arrive.

---

## 11. Home Assistant Integration

### MQTT discovery

mimirheim can publish [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery) payloads so that HA automatically creates entities for the schedule outputs and per-device setpoints.

Enable in config:

```yaml
homeassistant:
  enabled: true
  discovery_prefix: homeassistant   # default; change only if HA uses a custom prefix
  device_name: mimirheim                 # human-readable label in HA device registry
  # device_id: my-mimirheim              # optional; defaults to mqtt.client_id
```

Discovery payloads are published retained on every broker connection. The following HA entities are created:

| Entity | HA type | State topic | Value |
|---|---|---|---|
| `{device_name} Grid Import` | sensor (power) | `outputs.current` | `grid_import_kw` |
| `{device_name} Grid Export` | sensor (power) | `outputs.current` | `grid_export_kw` |
| `{device_name} Solve Status` | sensor | `outputs.last_solve` | `status` |
| `{device_name} {device_name} setpoint` | sensor (power) | `{prefix}/device/{name}/setpoint` | `kw` |

The last row is repeated for every configured device across all device sections.

All entities use `outputs.availability` as their `availability_topic`, so they appear as unavailable in HA when mimirheim is offline.

### Publishing inputs from HA

The simplest way to publish a battery SOC from HA to mimirheim is an automation triggered by the sensor state change:

```yaml
automation:
  trigger:
    - platform: state
      entity_id: sensor.battery_soc_kwh
  action:
    - service: mqtt.publish
      data:
        topic: mimir/input/bat/soc
        retain: true
        payload: "{{ states('sensor.battery_soc_kwh') | float }}"
```

For PV and load forecasts, publish a flat JSON array (one value per 15-minute step) retained to the configured `topic_forecast` topic.

---

## 12. Testing

### Running the tests

```bash
uv run pytest          # unit + scenario tests (fast, no broker required)
uv run pytest -m integration   # integration tests (require in-process broker, ~20 s)
uv run pytest --update-golden  # regenerate golden files after solver changes
```

### Test layers

**Unit tests** (`tests/unit/`) — device constraint logic, objective builder, config schema validation, input parser, MQTT publisher, HA discovery, and readiness state. All run without a broker or solver binary. Coverage includes happy paths and validation rejection (sad paths) for every Pydantic model.

**Scenario tests** (`tests/scenarios/`) — golden file regression. `build_and_solve()` is a pure function; each scenario is a directory with `input.json`, `config.yaml`, and `golden.json`. The test calls the solver and compares the result field-by-field. Golden files are committed and updated only deliberately with `--update-golden`.

| Scenario | Assertion |
|---|---|
| `flat_price` | Battery does not cycle; no economic incentive to do so |
| `high_price_spread` | Battery charges when prices are low, discharges when high |
| `ev_not_plugged` | Schedule produced without EV (`available: false`); no crash |

**Integration tests** (`tests/integration/`) — full MQTT round-trip against an `amqtt` in-process broker. Marked `@pytest.mark.integration` and excluded from the default test run. Covers retained message delivery on connect, the happy-path schedule publish, and the infeasible-solve status path.


