# Plan 56 — Config editor YAML comment preservation

## Motivation

The config editor currently uses PyYAML's `yaml.dump()` to write configuration
files after validation. This strips all YAML comments from the file, forcing
users to choose between using the GUI editor OR maintaining documentation in
their config files, but not both.

This is a significant usability issue because:
1. Users lose their carefully written documentation when they use the GUI
2. The GUI and manual editing workflows are mutually exclusive
3. Complex configurations benefit from inline comments explaining intent
4. Example configs and wiki documentation encourage comment usage

The solution is to use **ruamel.yaml** for round-trip YAML editing, which
preserves comments, formatting, and key ordering during write operations.

---

## Current behavior

**Write path in `server.py`:**
```python
# POST /api/config
data = json.loads(body)  # dict from frontend
yaml_str = yaml.dump(data, default_flow_style=False, allow_unicode=True)
write_atomically(yaml_path, yaml_str)
```

**Problem:** `yaml.dump(dict)` constructs YAML from scratch. All comments are lost.

**Example:**
```yaml
# User's config with comments
batteries:
  home_battery:
    capacity_kwh: 10.0  # Tesla Powerwall 2
    # Charge efficiency degrades above 0.8 SOC
    charge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
```

After GUI edit becomes:
```yaml
batteries:
  home_battery:
    capacity_kwh: 10.0
    charge_segments:
    - power_max_kw: 5.0
      efficiency: 0.95
```

---

## Relevant source locations

```
mimirheim_helpers/config_editor/config_editor/server.py
  — _api_post_config() (line ~535)
  — _api_post_helper_config() (line ~647)

pyproject.toml
  — [project.optional-dependencies] config-editor extra

tests/unit/test_config_editor_server.py
  — Tests for config save endpoint

mimirheim_helpers/config_editor/README.md
  — User-facing documentation
```

---

## Design decisions

### D1. Use ruamel.yaml for comment-preserving round-trip editing

**ruamel.yaml** is the standard Python library for preserving YAML structure,
comments, and formatting during read-modify-write cycles. It's actively
maintained, widely used (24M+ PyPI downloads/month), and designed specifically
for this use case.

**Alternatives considered:**
- **PyYAML with comment tracking:** Not supported. PyYAML discards comments at parse time.
- **Manual comment extraction/reinsertion:** Fragile and error-prone. Would require parsing comments as strings, tracking line numbers, handling edge cases.
- **Generate fresh comments from schema:** Users want their own comments, not auto-generated descriptions.

**Decision:** Add `ruamel.yaml>=0.18` as a dependency of the `config-editor` extra.

### D2. Deep merge strategy preserves existing structure

When the GUI sends updated config, we cannot simply replace the entire file
because that would still lose comments attached to unchanged sections.

**Strategy:**
1. Load existing YAML with ruamel.yaml (preserves comment objects)
2. Deep-merge new values into existing structure
3. Write back with ruamel.yaml

**Example:**
```yaml
# Existing file with comment
batteries:
  home_battery:
    capacity_kwh: 10.0  # Tesla Powerwall 2
    charge_segments: [...]
```

User edits `capacity_kwh: 12.0` in GUI:
```yaml
# Comment preserved!
batteries:
  home_battery:
    capacity_kwh: 12.0  # Tesla Powerwall 2
    charge_segments: [...]
```

**Deep merge logic:**
- Recursively walk the dict tree
- Update values where new data exists
- Preserve comments on unchanged lines
- Preserve key ordering (ruamel.yaml default)

### D3. Handle new keys gracefully (no comments on additions)

When the GUI adds a completely new section (e.g., adding a second battery),
there are no existing comments to preserve. This is expected and acceptable:
users can add comments manually after the fact.

The deep merge ensures that:
- Existing commented sections stay intact
- New sections are inserted with standard formatting
- Users can add comments to new sections between GUI edits

### D4. Atomic writes with temp file remain unchanged

