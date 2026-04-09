# Plan 47 — Rework mimirheim main app HA discovery to single-topic device JSON

## Motivation

The current `mimirheim.io.ha_discovery` implementation publishes one retained MQTT
message per entity: a config run produces upwards of 20–50 individual topics of
the form:

    homeassistant/{component}/{device_id}_{entity_id}/config

Removing all entities requires republishing an empty payload to every individual
topic — and to know which topics exist, a secondary paho connection with a
wildcard subscription and a sleep is used to collect retained state from the
broker before diffing.

With 30+ entities this is fragile and operationally awkward.

Home Assistant 2024.2 introduced the MQTT *device JSON discovery* format: a
**single** retained message at `homeassistant/device/{device_id}/config` that
defines the device block plus all its component entities in one payload. Removing
all entities for a device requires publishing an empty payload to exactly one
topic. There is no state drift: the broker either holds the payload (device
exists) or does not (device does not exist).

`active_discovery_topics()` and `cleanup_stale_discovery()` become dead code
under the new format and are deleted in this plan.

This plan rewrites `publish_discovery` in `mimirheim/io/ha_discovery.py` to the new
format and removes `active_discovery_topics` and `cleanup_stale_discovery`
entirely.

---

## Critical design decisions

### Home Assistant version requirement

The device JSON format requires HA 2024.2 or later. This must be stated clearly
in the config example YAML and release notes. Deployments running HA < 2024.2
must upgrade HA before applying this plan's code.

Do not maintain backward compatibility with the old per-entity format. A
compatibility shim that publishes both formats simultaneously would be harder to
reason about than the clean break, and the HA 2024.2 release is now over two
years old.

### Single discovery topic per mimirheim instance

After this plan: `publish_discovery()` publishes exactly one retained message:

    homeassistant/device/{device_id}/config

The payload is a JSON object:

```json
{
  "device": {
    "identifiers": ["{device_id}"],
    "name": "{device_name}",
    "manufacturer": "Mimirheim"
  },
  "origin": {
    "name": "Mimirheim"
  },
  "components": {
    "{device_id}_grid_import_kw": {
      "platform": "sensor",
      "name": "Grid Import Forecast",
      "unique_id": "{device_id}_grid_import_kw",
      "state_topic": "...",
      ...
    },
    ...
  }
}
```

The `components` map key is the entity's `unique_id`. The `platform` key (not
`component`) identifies the entity type inside the device JSON format.

### Availability is optional inside device JSON

In the per-entity format, availability was specified per-entity via an
`availability` block. In the device JSON format, availability can be specified
at the device level (using the top-level `availability` key in the payload), and
all component entities inherit it. This simplifies the payload and reduces its
size. Use device-level availability.

### The payload size concern is manageable

A mimirheim instance with 10 devices has roughly 30 entities. Each entity definition
is approximately 200–400 bytes of JSON. Total payload: ~9–12 KB. MQTT brokers on
Raspberry Pi handle this without issue (Mosquitto's default maximum message size
is 256 MB; practical broker-enforced limits start around 65 KB).

---

## Relevant source locations

```
mimirheim/io/ha_discovery.py             — rewrite publish_discovery();
                                      delete active_discovery_topics() and
                                      cleanup_stale_discovery() entirely
mimirheim/config/schema.py               — no changes required
mimirheim/config/example.yaml            — add HA version note in ha: comment block
tests/unit/test_ha_discovery.py     — major update to all tests
```

---

## Tests first

The existing `tests/unit/test_ha_discovery.py` tests assertions about multiple
topics, per-entity payload shapes, and exact publish counts that will all fail
after the rewrite. Update the test file to match the new format **before**
touching the implementation.

The test file must contain tests for both the new format and the migration
cleanup. Run `uv run pytest tests/unit/test_ha_discovery.py` — all updated tests
must fail against the current implementation before you begin.

### New test cases to add (replace old format-specific tests):

```python
# --- Basic publication ---

def test_publish_discovery_publishes_exactly_one_payload() -> None:
    """publish_discovery() makes exactly one client.publish() call."""

def test_discovery_topic_is_device_json_format() -> None:
    """The single topic is homeassistant/device/{device_id}/config."""

def test_payload_is_valid_json_with_device_and_components() -> None:
    """The payload has top-level keys 'device', 'origin', 'components',
    and 'availability'."""

def test_device_block_has_correct_identifiers() -> None:
    """device.identifiers == [device_id]."""

def test_components_contains_expected_entities() -> None:
    """For a config with 1 battery + 1 PV + 1 static_load, the components
    map contains: grid_import_kw, grid_export_kw, solve_status, strategy,
    trigger_run, and three setpoint sensors (9 total)."""

def test_each_component_has_platform_and_unique_id() -> None:
    """Every entry in components has 'platform' and 'unique_id' keys."""

def test_component_unique_id_matches_map_key() -> None:
    """Each component's unique_id equals the key it is stored under."""

def test_no_publish_when_ha_disabled() -> None:
    """publish_discovery() is a no-op when homeassistant.enabled is False."""

def test_device_level_availability_is_present() -> None:
    """The top-level 'availability' key is present; no per-component
    availability key exists."""
```

