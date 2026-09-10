# 4. Testing architecture

## Core requirement: `build_and_solve()` is a pure function

The solver entry point must have no side effects and no I/O dependencies:

```python
def build_and_solve(bundle: SolveBundle, config: MimirheimConfig) -> SolveResult: ...
```

`SolveBundle` carries all inputs (forecasts, device states, current time). `SolveResult` carries the full schedule and current strategy. Neither knows anything about MQTT, files, or time. This makes the solver directly instantiable in tests with no infrastructure.

The MQTT layer and the test harness are both just different ways to build a `SolveBundle` and consume a `SolveResult`.

---

## Test layers

### Layer 1 — Unit tests (no I/O, no broker)

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

### Layer 2 — Solver regression tests with golden files

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

### Layer 3 — MQTT integration tests (in-process broker)

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

## Golden file format

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

## Test directory layout

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

## CI matrix

| Test layer | Requires broker | Requires solver | Speed | Run on |
|---|---|---|---|---|
| Unit | No | Partial (LP only) | < 5 s | Every commit |
| Scenario / golden | No | Yes (full MILP) | 10–60 s | Every commit |
| Integration | amqtt (pip) | No | < 10 s | Every commit |

All three layers run on a plain `pip install -e .[dev]` with no system dependencies beyond Python ≥ 3.11.
