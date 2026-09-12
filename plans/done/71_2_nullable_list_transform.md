# Step 71 (part 2 of 5) — The `nullable-list` transform

## Purpose

This step implements the one concrete adapter transform specified by
IMPLEMENTATION_DETAILS.md: `nullable-list`. It handles a field typed as
either `None` or a non-empty list (a JSON Schema `anyOf` between an
array-with-minimum and a null type), rewriting it into a plain, unenforced
array on the schema side and converting an empty submitted list back to
`None` on the data side.

This step depends on 71_1's dispatch mechanism (`register_transform`,
`transform_schema`, `transform_incoming_data`) and adds nothing to the
registry or to any server. It remains pure Python, unit-tested without a
browser or rendering library.

---

## References

- `mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md` — section
  "The `nullable-list` transform" (read this section in full before writing
  any code; it is the exact specification for this step)
- `plans/71_1_registry_and_adapter_dispatch.md` — must be complete first
- AGENTS.md — complete type annotations, Google-style docstrings

---

## Files to create

```
mimirheim_helpers/config_editor_v2/config_editor_v2/
    transforms.py       — nullable_list_transform, registered under "nullable-list"

tests/unit/test_nullable_list_transform.py
```

## Files to modify

- `mimirheim_helpers/config_editor_v2/config_editor_v2/__init__.py` or a
  small module-init routine — must call `register_transform("nullable-list",
  ...)` at import time, so any code that imports the package has the
  transform available without an explicit registration call.
- `tests/conftest.py` — add a fixture model with a field typed
  `list[str] | None` with `min_length=2` on the array branch and
  `x-mimir-adapter: "nullable-list"` in `json_schema_extra`, matching the
  `anyOf` shape the transform is designed to rewrite.

---

## `transforms.py` — schema-side behaviour

Given the field's raw JSON Schema fragment (the `anyOf` between an
array-with-minimum and null):

1. Replace the `anyOf` with a plain `{"type": "array", "items": ...}` schema,
   with no `minItems` key.
2. If the original array branch declared `minItems > 0`, carry that number
   forward as `x-mimir-min-length-hint` (or equivalent `x-mimir-` key) in the
   rewritten schema, per the Namespace convention. Do not set it as the
   rewritten schema's own `minItems`.
3. If the original array branch declared no minimum, or a minimum of zero,
   omit the hint key entirely rather than writing a zero.

Write the constraint and its rationale as an inline comment, per AGENTS.md's
"comment every non-trivial constraint" rule: the comment must state that this
is an advisory-only hint, and either explain in three lines or fewer why
enforcement is deliberately dropped at this layer, or point to the
"nullable-list" subsection of `IMPLEMENTATION_DETAILS/NN_slug.md` — for a
helper without a root IMPLEMENTATION_DETAILS.md of its own, point instead at
the "The `nullable-list` transform" section of this helper's own
`IMPLEMENTATION_DETAILS.md`.

## `transforms.py` — data-side behaviour

Given a submitted value for the field:

- An empty list (`[]`) is converted to `None`.
- Any non-empty list is passed through unchanged.
- `None` is passed through unchanged (a field that was never touched).

This conversion must happen before the real Pydantic model sees the value —
it is what makes an untouched list editor equivalent, from the model's point
of view, to never having set the field, per IMPLEMENTATION_DETAILS.md's
explanation of the alternative-pair validation problem this solves.

---

## Tests (`tests/unit/test_nullable_list_transform.py`)

Cover both directions and the specific failure mode the transform exists to
prevent:

- `test_schema_anyof_replaced_with_plain_array` — the rewritten schema has no
  `anyOf` key and `type == "array"`.
- `test_schema_min_items_not_enforced_after_transform` — the rewritten schema
  has no `minItems` key, even though the source declared one.
- `test_schema_min_length_hint_preserved_as_advisory` — the rewritten schema
  carries the original minimum under the `x-mimir-` advisory key, with the
  correct integer value.
- `test_schema_no_hint_when_source_has_no_minimum` — a source field with no
  minimum produces a rewritten schema with no advisory hint key at all.
- `test_data_empty_list_converted_to_none` — submitting `[]` produces `None`.
- `test_data_nonempty_list_passed_through` — submitting `["a", "b"]` produces
  `["a", "b"]` unchanged.
- `test_data_none_passed_through` — submitting `None` produces `None`.
- `test_alternative_pair_empty_untouched_field_validates` — the scenario
  IMPLEMENTATION_DETAILS.md describes directly: build a fixture Pydantic
  model with two nullable-list fields validated as an exactly-one-of pair
  (a `model_validator` checking `is not None` on exactly one). Submit data
  with one field populated and the other as an empty list from the form.
  Run both fields through `transform_incoming_data`, then
  `model_validate`. Assert validation succeeds. This is the test that would
  fail without the empty-list-to-None conversion — write it to fail first
  against a stub that skips the conversion, confirm the failure, then
  implement the conversion.
- `test_short_nonempty_list_not_rejected_by_transform` — a list shorter than
  the real minimum (e.g. one item where two are required) passes through the
  data transform unchanged; the transform does not enforce it. This
  documents the accepted trade-off from IMPLEMENTATION_DETAILS.md — the real
  Pydantic model's own validation is what catches this, not this transform,
  and that later check is out of scope for this step's tests.

---

## Acceptance criteria

- All tests in `test_nullable_list_transform.py` pass.
- `test_alternative_pair_empty_untouched_field_validates` was observed to
  fail before the data-side conversion was implemented, and passes after.
- `uv run pytest` shows no regressions.
- `uv run ruff check .` is clean.
- No grouped "choice control" UI concept is introduced — both fields in an
  alternative pair remain independently transformed and independently
  rendered, per the explicit non-goal in IMPLEMENTATION_DETAILS.md.

---

## Commit

```bash
git add mimirheim_helpers/config_editor_v2/
git commit -m "feat(config-editor-v2): implement the nullable-list adapter transform

Rewrites a None-or-non-empty-list field's anyOf schema into a plain
array with an advisory (non-enforced) minimum-length hint, and
converts an empty submitted list back to None before validation so
that leaving a list editor empty is equivalent to never setting the
field.
"
```