### Existing tests to update

All existing tests that assert on topic count, topic structure, or payload shape
must be rewritten to match the new single-topic device JSON format. The following
tests are invalidated by the new format and should be deleted, not adapted:

- `test_correct_number_of_payloads` → replaced by `test_publish_discovery_publishes_exactly_one_payload`
- `test_discovery_topic_structure` → replaced by `test_discovery_topic_is_device_json_format`
- `test_all_publishes_are_retained_qos1` → keep but verify the single payload
- `test_payload_is_valid_json_with_required_keys` → replaced by `test_payload_is_valid_json_with_device_and_components`
- `test_device_block_uses_configured_device_id` → keep, same semantics
- `test_device_block_defaults_to_client_id` → keep, same semantics
- `test_custom_discovery_prefix` → keep, same semantics
- `test_device_setpoint_topics_use_mqtt_prefix` → check setpoint state_topics inside components map
- All per-device entity tests (battery SOC, EV, deferrable load, etc.) → assert entity exists in `payload["components"]` map rather than as a separate publish call
- All `active_discovery_topics` and `cleanup_stale_discovery` tests → **delete entirely**

---

## Implementation

### `mimirheim/io/ha_discovery.py` — rewrite `publish_discovery()`

```python
def publish_discovery(client: Any, config: MimirheimConfig) -> None:
    """Publish the mimirheim MQTT device JSON discovery payload.

    Publishes a single retained QoS-1 message to:
        {discovery_prefix}/device/{device_id}/config

    The payload contains a 'device' block, an 'origin' block, a top-level
    'availability' entry, and a 'components' map with one entry per mimirheim
    entity. Home Assistant processes this as a device with all entities
    simultaneously.

    Requires Home Assistant 2024.2 or later.

    This function is idempotent: calling it multiple times overwrites the
    same retained topic with the same content.

    Args:
        client: A paho-mqtt Client with an active connection.
        config: Static system configuration.
    """
    if not config.homeassistant.enabled:
        return

    ha = config.homeassistant
    disc_prefix = ha.discovery_prefix
    device_id = ha.device_id or config.mqtt.client_id

    device_block = {
        "identifiers": [device_id],
        "name": ha.device_name,
        "manufacturer": "Mimirheim",
    }
    availability = {
        "topic": config.outputs.availability,
        "payload_available": "online",
        "payload_not_available": "offline",
    }

    components: dict[str, dict[str, Any]] = {}

    def _add(unique_id: str, platform: str, entity: dict[str, Any]) -> None:
        entity["platform"] = platform
        entity["unique_id"] = unique_id
        components[unique_id] = entity

    # --- Grid sensors ---
    _add(f"{device_id}_grid_import_kw", "sensor", {
        "name": "Grid Import Forecast",
        "state_topic": config.outputs.current,
        "value_template": "{{ value_json.grid_import_kw | round(2) }}",
        "unit_of_measurement": "kW",
        "device_class": "power",
        "entity_category": "diagnostic",
    })
    # ... (same pattern for all entities currently in publish_discovery)

    topic = f"{disc_prefix}/device/{device_id}/config"
    payload = {
        "device": device_block,
        "origin": {"name": "Mimirheim"},
        "availability": availability,
        "components": components,
    }
    client.publish(topic, json.dumps(payload), qos=1, retain=True)
    logger.debug(
        "Published HA device JSON discovery for %s (%d entities).",
        device_id,
        len(components),
    )
```

`active_discovery_topics()` and `cleanup_stale_discovery()` are **deleted**.
Remove all call sites in `mimirheim/__main__.py` and any tests that reference them.

The invariant test that previously checked `active_discovery_topics()` mirrors
`publish_discovery()` is replaced by `test_publish_discovery_publishes_exactly
_one_payload` — one publish is one topic, so the invariant is trivially self-
enforcing.

---

## Example config note

Add a comment to `mimirheim/config/example.yaml` in the `homeassistant:` block:

```yaml
homeassistant:
  # Requires Home Assistant 2024.2 or later. The HA MQTT device JSON
  # discovery format (single retained topic per mimirheim instance) is used.
  # Earlier HA versions are not supported.
  enabled: true
  discovery_prefix: homeassistant
  device_id: hioo_home          # optional: overrides mqtt.client_id
  device_name: "mimirheim optimiser"
```

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_ha_discovery.py   # all tests green
uv run pytest                                    # full suite green
```

Behavioural checks:

1. `publish_discovery()` called with a 10-device config: exactly one `client.publish()`
   call, with topic `homeassistant/device/{device_id}/config`, payload containing
   `components` map with the expected number of entries.

2. Against a live HA instance (manual check): after a broker restart and mimirheim
   reconnect, all devices and entities re-appear in HA without manual
   intervention.

---
