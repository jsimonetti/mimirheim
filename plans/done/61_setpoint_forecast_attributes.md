# Plan 61 — Solver forecast attributes on HA setpoint sensors

## Purpose

After plan 59 (helper forecast sensors), all input series — prices, PV forecast,
baseload forecast — are available as HA entities with `json_attributes`. The
solver-derived series — grid flows and per-device power/SOC trajectories — are
not yet accessible in the same way.

This plan adds `json_attributes_topic` and `json_attributes_template` to each
per-device setpoint sensor in `ha_discovery.py`. The template extracts this
device's data from `outputs.schedule`, which is already published retained after
every solve. No new MQTT topics are introduced. No output payload format changes.

After this plan, an `apexcharts-card` dashboard can be built entirely from HA
entities without the reporter `chart_topic`, using `json_attributes_path:
$.forecast` on each sensor.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §4 — output topic contract. `outputs.schedule` format is the source of truth;
  this plan reads it but does not change it.
- §6 — module boundary rules. `ha_discovery.py` already imports from
  `mimirheim.core.bundle` and `mimirheim.config.schema`; no new boundaries crossed.
- §14 — HA discovery derivation. All changes are inside `publish_discovery()`.

---

## Scope

**In scope:**
- `pyproject.toml` — add `jinja2>=3.0` to the `[dependency-groups] dev` list so
  tests can render template strings. `jinja2` is not added to production
  dependencies; `ha_discovery.py` produces template strings but does not execute
  them.
- `mimirheim/io/ha_discovery.py` — add `json_attributes_topic` and
  `json_attributes_template` to all per-device setpoint sensors and to the
  existing grid sensors.
- `tests/unit/test_ha_discovery.py` — new tests for the forecast attribute fields,
  including template-rendering tests that execute the Jinja2 template against a
  mock schedule payload and assert on the resulting JSON structure.
- `README.md` §11 (Home Assistant integration) — update the entity table and add
  a paragraph describing forecast attributes and `apexcharts-card` usage.
- `wiki/Reference/MQTT-Topics.md` — note that setpoint and grid sensor entities
  now carry `forecast` JSON attributes derived from `outputs.schedule`.

**Not in scope:**
- Any change to `outputs.schedule` payload format.
- Any change to the per-device setpoint topic payload (`{"kw": ..., "type": ...}`).
- The reporter (plan 62 handles chart_topic deprecation once this plan is complete).
- Helper forecast sensors (plan 59).

---

## Decisions

### Source topic

`outputs.schedule` is the single source for all forecast attributes. It is
already published retained after every solve. Every setpoint sensor and both
grid sensors use `json_attributes_topic: config.outputs.schedule`.

### Template strategy

The Python code in `ha_discovery.py` knows the device type at discovery-generation
time. The Jinja2 `json_attributes_template` string is constructed in Python
differently per type — no runtime Jinja2 conditionals needed. This keeps the
rendered template simple and HA evaluation fast.

The rendered template always produces a JSON object with a single key:

```json
{"forecast": [ ... ]}
```

`apexcharts-card` reads the array via `json_attributes_path: $.forecast`.

### Template shape per device type

**Storage devices** (batteries, EVs, hybrid inverters):

Each step includes `kw` (power, signed: positive = discharge, negative = charge)
and `soc_kwh` (terminal SOC at end of this step, from `device_soc_kwh`).

```json
{"forecast": [{"kw": -2.4, "soc_kwh": 5.2}, ...]}
```

Jinja2 template (constructed in Python with `device_name` substituted):

```jinja2
{%- set ns = namespace(f=[]) -%}
{%- for s in value_json.schedule -%}
{%- set ns.f = ns.f + [{"kw": s.devices["<device_name>"].kw, "soc_kwh": s.device_soc_kwh.get("<device_name>")}] -%}
{%- endfor -%}
{{ {"forecast": ns.f} | tojson }}
```

Note: bracket notation (`s.devices["name"]`) is used throughout rather than dot
notation (`s.devices.name`) to handle device names that contain hyphens or other
characters that dot notation cannot address in Jinja2.

**Non-storage devices** (PV arrays, static loads, deferrable loads):

Each step includes only `kw`. No `soc_kwh` key is present.

```json
{"forecast": [{"kw": 3.1}, ...]}
```

Jinja2 template:

