# Step 71 (part 3 of 5) — All-or-nothing validation and atomic, comment-preserving save

## Purpose

This step implements the save path's core guarantee: every registered
configuration is validated against its real Pydantic model before any file
is written, validation failures are attributed to the specific registry
entry and field they came from, and each file that does get written is
written atomically with existing comments preserved. No HTTP server exists
yet — this step operates on a plain in-memory mapping of registry entry to
submitted data, exercised directly by tests.

This is the step where JSON Schema stops being treated as authoritative.
Every value that reaches validation in this step's tests has already passed
through 71_1's dispatch and 71_2's `nullable-list` transform; this step's
job is what happens next: real Pydantic validation, attributed errors, and
the write itself.

---

## References

- `mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md` — sections
  "Why validation always goes through the real Pydantic model" and "Save
  semantics"
- `mimirheim_helpers/config_editor/config_editor/server.py` (v1),
  `_write_yaml_preserving_comments` — reference implementation of
  comment-preserving atomic YAML writes using `ruamel.yaml`. Read this
  function before writing the new one; config-editor-v2 may reuse the same
  approach but must not import from v1's package.
- `plans/71_1_registry_and_adapter_dispatch.md`,
  `plans/71_2_nullable_list_transform.md` — must be complete first
- AGENTS.md — exception handling rule (catch the most specific type, never a
  bare `except Exception` without re-raising or logging with traceback)

---

## Files to create

```
mimirheim_helpers/config_editor_v2/config_editor_v2/
    save.py       — validate_all(), write_all(), SaveError / FieldError types

tests/unit/test_save_validation.py
```

## Files to modify

- `pyproject.toml` — add `ruamel.yaml` as a dependency of a not-yet-declared
  `config-editor-v2` extra. Declaring the extra itself, and wiring the
  package into `[tool.hatch.build.targets.wheel]`, is deferred to 71_5 when
  the package becomes runnable end to end; this step only needs the library
  installed in the dev environment to write and run its tests. Confirm with
  the user before adding a dependency-groups entry versus a new extra if the
  correct placement is ambiguous once you reach this point.

---

## `save.py`

```python
class FieldError(BaseModel):
    """One validation failure, attributed to its source.

    Attributes:
        entry_name: The RegistryEntry.name this error came from.
        loc: The Pydantic error location tuple, as strings, identifying the
            specific field path within that entry's model.
        message: The human-readable error message from Pydantic.
    """
    model_config = ConfigDict(extra="forbid")

    entry_name: str
    loc: list[str]
    message: str


def validate_all(
    entries: list[RegistryEntry],
    submitted: dict[str, dict[str, Any]],
) -> tuple[dict[str, BaseModel], list[FieldError]]:
    """Validates every registered entry's submitted data against its model.

    `submitted` maps a RegistryEntry.name to that entry's data dict, already
    run through the adapter's incoming-data transform. An entry with no key
    in `submitted` is validated against its model's own defaults, per
    IMPLEMENTATION_DETAILS.md's requirement that an untouched configuration
    must still validate successfully.

    Returns:
        A tuple of (validated models keyed by entry name, accumulated field
        errors). If the error list is non-empty, the validated-models dict
        must be treated as unusable for writing — every entry is validated,
        but nothing is written, even the entries that individually passed.

    Raises:
        ImportError: Propagated from `resolve_model` if a registry entry's
            model cannot be imported. This is a configuration bug in the
            registry itself, not a user validation failure, and must not be
            caught and converted into a FieldError.
    """


def write_all(
    validated: dict[str, BaseModel],
    entries: list[RegistryEntry],
    config_dir: Path,
) -> None:
    """Writes every validated model to its registered YAML file, atomically.

    Each file is written independently: `model_dump()` to a dict, merged
    into the existing on-disk YAML via ruamel.yaml's round-trip loader (so
    existing comments and key order survive), written to a temp file in the
    same directory, then renamed over the target. A failure partway through
    writing one file must not leave that file's temp artefact behind, but
    per Save semantics, `write_all` is only ever called after `validate_all`
    has confirmed every entry passes — this function does not itself decide
    whether to write; the caller (71_5's server) is responsible for calling
    it only when the error list from `validate_all` is empty.
    """
```

`validate_all` must catch `pydantic.ValidationError` specifically and
convert it to `FieldError` entries — no other exception type is caught here,
per AGENTS.md's exception handling rule. An `ImportError` from a broken
registry entry is a different failure mode (a bug in this editor's own
configuration, not a user's invalid submission) and must propagate.

---

## Tests (`tests/unit/test_save_validation.py`)

Use the fixture models from `tests/conftest.py` (extend with a second fixture
model if needed so at least two registry entries are exercised together).

- `test_all_valid_entries_produce_no_errors` — two entries, both valid
  submissions, `validate_all` returns an empty error list and both models in
  the validated dict.
- `test_one_invalid_entry_blocks_the_other` — two entries, one with invalid
  data. `validate_all` returns a non-empty error list. Assert via `write_all`
  not being called (or via a follow-up integration-style check in 71_5) that
  no file is written for the entry that individually validated.
- `test_error_attributed_to_correct_entry_and_field` — the `FieldError` for
  a known bad field names the correct `entry_name` and the correct `loc`.
- `test_untouched_entry_validates_against_defaults` — an entry absent from
  `submitted` still appears in the validated dict with no error, using its
  model's defaults. If a fixture model needs a default-valid state added
  specifically to make this test meaningful, add it.
- `test_registry_import_error_propagates_not_swallowed` — a registry entry
  with a broken `model_path` causes `validate_all` to raise `ImportError`,
  not to add a `FieldError` and continue.
- `test_write_all_preserves_existing_comments` — write an initial YAML file
  by hand with a comment, call `write_all` with a validated model that
  changes one value, assert the comment is still present in the file
  afterward and the changed value is updated.
- `test_write_all_is_atomic_on_rename_failure` — mock the rename step to
  raise `OSError`; assert the original file on disk is unchanged (mirrors
  v1's `test_post_config_atomic_write`).

---

## Acceptance criteria

- All tests in `test_save_validation.py` pass.
- `uv run pytest` shows no regressions.
- `uv run ruff check .` is clean.
- No file is ever written by any test unless every entry's data validated
  successfully in that test.
- `save.py` has no import of anything from `mimirheim_helpers/config_editor/`
  (v1).

---

## Commit

```bash
git add mimirheim_helpers/config_editor_v2/ pyproject.toml
git commit -m "feat(config-editor-v2): validate-all-before-write-any save semantics

Adds validate_all() (real Pydantic validation per registry entry,
errors attributed to entry and field, untouched entries validated
against defaults) and write_all() (atomic, comment-preserving YAML
write via ruamel.yaml, only ever called once every entry has passed
validation).
"
```
