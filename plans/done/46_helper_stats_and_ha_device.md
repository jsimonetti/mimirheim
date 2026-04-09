# Plan 46 — Helper run statistics and extended HA discovery

## Motivation

Every `HelperDaemon` subclass runs a fetch-and-publish cycle whenever a trigger
arrives, but currently publishes nothing about its own execution. Operators have
no way to determine from the broker alone:

- When the tool last ran successfully.
- How long the run took.
- How many hours of data were produced.

Adding a statistics topic per helper makes tooling health observable without
opening log files or container stdout. Publishing the statistics as MQTT also
makes them directly consumable as Home Assistant sensors without additional
middleware.

This plan also extends `helper_common`'s HA discovery to register the new stats
sensors under the same HA device as the existing trigger button, so operators see
all per-tool information in one HA device card. Each helper remains responsible
for publishing and cleaning up its own discovery payloads.

---

## Critical design decisions

### `CycleResult` replaces `datetime | None` as the return type of `_run_cycle()`

`HelperDaemon._run_cycle()` currently returns either `None` (success) or a
`datetime` (rate-limit suppression: ignore all triggers until this UTC time).

Adding per-cycle metadata requires a richer return value. The cleanest path is a
new `CycleResult` dataclass:

```python
@dataclass
class CycleResult:
    success: bool = True
    suppress_until: datetime | None = None
    horizon_hours: float | None = None
    exit_code: int | None = None
    exit_message: str | None = None
```

`suppress_until` replaces the raw `datetime` return. `horizon_hours` is the
number of hours of data the cycle produced (e.g. 24 for a nordpool run that
fetched today + tomorrow). `exit_code` and `exit_message` are reserved for
future use; all existing helpers return `None` for both fields.

`success` is set by the daemon's except branch, not by the helper itself.
When `_run_cycle()` raises, the daemon constructs a synthetic
`CycleResult(success=False)` before publishing stats. This makes failure
explicit in the stats payload rather than requiring consumers to infer it
from null `horizon_hours` — a helper may return `horizon_hours=None` on
success when it simply does not track that value.

This is a **breaking change** to the `_run_cycle()` abstract method contract.
Every concrete subclass must be migrated in this plan. The five subclasses that
need updating are listed in the implementation section.

Do not add a compatibility shim. The helpers are all internal; a clean break
now prevents a permanently bifurcated API.

### `stats_topic` is a standalone config field, independent of HA discovery

Stats are useful regardless of whether HA is involved (e.g. for monitoring via
any MQTT subscriber). The `stats_topic` field is therefore a top-level optional
field in each helper's Pydantic config model, not nested under `ha_discovery:`.

When `stats_topic` is `None` (the default), stats are logged at DEBUG level but
not published to MQTT. Existing deployments that do not add `stats_topic:` to
their YAML files are unaffected.

HA discovery for the stats sensors is only published when **both** `stats_topic`
is non-None **and** `ha_discovery.enabled` is True.

### Timing and stats are collected by `HelperDaemon`, not by the helper

`HelperDaemon._on_message()` already wraps `_run_cycle()` in a try/except. The
timing and stats publication happen in that same wrapper. The helper sets
`horizon_hours` in the returned `CycleResult` if it knows it; otherwise the
field is `None` and the published sensor shows `unknown` in HA.

`exit_code` and `exit_message` are always `null` in the initial implementation.
The fields exist in `CycleResult` and in the published JSON for forward
compatibility; individual helpers can populate them in a future plan.

### Stale discovery cleanup is `HelperDaemon`'s responsibility

Currently `publish_trigger_discovery()` publishes one button entity without any
cleanup of previous discovery topics. After this plan, helpers publish a button
plus up to four sensor entities. If a future config change removes `stats_topic`,
the orphaned sensor topics must be erased from the broker.

Because the full set of topics a given `tool_name` could ever produce is
statically known — one button plus at most four sensors — cleanup does not
require querying the broker. On every connect, `HelperDaemon` calls
`_refresh_discovery()` which:

1. Computes `active`: the topics that should exist given the current config.
2. Computes `possible`: every topic this `tool_name` could ever produce
   (the union of the button plus all four stats sensor slots).
3. For every topic in `possible − active`: publishes an empty retained payload
   to delete it from the broker. Publishing empty to a topic with no retained
   message is a broker no-op, so this is unconditionally safe.