```jinja2
{%- set ns = namespace(f=[]) -%}
{%- for s in value_json.schedule -%}
{%- set ns.f = ns.f + [{"kw": s.devices["<device_name>"].kw}] -%}
{%- endfor -%}
{{ {"forecast": ns.f} | tojson }}
```

**Grid sensors** (the existing `grid_import_kw` and `grid_export_kw` sensors in
`ha_discovery.py`, which use `outputs.current` as their `state_topic`):

The grid does not appear in `schedule.devices`, so the template iterates the
schedule steps directly.

```json
{"forecast": [{"grid_import_kw": 0.0, "grid_export_kw": 1.8}, ...]}
```

Both grid sensors share the same `json_attributes_topic` and template. HA will
store the same attribute array on both entities — this is an accepted duplication
since the two sensors are logically paired and no distinct device entry exists for
the grid.

Jinja2 template (identical for both grid sensors):

```jinja2
{%- set ns = namespace(f=[]) -%}
{%- for s in value_json.schedule -%}
{%- set ns.f = ns.f + [{"grid_import_kw": s.grid_import_kw, "grid_export_kw": s.grid_export_kw}] -%}
{%- endfor -%}
{{ {"forecast": ns.f} | tojson }}
```

### Device type classification in ha_discovery.py

The setpoint loop currently iterates `all_device_names`, a flat list built from
all config sections. For this plan, the loop must know which devices are storage
(battery, EV, hybrid inverter) and which are non-storage (PV, static load,
deferrable load, thermal, HP).

Split `all_device_names` into two sets at the top of the loop:

```python
storage_device_names = {
    *config.batteries,
    *config.ev_chargers,
    *config.hybrid_inverters,
}
```

All remaining device types are non-storage.

Thermal boilers, space heating HPs, and combi heat pumps: these are also
non-storage and do not appear in `schedule.devices` in the current
implementation. Their setpoint sensor should still receive `json_attributes_topic`
pointing at `outputs.schedule`, but the template will produce `kw` only.
If a thermal device has no entry in `schedule.devices` for a given step the
template will fail silently in HA (the attribute will be absent). This is an
accepted limitation; thermal device forecasting is a future concern.

### `enabled_by_default`

Leave setpoint sensors at their current default (`enabled_by_default` not set,
which defaults to `true` in HA). Attributes are attached to the same entity the
user has already opted into. Do not add `enabled_by_default: false`.

### HA recorder and attribute size

A 96-step forecast array with two float fields per step is approximately 3–4 kB.
HA records attribute changes alongside state changes. With four solves per day,
each storage device contributes roughly 4 × 4 kB = 16 kB/day in the recorder.
This is well within normal HA recorder volume for a monitoring installation and
does not require mitigation. Users who want to exclude it can add
`recorder.exclude.entities` in HA configuration.

---

## Files to create or edit

```
mimirheim/
  io/
    ha_discovery.py              ← add json_attributes_topic / template to setpoint and grid sensors
tests/
  unit/
    test_ha_discovery.py         ← new tests for forecast attribute fields
```

No new files. No new MQTT topics. No changes to output schemas.

---

## TDD workflow

### Step 0 — add jinja2 as a dev dependency

`ha_discovery.py` generates Jinja2 template strings that HA evaluates at runtime.
String-content checks alone (e.g. `assert "soc_kwh" in template`) are not
sufficient to catch malformed templates, wrong key names, or bad loop logic.
Tests must render the templates against a mock schedule payload and assert on the
resulting JSON.

`jinja2` is not currently installed in the project. Add it to the dev dependency
group and sync:

```bash
uv add --dev "jinja2>=3.0"
uv sync
```

Add a shared `_render_template()` helper at the top of `test_ha_discovery.py`:

```python
import json
import jinja2

def _render_template(template_str: str, value_json: dict) -> dict:
    """Render a Jinja2 template string as HA would, return parsed JSON.

    HA evaluates json_attributes_template with ``value_json`` bound to the
    parsed MQTT payload. This helper replicates that environment using a
    standard jinja2.Environment with the tojson filter available.

    The template is expected to produce a JSON object string. An AssertionError
    is raised if the rendered output is not valid JSON or not a dict.
    """
    env = jinja2.Environment()
    # HA's tojson filter is standard Jinja2; no extra configuration needed.
    tmpl = env.from_string(template_str)
    rendered = tmpl.render(value_json=value_json)
    result = json.loads(rendered)
    assert isinstance(result, dict), f"Template did not produce a JSON object: {rendered!r}"
    return result
```

