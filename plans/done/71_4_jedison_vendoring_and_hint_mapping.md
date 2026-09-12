# Step 71 (part 4 of 5) — Vendor Jedison/Bootstrap and the Jedison-specific hint mapping

## Purpose

This step brings in the concrete rendering library and builds the second
half of the adapter: the mapping from this editor's `x-mimir-` namespaced
hints (label, grouping key, the `nullable-list` advisory minimum, and any
others introduced so far) to the specific attributes Jedison expects on a
schema (e.g. `title`, a category/grouping property). IMPLEMENTATION_DETAILS.md
explicitly defers this mapping to "implementation detail of the
Jedison-specific half of the adapter, and is defined when that half is
built" — this step is where it is defined.

This step does not build the HTTP server or wire the registry's real
entries in; it proves the schema-transform pipeline produces a document
Jedison can render, and that the vendored assets are present and licensed
correctly. The full running service is 71_5.

A pre-implementation spike already rendered Jedison v1.21.1 against
hand-written schemas in a real browser (Playwright/Chromium) and confirmed
two load-bearing assumptions from IMPLEMENTATION_DETAILS.md: an untransformed
`anyOf` array-or-null field renders as a `<select>` switcher between two
indistinguishable, identically-labelled options — confirming the
`nullable-list` transform (71_2) is necessary, and worse than assumed if it
were skipped — and the transform's intended plain-array-no-`minItems` output
renders as an ordinary empty add/remove list editor with no forced items,
exactly as intended. This step proceeds on the design below with that
confirmation already in hand rather than as an open question.

---

## References

- `mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md` — sections
  "Rendering library" and the "specific mapping" item in "Open follow-up
  work" (this item stops being open follow-up work as of this step)
- `plans/71_1_registry_and_adapter_dispatch.md` through
  `plans/71_3_validation_and_atomic_save.md` — must be complete first

Vendor **Jedison v1.21.1** and **Bootstrap 5.3.8**. A spike (see
`brainstorm.md`-adjacent scratch work, not checked in) rendered real schemas
against these versions in a browser via Playwright before this step was
written, and confirmed the design below against actual rendered output, not
documentation alone. Bootstrap 5.3.8 is not an explicitly stated Jedison
requirement — Jedison has no runtime dependency on Bootstrap — it is the
version resolved from Jedison v1.21.1's own `bootstrap: "^5.3.0"`
devDependency lockfile at that release. Treat it as the confirmed-working
pairing, not a documented contract; re-verify if Jedison is ever upgraded.

---

## Files to create

```
mimirheim_helpers/config_editor_v2/config_editor_v2/
    jedison_mapping.py     — x-mimir- to Jedison attribute mapping
    static/
        vendor/
            jedison/
                jedison.umd.js
                LICENSE
                VERSION
            bootstrap/
                bootstrap.min.css
                bootstrap.min.css.map
                bootstrap.bundle.min.js
                bootstrap.bundle.min.js.map
                LICENSE
                VERSION

tests/unit/test_jedison_mapping.py
```

The vendored footprint is smaller than a typical "distribution" implies.
Jedison ships as a single dependency-free UMD file (`dist/umd/jedison.umd.js`
from the npm `jedison` package) and defines no CSS of its own — it emits
Bootstrap-classed markup directly (`btn-primary`, `data-bs-toggle`, etc.)
rather than shipping a theme. There is no separate "Jedison CSS" to vendor.
Bootstrap contributes its own two standard flat files.

---

## Vendoring

Both libraries are checked into `static/vendor/` as-is, each alongside the
license file it ships under, per IMPLEMENTATION_DETAILS.md's requirement
that redistribution terms be satisfied without depending on the vendored
library's original hosting remaining available. Do not fetch either library
from a CDN or package registry at runtime. Load order in `index.html` (71_5)
matters: Bootstrap's CSS first, then `jedison.umd.js`, then Bootstrap's JS
bundle (or after — Jedison does not execute at load time, only when invoked
against a schema), confirmed working via a plain `<script>`/`<link>` tag
`file://` load with no build step.

Record the exact version vendored in each library's `VERSION` file so a
future upgrade knows what it is replacing.

---

## `jedison_mapping.py`

