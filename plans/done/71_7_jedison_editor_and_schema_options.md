# Step 71 (part 7 of 7) — Investigate Jedison's editors and options, apply the appropriate ones

## Purpose

Steps 71_1 through 71_6 built the schema pipeline (registry, adapter,
`nullable-list`, the Jedison hint mapping, save semantics, the HTTP
service, and the label/grouping migration) and fixed two concrete rendering
defects found by hand-testing (`Jedison.ThemeBootstrap5` instead of the
unstyled base `Theme`, and `x-objectAdd: False` on closed objects). Beyond
label, grouping, and those two fixes, config-editor-v2 has not yet made any
deliberate choice about which of Jedison's many editors and options best
suit each field *shape* in this project's real models. Every field today
renders through whatever Jedison's schema-shape defaults happen to produce.

This step is explicitly investigate-first: read Jedison's own editor and
options documentation directly, cross-check anything uncertain against the
vendored bundle's actual source (the technique demonstrated below), record
the findings as the permanent reference in
`mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md`, and apply a
bounded, high-confidence set of improvements identified by that
investigation. This is a first pass, not an exhaustive one: per the
project's own expectation, individual fields will be revisited later as
real usage surfaces specific rendering problems. Do not attempt to
hand-tune every field in every model; prioritize the shapes that recur
across many fields (so one decision improves many fields at once) and the
shapes most central to this project's actual data (the named-map device
fields — batteries, PV arrays, EV chargers, and so on — are this project's
single most important editing surface).

---

## References

