# Plan 40 — Rewrite scheduler to use APScheduler

## Motivation

The custom min-heap loop in `loop.py` has produced two separate timing bugs that
required non-trivial investigation and fixes:

1. An infinite spin when a 1-second tolerance window caused `next_fire(cron, now)`
   to return a time still within the window.
2. A premature fire when the initial heap was built with `now` a few hundred
   milliseconds before a cron boundary, so the first sleep was short enough that
   `now` after waking was still before the scheduled time — but within the
   (since-removed) tolerance.

Both bugs stem from writing a scheduler from scratch using low-level primitives.
APScheduler 3.x is a well-maintained library that handles sleep, drift, coalescing,
and missed-fire logic correctly. Replacing the custom loop with APScheduler eliminates
the class of bugs described above and reduces the amount of scheduling logic the
project must own and test.

---

## Relevant documentation

- APScheduler 3.x user guide: https://apscheduler.readthedocs.io/en/3.x/userguide.html
- `CronTrigger.from_crontab()` accepts standard five-field cron expressions.
- `BackgroundScheduler` runs in a background thread and returns control to the
  caller immediately after `start()` — the main thread can then block on
  `stop_event.wait()` and call `scheduler.shutdown()` afterwards.

---

## Decision: APScheduler version

Use `apscheduler>=3.10,<4.0` explicitly. Version 4 is an asyncio-first rewrite with an
entirely different API and is not appropriate for this synchronous, thread-based daemon.

---

## Decision: config format

No change to the YAML format. `CronTrigger.from_crontab()` accepts the same five-field
cron strings already used in `dev/scheduler.yaml` and `example.yaml`. The Pydantic model
structure in `config.py` is unchanged; only the validation back-end inside
`_validate_schedules` changes.

---

## Files to create or modify

| File | Change |
|------|--------|
| `pyproject.toml` | Remove `croniter>=3.0`. Add `apscheduler>=3.10,<4.0`. |
| `scheduler/config.py` | Replace `croniter.is_valid()` with `CronTrigger.from_crontab()` inside a try/except for validation. |
| `scheduler/loop.py` | Full rewrite. Remove `next_fire()` and `run_loop()`. Add `run()` using `BackgroundScheduler`. |
| `scheduler/__main__.py` | Update import from `run_loop` to `run`. No other change. |
| `tests/unit/test_config.py` | Behaviorally unchanged. Tests continue to assert the same acceptance and rejection behaviour; internal mechanics change because the validator now uses APScheduler. No rewrite required — all tests must continue to pass unmodified. |
| `tests/unit/test_loop.py` | Full rewrite. Clock-injection tests become irrelevant. Replace with job-registration and stop-behaviour tests described below. |

---

## New `scheduler/loop.py` design

```python
"""Schedule loop using APScheduler.

...module docstring...
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR, JobExecutionEvent

def run(
    client: Any,
    schedules: list[tuple[str, str]],
    stop_event: threading.Event,
    *,
    _scheduler: BackgroundScheduler | None = None,
) -> None:
    scheduler = _scheduler or BackgroundScheduler(timezone=utc)

    for i, (cron_expr, topic) in enumerate(schedules):
        scheduler.add_job(
            _publish,
            CronTrigger.from_crontab(cron_expr, timezone=utc),
            args=[client, topic],
            id=f"job_{i}",
            coalesce=True,       # if missed, fire once not many times
            max_instances=1,     # never overlap for the same job
        )

    def _on_executed(event: JobExecutionEvent) -> None:
        logger.info(
            "Triggered job %s (scheduled %s)",
            event.job_id,
            event.scheduled_run_time.isoformat(),
        )

    scheduler.add_listener(_on_executed, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.start()
    logger.info("Scheduler running with %d schedule entries.", len(schedules))

    stop_event.wait()
    scheduler.shutdown()
    logger.info("Scheduler loop stopped.")


def _publish(client: Any, topic: str) -> None:
    client.publish(topic, payload=b"", qos=0, retain=False)
```

The `_scheduler` injection point exists solely for tests. Production code always
passes `None`, which causes `run()` to create a default `BackgroundScheduler`.

