# Mimirheim — Implementation Details

This document records internal implementation decisions: library choices, architectural rationale, and constraints that are not visible in the public API or configuration schema. It is intended for contributors and maintainers, not end users.

---

## Contents

1. [Configuration parsing & validation](#1-configuration-parsing--validation)
2. [Solver backend](#2-solver-backend)
3. [Config schema design](#3-config-schema-design)
4. [Testing architecture](#4-testing-architecture)
5. [Debug solve dump](#5-debug-solve-dump)
6. [Pydantic config models as device constructor arguments](#6-pydantic-config-models-as-device-constructor-arguments)
7. [SolveBundle and per-device input models](#7-solvebundle-and-per-device-input-models)
8. [MIP model design: device contract, split variables, piecewise efficiency, and objective builder](#8-mip-model-design-device-contract-split-variables-piecewise-efficiency-and-objective-builder)
9. [Concurrency model](#9-concurrency-model)
10. [Fault resilience](#10-fault-resilience)
11. [Development environment and dependency management](#11-development-environment-and-dependency-management)
12. [MQTT topic naming convention and auto-derivation](#14-mqtt-topic-naming-convention-and-auto-derivation)

---

## 1. Configuration parsing & validation

**Decision: Pydantic v2**

mimirheim uses [Pydantic v2](https://docs.pydantic.dev/latest/) for YAML config loading, validation, and schema generation.

### Rationale

- **Single source of truth.** Field types, constraints (`ge=0`, `le=1`), defaults, and documentation (`title`, `description`) are declared once on the model. No separate JSON Schema file to keep in sync.
- **JSON Schema generation.** `model.model_json_schema()` produces a JSON Schema that can be consumed directly by UI form libraries (jsonforms, react-jsonschema-form). This is the intended path to an auto-generated configuration UI.
- **Rich field annotations.** `Field(title=..., description=..., examples=[...])` carry UI hints at the model level. Annotate all fields from the start so the generated schema is useful.
- **v2 performance.** The Rust core (pydantic-core) makes re-validating config on every solve negligible.

### Known limitations

- **JSON Schema ≠ UI schema.** Pydantic generates the data schema. Most form libraries also require a separate *UI schema* for field ordering, grouping, and conditional visibility (e.g. `strategy_weights` only shown when `strategy: balanced`). That layer must be written once per target UI library.
- **`additionalProperties` maps.** Sections like `batteries: dict[str, BatteryConfig]` generate `additionalProperties` schemas. Most form generators do not render "add a named entry to a map" well out of the box; a custom widget will be needed for device lists.

### Usage pattern

```python
# config/schema.py
import yaml
from pydantic import BaseModel, Field, model_validator

class BatteryConfig(BaseModel):
    capacity_kwh: float = Field(gt=0, title="Capacity", description="Usable battery capacity in kWh")
    ...

class MimirheimConfig(BaseModel):
    batteries: dict[str, BatteryConfig] = Field(default_factory=dict)
    ...

    @model_validator(mode="after")
    def device_names_unique(self) -> "MimirheimConfig":
        # names must be unique across all device sections (they become output keys)
        ...

def load_config(path: str) -> MimirheimConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return MimirheimConfig.model_validate(raw)
```

### Unknown fields are forbidden

All Pydantic models in Mimirheim — config models, `SolveBundle`, per-device input models — must set:

```python
model_config = ConfigDict(extra="forbid")
```

This means a config file or MQTT bundle containing an unrecognised field raises a hard validation error immediately, rather than silently succeeding. The benefit: schema changes are always explicit. A renamed field produces a clear error at load time rather than a silent wrong value or stale field lingering undetected.

Versioning (a top-level `version:` field in `config.yaml`, `schema_version` in golden files) is deferred until the first schema stabilises. `extra="forbid"` is the minimal safeguard that makes the absence of versioning safe during early development.

---

## 2. Solver backend

**Decision: CBC (COIN-OR Branch and Cut), abstracted interface**

mimirheim uses [CBC](https://github.com/coin-or/Cbc) (free, Eclipse Public Licence 2.0) via the
[`python-mip`](https://www.python-mip.com/) package for MILP solving. CBC is bundled as a
compiled shared library inside `python-mip`; no external binary installation is required.

### Rationale

The benchmark scenario `prosumer_ev_48h` (192 time steps, 768 binary variables) was measured
under both solvers on the same model:

| Solver | Method | Total time |
|---|---|---|
| HiGHS via `highspy` | Python API (addVar/addConstr) | ~21 s |
| HiGHS | CLI from MPS file | ~6 s |
| CBC | CLI from MPS file | **~0.2 s** |
| CBC via `python-mip` | Python API | **~1 s** |

The dominant cause is cut generation. CBC's aggressive Gomory cuts are highly effective on the
temperature-coupled binary chains that thermal device constraints (boiler, combi heat pump, space
heating HP) produce. At the root node, CBC tightens the LP relaxation enough to prove optimality
with very few branch-and-bound nodes. HiGHS converges slowly on the same structure.

A secondary cause is model-build overhead: `highspy` adds variables and constraints one at a time
via FFI calls, producing approximately 8 seconds of pure Python→C++ overhead at 192 steps before
the solver even starts. `python-mip` via CBC has similar call-by-call overhead but the solver
itself is so much faster that it dominates less.

BC is free, redistributable, and well-established (it is the default solver in PuLP and many other
open-source optimisation tools). No licence management is required.

See `SOLVER_REWRITE.md` for the full measurement methodology and decision record.

### Configurable time limit

A `time_limit_seconds` cap (default: 59 s) prevents the solver from blocking the re-solve loop.
If the limit is hit, CBC returns the best incumbent found so far. This is acceptable for a
rolling-horizon strategy — a slightly suboptimal schedule is better than no schedule.

### SolverBackend interface

The solver is not called directly from device or objective code. All interactions go through a
thin `SolverBackend` Protocol so any compliant backend can be substituted without touching
model-building code:

```python
# mimirheim/core/solver_backend.py
from typing import Any, Protocol

class SolverBackend(Protocol):
    def add_var(self, lb: float = 0.0, ub: float = 1e30, integer: bool = False) -> Any: ...
    def add_constraint(self, expr) -> None: ...
    def set_objective_minimize(self, expr) -> None: ...
    def set_objective_maximize(self, expr) -> None: ...
    def solve(self, time_limit_seconds: float) -> str: ...   # returns "optimal" | "feasible" | "infeasible"
    def var_value(self, var: Any) -> float: ...
    def add_sos2(self, variables: list[Any], weights: list[float]) -> None: ...
    def objective_value(self) -> float: ...
```

`ModelContext.solver` is typed as `SolverBackend`. The concrete implementation is
`CBCSolverBackend`, which wraps `mip.Model`. Device classes never import `mip` directly.

### python-mip API mapping

| `SolverBackend` method | `python-mip` equivalent |
|---|---|
| `add_var(lb, ub, integer)` | `model.add_var(lb=lb, ub=ub, var_type=INTEGER\|CONTINUOUS)` |
| `add_constraint(expr)` | `model += expr` |
| `set_objective_minimize(expr)` | `model.objective = mip.minimize(expr)` |
| `set_objective_maximize(expr)` | `model.objective = mip.maximize(expr)` |
| `solve(t)` | `model.optimize(max_seconds=t)`, then map `OptimizationStatus` |
| `var_value(var)` | `var.x` |
| `objective_value()` | `model.objective_value` |
| `add_sos2(vars, weights)` | Big-M binary emulation (see below) |

### SOS2 implementation

Neither `highspy` nor `python-mip` exposes a native SOS2 constraint API that maps cleanly to
the `SolverBackend` Protocol. The `add_sos2` method is implemented via a Big-M binary emulation
that is solver-agnostic and therefore portable across any backend:

```
For N weight variables w[0..N-1], create N-1 binary variables b[0..N-2]:
    sum(b_i) == 1                       (exactly one segment active)
    w[0]   <= b[0]
    w[i]   <= b[i-1] + b[i]             (interior variables)
    w[N-1] <= b[N-2]
```

When `b[i] = 1`, only `w[i]` and `w[i+1]` can be nonzero; all others are forced to zero by
their upper-bound constraints. This correctly models piecewise-linear interpolation along a
single segment at a time.

---

## 3. Config schema design

**Decision: typed sections instead of a discriminated device map**

The configuration uses typed top-level sections (`batteries:`, `pv_arrays:`, `ev_chargers:`, etc.) rather than a single `devices:` map with a `type:` discriminator field.

### Rationale

A `dict[str, Battery | PV | EV | ...]` discriminated union produces `oneOf` / `anyOf` blocks in JSON Schema. While Pydantic handles these correctly, many UI form generators (json-editor, AutoForm, simpler jsonforms configurations) either fail to render them or require significant custom configuration to do so correctly.

Typed sections produce `additionalProperties: { $ref: "#/$defs/BatteryConfig" }` — a pattern every form library understands. Each section is a homogeneous map with a single, unambiguous schema.

### Consequence

Device names must be unique across all sections since they become keys in the MQTT output payload. This is enforced by a `model_validator` on `MimirheimConfig` at load time. The `type` field present in output payloads is derived at solve time from which section a device belongs to — it is not stored in the config.

---

## 4. Testing architecture

### Core requirement: `build_and_solve()` is a pure function

The solver entry point must have no side effects and no I/O dependencies:

```python
def build_and_solve(bundle: SolveBundle, config: MimirheimConfig) -> SolveResult: ...
```

`SolveBundle` carries all inputs (forecasts, device states, current time). `SolveResult` carries the full schedule and current strategy. Neither knows anything about MQTT, files, or time. This makes the solver directly instantiable in tests with no infrastructure.

The MQTT layer and the test harness are both just different ways to build a `SolveBundle` and consume a `SolveResult`.

---

### Test layers

#### Layer 1 — Unit tests (no I/O, no broker)

Scope: individual device constraint logic, objective builder, horizon calculation, config validation, input parsing.

- Run entirely in-process. No network, no files beyond the test itself.
- Each device is tested with a minimal horizon (T=4) by constructing the LP directly via `CBCSolverBackend` and `ModelContext`.
- Pydantic models are tested with valid and invalid dicts; `model_validate` must raise on bad input.
- MQTT IO classes are tested by injecting a **mock paho client** at construction. Assert that `client.publish()` is called with the correct topic, QoS, retain flag, and JSON payload.

```python
# Example: test that battery publish uses the right topic
def test_publish_battery_device_topic(mock_paho_client):
    publisher = MqttPublisher(client=mock_paho_client, config=mqtt_config)
    publisher.publish_strategy(result)
    mock_paho_client.publish.assert_any_call(
        "mimir/strategy/device/battery_main",
        payload=ANY,
        qos=1,
        retain=True,
    )
```

#### Layer 2 — Solver regression tests with golden files

Scope: `build_and_solve()` end-to-end, no MQTT involved.

Each scenario is a directory under `tests/scenarios/<scenario_name>/`:

```
tests/scenarios/high_price_spread/
  input.json       # SolveBundle serialised to JSON
  config.yaml      # MimirheimConfig for this scenario
  golden.json      # expected SolveResult serialised to JSON
```

The test runner:
1. Loads `input.json` → `SolveBundle`
2. Loads `config.yaml` → `MimirheimConfig`
3. Calls `build_and_solve(bundle, config)` → `SolveResult`
4. Compares result to `golden.json` field-by-field with tolerance on floats (`pytest.approx`)

**Updating golden files** is an explicit workflow, not automatic:

```bash
pytest --update-golden
```

This flag causes the test to overwrite `golden.json` with the current output instead of asserting against it. The diff is then reviewed in code review like any other change. Golden files are committed to the repository.

**Float tolerance**: solver output values are compared with `abs=1e-4` (0.1 W). Objective value is compared with `rel=1e-3`. Timestamps and integer fields are compared exactly.

#### Layer 3 — MQTT integration tests (in-process broker)

Scope: the full loop — MQTT message arrives → parsed → readiness state updates → solve triggered → strategy published to MQTT.

**Decision: `amqtt` (pure Python, in-process)**

`amqtt` (formerly `hbmqtt`) is a pure-Python MQTT 3.1.1 / 5.0 broker and client library. It runs inside the same Python process using `asyncio`. No Docker, no system packages, no external service. Install via `pip install amqtt`.

```python
# conftest.py
import pytest
import asyncio
from amqtt.broker import Broker

@pytest.fixture
async def mqtt_broker():
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": "127.0.0.1:11883"}}})
    await broker.start()
    yield "mqtt://127.0.0.1:11883"
    await broker.stop()
```

Integration tests publish real MQTT messages to the in-process broker and assert on messages received from it. This exercises retained message semantics, topic routing, and the readiness state machine under realistic conditions without any external dependency.

**Why not just mock paho for integration tests?**
Mocks verify call signatures, not behaviour. Retained message semantics, QoS, reconnection handling, and multi-topic subscription interactions are only testable against a real broker. The in-process broker gives that without infrastructure burden.

**Why not a containerised Mosquitto?**
Containers require Docker or Podman in the developer and CI environment. This is not a safe assumption for all contributors and makes `pytest` not runnable with a plain `pip install`. `amqtt` installs with pip and runs in-process — setup is zero.

---

### Golden file format

`input.json` is a serialised `SolveBundle`:

```json
{
  "solve_time_utc": "2026-03-30T14:00:00Z",
  "prices": [ { "t": 0, "import_price_eur_kwh": 0.22, "export_price_eur_kwh": 0.18, "confidence": 1.0 } ],
  "pv_forecast": [ { "t": 0, "power_kw": 2.4, "confidence": 0.9 } ],
  "base_load": [ { "t": 0, "power_kw": 0.42 } ],
  "device_states": {
    "battery_main": { "soc_kwh": 5.0 },
    "ev_charger":   { "soc_kwh": 20.0, "plugged_in": true },
    "washing_machine": { "window_earliest": "2026-03-30T14:00:00Z", "window_latest": "2026-03-30T18:00:00Z" }
  }
}
```

`golden.json` is a serialised `SolveResult` (same structure as `mimir/strategy/schedule` plus `mimir/strategy/current`).

`input.json` uses the flat-array `SolveBundle` format (already resampled to the 15-minute grid). The wire-format step models (`PriceStep`, `PowerForecastStep`) do not appear in golden files; they are internal to `ReadinessState`.

---

### Test directory layout

```
tests/
  unit/
    test_battery_constraints.py
    test_pv_constraints.py
    test_deferrable_load_constraints.py
    test_ev_constraints.py
    test_objective_builder.py
    test_horizon.py
    test_config_schema.py          # pydantic model_validate happy/sad paths
    test_input_parser.py           # json_path, unit conversion, unavailable handling
    test_mqtt_publisher.py         # mock paho: correct topics, retain flags, payloads
    test_readiness.py              # state machine: missing inputs block solve
  scenarios/
    high_price_spread/
      input.json
      config.yaml
      golden.json
    flat_price/
      ...
    negative_export_price/
      ...
    ev_not_plugged/
      ...
    low_confidence_horizon/
      ...
    zero_export_constrained/
      ...
  integration/
    test_mqtt_roundtrip.py         # amqtt in-process broker: publish input → assert output
    test_readiness_mqtt.py         # retained message on restart, staleness expiry
```

---

### CI matrix

| Test layer | Requires broker | Requires solver | Speed | Run on |
|---|---|---|---|---|
| Unit | No | Partial (LP only) | < 5 s | Every commit |
| Scenario / golden | No | Yes (full MILP) | 10–60 s | Every commit |
| Integration | amqtt (pip) | No | < 10 s | Every commit |

All three layers run on a plain `pip install -e .[dev]` with no system dependencies beyond Python ≥ 3.11.

---

## 5. Debug solve dump

### Decision: structured JSON dump when debug.enabled is True

When `config.debug.enabled` is True, mimirheim writes two files to a configurable dump directory after every solve:

```
<dump_dir>/
  <iso_timestamp>_input.json    # SolveBundle — identical to golden file input.json (null fields omitted)
  <iso_timestamp>_output.json   # SolveResult — human-readable post-processed form (see below)
```

Timestamps use UTC ISO 8601 with seconds, e.g. `2026-03-30T14-15-00Z_input.json`.

### Output file format

The output file is a post-processed form of `SolveResult` optimised for human readability and self-contained analysis:

- Step index `t` is replaced by the UTC datetime string for that step (computed from `bundle.solve_time_utc + t * 15 min`).
- Per-step `import_price_eur_per_kwh` and `export_price_eur_per_kwh` are added to each schedule entry from the corresponding bundle horizon arrays, making the output file self-contained.
- All float values are rounded to 4 decimal places (0.1 W resolution).
- Floating-point solver residuals smaller than 1e-6 in absolute value are clamped to 0.0 (e.g. `-1.3e-12` becomes `0.0`).
- Null device fields (`power_limit_kw`, `zero_export_mode`) are omitted.

The input file is written verbatim from `SolveBundle.model_dump_json` with null fields omitted. It remains format-compatible with golden scenario input files.

### Rationale

- **Debug level, not always-on.** Production solves run at INFO. Writing files on every solve (every 15 minutes, indefinitely) would grow unbounded. The dump only activates when a developer or user explicitly sets `debug.enabled: true`.
- **Human-readable output.** Datetime strings replace opaque step indices. Prices are co-located with the schedule step that uses them. Solver noise is suppressed. This makes the output useful for debugging without further post-processing.
- **Structured JSON, not log lines.** Solver inputs and outputs are rich structured data. Separate files are unambiguous and directly loadable by analysis scripts.

### Configuration

```yaml
debug:
  enabled: true                  # enables DEBUG logging and dump file writing
  dump_dir: "/tmp/mimirheim_dumps"   # directory for solve dumps; null = no files written
  max_dumps: 50                  # rotate oldest files when limit is reached; 0 = unlimited
```

`dump_dir: null` disables file writing entirely. This is the default.

`debug_dump` is called by the solve loop after `build_and_solve()` returns when `config.debug.enabled` is True.

### Security note

Dump files contain energy price data, device state (SOC values, EV plug state), and schedule decisions. They do not contain credentials or personal identifiers. The dump directory should have filesystem permissions restricted to the mimirheim process user. Document this in the deployment guide.

**Decision: device solver classes accept their Pydantic `*Config` model directly**

Each device solver class takes its validated Pydantic config model as a constructor argument rather than unpacking individual fields:

```python
# mimirheim/devices/battery.py
from mimirheim.config.schema import BatteryConfig

class Battery:
    def __init__(self, name: str, config: BatteryConfig) -> None:
        self.name = name
        self.config = config
        # access as self.config.capacity_kwh, self.config.charge_segments, etc.
```

Devices are instantiated in the build pipeline by iterating over the typed config sections:

```python
# mimirheim/core/model_builder.py
batteries = [
    Battery(name=name, config=cfg)
    for name, cfg in hioo_config.batteries.items()
]
```

### Rationale

- **No parameter duplication.** Adding a field to `BatteryConfig` (e.g. `soc_init_kwh`) automatically makes it available inside `Battery` without updating a constructor signature. With unpacked arguments, every new field requires two changes.
- **Validation already done.** By the time a device class is constructed, the config has passed Pydantic validation. No defensive checks are needed inside device classes for missing or out-of-range values.
- **`model_dump()` is the serialisation path.** When assembling `SolveBundle.device_states` or writing debug dumps, `config.model_dump()` produces the correct JSON with no manual field listing.
- **Refactoring is safe.** Renaming a field in `BatteryConfig` produces a Pydantic validation error immediately at load time, not a silent wrong value deep in the solver.

### Boundary rule

Device classes (`mimirheim/devices/`) may import from `mimirheim/config/schema.py` as a typed argument, but must not import from `mimirheim/io/` or any MQTT/YAML machinery. Config flows downward from IO → config → devices. Devices never call back into IO.

### Confidence is external, not internal

A key divergence from common reference implementations: mimirheim does **not** compute confidence internally using decay parameters (`alpha_price`, `alpha_pv`). Confidence is a per-step float in `SolveBundle`, supplied by the publisher.

Internally computing confidence would:
- Hardcode a specific decay model (exponential) that may not suit all publishers
- Require solver config changes to tune forecast quality
- Prevent publishers from using richer models (ML uncertainty bands, ensemble spread)

The publisher computes confidence from whatever model it uses and injects it into the input schema. `mimirheim/core/confidence.py` contains only *helpers that consume* per-step confidence values — it does not produce them. This is the correct separation.

---

## 7. SolveBundle and per-device input models

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

### Why Pydantic for input models too

MQTT is a system boundary: payloads arrive from external inverters, sensors, and third-party publishers that mimirheim does not control. Validating at this boundary means:

- **Range checks** catch misbehaving hardware before it corrupts a solve (a faulty battery BMS reporting `soc_kwh = -999` gets rejected, not silently optimised around).
- **Staleness checks** are enforced declaratively in `model_validator`, not scattered across the IO layer.
- **Coercion** from raw MQTT bytes/strings to typed Python values happens in one place (the Pydantic model), not ad-hoc throughout the codebase.

### Forecast resampling (`mimirheim/core/forecast.py`)

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

`horizon_end` is the *minimum* of the last known timestamp across all forecast series. This prevents the solver from extrapolating: no series is extended beyond its last data point.

**Price resampling (step function):** The price for a given `ts` applies until the next timestamp in the array. Prices between known steps are constant; the last known price does not extend beyond `horizon_end`.

**Power resampling (linear interpolation):** PV generation and static load are interpolated linearly between adjacent known points. Linear interpolation produces smoother ramps and avoids the abrupt jumps that step-function resampling would introduce for slowly varying quantities.

**Gap detection:** `find_gaps()` scans a sorted series within `[solve_start, horizon_end]` and returns intervals wider than `readiness.max_gap_hours`. Gaps are reported as warnings and filled by the resampler — they do not block the solve.

`SolveBundle.model_dump()` produces the exact JSON written to golden input files and debug dumps, with no manual field listing. `SolveBundle.model_validate(json)` replays any dump as a regression test directly. This was a design goal from §4 and §5; keeping all inputs in one validated model is what makes it work for free.

### Boundary rule for inputs

Input models (`BatteryInputs`, `EvInputs`, etc.) live in `mimirheim/core/bundle.py`. The IO layer (`mimirheim/io/`) constructs them from MQTT messages. Device classes (`mimirheim/devices/`) receive them as arguments to `add_variables()` and `add_constraints()`. Device classes never call into `mimirheim/io/` — inputs are handed down, not fetched.

---

## 8. MIP model design: device contract, split variables, piecewise efficiency, and objective builder

### ModelContext

`ModelContext` is a short-lived container created once per solve in `build_and_solve()` and threaded through every model-building call. It holds the three things every device and the objective builder need without being passed as individual arguments:

```python
# mimirheim/core/context.py
from highspy import Highs  # or abstracted SolverBackend

class ModelContext:
    def __init__(self, solver: Highs, horizon: int, dt: float) -> None:
        self.solver = solver     # the live solver instance — devices add variables and constraints here
        self.T = range(horizon)  # time index; len(T) == len(bundle.horizon_prices)
        self.dt = dt             # time step duration in hours (0.25 for quarter-hourly)
```

`ModelContext` does **not** carry `SolveBundle` or `MimirheimConfig`. Those are passed explicitly where needed so that the data flow stays visible at each call site. Devices receive their slice of the bundle via `add_constraints(ctx, inputs)` and read their config from `self.config` (set at construction per §6).

`dt` is always `0.25` (15 minutes). The horizon length $H$ is variable — it equals the number of 15-minute steps between `solve_start` and `horizon_end` as computed by `compute_horizon_steps()`. The time step duration is fixed regardless of horizon length.

### Device method contract

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

### Grid device

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

### Split charge/discharge variables

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

### Piecewise efficiency (battery and EV)

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

### Wear cost in objective terms

Battery and EV degradation is modelled as a per-kWh throughput cost added to the objective. This prevents the solver from cycling the battery aggressively to exploit small price spreads that do not justify the wear:

```
objective_terms(t)  =  +wear_cost_eur_per_kwh × (total_charge[t] + total_discharge[t]) × dt
```

`wear_cost_eur_per_kwh` is a config field on `BatteryConfig` and `EvConfig`. The term is added to the minimisation objective, so a positive value discourages cycling. Setting it to zero disables wear modelling. A typical value for a lithium battery is 0.02–0.05 €/kWh throughput. The correct value depends on the battery's cycle life warranty and replacement cost, so it is left to the user to configure.

### Vendor capability flags

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

### ObjectiveBuilder

A single `ObjectiveBuilder` class is responsible for translating the `strategy` field from `SolveBundle` into the MIP objective expression. Nothing else in the model-building pipeline sets the objective.

```python
# mimirheim/core/objective.py
class ObjectiveBuilder:
    def build(self, ctx: ModelContext, devices: list[Device], grid: Grid, bundle: SolveBundle, config: MimirheimConfig) -> None: ...
```

Internally it branches on `bundle.strategy`:

- **`minimize_cost`** — weighted objective dominated by `confidence[t] × (import_price[t] × import[t] − export_price[t] × export[t])`. Import and export penalties derived from `constraints` block are added.
- **`minimize_consumption`** — lexicographic two-solve: first minimise total grid import (Phase 1), then minimise full net cost subject to the import bound found in Phase 1 (Phase 2). Phase 2 uses the same objective as `minimize_cost`, so export revenue and device wear are still optimised within the import constraint.
- **`balanced`** — weighted combination of cost and self-sufficiency terms using `balanced_weights` from config.

All three modes add:
- Device wear terms from `device.objective_terms(t)` for each device
- Import/export hard cap enforcement from `constraints.import_limit_kw` / `constraints.export_limit_kw` (added as constraints, not objective terms)
- Confidence weighting: every economic term is multiplied by `bundle.horizon_confidence[t]`

The lexicographic solve for `minimize_consumption` is the only case where `build_and_solve()` calls the solver twice internally. The two solves are hidden behind the same `build_and_solve()` signature — callers see one call and one `SolveResult`.

### Power balance constraint

The power balance is the central constraint that couples all devices. It is assembled inside `build_and_solve()` after all devices have added their variables — not inside any device or in a separate class:

```python
for t in ctx.T:
    ctx.solver.add_constraint(
        sum(d.net_power(t) for d in devices) + grid.net_power(t) == 0
    )
```

This enforces that at every time step, total production equals total consumption. PV and discharging batteries contribute positive `net_power`; loads and charging devices contribute negative. The grid variable absorbs any imbalance within its configured limits. If no feasible balance exists (e.g. load exceeds all generation plus import limit), the solve returns `infeasible`.

### Thermal boiler and DHW tank dynamics

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

### Space heating heat pump model

`SpaceHeatingDevice` supports two modes configured via `SpaceHeatingConfig.mode`:

- **On/off** — `hp_on[t] ∈ {0, 1}`. Heat output at step $t$ is `hp_on[t] * elec_power_kw * cop`. An optional `min_run_steps` constraint prevents frequent cycling.
- **SOS2 (modulating)** — An SOS2 set allows the heat pump to operate at any power level between `min_power_fraction * elec_power_kw` and `elec_power_kw`, or be completely off. SOS2 encodes a piecewise linear function without additional binaries; only two adjacent breakpoint weights are non-zero at any feasible solution.

**Degree-days path (no BTM):** When `building_thermal` is not configured, a single constraint requires the total heat delivered over the horizon to meet the demand `heat_needed_kwh`:

```
Σ_t hp_heat[t] * dt >= heat_needed_kwh
```

The device is excluded entirely (no variables added) when `heat_needed_kwh == 0.0` and no BTM is configured.

**BTM path:** See the Building thermal model section below.

### Combi heat pump model

`CombiHeatPumpDevice` operates in one of three states per time step: DHW mode, SH mode, or idle. A mutual exclusion constraint ensures at most one mode is active:

```
dhw_mode[t] + sh_mode[t] <= 1,    dhw_mode[t], sh_mode[t] ∈ {0, 1}
```

The DHW mode feeds the hot-water tank (same dynamics as `ThermalBoilerDevice`). The SH mode delivers space heating power `sh_mode[t] * elec_power_kw * cop_sh` kW of thermal output.

When `building_thermal` is not configured, the SH mode power must satisfy the aggregate demand `heat_needed_kwh` over the horizon (same as the space heating HP degree-days constraint). When `building_thermal` is configured, the BTM replaces the degree-days constraint for the SH portion.

### Building thermal model (BTM)

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

---

## 9. Arbitration engine and closed-loop enforcer selection

**Module: `mimirheim/core/control_arbitration.py`**

After `build_and_solve()` and `apply_gain_threshold()`, the solve loop calls:

```python
result = assign_control_authority(result, bundle, config)
```

This pure function sets `DeviceSetpoint.zero_exchange_active` (and `loadbalance_active` for EVs) on every step in the schedule. It is the sole code path that sets these fields.

### Why the solver does not zero out closed-loop device variables

When a battery or EV will operate in closed-loop zero-exchange mode for a step, the solver variable for that device is **not** suppressed or fixed to zero. This is intentional.

The device will still be physically charging or discharging — it is just doing so autonomously under firmware control rather than following a numeric setpoint from mimirheim. If the solver does not model that behavior, the SOC trajectory across the horizon becomes incorrect: the solver assumes the battery is idle when it is actually absorbing or supplying several kilowatts. Incorrect SOC estimates cause wrong decisions on the steps immediately before and after the closed-loop step.

The correct model is: let the solver plan a numeric setpoint for the battery on closed-loop steps, because that plan represents the best prediction of what the hardware will actually do (absorbing surplus to achieve near-zero exchange). The post-process layer then overrides the published command with the closed-loop enable flag. The solver plan is advisory; the hardware firmware performs the real-time enforcement.

SOC state is continuous across all steps, including closed-loop steps. If the hardware does not track the solved setpoint exactly (expected — firmware PID loops are not perfect), the next solve cycle corrects the SOC trajectory using the fresh reading from MQTT.

### Enforcer selection

A step is **near-zero-exchange** when:

```
grid_import_kw <= exchange_epsilon_kw  AND  grid_export_kw <= exchange_epsilon_kw
```

Only near-zero-exchange steps trigger enforcer selection. All other steps clear all closed-loop flags.

A device is an **eligible candidate** for a near-zero-exchange step when:

1. Its capability flag is set (`zero_exchange` for batteries/EVs, `zero_export` for PV).
2. For EVs: the vehicle is plugged in (`bundle.ev_inputs[name].available` is True).
3. Its absorption headroom is >= `config.control.headroom_margin_kw`.

**Absorption headroom** is the additional power the device can absorb at its current operating point: for batteries and EVs, `max_charge_kw - actual_charge_kw + actual_discharge_kw`; for PV, the current production kW.

Candidates are scored by a four-level cascade (descending; later levels break ties):

1. Efficiency at the expected compensation power (PV always scores 0.0 — last resort).
2. Headroom margin (headroom minus expected compensation — more slack is better).
3. Wear proxy: lower `wear_cost_eur_per_kwh` wins.
4. Type priority (battery=3, EV=2, PV=1), then device name (lexicographic).

**Hysteresis:** a challenger must exceed the current enforcer's efficiency score by `config.control.switch_delta` to trigger a switch.

**Minimum dwell:** once selected, a device holds the enforcer role for at least `config.control.min_enforcer_dwell_steps` consecutive steps, unless it becomes ineligible.

### Loadbalance suppression

When a battery is the `zero_exchange_active` enforcer for a step, any EV with `capabilities.loadbalance=True` receives `loadbalance_active=False` for that step. The battery's closed-loop controller and an EVSE loadbalance controller both target the same grid current transformer; only one may be authoritative per step.

An EV that is itself the `zero_exchange_active` enforcer receives `zero_exchange_active=True` and `loadbalance_active=False`. `loadbalance_active=True` is only set on steps where the EV is not the `zero_exchange` enforcer.

### Exchange-shaping secondary term

Under a net-of-meter (NoM) tariff, import and export prices are symmetric. The `minimize_cost` objective naturally produces near-zero exchange as a consequence — there is no economic benefit to importing energy you could supply from storage. However, when prices are flat or very close to symmetric, the solver is indifferent among solutions with the same net cost but different gross exchange magnitudes. Floating-point degeneracy can cause it to choose a solution with unnecessary cycling.

The `objectives.exchange_shaping_weight` field adds an optional secondary term:

```
lambda * sum_t(import_t + export_t)
```

to the objective. The weight `lambda` must be orders of magnitude smaller than typical energy prices so it cannot reverse a dispatch decision that is economically justified. A value of `1e-4` EUR/kWh is appropriate for European retail tariff levels (0.20–0.35 EUR/kWh): the maximum influence on a 24-hour horizon with 10 kW continuous exchange is `1e-4 * 10 * 96 = 0.096 EUR`, which is well below typical dispatch profitability thresholds.

The term is applied in all three strategies (`minimize_cost`, `minimize_consumption`, `balanced`). In `minimize_consumption`, it is added only in phase 2 so it does not distort the phase-1 import minimisation.

The term is implemented in `mimirheim/core/objective.py` in the `_exchange_shaping_terms` helper method.

---

## 11. Concurrency model

**Decision: paho-mqtt network loop in a background thread; solver runs on the main thread; a single-item queue decouples them**

Three activities need to coexist:
1. paho-mqtt network I/O (continuous — receives retained messages, sends publishes)
2. Readiness state tracking (updates on every incoming message)
3. Solving (blocking, up to 59 s)

### Thread structure

```
Main thread:       solve loop — blocks on queue.get(), calls build_and_solve(), publishes result
Background thread: paho loop_start() — handles network I/O, fires on_message callbacks
```

paho's `loop_start()` spawns exactly one background thread. All `on_message` callbacks fire on that thread.

### Readiness state

A `ReadinessState` object holds the latest validated input for each expected topic (battery SOC, EV state, prices, etc.). It is updated by `on_message` callbacks and read by the main thread when assembling a `SolveBundle`.

Access must be protected by a `threading.Lock`. The lock is held only long enough to copy the current state — never during a solve:

```python
# on_message callback — data topic (background thread)
with state_lock:
    readiness_state.update(topic, validated_inputs)
    # data topics do not queue a solve; only the trigger topic does

# on_message callback — trigger topic (background thread)
with state_lock:
    if readiness_state.is_ready():
        solve_queue.put_nowait(readiness_state.snapshot())  # SolveBundle
    else:
        logger.warning("Trigger received but inputs not ready; solve skipped")

# main thread
bundle = solve_queue.get()   # blocks until trigger fires and inputs are ready
result = build_and_solve(bundle, config)
publisher.publish(result)
```

`solve_queue` is a `queue.Queue(maxsize=1)`. If a new bundle arrives while a solve is already running, `put_nowait` raises `queue.Full` — the new bundle is discarded and a DEBUG log is emitted. This prevents a backlog of stale solves queuing up. The next completed solve cycle will pick up the freshest state.

### No concurrent solves

Only one solve runs at a time. There is no thread pool for solving. The 59 s time limit exists precisely to bound how stale the next solve cycle can be. Running overlapping solves would share no benefit and would race on the solver instance.

### `build_and_solve()` is thread-safe

`build_and_solve()` receives a `SolveBundle` snapshot and a `MimirheimConfig` that is immutable after load. It creates a fresh `ModelContext` with a new solver instance on every call. It has no shared mutable state and is safe to call from any thread — though in practice it is always called from the main thread.

---

## 12. Fault resilience

### Infeasible solve

If `build_and_solve()` returns `solve_status: "infeasible"`, mimirheim must **not** publish a new schedule. The previous retained topics remain on the broker unchanged, so downstream consumers continue operating on the last good schedule. Log at ERROR level with the bundle timestamp so the operator can correlate with debug dumps.

Infeasibility in practice almost always means a config error (import limit too low to cover static load, EV energy requirement exceeds available window) rather than a transient condition. It should not be silently retried.

### Solver exception

Any exception raised inside `build_and_solve()` is caught in the main thread's solve loop, logged at ERROR with a full traceback, and the loop continues waiting for the next bundle. The retained MQTT schedule is not touched. The process does not exit.

### Stale inputs

Sensor inputs (battery SOC, EV state) use a presence-only readiness model. mimirheim checks only that a value has been received at least once since startup; there is no configurable staleness window. The most recently retained message on the broker is the authoritative current value.

Retain is required on all sensor topics. This ensures mimirheim receives the last known value immediately on (re)connect and is never blocked waiting for the next state change after a restart.

### MQTT disconnection

paho's `loop_start()` reconnects automatically with exponential backoff. Retained topics on the broker are preserved across disconnections — downstream consumers see no interruption. mimirheim will republish the latest schedule on reconnect via paho's `on_connect` callback, which should call `publisher.republish_last_result()` if a prior result exists. This ensures the retained topics are current even if the broker restarted and lost its retained state.

### Config load failure

A config validation error at startup (Pydantic raises on `load_config()`) must exit the process immediately with a non-zero code and a clear error message. There is no fallback config and no partial startup. This is intentional — a misconfigured mimirheim that silently starts is worse than one that refuses to start.

### `mimir/status/last_solve` topic

After every solve attempt — successful or not — the main thread publishes a **retained** status message to `mimir/status/last_solve` (configurable). This is published even when the schedule is not updated (infeasible, exception, stale inputs).

Publishing this message is the last action in the solve loop iteration, after `debug_dump()` and after any schedule publish. If publishing the status itself fails (broker disconnect), the error is logged but does not affect the solve loop.

The `detail` field on error payloads must be informative enough for an operator to act without reading logs, but must not include raw exception tracebacks (those go to the log only). A one-sentence diagnosis is sufficient.

---

## 13. Development environment and dependency management

**Decision: uv with a committed lockfile**

mimirheim uses [uv](https://github.com/astral-sh/uv) for environment and dependency management. `pyproject.toml` declares all dependencies and their version constraints. `uv.lock` is committed to the repository and guarantees reproducible installs on all machines and in CI.

### Rationale

`requirements.txt` has no dependency resolution metadata and poor lockfile semantics. `pip-tools` improves on this but is a workaround for a weak foundation. uv supersedes both:

- Written in Rust — environment creation and dependency resolution are significantly faster than pip.
- `uv sync` creates `.venv` in the project root and installs all dependencies from the lockfile in one command. No manual venv creation step.
- Dependency groups (`dev-dependencies`) separate production and development dependencies cleanly.
- `uv run` executes commands inside the managed environment without requiring explicit activation.
- `uv.lock` is cross-platform and safe to commit. It is the source of truth for the resolved dependency graph.

### Common commands

```bash
# First-time setup — creates .venv and installs all dependencies
uv sync

# Add a production dependency (updates pyproject.toml and uv.lock)
uv add highspy

# Add a development-only dependency
uv add --dev pytest amqtt pytest-asyncio

# Run the test suite
uv run pytest

# Run the application
uv run python -m mimirheim --config config.yaml
```

### Dependency groups

`pyproject.toml` separates runtime and development dependencies:

```toml
[project]
dependencies = [
    "highspy>=1.7",
    "pydantic>=2.7",
    "paho-mqtt>=2.0",
    "pyyaml>=6.0",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "amqtt>=0.11",
    "pytest-asyncio>=0.23",
]
```

The `dev-dependencies` group is installed by `uv sync` by default in a development checkout. In a production deployment, `uv sync --no-dev` installs only the runtime dependencies.

### `.venv` management

uv creates `.venv` in the project root. This directory is listed in `.gitignore` and must never be committed. The lockfile (`uv.lock`) is committed and must be kept up to date — run `uv lock` after any manual edit to `pyproject.toml` dependency constraints.

---

## 14. MQTT topic naming convention and auto-derivation

### Motivation

All MQTT topics used by mimirheim follow a predictable naming convention derived
from `mqtt.topic_prefix`. Before Plans 49 and 50, operators had to transcribe
these topics explicitly into every device entry in the YAML. Changing the
prefix therefore required editing dozens of topic strings by hand and risked
missing one.

After Plans 49 and 50, all topic fields default to `None` in the schema.
`MimirheimConfig._derive_global_topics` and `MimirheimConfig._derive_device_topics` run
as `model_validator(mode="after")` validators during config load and fill in
`None` fields using the convention in the tables below. Explicit values supplied
in the YAML are preserved; only `None` fields are filled in.

This means the `outputs:` section of the YAML, previously required, is now
entirely optional. An operator who uses only the defaults can omit every MQTT
topic string from their config file.

### Derivation mechanics

The two derivation validators run in definition order on `MimirheimConfig`:

1. `_derive_global_topics` — fills the six system-level topics.
2. `_derive_device_topics` — fills all per-device topics by iterating each
   named device map.

Both validators mutate the model in place. This is safe because mimirheim's Pydantic
models are not frozen (`frozen=True` is not set in any `model_config`). Downstream
code (the MQTT client, publisher, and readiness tracker) reads topic strings from
the validated config and always receives a resolved non-`None` value — no `or`
fallback is needed at any read site.

Device output topics (e.g. `exchange_mode`, `loadbalance_cmd`) are derived for
all devices regardless of whether the corresponding capability is enabled. A
disabled capability means the topic is present in the config but never published.
This is intentional: the topic path is stable so an operator can subscribe to it
before enabling the capability without reconfiguring broker subscriptions.

### Global topic naming convention

The six global topics are derived from `mqtt.topic_prefix` (`p`):

| Config field | Derived topic (prefix = `mimirheim`) |
|---|---|
| `outputs.schedule` | `mimir/strategy/schedule` |
| `outputs.current` | `mimir/strategy/current` |
| `outputs.last_solve` | `mimir/status/last_solve` |
| `outputs.availability` | `mimir/status/availability` |
| `inputs.prices` | `mimir/input/prices` |
| `reporting.notify_topic` | `mimir/status/dump_available` |

Two further topics are not configurable and are always constructed directly from
the prefix in the IO layer:

| Purpose | Always derived as |
|---|---|
| Strategy selection input | `{prefix}/input/strategy` |
| Solve trigger input | `{prefix}/input/trigger` |

### Device-level topic naming convention

All device topics follow `{p}/{direction}/{device-type}/{name}/{field}`. Input
topics use `{p}/input/...`; output topics use `{p}/output/...`.

#### Input topics

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

#### Output topics

| Config field | Derived topic |
|---|---|
| `batteries.{name}.outputs.exchange_mode` | `{p}/output/battery/{name}/exchange_mode` |
| `ev_chargers.{name}.outputs.exchange_mode` | `{p}/output/ev/{name}/exchange_mode` |
| `ev_chargers.{name}.outputs.loadbalance_cmd` | `{p}/output/ev/{name}/loadbalance` |
| `pv_arrays.{name}.outputs.power_limit_kw` | `{p}/output/pv/{name}/power_limit_kw` |
| `pv_arrays.{name}.outputs.zero_export_mode` | `{p}/output/pv/{name}/zero_export_mode` |
| `pv_arrays.{name}.outputs.on_off_mode` | `{p}/output/pv/{name}/on_off_mode` |
| `deferrable_loads.{name}.topic_recommended_start_time` | `{p}/output/deferrable/{name}/recommended_start` |

### Overriding a derived topic

Set the field explicitly in the YAML to override the derived value. The
derivation validator only fills in `None` fields; any non-`None` value is left
unchanged.

```yaml
# Override the prices topic when sharing a broker across multiple mimirheim instances:
inputs:
  prices: "shared/input/prices"

# Override a battery SOC topic to read from a Home Assistant sensor directly:
batteries:
  battery_main:
    capacity_kwh: 5.4
    inputs:
      soc:
        topic: "homeassistant/sensor/battery_soc/state"
        unit: percent
```

### Minimal configuration pattern

With all topics derived, a device entry only needs its physical parameters. No
MQTT topic needs to appear in the YAML for a standard single-broker deployment:

```yaml
mqtt:
  host: localhost
  topic_prefix: mimir

grid:
  import_limit_kw: 17.0
  export_limit_kw: 17.0

batteries:
  battery_main:
    capacity_kwh: 5.4
    inputs:
      soc:
        unit: percent            # topic derived to mimir/input/battery/battery_main/soc

pv_arrays:
  roof_pv:
    max_power_kw: 4.5           # topic derived to mimir/input/pv/roof_pv/forecast

static_loads:
  base_load: {}                 # topic derived to mimir/input/baseload/base_load/forecast
```