- `mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md` — read the
  whole document again before starting; you will be adding a new section to
  it. In particular re-read "The adapter" and "Rendering library" (this
  step's findings extend, not replace, the latter).
- `mimirheim_helpers/config_editor_v2/config_editor_v2/jedison_mapping.py`
  — the Jedison-specific half of the adapter. Any new hint-translation
  logic this step adds belongs here, following the existing pattern
  (`to_jedison_schema` for per-field concerns, `to_jedison_object_schema`
  for whole-schema concerns such as grouping and, now, `x-objectAdd`).
- `mimirheim_helpers/config_editor_v2/config_editor_v2/static/vendor/jedison/jedison.umd.js`
  — the vendored bundle. When documentation is ambiguous or you are not
  fully confident in an option name, grep this file directly for the
  literal string before relying on it (see "Verifying against the vendored
  bundle" below) — this is exactly how `Jedison.ThemeBootstrap5` and
  `x-objectAdd` were confirmed during this project's own hand-testing.
- `plans/71_1_registry_and_adapter_dispatch.md` through
  `plans/71_6_migrate_ui_hints_to_x_mimir_namespace.md` (in `plans/done/`)
  — must be complete first.
- AGENTS.md — complete type annotations, Google-style docstrings,
  `extra="forbid"`, specific exception handling, no unsolicited scope
  creep. This step must not hand-tune fields beyond what its own
  investigation justifies.

### Jedison documentation to read directly

Fetch and read these pages yourself before making any decision; do not rely
on secondhand summaries, including any produced by an LLM tool call, for
the exact option names and default values you plan to use in code — verify
each one you actually apply against either the live doc page or the
vendored bundle's own source.

- `https://germanbisurgi.github.io/jedison-docs/options` — form-wide
  config options passed to `Jedison.Create({...})`.
- `https://germanbisurgi.github.io/jedison-docs/schema-options` — the full
  `x-*` schema-level vocabulary (grouping, object-add, array add/delete/
  move/sort, enum titles, collapsing, grid layout, and more).
- `https://germanbisurgi.github.io/jedison-docs/themes` — theme selection
  (already resolved: `Jedison.ThemeBootstrap5`).
- The editor reference pages, one family per JSON Schema type — each family
  has a `-default` page and several sibling `-<variant>` pages; read the
  `-default` page for each type your investigation touches, and any
  specific variant page relevant to a decision you make:
  - `string-editor-default` (siblings include `textarea`, `select`,
    `radio`, `radio-inline`, and format-plugin variants such as
    `flatpickr`, `milkdown` — most are irrelevant to this project's plain
    config fields; do not add a dependency on a plugin format without a
    concrete field that needs it)
  - `boolean-editor-default` (siblings: `checkbox`, `select`, `radios`,
    `radios-inline`)
  - `number-editor-default` (siblings: `range`, `select`, `radios`,
    `nullable`)
  - `object-editor-default` (siblings include `categories-vertical`,
    `categories-horizontal`, `grid`, `accordion`, `radios`,
    `radios-inline` — categories-vertical is already this project's chosen
    default per step 71_4; confirm nothing here changes that)
  - `array-editor-default` (siblings include `table`, `table-object`,
    `tuple`, `nav-vertical`, `nav-horizontal`, checkbox-based variants)
  - `null-editor-default`

### Verifying against the vendored bundle

The bundle is minified but not obfuscated beyond variable renaming — string
literals (option names, CSS classes) survive intact and are greppable:

```bash
grep -o "ThemeBootstrap[0-9]*" mimirheim_helpers/config_editor_v2/config_editor_v2/static/vendor/jedison/jedison.umd.js | sort -u
grep -o "x-[a-zA-Z]*" mimirheim_helpers/config_editor_v2/config_editor_v2/static/vendor/jedison/jedison.umd.js | sort -u
grep -o "form-control\|form-check\|form-select\|btn-[a-zA-Z-]*" mimirheim_helpers/config_editor_v2/config_editor_v2/static/vendor/jedison/jedison.umd.js | sort -u
```

Use this whenever a doc page is unclear about the exact key spelling
(`x-format` vs a type-specific key, `x-arrayTable` as a boolean flag vs a
value for `x-format`, and so on) — do not guess and do not ship an option
name you have not confirmed appears in the bundle or is explicitly
documented with a code example.

---

## Investigation deliverable: a new IMPLEMENTATION_DETAILS.md section

Add a new subsection under "Rendering library" (or promote it to its own
top-level section if it reads better once written — your call), something
like "Editor and option selection reference". This is the durable record
future field-level work should consult instead of re-researching Jedison
from scratch. For each JSON Schema shape this project's real models
actually contain, record: which editor Jedison selects by default, whether
this project overrides it (and with which exact `x-*` key and value), and
why. Keep entries for shapes you decided *not* to override too, so a future
reader knows that was a deliberate decision, not an oversight.

At minimum, cover:

- Plain object (a closed, `extra="forbid"` record) — categories-vertical
  grouping (71_4) plus `x-objectAdd: False` (already applied, hand-testing
  fix) — confirm and cross-reference, no new decision needed here.
- A dict-typed field with named entries (`dict[str, SomeConfig]` — the
  `batteries`, `pv_arrays`, `ev_chargers`, and similar fields on
  `MimirheimConfig`, and the analogous named-map fields on some helper
  configs). This is the priority investigation target — see below.
- Plain string, plain number/integer, plain boolean — confirm the default
  editors are adequate as-is, or identify a concrete field type in this
  project's real models that benefits from a variant (for example: is
  there any field long enough to warrant `textarea`? Is there any
  genuinely secret-like field, such as `mqtt.password`, that should mask
  its input?).
- `enum`/`Literal`-typed fields (check whether any real model has one —
  grep `Literal\[` across the files listed in step 71_6's Scope section —
  and confirm whether Jedison already renders these as a `<select>` with
  no extra hint needed, which is the common default behavior for
  JSON-Schema-driven form libraries).
- Numeric fields with `ge`/`le`/`gt`/`lt` constraints — confirm whether
  Jedison already reflects these as native HTML `min`/`max` attributes by
  default, or whether `x-useConstraintAttributes` (mentioned in the
  schema-options reference) needs to be turned on, and if so, where (a
  global `Create()` option in `app.js`, versus a per-field hint — prefer
  the global option if it is safe to enable universally, to avoid needing
  a per-field hint on every numeric field in every model).
- The `nullable-list` transform's output shape (a plain array with an
  advisory `x-mimir-min-length-hint`, from step 71_2) — confirm it still
  renders via the plain array-default editor sensibly now that grouping
  and `x-objectAdd` are also in play; this should need no new work, but
  confirm rather than assume.