4. For every topic in `active`: publishes the discovery payload.

This uses only the already-connected main paho client. There is no secondary
connection, no `time.sleep`, and no timing window where the broker could
drop the subscription before all retained messages arrive.

---

## Stats payload

Published retained, QoS 1, to `stats_topic` after every cycle (success or
unhandled-exception):

```json
{
  "ts": "2026-04-02T14:00:00Z",
  "success": true,
  "duration_s": 1.23,
  "horizon_hours": 24,
  "exit_code": null,
  "exit_message": null
}
```

`ts` is the UTC ISO-8601 timestamp at which the cycle **started** (not finished).
`duration_s` is the wall-clock seconds from start to finish, rounded to two
decimal places. `success` is `false` when `_run_cycle()` raised an unhandled
exception; `true` otherwise. `horizon_hours` is null when the helper does not
return it. A `horizon_hours` of null does not imply failure; the `success` flag
is the authoritative signal.

---

## Relevant source locations

```
mimirheim_helpers/common/helper_common/
    cycle.py            — new: CycleResult dataclass
    daemon.py           — modify: wrap _run_cycle, call cleanup, publish stats
    discovery.py        — modify: add publish_stats_discovery(); extend
                          publish_trigger_discovery() to also publish stats sensors;
                          add _expected_helper_discovery_topics()

mimirheim_helpers/baseload/static/baseload_static/__main__.py
mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/__main__.py
mimirheim_helpers/baseload/homeassistant/baseload_ha/__main__.py
mimirheim_helpers/prices/nordpool/nordpool/__main__.py
mimirheim_helpers/pv/forecast.solar/pv_fetcher/__main__.py
    — all five: update _run_cycle() return type annotation and add
      stats_topic field to their Pydantic config models

mimirheim_helpers/common/tests/unit/
    test_daemon.py          — add new test cases (expand existing file)
    test_discovery.py       — new: unit tests for stats discovery helpers
```

---

## Tests first

Write all tests before touching any implementation code. Run
`cd mimirheim_helpers/common && uv run pytest` — all new tests must fail before
implementation begins.

### `mimirheim_helpers/common/tests/unit/test_daemon.py` — new test cases

Add to the existing test file (do not replace it):

```python
# test_stats_are_published_after_cycle
def test_stats_are_published_after_successful_cycle() -> None:
    """After a successful _run_cycle, HelperDaemon publishes a stats JSON
    payload to stats_topic containing ts, success, duration_s, horizon_hours,
    exit_code, and exit_message."""
    # Concrete config with stats_topic set.
    # Mock client.publish() and assert it is called with stats_topic.
    # Verify payload is valid JSON with the expected keys.

# test_stats_published_even_when_cycle_raises
def test_stats_published_after_unhandled_exception() -> None:
    """If _run_cycle raises, stats are still published with success=False.
    duration_s is recorded, horizon_hours is null, exit_code is null for now."""

# test_stats_not_published_when_stats_topic_is_none
def test_stats_not_published_when_stats_topic_is_none() -> None:
    """When config.stats_topic is None, no publish call is made for stats."""

# test_cycle_result_suppress_until_honoured
def test_cycle_result_suppress_until_is_stored() -> None:
    """When _run_cycle returns CycleResult(suppress_until=<datetime>), the
    daemon stores it and subsequent triggers are rate-limited exactly as
    before."""

# test_cycle_result_none_clears_ratelimit
def test_cycle_result_none_clears_ratelimit() -> None:
    """Returning CycleResult() (no suppress_until) clears any previous
    rate-limit, identical to the old behaviour of returning None."""

# test_horizon_hours_propagated_to_stats
def test_horizon_hours_propagated_from_cycle_result() -> None:
    """When _run_cycle returns CycleResult(horizon_hours=24.0), the stats
    payload's horizon_hours field is 24.0."""
```

### `mimirheim_helpers/common/tests/unit/test_discovery.py` — new file

