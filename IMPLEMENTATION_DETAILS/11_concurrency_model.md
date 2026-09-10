# 11. Concurrency model

**Decision: paho-mqtt network loop in a background thread; solver runs on the main thread; a single-item queue decouples them**

Three activities need to coexist:
1. paho-mqtt network I/O (continuous — receives retained messages, sends publishes)
2. Readiness state tracking (updates on every incoming message)
3. Solving (blocking, up to `solver.time_limit_seconds`)

## Thread structure

```
Main thread:       solve loop — blocks on queue.get(), calls build_and_solve(), publishes result
Background thread: paho loop_start() — handles network I/O, fires on_message callbacks
```

paho's `loop_start()` spawns exactly one background thread. All `on_message` callbacks fire on that thread.

## Readiness state

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

## No concurrent solves

Only one solve runs at a time. There is no thread pool for solving. The `solver.time_limit_seconds` cap exists precisely to bound how stale the next solve cycle can be. Running overlapping solves would share no benefit and would race on the solver instance.

## `build_and_solve()` is thread-safe

`build_and_solve()` receives a `SolveBundle` snapshot and a `MimirheimConfig` that is immutable after load. It creates a fresh `ModelContext` with a new solver instance on every call. It has no shared mutable state and is safe to call from any thread — though in practice it is always called from the main thread.