### Priority: the named-map (dict-typed) device fields

`MimirheimConfig.batteries` and its siblings are this project's most
important editing surface, and the one this step should invest the most
investigation effort in. Each is `dict[str, SomeConfig]`, rendered by
Pydantic as `{"type": "object", "additionalProperties": {"$ref": ...}}` —
no fixed `properties`, every key is user-chosen. Determine:

- Which Jedison editor actually handles this shape (a schema with
  `additionalProperties` as a schema and no `properties` at all is not the
  same shape as the closed-record objects `x-objectAdd` targets — confirm
  this empirically, since `additionalProperties` as a schema was
  deliberately left outside `x-objectAdd`'s scope in the hand-testing fix
  precisely because it needs its own "add a new named entry" affordance,
  not the closed-record "Add property" button).
- Whether Jedison prompts the user for the new key's name in a way that
  reads clearly out of the box, or whether an option exists to customize
  that prompt.
- Whether v1's own `ui_instance_name_description` convention (present on
  every named-map model — see
  `tests/unit/test_schema_ui_annotations.py::test_named_map_model_has_instance_name_description`
  for the full list of models that carry it, and grep each model's own
  `model_config = ConfigDict(json_schema_extra={"ui_instance_name_description": ...})`
  for the actual text) has a Jedison equivalent worth wiring up (for
  example, a hint that customizes the "add" prompt's own label or help
  text). If Jedison has no such hook, say so explicitly in the new
  IMPLEMENTATION_DETAILS.md section rather than silently skipping it, so a
  future reader knows this was checked and found unsupported, not
  forgotten. Migrating `ui_instance_name_description` into a new
  `x-mimir-` hint (with `jedison_mapping.py` translating it into whatever
  Jedison hook you find, if any) is in scope for this step if the
  investigation finds a real hook to wire it to.

---

## Implementation

Whatever concrete decisions the investigation above produces, apply them:

- New `x-*` hints that are genuinely global (apply the same way to every
  field of a given shape, with no per-model judgment involved — for
  example, turning on a numeric-constraint option for every number field)
  belong as a `Jedison.Create({...})` option in
  `mimirheim_helpers/config_editor_v2/config_editor_v2/static/app.js`,
  next to the existing `theme`/`objectAdd` comments explaining prior
  decisions there.
- New hint *translations* (this project's own `x-mimir-` source hint
  mapped to a Jedison-native destination key, following the existing
  `x-mimir-label`/`x-mimir-group` pattern) belong in
  `jedison_mapping.py`, as either an extension of `to_jedison_schema` (a
  per-field concern) or `to_jedison_object_schema` (a whole-schema
  concern), matching whichever of those two functions' existing docstrings
  best fits the new concern.
- If the investigation identifies specific real fields that need a
  model-level change (for example, an explicit `x-mimir-` hint on
  `mqtt.password` to request masking, since that decision is inherently
  per-field, not a blanket rule for every string field), make that change
  directly in the relevant `config.py` (or `mimirheim/config/schema.py`),
  in the same `json_schema_extra` dict already touched by step 71_6, using
  the same non-destructive add-alongside-existing-keys approach.

Do not add a hint-translation code path for a Jedison option you have not
both confirmed exists (per "Verifying against the vendored bundle" above)
and identified a concrete field in this project's real models that needs
it. An unused, speculative option is scope creep, not a decent base.

---

## Tests

