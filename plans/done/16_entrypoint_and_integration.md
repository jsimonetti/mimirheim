# Step 16 — Entry point and integration tests

## References

- IMPLEMENTATION_DETAILS §9 (concurrency model: main thread solve loop, Queue(maxsize=1))
- IMPLEMENTATION_DETAILS §10 (fault resilience: exception handling, infeasible, stale inputs, config failure)
- IMPLEMENTATION_DETAILS §4 (amqtt integration tests, in-process broker fixture)

---

## Files to create

- `mimirheim/__main__.py`
- `tests/integration/test_mqtt_roundtrip.py`
- `tests/integration/test_readiness_mqtt.py`
- Update `tests/conftest.py` with the amqtt broker fixture

---

## Tests first

Write the integration tests before writing `__main__.py`. These tests are the acceptance criteria for the entry point.

### tests/conftest.py — amqtt broker fixture

```python
import pytest
from amqtt.broker import Broker

@pytest.fixture
async def mqtt_broker():
    config = {"listeners": {"default": {"type": "tcp", "bind": "127.0.0.1:11884"}}}
    broker = Broker(config)
    await broker.start()
    yield "mqtt://127.0.0.1:11884"
    await broker.stop()
```

Use port 11884 to avoid conflicting with any locally running broker.

### tests/integration/test_mqtt_roundtrip.py

These tests start the full application stack (except `__main__` itself — import and call the components directly).

- `test_full_stack_publishes_schedule` — publish price and battery SOC topics to the in-process broker; assert the schedule topic receives a retained message within 5 seconds
- `test_infeasible_solve_publishes_error_status` — configure impossible constraints (import_limit_kw=0 with a static load); assert `mimir/status/last_solve` contains `"status": "error"` and the schedule topic is not updated

### tests/integration/test_readiness_mqtt.py

- `test_retained_messages_trigger_solve_on_connect` — publish retained inputs to broker, then connect a new mimirheim instance; assert the solve is triggered without re-publishing (retained messages alone cause readiness)
- `test_stale_input_detected_after_expiry` — publish a battery input, advance past the staleness window, publish a fresh price; assert no solve is triggered (battery is stale)

Run `uv run pytest tests/integration/` — all tests must fail before `__main__.py` is written.

---

## Implementation

### mimirheim/__main__.py

```python
def main() -> None:
    # 1. Parse --config argument
    # 2. load_config() — exit(1) with clear message on ValidationError or FileNotFoundError
    # 3. Construct ReadinessState, MqttPublisher, MqttClient, solve_queue
    # 4. mqtt_client.start()  (calls loop_start internally)
    # 5. Register SIGTERM/SIGINT handlers for clean shutdown
    # 6. Solve loop:
    #    while running:
    #        bundle = solve_queue.get()
    #        try:
    #            result = build_and_solve(bundle, config)
    #            if result.solve_status != "infeasible":
    #                publisher.publish_result(result)
    #        except Exception:
    #            log ERROR with traceback; result = None
    #        publisher.publish_last_solve_status(result, error=...)
    #        _maybe_dump(bundle, result, config.debug.dump_dir, config.debug.max_dumps)

if __name__ == "__main__":
    main()
```

Key implementation points from IMPLEMENTATION_DETAILS §9 and §10:

- `solve_queue = queue.Queue(maxsize=1)` — the `on_message` callback calls `put_nowait`; discards bundles if a solve is in progress
- The lock is held only inside `ReadinessState.update` and `ReadinessState.snapshot`, never across a solve call
- `SIGTERM`/`SIGINT` set a `running` flag to False; the loop exits cleanly after the current solve completes
- Config load failure must print the Pydantic validation error in human-readable form and exit with code 1; it must not produce a Python traceback as the last visible output

---

## Acceptance criteria

```bash
uv run pytest tests/integration/
uv run pytest  # full suite — all three layers green
```

---

## Done

```bash
mv plans/16_entrypoint_and_integration.md plans/done/
```