The existing atomic write pattern (write temp file, then `os.replace()`) is
preserved. This ensures that partial writes never leave corrupted config files
if the container restarts mid-write.

The ruamel.yaml integration only changes what gets written to the temp file,
not the atomicity mechanism.

### D5. Extract to helper function for reuse

Both `_api_post_config()` (mimirheim.yaml) and `_api_post_helper_config()`
(helper YAML files) need comment preservation. Extract the round-trip write
logic to a shared helper function:

```python
def _write_yaml_preserving_comments(
    data: dict[str, Any],
    file_path: Path
) -> str:
    """Write YAML file while preserving existing comments and formatting.
    
    Returns the YAML string that was written (for logging).
    """
```

This function encapsulates:
- Loading existing file with ruamel.yaml (if it exists)
- Deep merging new data
- Writing atomically
- Returning the YAML string for logging

### D6. Graceful handling of malformed existing YAML

If the existing YAML file has syntax errors that prevent ruamel.yaml from
loading it, fall back to writing a fresh file. The user's broken YAML would
have prevented mimirheim from starting anyway, so overwriting it with valid
YAML is acceptable.

**Implementation:**
```python
try:
    existing = yaml_handler.load(file_path)
except (yaml.YAMLError, FileNotFoundError):
    # File doesn't exist or is malformed: write fresh
    existing = None
```

### D7. Preserve ruamel.yaml formatting preferences

Configure ruamel.yaml with sensible defaults that match the existing style:
- `default_flow_style=False` — use block style (multi-line)
- `preserve_quotes=True` — maintain string quoting style
- `width=4096` — prevent aggressive line wrapping
- No comment wrapping (preserve long comments as-is)

---

## TDD workflow

### Step 1 — Install dependency and import

Add to `pyproject.toml`:
```toml
config-editor = [
    "ruamel.yaml>=0.18",
]
```

Add import to `server.py`:
```python
from ruamel.yaml import YAML
```

### Step 2 — Write the helper function

Implement `_write_yaml_preserving_comments()` in `server.py`:
- Create ruamel.yaml handler with formatting preferences
- Load existing file (if it exists and is parseable)
- Deep-merge new data into existing structure
- Write atomically to temp file, then `os.replace()`
- Return YAML string for logging

### Step 3 — Write failing unit test

Add to `tests/unit/test_config_editor_server.py`:

```python
def test_config_save_preserves_comments(tmp_path):
    """Config editor preserves YAML comments on save."""
    config_yaml = tmp_path / "mimirheim.yaml"
    
    # Write initial config with comments
    config_yaml.write_text("""
# Main grid connection
grid:
  import_limit_kw: 25.0  # Utility meter limit
  export_limit_kw: 10.0  # Contract restriction
  
batteries:
  home_battery:
    capacity_kwh: 10.0  # Tesla Powerwall 2
    charge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
""")
    
    # Edit via API: change only capacity_kwh
    server = ConfigEditorServer(config_dir=tmp_path, ...)
    response = server._api_post_config(json.dumps({
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 10.0},
        "batteries": {
            "home_battery": {
                "capacity_kwh": 12.0,  # Changed from 10.0
                "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}]
            }
        },
        # ... minimal valid config
    }).encode())
    
    assert response[0] == 200  # Success
    
    # Read back and verify comments are preserved
    result = config_yaml.read_text()
    assert "# Main grid connection" in result
    assert "# Utility meter limit" in result
    assert "# Tesla Powerwall 2" in result
    
    # Verify the value was updated
    assert "capacity_kwh: 12.0" in result
```

Confirm the test **fails** with the current PyYAML implementation (comments
stripped).

### Step 4 — Replace yaml.dump() calls with helper

In `server.py`, replace both write locations:

**In `_api_post_config()`:**
```python
# Before:
yaml_str = yaml.dump(data, default_flow_style=False, allow_unicode=True)
yaml_path = self._config_dir / "mimirheim.yaml"
# [atomic write code]

# After:
yaml_path = self._config_dir / "mimirheim.yaml"
yaml_str = _write_yaml_preserving_comments(data, yaml_path)
```

