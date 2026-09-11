# Step 71 (part 1 of 5) — Registry and adapter transform dispatch

## Purpose

This step builds the two foundational, rendering-library-agnostic pieces that
every later part of config-editor-v2 depends on:

1. **The registry** — a static, hand-maintained list of configuration files
   this editor can produce, each entry naming a YAML filename and a fully
   qualified import path to the Pydantic model that owns it.
2. **The adapter's transform dispatch mechanism** — the part of the adapter
   that reads a field's `json_schema_extra["x-mimir-adapter"]` hint and routes
   that field to a named transform, with a no-op passthrough for fields that
   carry no such hint.

No concrete transform is implemented in this step beyond a trivial identity
transform used to prove dispatch works. The `nullable-list` transform is
step 71_2. No HTTP server, no rendering library, and no save logic exist yet.
This step is pure Python, fully unit-testable without a browser or a running
process.

---

## References

- `mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md` — sections
  "Registry", "The adapter", "Transform dispatch", "Namespace convention"
- `mimirheim_helpers/config_editor/config_editor/server.py` (v1) — structural
  reference only; config-editor-v2 does not import from or depend on v1
- AGENTS.md — `extra="forbid"` rule, complete type annotations, Google-style
  docstrings

config-editor-v2 is a separate helper from `mimirheim_helpers/config_editor/`
(v1). It shares no code, process, or registry state with v1, per the
Purpose section of its IMPLEMENTATION_DETAILS.md. Do not import from v1.

---

## Files to create

```
mimirheim_helpers/config_editor_v2/
    __init__.py
    config_editor_v2/
        __init__.py
        registry.py       — RegistryEntry model, REGISTRY list, resolution helper
        adapter.py         — transform dispatch, transform registration mapping,
                              identity/passthrough behaviour
    tests/
        __init__.py
        conftest.py         — fixture Pydantic models used across all test files
        unit/
            __init__.py
            test_registry.py
            test_adapter_dispatch.py
```

Do not create a `pyproject.toml` extra, a `server.py`, or any `static/`
assets in this step. Those belong to later parts (71_4, 71_5).

---

## `registry.py`

```python
class RegistryEntry(BaseModel):
    """Describes one top-level configuration file this editor can produce.

    Attributes:
        name: Human-readable name shown in the editor's navigation.
        filename: The YAML filename this entry writes to, relative to the
            configured config directory.
        model_path: Fully qualified dotted import path to the Pydantic model
            class that defines and validates this file's contents, e.g.
            "mimirheim.config.schema.MimirheimConfig".
    """
    model_config = ConfigDict(extra="forbid")

    name: str
    filename: str
    model_path: str


def resolve_model(entry: RegistryEntry) -> type[BaseModel]:
    """Imports and returns the Pydantic model class named by an entry.

    Raises:
        ImportError: If the module in `entry.model_path` cannot be imported.
        AttributeError: If the module does not define the named class.
        TypeError: If the resolved attribute is not a BaseModel subclass.
    """
```

`REGISTRY: list[RegistryEntry]` is a plain module-level list, hand-edited to
add a new configuration source. This step ships it empty or with a single
placeholder entry pointing at a fixture model in the test suite's own
package — do not register `mimirheim.config.schema.MimirheimConfig` yet.
Wiring real mimirheim and helper configs into `REGISTRY` is deferred to
71_5, once the full save path exists and can be exercised against a real
model without partial support.

`resolve_model` must re-raise `ImportError`/`AttributeError` rather than
swallowing them into a generic exception, per AGENTS.md's exception handling
rule: a broken registry entry must surface exactly why resolution failed.

---

## `adapter.py`