Add a `_make_mock_schedule()` fixture that produces a minimal but realistic
`SolveResult`-like dict (the shape of `outputs.schedule` payload):

```python
def _make_mock_schedule(
    *,
    battery_name: str = "home_battery",
    pv_name: str = "roof_pv",
    load_name: str = "base_load",
    n_steps: int = 3,
) -> dict:
    """Return a minimal SolveResult-shaped dict for template rendering tests.

    Two steps is enough to verify the loop body; three is sufficient to rule
    out off-by-one edge cases.
    """
    schedule = []
    for i in range(n_steps):
        schedule.append({
            "t": i,
            "grid_import_kw": float(i) * 0.5,
            "grid_export_kw": float(i) * 0.1,
            "devices": {
                battery_name: {"kw": -1.0 + i * 0.5, "type": "battery"},
                pv_name:      {"kw": 2.0 + i * 0.2, "type": "pv"},
                load_name:    {"kw": 0.4,             "type": "static_load"},
            },
            "device_soc_kwh": {
                battery_name: 5.0 + i * 0.3,
            },
        })
    return {
        "strategy": "minimize_cost",
        "objective_value": 1.23,
        "solve_status": "optimal",
        "schedule": schedule,
        "deferrable_recommended_starts": {},
    }
```

### Step 1 — write failing tests

Add two new test classes to `tests/unit/test_ha_discovery.py`:
`TestForecastAttributePresence` (field checks, fast) and
`TestForecastTemplateRendering` (Jinja2 execution, substantive).

All tests will fail until the implementation in Step 2 is complete.