**In `_api_post_helper_config()`:**
```python
# Before:
yaml_str = yaml.dump(config_dict, default_flow_style=False, allow_unicode=True)
# [atomic write code]

# After:
yaml_str = _write_yaml_preserving_comments(config_dict, fpath)
```

### Step 5 — Verify test passes

```bash
uv run pytest tests/unit/test_config_editor_server.py::test_config_save_preserves_comments -v
```

Confirm the test now **passes** with ruamel.yaml.

### Step 6 — Manual integration test

1. Start config editor with example config containing comments
2. Edit a field in the GUI
3. Save
4. Inspect the YAML file on disk
5. Verify comments are intact and value is updated

### Step 7 — Edge case tests

Add tests for:
- **Empty file:** First save creates new file (no comments to preserve)
- **Malformed existing YAML:** Falls back to fresh write
- **Adding new device:** New section has no comments (expected)
- **Deleting a device:** Comments for remaining devices preserved

---

## Files to modify

```
pyproject.toml
  — Add ruamel.yaml to config-editor optional dependency

mimirheim_helpers/config_editor/config_editor/server.py
  — Import ruamel.yaml
  — Add _write_yaml_preserving_comments() helper function
  — Replace yaml.dump() in _api_post_config()
  — Replace yaml.dump() in _api_post_helper_config()

tests/unit/test_config_editor_server.py
  — Add test_config_save_preserves_comments()
  — Add test_config_save_handles_malformed_yaml()
  — Add test_config_save_new_file_no_comments()

mimirheim_helpers/config_editor/README.md
  — Add note about comment preservation feature
```

---

## Acceptance criteria

All of the following must be true:

1. ✅ `ruamel.yaml>=0.18` added to `config-editor` extra in pyproject.toml
2. ✅ `_write_yaml_preserving_comments()` helper function implemented
3. ✅ Both write locations use the new helper (mimirheim.yaml and helper configs)
4. ✅ Test `test_config_save_preserves_comments` passes
5. ✅ Manual test: edit config in GUI, verify comments intact on disk
6. ✅ Edge case tests pass (empty file, malformed YAML, new sections)
7. ✅ No regressions: existing config editor tests still pass
8. ✅ README documents the comment preservation feature

---

## Risks and mitigations

**Risk 1: ruamel.yaml dependency size**
- **Impact:** ruamel.yaml is ~800KB (larger than PyYAML's ~200KB)
- **Mitigation:** Only installed when `[config-editor]` extra is used. Core mimirheim unaffected.

**Risk 2: Deep merge complexity with nested structures**
- **Impact:** Complex merge logic could have bugs with deeply nested dicts
- **Mitigation:** Start with simple recursive merge. Add complexity only if needed. Test with actual mimirheim configs (which have modest nesting depth).

**Risk 3: Performance regression on large configs**
- **Impact:** ruamel.yaml is slower than PyYAML for parsing
- **Mitigation:** Config saves are infrequent (user-initiated). 100-200ms extra latency is acceptable for comment preservation.

**Risk 4: Formatting drift from original file**
- **Impact:** ruamel.yaml may reformat some aspects (spacing, list style)
- **Mitigation:** Configure ruamel.yaml to preserve formatting preferences. Acceptable trade-off for comment preservation.

---

## Follow-up work (out of scope)

- **Syntax highlighting for comments in GUI:** Would require Monaco editor or similar rich text component. Not needed for v1.
- **GUI-editable comments:** Allow users to add/edit comments directly in the form. Requires UX design and significant frontend work.
- **Comment templates from schema:** Auto-generate helpful comments from field descriptions. Nice-to-have but not essential.

---

## References

- ruamel.yaml documentation: https://yaml.readthedocs.io/en/latest/
- AGENTS.md: "Be critical" — this plan requires dependency addition justification
- `mimirheim_helpers/config_editor/README.md` — current config editor docs
