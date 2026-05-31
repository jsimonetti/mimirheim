# Plan 60 — HA discovery: shared device for multi-button helpers

## Purpose

`pv-ml-learner` calls `publish_trigger_discovery()` twice — once for the training
trigger and once for the inference trigger. Because `tool_name` is currently used
as both the button entity object_id **and** the HA device identifier, the two calls
produce two separate HA device cards ("PV ML Learner Train" and "PV ML Learner
Infer") instead of one unified card with two buttons.

This plan adds optional `device_id` and `device_label` parameters to
`publish_trigger_discovery()` so that multiple calls can share a single HA device
entry while keeping their entity namespaces independent. The pv-ml-learner
`_publish_discovery()` method is then updated to pass a shared `device_id`.

---

## Relevant IMPLEMENTATION_DETAILS sections

All changes are within `mimirheim_helpers/`. No core solver code is touched.

- §6 — module boundary rules.
- §10 — fault resilience (discovery failures must not crash the daemon).

---

## Scope

**In scope:**
- `helper_common/discovery.py` — add `device_id` and `device_label` keyword
  parameters to `publish_trigger_discovery()`; update the device block construction.
- `common/tests/unit/test_discovery.py` — new tests for the `device_id` path and
  the fallback behaviour.
- `pv/pv_ml_learner/pv_ml_learner/__main__.py` — pass a shared `device_id` in
  both `publish_trigger_discovery()` calls inside `_publish_discovery()`.
- `pv/pv_ml_learner/tests/unit/test_discovery.py` (new file) — unit tests for
  `PvLearnerDaemon._publish_discovery()` verifying that both buttons reference the
  same HA device identifier.

**Not in scope:**
- Any other caller of `publish_trigger_discovery()` (all other helpers use a
  single call; their behaviour is unchanged).
- `_all_possible_helper_discovery_topics()` and
  `_active_helper_discovery_topics()` — the topic namespaces are unchanged;
  these functions continue to operate on a single `tool_name` per call and do not
  need modification.
- The HA stats sensors published for pv-ml-learner (both `pv_ml_learner_train_*`
  and `pv_ml_learner_infer_*` sensor topics will appear under the shared device
  card automatically once the device block is corrected).
- Any UI or payload changes other than the device block.

---

## Decisions

### Option A — shared `device_id` parameter (chosen)

Add two optional keyword parameters to `publish_trigger_discovery()`:

```python
def publish_trigger_discovery(
    client: Any,
    *,
    tool_name: str,
    tool_label: str,
    trigger_topic: str,
    stats_topic: str | None = None,
    device_id: str | None = None,       # new
    device_label: str | None = None,    # new
    discovery_prefix: str = "homeassistant",
) -> None:
```

When `device_id` is not supplied, the device block is built from `tool_name` and
`tool_label` exactly as today — all existing callers are unaffected.

When `device_id` is supplied, the device block uses it as the HA `identifiers`
value and `device_label` (falling back to `tool_label` if `device_label` is None)
as the display name. The entity object_id and all topic names continue to use
`tool_name`, so cleanup logic and `_all_possible_helper_discovery_topics()` are
unchanged.

### Option B — accept a list of buttons in a single call (rejected)

This would merge both trigger topics into one call and remove the duplicate device
block entirely. It requires a breaking change to the `trigger_topic` parameter (now
a list), changes to `_all_possible_helper_discovery_topics()` (must accept a list
of `tool_name` values), and a more complex cleanup loop. The resulting API is
harder to read at call sites and provides no benefit over Option A for the single
test case where it would be used.

### Stats sensors under the merged device

After this change, HA will see eight stats sensor entities under the single
"PV ML Learner" device card: `pv_ml_learner_train_last_run_ts`, …,
`pv_ml_learner_infer_last_run_ts`, …. This is correct and more useful than the
current two-device layout — training and inference run stats are visible in one
place.

### Naming

The shared `device_id` for pv-ml-learner should be `"pv_ml_learner"` (stable,
matches the Python package name). The `device_label` should be taken from
`cfg.ha_discovery.device_name` (the existing user-configurable label), which is
already used today as the base label before the `" Train"` / `" Infer"` suffixes
are appended. With the shared device, the device name is just `device_name`
directly; `tool_label` for each button entity can be `"Train"` and `"Infer"`.

---

## Files to create or edit

```
mimirheim_helpers/
  common/
    helper_common/
      discovery.py               ← add device_id / device_label params; update device block
    tests/unit/
      test_discovery.py          ← new tests for device_id path + fallback
  pv/pv_ml_learner/
    pv_ml_learner/
      __main__.py                ← pass device_id="pv_ml_learner" in _publish_discovery()
    tests/unit/
      test_discovery.py          ← new file; tests for _publish_discovery() device grouping
```

---

## TDD workflow

### Step 1 — test_discovery.py: device_id parameter

**Before touching `discovery.py`**, add a new test class to
`mimirheim_helpers/common/tests/unit/test_discovery.py`:

```python
class TestDeviceId:
    def test_button_device_block_uses_tool_name_when_device_id_not_supplied(self) -> None:
        """When device_id is not supplied, device identifiers == [tool_name]."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
        )
        button_call = next(
            c for c in client.publish.call_args_list if "/button/" in c.args[0]
        )
        payload = json.loads(button_call.args[1])
        assert payload["device"]["identifiers"] == [_TOOL_NAME]
        assert payload["device"]["name"] == _TOOL_LABEL

    def test_button_device_block_uses_device_id_when_supplied(self) -> None:
        """When device_id is supplied, device identifiers == [device_id]."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            device_id="shared_device",
            device_label="Shared Device",
        )
        button_call = next(
            c for c in client.publish.call_args_list if "/button/" in c.args[0]
        )
        payload = json.loads(button_call.args[1])
        assert payload["device"]["identifiers"] == ["shared_device"]
        assert payload["device"]["name"] == "Shared Device"

    def test_device_label_falls_back_to_tool_label_when_not_supplied(self) -> None:
        """When device_id is supplied but device_label is not, name falls back
        to tool_label."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            device_id="shared_device",
        )
        button_call = next(
            c for c in client.publish.call_args_list if "/button/" in c.args[0]
        )
        payload = json.loads(button_call.args[1])
        assert payload["device"]["identifiers"] == ["shared_device"]
        assert payload["device"]["name"] == _TOOL_LABEL

    def test_two_calls_with_same_device_id_produce_same_device_block(self) -> None:
        """Two separate calls with the same device_id produce payloads whose
        device blocks are identical. This is what causes HA to group them under
        one device card."""
        call_a = _make_client()
        call_b = _make_client()

        publish_trigger_discovery(
            call_a,
            tool_name="pv_ml_learner_train",
            tool_label="Train",
            trigger_topic="mimir/tools/train/trigger",
            device_id="pv_ml_learner",
            device_label="PV ML Learner",
        )
        publish_trigger_discovery(
            call_b,
            tool_name="pv_ml_learner_infer",
            tool_label="Infer",
            trigger_topic="mimir/tools/infer/trigger",
            device_id="pv_ml_learner",
            device_label="PV ML Learner",
        )

        button_a = json.loads(next(
            c for c in call_a.publish.call_args_list if "/button/" in c.args[0]
        ).args[1])
        button_b = json.loads(next(
            c for c in call_b.publish.call_args_list if "/button/" in c.args[0]
        ).args[1])

        assert button_a["device"] == button_b["device"]

    def test_stats_sensor_device_block_uses_device_id_when_supplied(self) -> None:
        """Stats sensors published alongside a device_id button use the same
        device block."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
            device_id="shared_device",
            device_label="Shared Device",
        )
        for c in client.publish.call_args_list:
            raw = c.args[1]
            if raw is None:
                continue
            payload = json.loads(raw)
            if "device" in payload:
                assert payload["device"]["identifiers"] == ["shared_device"]

    def test_topic_names_are_still_based_on_tool_name_not_device_id(self) -> None:
        """The MQTT topic paths use tool_name, not device_id. Providing device_id
        does not alter the topic layout or the cleanup logic."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            device_id="shared_device",
        )
        touched_topics = {c.args[0] for c in client.publish.call_args_list}
        assert f"{_PREFIX}/button/{_TOOL_NAME}/config" in touched_topics
        assert f"{_PREFIX}/button/shared_device/config" not in touched_topics
```

Run `uv run pytest mimirheim_helpers/common/tests/unit/test_discovery.py` — all
new tests must fail with `TypeError` (unexpected keyword argument `device_id`).

### Step 2 — discovery.py: implement device_id parameter

Update `publish_trigger_discovery()` in `helper_common/discovery.py`:

1. Add `device_id: str | None = None` and `device_label: str | None = None` as
   keyword-only parameters after `discovery_prefix`.
2. Build the device block as:

   ```python
   effective_device_id = device_id if device_id is not None else tool_name
   effective_device_label = device_label if device_label is not None else tool_label
   device_block: dict[str, Any] = {
       "identifiers": [effective_device_id],
       "name": effective_device_label,
       "manufacturer": "Mimirheim",
   }
   ```

3. No other changes to the function body. The topic names, the cleanup loop, and
   the stats sensor payloads are all unchanged.

Run `uv run pytest mimirheim_helpers/common/tests/unit/test_discovery.py` — all
tests including the new `TestDeviceId` class must pass.

### Step 3 — pv_ml_learner/tests/unit/test_discovery.py: new test file

Create `mimirheim_helpers/pv/pv_ml_learner/tests/unit/test_discovery.py` with
tests for `PvLearnerDaemon._publish_discovery()`:

```python
"""Unit tests for PvLearnerDaemon._publish_discovery().

Verifies that the train and infer trigger buttons are grouped under the same
HA device when ha_discovery.enabled is True.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from pv_ml_learner.config import PvLearnerConfig


def _make_minimal_config(*, ha_enabled: bool = True) -> PvLearnerConfig:
    """Return a minimal PvLearnerConfig sufficient to call _publish_discovery()."""
    # Use model_validate to build the config from a dict so the test does not
    # depend on the PvLearnerConfig constructor signature directly.
    ...  # to be completed during implementation using the actual config schema


class TestPublishDiscovery:
    def test_train_and_infer_buttons_share_device_identifiers(self) -> None:
        """Both the train and infer button discovery payloads must reference the
        same HA device identifiers. This causes HA to show one device card with
        two buttons instead of two separate device cards."""
        ...  # to be completed during implementation

    def test_device_identifier_is_pv_ml_learner(self) -> None:
        """The shared device identifier is 'pv_ml_learner', independent of the
        configured device_name label."""
        ...

    def test_button_topics_are_distinct(self) -> None:
        """The two button entities have different MQTT topic paths, confirming
        they remain independent entities under the shared device."""
        ...

    def test_no_discovery_published_when_ha_disabled(self) -> None:
        """When ha_discovery.enabled is False, _publish_discovery() publishes
        nothing."""
        ...
```

These tests will fail until `_publish_discovery()` is updated in Step 4.

Note: `PvLearnerDaemon.__init__` requires a real SQLite path and a running MQTT
connection. Test `_publish_discovery()` by constructing a daemon with
`MagicMock` as the MQTT client (see the existing integration test in
`tests/integration/test_daemon.py` for how this is done) or by extracting the
discovery call into a testable helper function.

If direct daemon instantiation is too heavy for a unit test, test the
`_publish_discovery()` logic at the `publish_trigger_discovery()` call level
instead: patch `publish_trigger_discovery` and assert it was called twice with
the same `device_id` argument.

### Step 4 — pv_ml_learner/__main__.py: pass shared device_id

Update `_publish_discovery()` in `PvLearnerDaemon`:

```python
def _publish_discovery(self, client: mqtt.Client) -> None:
    cfg = self._config
    ha = cfg.ha_discovery
    if not ha.enabled:
        return
    prefix = ha.discovery_prefix
    label = ha.device_name
    publish_trigger_discovery(
        client,
        tool_name="pv_ml_learner_train",
        tool_label="Train",
        trigger_topic=cfg.training.train_trigger_topic,
        stats_topic=cfg.stats_topic,
        device_id="pv_ml_learner",
        device_label=label,
        discovery_prefix=prefix,
    )
    publish_trigger_discovery(
        client,
        tool_name="pv_ml_learner_infer",
        tool_label="Infer",
        trigger_topic=cfg.training.inference_trigger_topic,
        stats_topic=cfg.stats_topic,
        device_id="pv_ml_learner",
        device_label=label,
        discovery_prefix=prefix,
    )
    logger.debug("Published HA discovery for train and infer trigger buttons.")
```

### Step 5 — complete the unit tests in test_discovery.py

Fill in the `...` stubs from Step 3 now that the implementation exists. Use
`unittest.mock.patch` on `publish_trigger_discovery` to keep the test fast and
free of MQTT and SQLite dependencies:

```python
from unittest.mock import MagicMock, patch, call

class TestPublishDiscovery:
    def test_train_and_infer_buttons_share_device_identifiers(self) -> None:
        config = _make_minimal_config(ha_enabled=True)
        client = MagicMock()
        with patch(
            "pv_ml_learner.__main__.publish_trigger_discovery"
        ) as mock_pub:
            daemon = ...  # build daemon or call helper
            daemon._publish_discovery(client)

        assert mock_pub.call_count == 2
        call_kwargs = [c.kwargs for c in mock_pub.call_args_list]
        device_ids = [k["device_id"] for k in call_kwargs]
        assert device_ids[0] == device_ids[1] == "pv_ml_learner"
```

### Step 6 — run the full test suite

```bash
uv run pytest
```

All tests must pass. No regressions.

---

## Acceptance criteria

- [ ] `publish_trigger_discovery()` accepts `device_id: str | None = None` and
      `device_label: str | None = None` keyword parameters.
- [ ] When `device_id` is `None`, the device block is built from `tool_name` and
      `tool_label` exactly as before (no change for all existing callers).
- [ ] When `device_id` is supplied, the device block uses it as `identifiers` and
      `device_label` (falling back to `tool_label`) as the name. Entity topic paths
      continue to use `tool_name`.
- [ ] `_all_possible_helper_discovery_topics()` and
      `_active_helper_discovery_topics()` are unchanged.
- [ ] `PvLearnerDaemon._publish_discovery()` passes `device_id="pv_ml_learner"`
      and `device_label=cfg.ha_discovery.device_name` to both calls.
- [ ] A unit test confirms that both button payloads share the same `device.identifiers`
      value when `device_id` is supplied.
- [ ] All existing tests continue to pass.
- [ ] `uv run pytest` exits 0 with no new failures.
- [ ] Wiki updated (see Step 7).

### Step 7 — documentation updates

This plan makes no user-visible config or topic changes. The only observable
change is that HA shows one "PV ML Learner" device card with two buttons instead
of two separate device cards.

**`wiki/Helpers/PV-ML-Learner.md`**

Update the Home Assistant discovery section. Replace any description of two
separate HA device entries ("PV ML Learner Train" and "PV ML Learner Infer") with
a description of one device card containing two button entities:

```
### Home Assistant entities

When `ha_discovery.enabled` is `true`, pv-ml-learner registers one HA device
called `{device_name}` (from `ha_discovery.device_name`) with two button entities:

| Entity | Action |
|--------|--------|
| `{device_name} Train` | Triggers a full training cycle (ingest actuals, retrain models, then infer) |
| `{device_name} Infer` | Triggers an inference-only cycle (update forecasts without retraining) |

Both buttons appear on the same device card in the HA dashboard.
```

No config reference pages need regeneration — `device_id` and `device_label` are
parameters to `publish_trigger_discovery()` in `helper_common`, not user-facing
config fields.