```python
class TestForecastAttributePresence:
    """Verify that json_attributes_topic and json_attributes_template are present
    in the correct components and reference the schedule topic."""

    def test_battery_setpoint_has_json_attributes_topic(self) -> None:
        """Battery setpoint sensor has json_attributes_topic == outputs.schedule."""
        components = _publish_and_get_components(_make_config())
        assert components["mimir-test_home_battery_setpoint_kw"].get(
            "json_attributes_topic"
        ) == "mimir/schedule"

    def test_pv_setpoint_has_json_attributes_topic(self) -> None:
        """PV setpoint sensor has json_attributes_topic == outputs.schedule."""
        assert _publish_and_get_components(_make_config())[
            "mimir-test_roof_pv_setpoint_kw"
        ].get("json_attributes_topic") == "mimir/schedule"

    def test_static_load_setpoint_has_json_attributes_topic(self) -> None:
        """Static load setpoint sensor has json_attributes_topic == outputs.schedule."""
        assert _publish_and_get_components(_make_config())[
            "mimir-test_base_load_setpoint_kw"
        ].get("json_attributes_topic") == "mimir/schedule"

    def test_grid_import_sensor_has_json_attributes_topic(self) -> None:
        """Grid import sensor has json_attributes_topic == outputs.schedule."""
        assert _publish_and_get_components(_make_config())[
            "mimir-test_grid_import_kw"
        ].get("json_attributes_topic") == "mimir/schedule"

    def test_grid_export_sensor_has_json_attributes_topic(self) -> None:
        """Grid export sensor has json_attributes_topic == outputs.schedule."""
        assert _publish_and_get_components(_make_config())[
            "mimir-test_grid_export_kw"
        ].get("json_attributes_topic") == "mimir/schedule"

    def test_ev_charger_setpoint_has_json_attributes_topic(self) -> None:
        """EV charger setpoint sensor has json_attributes_topic == outputs.schedule."""
        components = _publish_and_get_components(_make_config_with_ev())
        ev_uid = next(k for k in components if "car_ev" in k and "setpoint" in k)
        assert components[ev_uid].get("json_attributes_topic") == "mimir/schedule"

    def test_all_setpoint_sensors_have_json_attributes_template(self) -> None:
        """Every setpoint sensor component has a non-empty json_attributes_template."""
        components = _publish_and_get_components(_make_config())
        setpoint_uids = [k for k in components if k.endswith("_setpoint_kw")]
        assert setpoint_uids, "Expected at least one setpoint component"
        for uid in setpoint_uids:
            assert components[uid].get("json_attributes_template"), (
                f"Missing or empty json_attributes_template on {uid!r}"
            )

    def test_template_output_key_is_always_forecast(self) -> None:
        """All json_attributes_template strings produce a 'forecast' key."""
        components = _publish_and_get_components(_make_config())
        for uid, component in components.items():
            if "json_attributes_template" in component:
                assert '"forecast"' in component["json_attributes_template"], (
                    f"Template for {uid!r} does not produce a 'forecast' key"
                )

    def test_battery_template_uses_bracket_notation(self) -> None:
        """Battery template uses bracket notation devices["name"] not dot notation."""
        template = _publish_and_get_components(_make_config())[
            "mimir-test_home_battery_setpoint_kw"
        ].get("json_attributes_template", "")
        assert 'devices["home_battery"]' in template

    def test_storage_templates_reference_soc_kwh(self) -> None:
        """Battery and EV charger templates reference soc_kwh."""
        components = _publish_and_get_components(_make_config_with_ev())
        for uid in ("mimir-test_home_battery_setpoint_kw",):
            template = components.get(uid, {}).get("json_attributes_template", "")
            assert "soc_kwh" in template, f"Missing soc_kwh in template for {uid!r}"
        ev_uid = next(k for k in components if "car_ev" in k and "setpoint" in k)
        assert "soc_kwh" in components[ev_uid].get("json_attributes_template", "")

    def test_non_storage_templates_do_not_reference_soc_kwh(self) -> None:
        """PV and static load templates do not reference soc_kwh."""
        components = _publish_and_get_components(_make_config())
        for uid in ("mimir-test_roof_pv_setpoint_kw", "mimir-test_base_load_setpoint_kw"):
            template = components[uid].get("json_attributes_template", "")
            assert "soc_kwh" not in template, (
                f"Unexpected soc_kwh in non-storage template for {uid!r}"
            )

    def test_deferrable_load_template_does_not_reference_soc_kwh(self) -> None:
        """Deferrable load template does not reference soc_kwh."""
        components = _publish_and_get_components(_make_config_with_deferrable_rec_start())
        template = components["mimir-test_wash_setpoint_kw"].get("json_attributes_template", "")
        assert "soc_kwh" not in template

    def test_grid_templates_reference_both_grid_fields(self) -> None:
        """Grid templates reference grid_import_kw and grid_export_kw."""
        components = _publish_and_get_components(_make_config())
        for uid in ("mimir-test_grid_import_kw", "mimir-test_grid_export_kw"):
            template = components[uid].get("json_attributes_template", "")
            assert "grid_import_kw" in template
            assert "grid_export_kw" in template


class TestForecastTemplateRendering:
    """Render each template against a mock schedule payload and assert on the
    resulting JSON structure. These tests verify that the template string is
    syntactically valid Jinja2 and produces the expected output shape."""

    def test_battery_template_renders_forecast_array(self) -> None:
        """Battery template renders to a dict with a 'forecast' list of dicts
        each containing 'kw' and 'soc_kwh'."""
        components = _publish_and_get_components(_make_config())
        template_str = components["mimir-test_home_battery_setpoint_kw"]["json_attributes_template"]
        payload = _make_mock_schedule()

        result = _render_template(template_str, payload)

        assert "forecast" in result
        steps = result["forecast"]
        assert len(steps) == 3
        for step in steps:
            assert "kw" in step, f"Missing 'kw' key in battery forecast step: {step}"
            assert "soc_kwh" in step, f"Missing 'soc_kwh' key in battery forecast step: {step}"

    def test_battery_template_values_match_schedule(self) -> None:
        """Battery forecast values match the corresponding schedule entries."""
        components = _publish_and_get_components(_make_config())
        template_str = components["mimir-test_home_battery_setpoint_kw"]["json_attributes_template"]
        payload = _make_mock_schedule()

        result = _render_template(template_str, payload)
        steps = result["forecast"]

        for i, step in enumerate(steps):
            expected_kw = payload["schedule"][i]["devices"]["home_battery"]["kw"]
            expected_soc = payload["schedule"][i]["device_soc_kwh"]["home_battery"]
            assert step["kw"] == pytest.approx(expected_kw), (
                f"Battery kw mismatch at step {i}"
            )
            assert step["soc_kwh"] == pytest.approx(expected_soc), (
                f"Battery soc_kwh mismatch at step {i}"
            )

    def test_pv_template_renders_forecast_array(self) -> None:
        """PV template renders to a dict with a 'forecast' list of dicts
        each containing 'kw' but not 'soc_kwh'."""
        components = _publish_and_get_components(_make_config())
        template_str = components["mimir-test_roof_pv_setpoint_kw"]["json_attributes_template"]
        payload = _make_mock_schedule()

        result = _render_template(template_str, payload)

        assert "forecast" in result
        for step in result["forecast"]:
            assert "kw" in step
            assert "soc_kwh" not in step

    def test_pv_template_values_match_schedule(self) -> None:
        """PV forecast kw values match the corresponding schedule entries."""
        components = _publish_and_get_components(_make_config())
        template_str = components["mimir-test_roof_pv_setpoint_kw"]["json_attributes_template"]
        payload = _make_mock_schedule()

        result = _render_template(template_str, payload)

        for i, step in enumerate(result["forecast"]):
            expected_kw = payload["schedule"][i]["devices"]["roof_pv"]["kw"]
            assert step["kw"] == pytest.approx(expected_kw)

    def test_static_load_template_renders_forecast_array(self) -> None:
        """Static load template renders to a dict with a 'forecast' list."""
        components = _publish_and_get_components(_make_config())
        template_str = components["mimir-test_base_load_setpoint_kw"]["json_attributes_template"]
        payload = _make_mock_schedule()

        result = _render_template(template_str, payload)

        assert "forecast" in result
        assert len(result["forecast"]) == 3
        for step in result["forecast"]:
            assert "kw" in step
            assert "soc_kwh" not in step

    def test_grid_template_renders_both_series(self) -> None:
        """Grid template renders a 'forecast' list with grid_import_kw and
        grid_export_kw per step."""
        components = _publish_and_get_components(_make_config())
        template_str = components["mimir-test_grid_import_kw"]["json_attributes_template"]
        payload = _make_mock_schedule()

        result = _render_template(template_str, payload)

        assert "forecast" in result
        for i, step in enumerate(result["forecast"]):
            assert "grid_import_kw" in step
            assert "grid_export_kw" in step
            assert step["grid_import_kw"] == pytest.approx(
                payload["schedule"][i]["grid_import_kw"]
            )
            assert step["grid_export_kw"] == pytest.approx(
                payload["schedule"][i]["grid_export_kw"]
            )

    def test_grid_import_and_export_sensors_share_identical_template(self) -> None:
        """Both grid sensors use the same template string (not just the same logic).
        This ensures both entities expose the same forecast array in their
        attributes, which is required for apexcharts-card to use either one as
        the source."""
        components = _publish_and_get_components(_make_config())
        import_tmpl = components["mimir-test_grid_import_kw"]["json_attributes_template"]
        export_tmpl = components["mimir-test_grid_export_kw"]["json_attributes_template"]
        assert import_tmpl == export_tmpl

    def test_template_renders_correct_step_count(self) -> None:
        """Template output contains exactly as many forecast steps as the
        schedule contains, for a range of horizon lengths."""
        components = _publish_and_get_components(_make_config())
        template_str = components["mimir-test_home_battery_setpoint_kw"]["json_attributes_template"]

        for n_steps in (1, 4, 96):
            payload = _make_mock_schedule(n_steps=n_steps)
            result = _render_template(template_str, payload)
            assert len(result["forecast"]) == n_steps, (
                f"Expected {n_steps} steps, got {len(result['forecast'])}"
            )

    def test_template_handles_device_name_with_underscore(self) -> None:
        """Templates for device names containing underscores render correctly.
        Bracket notation must be used; dot notation silently fails in Jinja2
        when the key contains underscores (actually works, but this test ensures
        the actual values are correct regardless of notation)."""
        config = _make_config()   # home_battery contains an underscore
        components = _publish_and_get_components(config)
        template_str = components["mimir-test_home_battery_setpoint_kw"]["json_attributes_template"]
        payload = _make_mock_schedule()
        result = _render_template(template_str, payload)
        # If bracket notation is broken, the template would produce null/None
        # for kw and soc_kwh. Assert they are actual floats.
        for step in result["forecast"]:
            assert isinstance(step["kw"], float), f"kw is not a float: {step['kw']!r}"
            assert isinstance(step["soc_kwh"], float), f"soc_kwh is not a float: {step['soc_kwh']!r}"

    def test_template_handles_empty_schedule(self) -> None:
        """When the schedule is empty (infeasible solve), template renders an
        empty forecast list rather than raising."""
        components = _publish_and_get_components(_make_config())
        template_str = components["mimir-test_home_battery_setpoint_kw"]["json_attributes_template"]
        payload = _make_mock_schedule(n_steps=0)

        result = _render_template(template_str, payload)

        assert result == {"forecast": []}
```