```python
def to_jedison_schema(field_schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrites x-mimir- hints on an already-adapter-transformed field schema
    into the concrete attributes Jedison's schema-driven form renderer reads.

    This function runs after `adapter.transform_schema` (71_1) and any
    field-specific transform such as `nullable-list` (71_2) have already
    applied. It performs no domain-specific logic of its own — it only
    renames or restructures already-produced x-mimir- keys into Jedison's
    vocabulary. A field with no recognised x-mimir- key is passed through
    unchanged; an unrecognised x-mimir- key is left in place rather than
    dropped, so a future mapping addition is additive.
    """
```

The spike confirmed Jedison's actual schema vocabulary against its source
(`src/editors/object-categories.js`, `src/helpers/schema.js`) and against
real rendered output:

| Concept | Jedison's own key | Notes |
|---|---|---|
| Field display label | plain JSON Schema `title` | Native, no `x-` key at all. |
| Non-enforcing help text | plain JSON Schema `description` | Rendered as `<small class="jedi-description">` under the control. |
| Group fields into sections | `x-format` on the parent object: `"categories-horizontal"` or `"categories-vertical"` | Set once per object schema, not per field. |
| Which group a field belongs to | `x-category` on the child field | Falls back to the field's own title, or a default "Basic" bucket, if absent. |
| Group ordering (optional) | `x-categoryOrder` on the parent: array of category names | |
| Group label override (optional) | `x-categoriesDefaultLabel` on the parent | |

This step's mapping must therefore:

- Copy this editor's label hint straight into the field schema's `title` key
  (no renaming logic needed beyond assigning the value — Jedison reads
  `title` natively).
- Translate this editor's grouping hint into Jedison's two-part construct:
  set `x-format` once on the entry's top-level object schema, and
  `x-category` on each field carrying the hint.
- Write the `nullable-list` transform's advisory minimum-length hint (71_2)
  into the field's `description` key (Jedison's native help-text slot),
  appending to any existing description rather than overwriting it.

A field with no recognised x-mimir- key is passed through unchanged; an
unrecognised x-mimir- key is left in place rather than dropped, so a future
mapping addition is additive.

---

## Tests (`tests/unit/test_jedison_mapping.py`)

These tests assert on the shape of the produced dict; they do not require a
browser or DOM.

- `test_label_hint_becomes_jedison_title` — a field with the label hint
  produces a schema fragment with a `title` key equal to that value.
- `test_grouping_hint_sets_category_on_field_and_format_on_parent` — a group
  of fields sharing a grouping hint produces field schemas with matching
  `x-category` values, and the parent object schema gains
  `x-format: "categories-horizontal"` (or `"categories-vertical"`, whichever
  this step settles on as the default).
- `test_nullable_list_advisory_hint_surfaces_as_description` — a field that
  went through the `nullable-list` transform in 71_2 and carries the
  advisory minimum hint produces a schema fragment whose `description` key
  contains that number.
- `test_advisory_hint_appended_not_overwriting_existing_description` — a
  field with both a user-authored `description` and the advisory minimum
  hint retains the original text with the hint appended, not replaced.
- `test_unrecognised_x_mimir_hint_is_left_in_place` — a field with an
  invented `x-mimir-` key not covered by this step's mapping keeps that key,
  unmodified, in the output.
- `test_non_x_mimir_hint_untouched` — a non-namespaced hint intended for
  Jedison directly (per the Namespace convention, these are assumed to
  already be in Jedison's vocabulary) passes through unchanged.
- `test_vendored_jedison_license_file_present` — a plain filesystem check
  that `static/vendor/jedison/` contains a license file.
- `test_vendored_bootstrap_license_file_present` — same, for Bootstrap.

---

## Acceptance criteria

- All tests in `test_jedison_mapping.py` pass.
- `uv run pytest` shows no regressions.
- `uv run ruff check .` is clean.
- Vendored Jedison and Bootstrap assets are present under `static/vendor/`,
  each with its license file, and neither is referenced via a CDN URL
  anywhere in the codebase.
- The "specific mapping from x-mimir- hints to Jedison attributes" item in
  IMPLEMENTATION_DETAILS.md's "Open follow-up work" section is resolved by
  this step; update that document to move the item out of "Open follow-up
  work" once the mapping is implemented and tested, per this project's rule
  that documented decisions must not silently drift out of date.

---

## Commit

```bash
git add mimirheim_helpers/config_editor_v2/ mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md
git commit -m "feat(config-editor-v2): vendor Jedison/Bootstrap, add hint-to-Jedison mapping

Vendors the Jedison form renderer and Bootstrap theme with their
license files under static/vendor/, and implements the previously
deferred mapping from this editor's x-mimir- hints to the concrete
Jedison schema attributes (title, grouping, advisory help text).
"
```
