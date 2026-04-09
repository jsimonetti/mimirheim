# Step 15 — IO layer (input parser, MQTT publisher, MQTT client)

## References

- IMPLEMENTATION_DETAILS §7 (boundary rule: input models are in `mimirheim/core/bundle.py`; IO constructs them)
- IMPLEMENTATION_DETAILS §10 (MQTT reconnection, republish on connect)
- README.md (all MQTT topics, payload formats, QoS and retain flags)

---

## Files to create

- `mimirheim/io/input_parser.py`
- `mimirheim/io/mqtt_publisher.py`
- `mimirheim/io/mqtt_client.py`
- `tests/unit/test_input_parser.py`
- `tests/unit/test_mqtt_publisher.py`

---

## Tests first

### tests/unit/test_input_parser.py

- `test_parses_battery_soc_payload` — JSON `{"soc_kwh": 5.2, "timestamp": "<now ISO>"}` parses to `BatteryInputs`
- `test_rejects_stale_battery_payload` — `timestamp` more than 5 minutes ago raises `ValueError`
- `test_parses_price_list_payload` — JSON array of 96 floats parses to a list
- `test_parses_strategy_minimize_cost` — JSON `{"strategy": "minimize_cost"}` returns the string `"minimize_cost"`
- `test_parses_strategy_balanced` — JSON `{"strategy": "balanced"}` returns `"balanced"`
- `test_rejects_unknown_strategy` — `{"strategy": "go_wild"}` raises `ValueError`
- `test_rejects_malformed_json` — invalid JSON bytes raise `ValueError`, not an unhandled exception
- `test_rejects_battery_extra_field` — payload with unknown field raises `ValidationError` (extra="forbid")

### tests/unit/test_mqtt_publisher.py

Inject a mock paho client at construction (do not connect to a broker). Assert on `client.publish` calls.

- `test_publishes_schedule_topic` — correct topic string, `qos=1`, `retain=True`, payload is valid JSON
- `test_publishes_current_strategy_topic` — correct topic, `retain=True`
- `test_publishes_per_device_retained_topic` — one retained `publish` call per device in the schedule
- `test_publishes_last_solve_success` — `solve_status="optimal"`; last_solve topic payload contains `"status": "ok"`
- `test_publishes_last_solve_infeasible` — `solve_status="infeasible"`; last_solve payload contains `"status": "error"` and a non-empty `"detail"` string
- `test_republish_last_result_republishes_all_topics` — `republish_last_result()` calls `publish` for all topics with the same payloads as the previous `publish_result` call

Run `uv run pytest tests/unit/test_input_parser.py tests/unit/test_mqtt_publisher.py` — all tests must fail before proceeding.

---

## Implementation

### mimirheim/io/input_parser.py

Pure parsing functions — no network I/O, no state. Each function takes raw bytes or a string payload and returns a validated Pydantic model. Never swallows exceptions; raises `ValueError` on invalid JSON, `ValidationError` on schema violations.

```python
def parse_battery_inputs(payload: bytes | str) -> BatteryInputs: ...
def parse_ev_inputs(payload: bytes | str) -> EvInputs: ...
def parse_price_list(payload: bytes | str) -> list[float]: ...
def parse_strategy(payload: bytes | str) -> str:
    """Parse the mimir/input/strategy payload. Returns one of the three valid strategy strings."""
    ...
# ... one function per topic type
```

### mimirheim/io/mqtt_publisher.py

```python
class MqttPublisher:
    def __init__(self, client: Any, config: MimirheimConfig) -> None: ...

    def publish_result(self, result: SolveResult) -> None:
        """Publish SolveResult to all output topics. Stores result for republish."""

    def publish_last_solve_status(self, result: SolveResult | None, error: str | None) -> None:
        """Publish the retained status message to mimir/status/last_solve."""

    def republish_last_result(self) -> None:
        """Re-publish the last stored result. Called from on_connect."""
```

Topic strings come from `config.outputs`. All schedule and device topics use `qos=1, retain=True`. The `last_solve` topic uses `retain=True`. The `detail` field in error payloads must be a human-readable sentence, not a raw exception traceback.

### mimirheim/io/mqtt_client.py

Paho wrapper. Wires `on_message` → `input_parser` → `readiness_state.update`. Calls `publisher.republish_last_result()` from `on_connect`. Calls `client.loop_start()` in its `start()` method.

This module is deliberately thin. Business logic lives in `readiness.py`, `input_parser.py`, and `model_builder.py`. `mqtt_client.py` only dispatches; it does not interpret.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_input_parser.py tests/unit/test_mqtt_publisher.py
```

All tests green.

---

## Done

```bash
mv plans/15_io_layer.md plans/done/
```