Also add `_make_config_with_ev()` fixture:

```python
def _make_config_with_ev() -> MimirheimConfig:
    """Config with one battery and one EV charger, for storage-type template tests."""
    return MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimir-test", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "batteries": {
            "home_battery": {
                "capacity_kwh": 10.0,
                "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "wear_cost_eur_per_kwh": 0.005,
                "inputs": {"soc": {"topic": "mimir/input/bat/soc", "unit": "kwh"}},
            },
        },
        "ev_chargers": {
            "car_ev": {
                "capacity_kwh": 60.0,
                "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.92}],
                "inputs": {
                    "soc": {"topic": "mimir/input/ev/car_ev/soc", "unit": "kwh"},
                    "plugged_in_topic": "mimir/input/ev/car_ev/plugged_in",
                },
            },
        },
    })
```

Run `uv run pytest tests/unit/test_ha_discovery.py` — all new tests must fail
(`KeyError` or `AssertionError` because the fields are absent).

### Step 2 — implement in ha_discovery.py

In `publish_discovery()`, update the per-device setpoint sensor loop.

**Before (current):**

```python
for device_name in all_device_names:
    unique_id = f"{device_id}_{device_name}_setpoint_kw"
    _add(unique_id, "sensor", {
        "name": f"{device_name} setpoint",
        "state_topic": f"{mqtt_prefix}/device/{device_name}/setpoint",
        "value_template": "{{ value_json.kw | round(2) }}",
        "unit_of_measurement": "kW",
        "device_class": "power",
    })
```

