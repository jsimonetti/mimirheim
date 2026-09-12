# Step 71 (part 6 of 7) — Migrate existing ui_label/ui_group hints into the x-mimir- namespace

## Purpose

Every field in `mimirheim/config/schema.py` and in every helper's `config.py`
already carries a `ui_label` and a `ui_group` annotation, via
`json_schema_extra={"ui_label": "...", "ui_group": "basic"|"advanced"}` on
each `Field(...)` declaration. This is v1 `config-editor`'s own hint
vocabulary: its hand-built frontend
(`mimirheim_helpers/config_editor/config_editor/static/app.js`) reads
`fieldSchema.ui_label` and `fieldSchema.ui_group` directly to build labels
and the basic/advanced grouping toggle. Coverage is complete and enforced by
CI: `tests/unit/test_schema_ui_annotations.py` fails the build if any field,
anywhere (including every nested sub-model reachable via `$defs`), is
missing either annotation.

config-editor-v2's adapter (`jedison_mapping.py`, built in step 71_4) reads a
*different*, namespaced vocabulary: `x-mimir-label` (→ Jedison's `title`) and
`x-mimir-group` (→ Jedison's `x-category`/`x-format`). Because no registered
model uses these names today, every field config-editor-v2 renders falls
back to Pydantic's auto-generated title (the field name, title-cased) with no
grouping at all — the well-considered, already-reviewed `ui_label`/`ui_group`
content is invisible to it.

This step adds `x-mimir-label`/`x-mimir-group` **alongside** the existing
`ui_label`/`ui_group` keys, copying/deriving their values mechanically. It
does **not** remove, rename, or reword anything: v1's config-editor must
keep reading `ui_label`/`ui_group` completely unmodified, and the wording of
existing labels is out of scope here (revisiting individual field wording is
anticipated future work, not part of this mechanical migration). This step
makes no code changes to config-editor-v2 itself: `jedison_mapping.py`
already knows how to translate `x-mimir-label`/`x-mimir-group` once they
exist (built in step 71_4) — the only thing missing is the hints themselves,
on the production models.

---

## References

- `mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md`, section
  "Namespace convention" — states explicitly that adopting the `x-mimir-`
  namespace for a model's existing hints "is migration work performed when
  that model is first registered with this editor" — this step is that
  migration work, performed after the fact for the models registered in
  step 71_5.
- `mimirheim_helpers/config_editor_v2/config_editor_v2/jedison_mapping.py`
  — read this in full. `to_jedison_schema` already reads `x-mimir-label`;
  `to_jedison_object_schema` already reads `x-mimir-group` and derives
  `x-category`/`x-format` from it. No changes are needed here for this step.
- `tests/unit/test_schema_ui_annotations.py` — read this in full before
  writing any migration code. Its recursive `$defs`-walking pattern
  (`_collect_missing_ui_labels`, `_collect_missing_ui_group`) is the
  reference implementation for this step's own coverage test; reuse or
  closely mirror it.
- `mimirheim_helpers/config_editor_v2/config_editor_v2/registry.py`'s
  `_build_registry()` — the authoritative list of which models are actually
  registered (and therefore need this migration). Read the live function,
  not a frozen list, since helpers may have been added or removed since
  this plan was written.
- `plans/71_1_registry_and_adapter_dispatch.md` through
  `plans/71_5_http_service_and_deployment.md` (in `plans/done/`) — must be
  complete first (they are, as of this writing).
- AGENTS.md — this step touches production Pydantic models across the
  repository; the `extra="forbid"` rule, complete type annotations, and
  Google-style docstring rules already apply to every file touched and must
  not be violated by the migration.

---

## Scope: files to modify

Every file below defines at least one Pydantic model reachable from
`config_editor_v2.registry.REGISTRY` (directly registered, or embedded as a
shared sub-model such as `MqttConfig`/`HomeAssistantConfig`, which every
registered model includes). Confirm this list against the live registry
before starting — a new helper may have been added since this plan was
written, and an existing one may have been removed:

```
mimirheim/config/schema.py
mimirheim_helpers/common/helper_common/config.py
mimirheim_helpers/scheduler/scheduler/config.py
mimirheim_helpers/reporter/reporter/config.py
mimirheim_helpers/baseload/homeassistant/baseload_ha/config.py
mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/config.py
mimirheim_helpers/baseload/static/baseload_static/config.py
mimirheim_helpers/pv/open-meteo/pv_openmeteo/config.py
mimirheim_helpers/pv/forecast.solar/pv_fetcher/config.py
mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/config.py
mimirheim_helpers/prices/zonneplan/zonneplan_prices/config.py
mimirheim_helpers/prices/epexpredictor/epexpredictor_prices/config.py
mimirheim_helpers/prices/nordpool/nordpool/config.py
```

Do **not** touch `mimirheim_helpers/config_editor/config_editor/config.py`
(v1's own bootstrap config) or
`mimirheim_helpers/config_editor_v2/config_editor_v2/config.py` (v2's own
bootstrap config). Neither is registered in `REGISTRY` — per
IMPLEMENTATION_DETAILS.md's "Registry" section and step 71_5's own registry
build function, neither editor edits its own bootstrap configuration through
itself — so migrating their hints serves no purpose here.

As of this writing, `ui_label` appears roughly 400 times across these 12
files (`mimirheim/config/schema.py` alone accounts for over half of that).
Re-count it yourself (`grep -rc ui_label <file>` per file above) rather than
trusting this number, since the codebase will have moved on since this plan
was written.

---

## The mechanical transform

For every `Field(...)` declaration whose `json_schema_extra` dict literal
contains a `"ui_label"` key, a `"ui_group"` key, or both, add the
corresponding key(s) below to the *same* dict literal, alongside the
existing keys (do not reorder or remove anything already there):

- `"x-mimir-label"`: the exact same string value as `"ui_label"` — a
  verbatim copy, not a reworded version. Improving label wording is
  explicitly out of scope for this mechanical migration.
- `"x-mimir-group"`: derived from `"ui_group"`'s value via this fixed
  mapping — `"basic"` → `"Basic"`, `"advanced"` → `"Advanced"`. These are
  the only two values `ui_group` takes today (enforced by
  `test_schema_ui_annotations.py`'s
  `elif field_schema["ui_group"] not in ("basic", "advanced")` check); if a
  third value exists by the time you run this, extend the mapping
  consistently (title-case it) rather than leaving it untranslated.

Example, before:

```python
host: str = Field(
    description="MQTT broker hostname or IP address.",
    json_schema_extra={"ui_label": "Broker host", "ui_group": "basic"},
)
```

After:

```python
host: str = Field(
    description="MQTT broker hostname or IP address.",
    json_schema_extra={
        "ui_label": "Broker host",
        "ui_group": "basic",
        "x-mimir-label": "Broker host",
        "x-mimir-group": "Basic",
    },
)
```

Given the volume (on the order of several hundred call sites across 12
files), writing a small one-off migration script is strongly recommended
over hand-editing every occurrence — but the mechanism is your choice
(a script using Python's `ast` module to locate each qualifying
`json_schema_extra` dict literal by source offset and splice in the new
keys works well and preserves formatting exactly; careful regex over a
`json_schema_extra=\{[^{}]*\}` span also works given these are always flat,
single-level dicts of string/bool values with no nested braces). Whichever
approach you choose:

- Do not use `ast.unparse()` or any other whole-file regeneration approach
  that would reformat code you are not touching — the diff for this step
  should show only the inserted keys, not incidental reformatting of
  surrounding lines.
- Run the script (or apply the edits) one file at a time, and after each
  file, run that file's own relevant test slice (see Tests below) before
  moving to the next, so a mistake in file N does not get compounded by the
  same mistake being made N more times before you notice.
- Do not touch this project's ordinary `Field(...)` arguments other than
  `json_schema_extra` (description, default, ge/le, etc.) — this step adds
  two keys to an existing dict literal and nothing else.

This step introduces no new code in `config_editor_v2` itself. Do not modify
`jedison_mapping.py`, `adapter.py`, or any other module under
`mimirheim_helpers/config_editor_v2/config_editor_v2/` — the translation
logic for `x-mimir-label`/`x-mimir-group` already exists (step 71_4) and is
already exercised by config_editor_v2's own test suite; this step only
needs the hints to exist on the models.

---

## Tests

### New file: `tests/unit/test_schema_x_mimir_annotations.py`

Placed at the repository root (not under
`mimirheim_helpers/config_editor_v2/tests/`), mirroring
`test_schema_ui_annotations.py`'s own placement and for the same reason
(`mimirheim_helpers/config_editor/AGENTS.md` explains why: this test
imports across package boundaries — mimirheim core plus every helper — so
it belongs with the root suite, not with any one package's own tests).

Write this test first, per this project's TDD discipline: it must fail
before the migration and pass after.

Mirror `test_schema_ui_annotations.py`'s recursive `$defs`-walking pattern
(reuse its helper functions via import if that reads cleanly, or duplicate
the walk logic — both are acceptable; duplication is fine for test code of
this size). For every field where `ui_label`/`ui_group` is present in the
live `model_json_schema()` output:

- `test_every_ui_label_has_a_matching_x_mimir_label` — walks
  `MimirheimConfig.model_json_schema()` (main schema plus every `$defs`
  entry, exactly as `test_all_fields_have_ui_label` does) and asserts that
  every field carrying `ui_label` also carries `x-mimir-label` with the
  **exact same value**. Report violations the same way the reference test
  does: a list of dotted field paths, printed in the assertion failure
  message, not just a bare `assert not violations`.
- `test_every_ui_group_has_a_matching_x_mimir_group` — same walk, asserting
  every field carrying `ui_group` also carries `x-mimir-group` equal to the
  fixed `"basic"` → `"Basic"` / `"advanced"` → `"Advanced"` mapping.
- Parametrized equivalents of both tests above for every helper model
  currently in `config_editor_v2.registry.REGISTRY` (import
  `REGISTRY` and `resolve_model` directly and iterate it, rather than
  hand-listing helper models as `test_schema_ui_annotations.py` does — this
  makes the test automatically track the live registry instead of drifting
  from it as helpers are added or removed).

### Regression check

- `uv run pytest tests/unit/test_schema_ui_annotations.py -v` must still
  pass unchanged — this migration must not touch, break, or remove any
  `ui_label`/`ui_group` value v1 depends on.
- `uv run pytest mimirheim_helpers/config_editor_v2/tests -q` must still
  pass unchanged — this step adds no config-editor-v2 code.
- `uv run pytest -q` (full suite) must show zero regressions.
- `uv run ruff check .` must be clean.

---

## Acceptance criteria

- `tests/unit/test_schema_x_mimir_annotations.py` exists, was observed
  failing before the migration, and passes after.
- Every field carrying `ui_label` in every file listed under "Scope" also
  carries `x-mimir-label` with an identical value.
- Every field carrying `ui_group` in every file listed under "Scope" also
  carries `x-mimir-group`, mapped `"basic"` → `"Basic"`,
  `"advanced"` → `"Advanced"`.
- No `ui_label` or `ui_group` value was changed, removed, or reworded.
- No file outside the "Scope" list was modified.
- No file under `mimirheim_helpers/config_editor_v2/` was modified.
- `uv run pytest` (full suite) shows no regressions.
- `uv run ruff check .` is clean.
- A manual spot check: open config-editor-v2 in a browser
  (`uv run python -m config_editor_v2 --config <path>`, per its own
  README/wiki page) and confirm at least one previously unlabeled-looking
  field (e.g. the Mimirheim tab's MQTT section) now shows a proper label
  and appears grouped into Basic/Advanced sections rather than a flat list
  with raw field names.

---

## Commit

```bash
git add mimirheim/config/schema.py \
        mimirheim_helpers/common/helper_common/config.py \
        mimirheim_helpers/scheduler/scheduler/config.py \
        mimirheim_helpers/reporter/reporter/config.py \
        mimirheim_helpers/baseload/homeassistant/baseload_ha/config.py \
        mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/config.py \
        mimirheim_helpers/baseload/static/baseload_static/config.py \
        mimirheim_helpers/pv/open-meteo/pv_openmeteo/config.py \
        mimirheim_helpers/pv/forecast.solar/pv_fetcher/config.py \
        mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/config.py \
        mimirheim_helpers/prices/zonneplan/zonneplan_prices/config.py \
        mimirheim_helpers/prices/epexpredictor/epexpredictor_prices/config.py \
        mimirheim_helpers/prices/nordpool/nordpool/config.py \
        tests/unit/test_schema_x_mimir_annotations.py
git commit -m "feat(config-editor-v2): migrate ui_label/ui_group into the x-mimir- namespace

Adds x-mimir-label and x-mimir-group alongside every existing
ui_label/ui_group annotation across mimirheim's core config schema
and every registered helper's config schema, so config-editor-v2's
already-built adapter (step 71_4) can render the same
already-reviewed labels and basic/advanced grouping v1 has used all
along. v1's config-editor is unaffected: ui_label/ui_group are left
exactly as they were, not renamed or removed.
"
```