---

## New tests in `tests/unit/test_loop.py`

The module doc-string must be updated to describe what the new tests cover.

### Tests to write (before implementing)

**1. `test_run_exits_when_stop_event_is_preset`**

Pre-set the stop_event before calling `run()`. Assert `client.publish` is never
called.  This verifies the loop does not fire any job during scheduler startup or
shutdown when `stop_event` is already set.

> Failure mode if implementation is wrong: assertion fails because the scheduler
> starts, fires a pending job, and then shuts down before stop_event is checked.
> (In practice unlikely because apscheduler computes `next_fire` from now, so
> no job is due immediately — but the test guards against this explicitly.)

**2. `test_run_registers_correct_number_of_jobs`**

Inject a `BackgroundScheduler` (not yet started). After `run()` returns, assert
`len(scheduler.get_jobs()) == len(schedules)`.

> Must use a pre-set stop_event so `run()` returns quickly.

**3. `test_run_registers_correct_cron_triggers`**

Inject a `BackgroundScheduler`. After `run()`, inspect each job's trigger.
Assert that `str(job.trigger)` or `job.trigger.fields` corresponds to the
cron expression supplied.

> The exact assertion format depends on how APScheduler exposes trigger
> fields. Use `CronTrigger.from_crontab(cron_expr).fields` as the reference
> and compare against the job's trigger.

**4. `test_run_does_not_register_jobs_for_empty_schedules`**

Empty `schedules` list — assert `scheduler.get_jobs()` is empty and the loop
exits cleanly.

> Config validation rejects empty schedules lists, but the loop itself should
> handle this gracefully regardless.

**5. `test_run_publishes_when_job_fires`**

This test verifies the actual publish path. Inject a `BackgroundScheduler` with
a pre-added `DateTrigger` job scheduled 200 ms in the future. The job calls
`client.publish("test/topic", payload=b"", qos=0, retain=False)` and then sets
`stop_event`. Pass an empty `schedules` list so `run()` does not add any
additional jobs. Assert `client.publish` was called exactly once with the correct
arguments.

This tests that `run()` correctly starts the scheduler, waits on `stop_event`,
and shuts down.

---

## APScheduler logging

APScheduler logs internally to the `apscheduler` logger at DEBUG level. The
`_on_executed` listener in `run()` logs fired jobs at INFO to `scheduler.loop`,
matching the existing log format as closely as possible.

Suppress `apscheduler`'s own INFO-level output in production by not changing
the root logger configuration. APScheduler's DEBUG output is only visible when
debug logging is explicitly enabled, which is appropriate.

---

## Acceptance criteria

All of the following must be true before moving this plan to `plans/done/`:

1. `uv run pytest` passes with no failures or regressions in any test across the
   whole workspace (`mimirheim` + all `mimirheim_helpers`).
2. The new `tests/unit/test_loop.py` contains at minimum tests 1–5 above, all
   green.
3. `tests/unit/test_config.py` passes unmodified (same assertions, different
   internal validator).
4. `croniter` is no longer listed in `pyproject.toml` or imported anywhere in
   `scheduler/`.
5. `loop.py` no longer contains a manual heap, sleep, or tolerance computation.
6. Running `uv run python -m scheduler --config dev/scheduler.yaml` connects to
   MQTT and logs `"Scheduler running with N schedule entries."` without error
   (manual smoke test — not automated).
7. `uv sync` succeeds and `uv.lock` is updated.

---

## TDD workflow

1. Write all five tests in `test_loop.py` first. Confirm they fail (most will fail
   with `ImportError` or because `run()` does not exist yet).
2. Update `pyproject.toml` and run `uv sync`.
3. Rewrite `loop.py`. Run `uv run pytest` — tests 1–4 should pass.
4. Implement test 5 and the publish path. All five should pass.
5. Update `config.py` validator. Run `uv run pytest` — `test_config.py` should
   still pass.
6. Update `__main__.py` import.
7. Run the full workspace test suite. Fix any regressions.
8. Move this file to `plans/done/`.
