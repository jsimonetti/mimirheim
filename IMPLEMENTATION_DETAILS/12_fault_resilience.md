# 12. Fault resilience

## Infeasible solve

If `build_and_solve()` returns `solve_status: "infeasible"`, mimirheim must **not** publish a new schedule. The previous retained topics remain on the broker unchanged, so downstream consumers continue operating on the last good schedule. Log at ERROR level with the bundle timestamp so the operator can correlate with debug dumps.

Infeasibility in practice almost always means a config error (import limit too low to cover static load, EV energy requirement exceeds available window) rather than a transient condition. It should not be silently retried.

## Solver exception

Any exception raised inside `build_and_solve()` is caught in the main thread's solve loop, logged at ERROR with a full traceback, and the loop continues waiting for the next bundle. The retained MQTT schedule is not touched. The process does not exit.

## Stale inputs

Sensor inputs (battery SOC, EV state) use a presence-only readiness model. mimirheim checks only that a value has been received at least once since startup; there is no configurable staleness window. The most recently retained message on the broker is the authoritative current value.

Retain is required on all sensor topics. This ensures mimirheim receives the last known value immediately on (re)connect and is never blocked waiting for the next state change after a restart.

## MQTT disconnection

paho's `loop_start()` reconnects automatically with exponential backoff. Retained topics on the broker are preserved across disconnections — downstream consumers see no interruption. mimirheim will republish the latest schedule on reconnect via paho's `on_connect` callback, which should call `publisher.republish_last_result()` if a prior result exists. This ensures the retained topics are current even if the broker restarted and lost its retained state.

## Config load failure

A config validation error at startup (Pydantic raises on `load_config()`) must exit the process immediately with a non-zero code and a clear error message. There is no fallback config and no partial startup. This is intentional — a misconfigured mimirheim that silently starts is worse than one that refuses to start.

## `mimir/status/last_solve` topic

After every solve attempt — successful or not — the main thread publishes a **retained** status message to `mimir/status/last_solve` (configurable). This is published even when the schedule is not updated (infeasible, exception, stale inputs).

Publishing this message is the last action in the solve loop iteration, after `debug_dump()` and after any schedule publish. If publishing the status itself fails (broker disconnect), the error is logged but does not affect the solve loop.

The `detail` field on error payloads must be informative enough for an operator to act without reading logs, but must not include raw exception tracebacks (those go to the log only). A one-sentence diagnosis is sufficient.
