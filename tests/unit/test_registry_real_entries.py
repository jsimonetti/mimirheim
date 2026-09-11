"""Tests for config-editor-v2's real, production registry entries.

This module lives at the repo root, not under
mimirheim_helpers/config_editor_v2/tests/, for the same reason v1's
equivalent cross-boundary tests do (see
mimirheim_helpers/config_editor/AGENTS.md): it imports config_editor_v2
alongside mimirheim's own config schema and every helper's config module,
crossing package boundaries that the helper's own isolated unit tests
(mimirheim_helpers/config_editor_v2/tests/) do not cross.

What this module does not do:
- It does not exercise the HTTP server; see test_server.py for that.
- It does not test the registry mechanism itself (resolve_model,
  RegistryEntry validation); those are covered by
  mimirheim_helpers/config_editor_v2/tests/unit/test_registry.py against
  small fixture models.
"""

from __future__ import annotations

from config_editor_v2.registry import REGISTRY, resolve_model
from config_editor_v2.save import validate_all
from mimirheim.config.schema import MimirheimConfig


def test_mimirheim_config_entry_resolves() -> None:
    """The real MimirheimConfig registry entry resolves via resolve_model."""
    entries = [entry for entry in REGISTRY if entry.filename == "mimirheim.yaml"]
    assert len(entries) == 1, "expected exactly one mimirheim.yaml registry entry"
    assert resolve_model(entries[0]) is MimirheimConfig


def test_untouched_registry_entries_never_block_a_save() -> None:
    """No registered entry, left untouched, ever blocks a save.

    Earlier in this step, `validate_all` treated an entry absent from
    `submitted` the same as one present with `{}`: both were passed through
    `model_cls.model_validate(...)`, and a resulting `ValidationError` became
    a blocking `FieldError`. Every real production model requires at least
    an `mqtt` block with no default value (most also require
    `trigger_topic`), so this made it impossible to save anything unless
    every registered helper's every required field was filled in
    simultaneously -- even for a user who only wants to configure
    `mimirheim.yaml`.

    `validate_all` now distinguishes "absent from `submitted`" (untouched)
    from "present with invalid data": an untouched entry is validated
    against its own defaults on a best-effort basis and, if that fails, is
    silently excluded from the save -- not written, and not a blocking
    error. See IMPLEMENTATION_DETAILS.md's "Save semantics".

    This test asserts both halves of that contract against the real
    registry: no errors, and -- since none of the 12 real entries currently
    have a valid all-defaults state -- nothing gets included in `validated`
    either. If this second assertion ever starts failing because a model
    gained a valid all-default state, that is good news (one more entry can
    now self-default) and the assertion should be updated to match, not
    treated as a regression to revert.
    """
    validated, errors = validate_all(REGISTRY, submitted={})
    assert errors == [], (
        "an untouched registry entry must never block a save; "
        f"got errors: {errors}"
    )
    assert validated == {}, (
        "none of the real registered models currently have a valid "
        "all-defaults state (each requires at least an `mqtt` block with no "
        "default); if this now contains entries, update this assertion to "
        "reflect which models can self-default"
    )