```python
# test_publish_trigger_only_when_no_stats_topic
def test_publish_trigger_discovery_publishes_only_button_when_stats_topic_none() -> None:
    """When stats_topic is None, _refresh_discovery emits exactly one
    publish call (the button entity, no deletions because possible == active)."""

# test_publish_stats_discovery_publishes_four_sensors
def test_refresh_discovery_publishes_sensor_entities_when_stats_topic_set() -> None:
    """When stats_topic is not None, five publish calls are made: one button
    and four sensor entities (last_run_ts, duration_s, horizon_hours,
    exit_message)."""

# test_stale_sensors_deleted_when_stats_topic_removed
def test_refresh_discovery_deletes_stale_stats_sensors() -> None:
    """When stats_topic is None (e.g. removed from config), _refresh_discovery
    publishes empty retained payloads to all four sensor topics so they are
    deleted from the broker and removed from HA."""

# test_possible_set_matches_publish_calls
def test_all_possible_discovery_topics_matches_publish_and_delete_targets() -> None:
    """_all_possible_helper_discovery_topics() returns exactly the union of
    topics that _refresh_discovery() would either publish or delete. This
    enforces that the two stay in sync."""

# test_stats_sensors_share_device_block_with_button
def test_stats_sensors_share_device_block_with_button() -> None:
    """All published discovery payloads (button + sensors) reference the same
    device identifiers block."""

# test_all_discovery_payloads_retained_qos1
def test_all_helper_discovery_payloads_are_retained_qos1() -> None:
    """Every helper discovery publish uses qos=1 and retain=True."""
```

Run `cd mimirheim_helpers/common && uv run pytest` and confirm all new tests fail.

---

## Implementation

### Step 1 — `helper_common/cycle.py` (new file)

```python
"""CycleResult — return value for HelperDaemon._run_cycle().

A single dataclass that replaces the previous datetime | None return type.
``suppress_until`` preserves the rate-limit suppression semantics. The
remaining fields carry per-cycle execution metadata published to stats_topic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CycleResult:
    """Return value for HelperDaemon._run_cycle().

    Attributes:
        success: True when the cycle completed without raising an exception.
            The daemon sets this to False in the except branch and still
            publishes stats so operators can see the failure timestamp and
            duration. Helpers do not set this field directly.
        suppress_until: When set, the daemon discards all trigger messages
            until this UTC datetime has passed. Used by helpers that contact
            external APIs with rate-limit responses (e.g. forecast.solar
            returning HTTP 429).
        horizon_hours: Number of hours of data produced by this cycle.
            For example, a nordpool run that publishes today and tomorrow's
            prices sets this to 48. None if the helper does not track this.
        exit_code: Reserved for future use. Always None in this plan.
        exit_message: Reserved for future use. Always None in this plan.
    """

    success: bool = True
    suppress_until: datetime | None = None
    horizon_hours: float | None = None
    exit_code: int | None = None
    exit_message: str | None = None
```

### Step 2 — `helper_common/discovery.py` (extend)

Add two pure functions and update `publish_trigger_discovery()`:

