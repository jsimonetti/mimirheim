# Step 14 — ReadinessState

## References

- IMPLEMENTATION_DETAILS §9 (concurrency model, ReadinessState, threading.Lock)
- IMPLEMENTATION_DETAILS §10 (stale inputs, 5-minute staleness window)

---

## Files to create

- `mimirheim/core/readiness.py`
- `tests/unit/test_readiness.py`

---

## Tests first

Create `tests/unit/test_readiness.py`. Tests must fail before any implementation exists.

- `test_readiness_not_ready_initially` — construct with expected topics; `is_ready()` returns False
- `test_readiness_ready_when_all_topics_provided` — feed a fresh validated input for every expected topic; `is_ready()` returns True
- `test_readiness_not_ready_when_one_topic_missing` — provide all topics except one; `is_ready()` returns False
- `test_readiness_stale_topic_blocks_ready` — update a topic, then advance the clock past the 5-minute window (use `unittest.mock.patch` to freeze/advance `datetime.now`); `is_ready()` returns False after expiry
- `test_readiness_update_replaces_stale_with_fresh` — after a stale entry, feed a fresh update for the same topic; `is_ready()` returns True if all other topics are also fresh
- `test_readiness_snapshot_returns_solve_bundle` — when ready, `snapshot()` returns a `SolveBundle` with the current state; assert `isinstance(result, SolveBundle)` is True
- `test_readiness_strategy_defaults_to_minimize_cost` — construct `ReadinessState` without receiving any strategy MQTT message; call `snapshot()` when all required topics are ready; assert `bundle.strategy == "minimize_cost"`
- `test_readiness_strategy_updated_from_mqtt` — call `update(strategy_topic, "minimize_consumption")`; `snapshot().strategy == "minimize_consumption"`
- `test_readiness_is_thread_safe` — launch 10 threads calling `update` concurrently; assert no exceptions are raised (basic smoke test for lock correctness)

Run `uv run pytest tests/unit/test_readiness.py` — all tests must fail before proceeding.

---

## Implementation

`mimirheim/core/readiness.py` — tracks per-topic freshness and assembles `SolveBundle` when all inputs are available.

```python
class ReadinessState:
    def __init__(self, config: MimirheimConfig) -> None: ...

    def update(self, topic: str, validated_input: Any) -> None:
        """Record a fresh validated input for the given topic. Thread-safe."""

    def is_ready(self) -> bool:
        """Return True if all expected topics have a non-stale entry. Thread-safe."""

    def snapshot(self) -> SolveBundle:
        """Assemble and return a SolveBundle from current state. Raises if not ready."""
```

### Internal state

- `_lock: threading.Lock` — held briefly for reads and writes; never held during a solve
- `_entries: dict[str, tuple[Any, datetime]]` — maps topic → (validated_input, received_at)
- `_expected_topics: set[str]` — derived from `config` at construction (one entry per expected MQTT input)

### Strategy handling

The strategy topic (`config.mqtt.inputs.strategy`) is **not** in `_expected_topics`. Its absence never blocks a solve. Instead, `ReadinessState` holds a `_current_strategy: str = "minimize_cost"` attribute. When the strategy topic is received, `update()` sets this attribute. `snapshot()` includes `strategy=self._current_strategy` when assembling `SolveBundle`.

### Staleness

A topic is stale if `datetime.now(UTC) - received_at > timedelta(minutes=5)`. The 5-minute window is hardcoded in v1. Stale entries are not removed from `_entries` — they are checked at read time in `is_ready()`.

### snapshot()

Assembles a `SolveBundle` from all current entries. Must be called only when `is_ready()` is True. Raises `RuntimeError` if called when not ready.

The snapshot is a copy — the caller receives an immutable view of the state at the moment of the call. The lock is held only during the snapshot assembly, not while the solver runs.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_readiness.py
```

All tests green.

---

## Done

```bash
mv plans/14_readiness.md plans/done/
```