**After:**

```python
storage_device_names = {
    *config.batteries,
    *config.ev_chargers,
    *config.hybrid_inverters,
}

for device_name in all_device_names:
    unique_id = f"{device_id}_{device_name}_setpoint_kw"
    if device_name in storage_device_names:
        attr_template = (
            "{%- set ns = namespace(f=[]) -%}"
            "{%- for s in value_json.schedule -%}"
            f'{{% set ns.f = ns.f + [{{"kw": s.devices["{device_name}"].kw, '
            f'"soc_kwh": s.device_soc_kwh.get("{device_name}")}}] %}}'
            "{%- endfor -%}"
            '{{ {"forecast": ns.f} | tojson }}'
        )
    else:
        attr_template = (
            "{%- set ns = namespace(f=[]) -%}"
            "{%- for s in value_json.schedule -%}"
            f'{{% set ns.f = ns.f + [{{"kw": s.devices["{device_name}"].kw}}] %}}'
            "{%- endfor -%}"
            '{{ {"forecast": ns.f} | tojson }}'
        )
    _add(unique_id, "sensor", {
        "name": f"{device_name} setpoint",
        "state_topic": f"{mqtt_prefix}/device/{device_name}/setpoint",
        "value_template": "{{ value_json.kw | round(2) }}",
        "unit_of_measurement": "kW",
        "device_class": "power",
        "json_attributes_topic": config.outputs.schedule,
        "json_attributes_template": attr_template,
    })
```

**Also update the two grid sensors.** The existing `grid_import_kw` and
`grid_export_kw` sensor definitions gain `json_attributes_topic` and
`json_attributes_template`. Both use the same template:

```python
_GRID_ATTR_TEMPLATE = (
    "{%- set ns = namespace(f=[]) -%}"
    "{%- for s in value_json.schedule -%}"
    '{% set ns.f = ns.f + [{"grid_import_kw": s.grid_import_kw, "grid_export_kw": s.grid_export_kw}] %}'
    "{%- endfor -%}"
    '{{ {"forecast": ns.f} | tojson }}'
)
```