```python
_STATS_SENSOR_IDS = (
    "last_run_ts",
    "duration_s",
    "horizon_hours",
    "exit_message",
)


def _all_possible_helper_discovery_topics(
    *,
    tool_name: str,
    discovery_prefix: str = "homeassistant",
) -> set[str]:
    """Return every HA discovery topic this tool could ever publish.

    This is the full static set: one button plus one sensor per stats field.
    It is independent of the current config; it represents every topic this
    ``tool_name`` could occupy on the broker under any configuration.

    Used by ``_refresh_discovery()`` to compute the deletion set:
    ``_all_possible_... - active`` gives every topic that may be stale and
    should be erased.

    Must be kept in sync with the publish calls in
    ``publish_trigger_discovery()``. ``test_all_possible_discovery_topics_
    matches_publish_and_delete_targets()`` enforces this.

    Args:
        tool_name: Stable snake_case identifier for the tool.
        discovery_prefix: HA MQTT discovery topic prefix.

    Returns:
        Set of all MQTT topic strings this tool name could ever produce.
    """
    topics = {f"{discovery_prefix}/button/{tool_name}/config"}
    for sensor_id in _STATS_SENSOR_IDS:
        topics.add(f"{discovery_prefix}/sensor/{tool_name}_{sensor_id}/config")
    return topics


def _active_helper_discovery_topics(
    *,
    tool_name: str,
    stats_topic: str | None,
    discovery_prefix: str = "homeassistant",
) -> set[str]:
    """Return the HA discovery topics that should exist given the current config.

    Args:
        tool_name: Stable snake_case identifier for the tool.
        stats_topic: When not None, stats sensor topics are included.
        discovery_prefix: HA MQTT discovery topic prefix.

    Returns:
        Set of MQTT topic strings that should be retained on the broker.
    """
    topics = {f"{discovery_prefix}/button/{tool_name}/config"}
    if stats_topic is not None:
        for sensor_id in _STATS_SENSOR_IDS:
            topics.add(f"{discovery_prefix}/sensor/{tool_name}_{sensor_id}/config")
    return topics


def publish_trigger_discovery(
    client: Any,
    *,
    tool_name: str,
    tool_label: str,
    trigger_topic: str,
    stats_topic: str | None = None,
    discovery_prefix: str = "homeassistant",
) -> None:
    """Refresh HA MQTT discovery for this helper tool.

    Unconditionally deletes every topic in the full possible set that is not
    in the active set, then publishes the active set. Uses only the supplied
    client; no secondary connection or sleep is required.

    Deletion is idempotent: publishing an empty retained payload to a topic
    with no retained message is a broker no-op.

    Args:
        client: A connected paho-mqtt Client instance.
        tool_name: Stable snake_case identifier. Used as object_id prefix and
            device identifier.
        tool_label: Human-readable display name (e.g. "Nordpool Prices").
        trigger_topic: MQTT topic that triggers the helper.
        stats_topic: MQTT topic where the helper publishes stats JSON.
            When None, only the button entity is published and any previously
            published sensor topics are deleted.
        discovery_prefix: HA MQTT discovery topic prefix.
    """
    possible = _all_possible_helper_discovery_topics(
        tool_name=tool_name, discovery_prefix=discovery_prefix
    )
    active = _active_helper_discovery_topics(
        tool_name=tool_name,
        stats_topic=stats_topic,
        discovery_prefix=discovery_prefix,
    )
    for stale_topic in sorted(possible - active):
        client.publish(stale_topic, payload=None, qos=1, retain=True)

    device_block = {
        "identifiers": [tool_name],
        "name": tool_label,
        "manufacturer": "Mimirheim",
    }

    # --- Trigger button ---
    client.publish(
        f"{discovery_prefix}/button/{tool_name}/config",
        json.dumps({
            "name": f"{tool_label} Trigger",
            "unique_id": f"{tool_name}_trigger",
            "command_topic": trigger_topic,
            "payload_press": "",
            "retain": False,
            "device": device_block,
        }),
        qos=1,
        retain=True,
    )

    # --- Stats sensors (only when stats_topic is set) ---
    if stats_topic is not None:
        _STATS_SENSORS = [
            (f"{tool_name}_last_run_ts", "Last Run", "{{ value_json.ts }}", None, None),
            (f"{tool_name}_duration_s", "Last Run Duration", "{{ value_json.duration_s | round(2) }}", "s", None),
            (f"{tool_name}_horizon_hours", "Horizon", "{{ value_json.horizon_hours }}", "h", None),
            (f"{tool_name}_exit_message", "Exit Message", "{{ value_json.exit_message }}", None, None),
        ]
        for sensor_id, name, template, unit, device_class in _STATS_SENSORS:
            payload: dict[str, Any] = {
                "name": name,
                "unique_id": sensor_id,
                "state_topic": stats_topic,
                "value_template": template,
                "entity_category": "diagnostic",
                "device": device_block,
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            client.publish(
                f"{discovery_prefix}/sensor/{sensor_id}/config",
                json.dumps(payload),
                qos=1,
                retain=True,
            )
```

### Step 3 — `helper_common/daemon.py` (modify)

1. Import `CycleResult` from `helper_common.cycle`.
2. Change `_run_cycle()` abstract method signature to `-> CycleResult | None`.
3. Update `_publish_discovery()` to pass `stats_topic` to `publish_trigger_discovery()`.
   `publish_trigger_discovery()` now handles deletion of stale topics internally;
   no separate cleanup step is needed on the daemon side.
4. Update `_on_connect()` to call `_publish_discovery()` directly (no pre-cleanup call).
5. Update `_on_message()` to:
   - Record wall-clock start time before calling `_run_cycle()`.
   - Record end time in both the normal return and the except branch.
   - On exception: construct `CycleResult(success=False)` for stats publication.
   - Extract `suppress_until` from the `CycleResult` (replaces the raw `if result is not None`).
   - Call `_publish_stats()` if `stats_topic` is configured.