### `mimirheim_helpers/config_editor_v2/tests/unit/test_jedison_mapping.py`

For every new hint-translation function or extension you add to
`jedison_mapping.py`, add fixture models to
`mimirheim_helpers/config_editor_v2/tests/conftest.py` and tests following
the file's existing pattern (see the `x-objectAdd` tests added during
hand-testing for the most recent example of this pattern: a fixture model
exercising the "should apply" case, one exercising the "should not apply"
case, and a schema-shape assertion, not a rendered-DOM assertion, since
this test file works entirely at the schema level).

### `tests/unit/test_server.py` and `tests/unit/test_registry_real_entries.py`

If any change affects the schema `GET /api/entries` returns for a real
registered model (for example, a new hint applied to `mqtt.password`),
add or extend a test there confirming the real, live schema reflects it —
not just a fixture model.

### Manual verification

Schema-shape assertions prove the JSON this editor produces is correct;
they do not prove Jedison actually renders it as intended. Before
considering this step done, run the real service
(`uv run python -m config_editor_v2 --config <path>`, against a scratch
config directory, never the real repository or `/config`) and visually
confirm, in a real browser, at least: the named-map fields render with a
usable add/remove/name flow, and any password-masked field actually masks
its input.

Real-browser verification tooling is available in this environment and
should be used, not treated as optional: check for an MCP Playwright tool
first (`ToolSearch` for "playwright" — if present, prefer it), and fall
back to the `playwright-cli` binary directly (confirmed present on PATH,
`playwright-cli --version`) if no MCP tool is available in your session.
Either lets you load the running service, click through a named-map field's
add/remove flow, and inspect the rendered DOM/computed styles for a
password field, the same way this project's original pre-71_4 Jedison spike
used Playwright to confirm the `nullable-list` transform's necessity before
that step was written. Do not skip this and rely on schema-shape assertions
alone — the theme and `x-objectAdd` defects fixed during hand-testing were
both invisible to schema-shape tests and only surfaced by looking at the
actual rendered page.

---

## Acceptance criteria

- A new IMPLEMENTATION_DETAILS.md section documents, for every schema
  shape investigated, what Jedison does by default, what (if anything) this
  project overrides, and why — including shapes deliberately left at
  Jedison's default.
- The named-map (dict-typed) device fields have a confirmed, sensible
  add/name/remove editing flow — this is the priority deliverable of this
  step.
- Any new `x-*` option or hint-translation added is both confirmed to
  exist in the vendored bundle or documented with a code example, and
  applied to a concrete, identified real field or field shape — no
  speculative additions.
- `uv run pytest mimirheim_helpers/config_editor_v2/tests -q` passes,
  including any new tests this step adds.
- `uv run pytest -q` (full suite) shows no regressions.
- `uv run ruff check .` is clean.
- No file outside `mimirheim_helpers/config_editor_v2/` is modified unless
  the investigation specifically justifies a per-field hint on a real
  model (for example, `mqtt.password`) — and any such change follows step
  71_6's non-destructive, add-alongside-existing-keys approach.

---

## Commit

Stage exactly the files this step's investigation led you to change (likely
a subset of: `jedison_mapping.py`, `static/app.js`,
`IMPLEMENTATION_DETAILS.md`, `tests/conftest.py`,
`tests/unit/test_jedison_mapping.py`, and possibly one or two `config.py`
files for a specific per-field hint such as password masking). Do not
`git add` broadly; list the files explicitly, matching the pattern of every
prior step's commit section.

```bash
git commit -m "feat(config-editor-v2): apply Jedison editor/option choices from a documented investigation

Adds an editor/option selection reference to IMPLEMENTATION_DETAILS.md
covering how Jedison renders each schema shape in this project's real
models, and applies the concrete, confirmed improvements that
investigation identified -- at minimum a sensible editing flow for
the named-map device fields (batteries, PV arrays, and similar).
"
```