Define this as a module-level constant above `publish_discovery()` to avoid
repeating it for both grid sensors.

### Step 3 — run the full test suite

```bash
uv run pytest
```

All tests must pass. No regressions.

### Step 4 — documentation updates

**README.md §11 (Home Assistant integration)**

Update the entity table to add `json_attributes` information for the setpoint
and grid sensors:

| Entity | HA type | State topic | Value | JSON attributes |
|---|---|---|---|---|
| `{device_name} Grid Import` | sensor (power) | `outputs.current` | `grid_import_kw` | `forecast` — array of `{grid_import_kw, grid_export_kw}` per step |
| `{device_name} Grid Export` | sensor (power) | `outputs.current` | `grid_export_kw` | `forecast` — array of `{grid_import_kw, grid_export_kw}` per step |
| `{device_name} {device_name} setpoint` | sensor (power) | `{prefix}/device/{name}/setpoint` | `kw` | `forecast` — array of `{kw}` per step (storage: also `soc_kwh`) |

Add a paragraph after the table explaining how to use the forecast attributes in
`apexcharts-card`:

```
### Forecast attributes for charting

Every setpoint sensor and both grid sensors carry a `forecast` JSON attribute
derived from `outputs.schedule`. The attribute is an array with one entry per
solver time step, starting from the current step (t=0).

For battery and EV charger setpoint sensors, each entry contains:
  - `kw` — scheduled power in kW (negative = charging, positive = discharging)
  - `soc_kwh` — terminal state of charge in kWh at the end of the step

For all other devices and the grid sensors, each entry contains only `kw`
(or `grid_import_kw` / `grid_export_kw` for the grid sensors).

In apexcharts-card, reference the forecast array via `json_attributes_path: $.forecast`:

    - entity: sensor.mimirheim_home_battery_setpoint
      attribute: forecast
      transform: "return x.map(s => [s.kw, s.soc_kwh])"
```

**`wiki/Reference/MQTT-Topics.md`**

In the per-device setpoint section, add a note:

```
The per-device setpoint topic carries the current-step setpoint. When HA MQTT
discovery is enabled, the corresponding HA sensor entity also carries a
``forecast`` JSON attribute sourced from ``outputs.schedule``. The attribute
contains one entry per solver step with per-device power (and SOC for storage
devices). It is not part of the MQTT topic payload itself — it is derived by
HA from the schedule topic via the discovery ``json_attributes_template``.
```

---

## Acceptance criteria

- [ ] `jinja2>=3.0` is added to `[dependency-groups] dev` in `pyproject.toml`.
- [ ] Every per-device setpoint sensor in the HA discovery payload has
      `json_attributes_topic` equal to `config.outputs.schedule`.
- [ ] Storage devices (batteries, EV chargers, hybrid inverters) have a
      `json_attributes_template` that includes both `kw` and `soc_kwh` per step.
- [ ] Non-storage devices (PV arrays, static loads, deferrable loads, thermal
      boilers, HPs) have a `json_attributes_template` that includes only `kw`
      per step. `soc_kwh` is absent.
- [ ] Both grid sensors (`grid_import_kw`, `grid_export_kw`) have
      `json_attributes_topic` equal to `config.outputs.schedule` and a template
      that includes `grid_import_kw` and `grid_export_kw` per step.
- [ ] All templates produce a JSON object with a single key `"forecast"`.
- [ ] All templates use bracket notation for `devices["name"]` lookups.
- [ ] `TestForecastTemplateRendering` tests execute the Jinja2 template against a
      mock schedule payload and assert on values, not just template string content.
- [ ] Template rendering tests cover: battery (kw + soc_kwh values correct), PV
      (kw only, no soc_kwh), grid (both fields), empty schedule (empty list), 96
      steps (correct count).
- [ ] `outputs.schedule` payload format is unchanged.
- [ ] Per-device setpoint topic payload (`{"kw": ..., "type": ...}`) is unchanged.
- [ ] No new MQTT topics are introduced.
- [ ] README.md §11 entity table updated to include `json_attributes` column.
- [ ] README.md §11 includes an `apexcharts-card` usage example for forecast attributes.
- [ ] `wiki/Reference/MQTT-Topics.md` setpoint section includes forecast attribute note.
- [ ] All existing tests continue to pass.
- [ ] `uv run pytest` exits 0 with no new failures.
