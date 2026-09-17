"""Tests for the FormSpec authored for ConfigEditorConfig.

Verifies the FormSpec stays aligned with ConfigEditorConfig's fields
(mimirheim_shared.alignment.assert_form_spec_complete) and that it can be
combined with the model into a Config Service Descriptor.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes

from config_editor.config import ConfigEditorConfig
from config_editor.formspec import CONFIG_EDITOR_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_config_editor_config() -> None:
    assert_form_spec_complete(ConfigEditorConfig, CONFIG_EDITOR_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "config-editor", "Config Editor", ConfigEditorConfig, CONFIG_EDITOR_CONFIG_FORM_SPEC
    )

    assert descriptor.owner_id == "config-editor"
    assert descriptor.form_spec == resolve_field_shapes(
        ConfigEditorConfig, CONFIG_EDITOR_CONFIG_FORM_SPEC
    )
    assert descriptor.json_schema == ConfigEditorConfig.model_json_schema()


def test_form_spec_has_both_tiers_represented() -> None:
    tiers = {field.tier for field in CONFIG_EDITOR_CONFIG_FORM_SPEC.fields.values()}
    assert len(tiers) == 2