```python
def _publish_stats(
    self,
    start_ts: datetime,
    duration_s: float,
    result: CycleResult,
) -> None:
    """Publish a stats JSON payload to stats_topic if configured.

    The caller is responsible for constructing ``result``. On a normal
    return, this is the ``CycleResult`` from ``_run_cycle()`` (or a default
    ``CycleResult()`` when the helper returned None). On an unhandled
    exception, the caller constructs ``CycleResult(success=False)`` before
    calling this method, so stats are always published regardless of outcome.

    Args:
        start_ts: UTC datetime at which _run_cycle started.
        duration_s: Wall-clock seconds from start to finish.
        result: CycleResult describing this cycle. success=False signals that
            _run_cycle raised an unhandled exception.
    """
    stats_topic = getattr(self._config, "stats_topic", None)
    if stats_topic is None:
        return
    payload = {
        "ts": start_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "success": result.success,
        "duration_s": round(duration_s, 2),
        "horizon_hours": result.horizon_hours,
        "exit_code": result.exit_code,
        "exit_message": result.exit_message,
    }
    self._client.publish(stats_topic, json.dumps(payload), qos=1, retain=True)
```

### Step 4 — Migrate all five concrete helpers

For each helper, two changes are required:

1. Add `stats_topic: str | None = Field(default=None, ...)` to the root Pydantic
   config model.
2. Change `_run_cycle()` return annotation from `datetime | None` or `None` to
   `CycleResult | None`, and update return statements:
   - `return None` → stays `return None` (None is still valid).
   - `return some_datetime` → `return CycleResult(suppress_until=some_datetime)`.
   - Helpers that know their horizon (nordpool, pv_fetcher) should return
     `CycleResult(horizon_hours=N)` on success.

**Affected files:**

| File | Current return | Migration |
|------|---------------|-----------|
| `baseload_static/__main__.py` | `-> None` | annotation update only |
| `baseload_ha_db/__main__.py` | `-> None` | annotation update only |
| `baseload_ha/__main__.py` | `-> None` | annotation update only |
| `nordpool/__main__.py` | `-> None` | add `horizon_hours` to success return |
| `pv_fetcher/__main__.py` | `-> datetime \| None` | convert rate-limit return to `CycleResult(suppress_until=...)` |

For nordpool, the published horizon is `len(prices) / 4` steps × 15 min or
simply the number of price hours returned by the fetcher. Determine this from
the fetcher return value.

For pv_fetcher, the existing `return reset_at` becomes
`return CycleResult(suppress_until=reset_at)`. On success, return
`CycleResult(horizon_hours=N)` where N is the number of forecast hours.

---

## Config schema additions

Each helper's root Pydantic config model gets:

```python
stats_topic: str | None = Field(
    default=None,
    description=(
        "MQTT topic for publishing per-cycle statistics (last run time, "
        "duration, horizon length). When None, statistics are not published."
    ),
)
```

Example YAML addition (nordpool.yaml):

```yaml
stats_topic: mimir/input/tools/prices/stats
```

No existing YAML file needs to be updated for this plan to be mergeable; the
field defaults to None and existing configs continue working without change.

---

## Acceptance criteria

```bash
cd mimirheim_helpers/common && uv run pytest            # all new tests pass
cd mimirheim_helpers/prices/nordpool && uv run pytest   # no regressions
cd mimirheim_helpers/pv/forecast.solar && uv run pytest # no regressions
cd mimirheim_helpers/baseload/static && uv run pytest   # no regressions
```

Behavioural checks (manual or integration test):

1. nordpool daemon started with `stats_topic: mimir/input/tools/prices/stats`
   in config; trigger sent; `mimir/input/tools/prices/stats` receives a retained
   JSON payload with `ts`, `duration_s`, `horizon_hours` set to a non-null float.

2. HA running with discovery enabled; `homeassistant/sensor/nordpool_prices_duration_s/config`
   appears in the broker; HA shows a "nordpool prices" device card with a trigger
   button and four diagnostic sensors.

3. `stats_topic` removed from nordpool config; daemon restarted; the four stats
   sensor discovery topics are erased from the broker; HA removes the entities.

---