```python
Transform = ...  # a small protocol or dataclass, not yet used for anything
                  # beyond the identity case in this step

_TRANSFORMS: dict[str, Transform] = {}


def register_transform(name: str, transform: Transform) -> None:
    """Adds a transform to the dispatch table under a chosen name."""


def transform_schema(field_schema: dict[str, Any]) -> dict[str, Any]:
    """Applies the field's x-mimir-adapter transform to its schema fragment.

    A field with no `x-mimir-adapter` key in `json_schema_extra` is returned
    unchanged except that all `x-mimir-` keys are left in place for a later
    stage to consume (see 71_4) and all non-`x-mimir-` keys are passed
    through untouched, per the Namespace convention section of
    IMPLEMENTATION_DETAILS.md.

    Raises:
        KeyError: If `x-mimir-adapter` names a transform that was never
            registered via `register_transform`.
    """


def transform_incoming_data(field_schema: dict[str, Any], value: Any) -> Any:
    """Applies the field's transform in the submit -> validate direction.

    Must be the exact inverse of `transform_schema`'s structural change, per
    IMPLEMENTATION_DETAILS.md's requirement that data run through the
    transform is acceptable input to the real Pydantic model's validation.
    """
```

This step registers exactly one transform for test purposes: an identity
transform (`register_transform("identity", ...)`) that leaves schema and data
unchanged. It exists only to prove the dispatch table works end to end before
`nullable-list` (71_2) is built against the same mechanism. Do not implement
`nullable-list` here.

A field with no `x-mimir-adapter` hint must not raise `KeyError` — that is
the passthrough path, and it is the default for the overwhelming majority of
fields on any real model.

---

## Tests

### `tests/conftest.py`

Defines two or three small fixture Pydantic models used by both test files
in this step and reused by 71_2 and 71_3:

- A model with a plain field carrying no `x-mimir-` hints at all.
- A model with a field carrying `x-mimir-adapter: "identity"`.
- A model with a field carrying an unregistered transform name, e.g.
  `x-mimir-adapter: "does-not-exist"`, used only to exercise the error path.

### `tests/unit/test_registry.py`

- `test_resolve_model_imports_class` — a `RegistryEntry` pointing at a
  fixture model resolves to that exact class.
- `test_resolve_model_raises_on_missing_module` — a bogus module path raises
  `ImportError`.
- `test_resolve_model_raises_on_missing_attribute` — a valid module path with
  a class name that does not exist raises `AttributeError`.
- `test_registry_entry_rejects_unknown_fields` — `RegistryEntry.model_validate`
  raises on an unexpected keyword, proving `extra="forbid"` is set.

### `tests/unit/test_adapter_dispatch.py`

- `test_field_without_hint_passes_through_unchanged` — schema and data for a
  hint-free field are returned identical to the input.
- `test_field_with_identity_transform_dispatches` — a field with
  `x-mimir-adapter: "identity"` is routed through the registered identity
  transform (assert via a transform that tags its output, not a true no-op,
  so the test can distinguish "dispatched" from "passed through").
- `test_unregistered_transform_name_raises_key_error` — a field naming a
  transform that was never registered raises `KeyError`, not a silent
  passthrough.
- `test_non_mimir_hints_pass_through_untouched` — a field with an
  `x-mimir-adapter` hint alongside an unrelated rendering-library hint (e.g.
  `"someLibraryOption": true`) retains that unrelated key in the transformed
  schema.

---

## Acceptance criteria

- All tests in `test_registry.py` and `test_adapter_dispatch.py` pass.
- `uv run pytest` shows no regressions in the rest of the suite.
- `uv run ruff check .` is clean.
- `REGISTRY` is empty or contains only test-fixture entries; no real
  mimirheim or helper model is registered yet.
- `adapter.py` exposes no server, no static assets, and no dependency on any
  rendering library — importing it requires only Pydantic and the standard
  library.

---

## Commit

```bash
git add mimirheim_helpers/config_editor_v2/
git commit -m "feat(config-editor-v2): add registry and adapter transform dispatch

Establishes the two foundational pieces config-editor-v2 builds on:
a hand-maintained registry of importable Pydantic models, and the
adapter's x-mimir-adapter dispatch table with passthrough for
unhinted fields. No concrete transform, server, or rendering library
is wired in yet.
"
```
