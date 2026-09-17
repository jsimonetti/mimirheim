"""Tests for the FormSpec authored for BaseloadConfig (homeassistant_db baseload).

Verifies the FormSpec stays aligned with BaseloadConfig's fields
(mimirheim_shared.alignment.assert_form_spec_complete), that it can be
combined with the model into a Config Service Descriptor, and that its one
Conditional Visibility rule evaluates as expected.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes
from mimirheim_shared.visibility import evaluate_condition

from baseload_ha_db.config import BaseloadConfig
from baseload_ha_db.formspec import BASELOAD_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_baseload_config() -> None:
    assert_form_spec_complete(BaseloadConfig, BASELOAD_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "baseload_ha_db",
        "Baseload (Home Assistant DB)",
        BaseloadConfig,
        BASELOAD_CONFIG_FORM_SPEC,
    )

    assert descriptor.owner_id == "baseload_ha_db"
    assert descriptor.form_spec == resolve_field_shapes(BaseloadConfig, BASELOAD_CONFIG_FORM_SPEC)
    assert descriptor.json_schema == BaseloadConfig.model_json_schema()


def test_form_spec_has_both_tiers_represented() -> None:
    tiers = {field.tier for field in BASELOAD_CONFIG_FORM_SPEC.fields.values()}
    assert len(tiers) == 2


def test_mimir_trigger_topic_is_only_visible_when_signal_mimir_is_enabled() -> None:
    condition = BASELOAD_CONFIG_FORM_SPEC.fields["mimir_trigger_topic"].visible_if
    assert condition is not None

    assert evaluate_condition(condition, {"signal_mimir": True}) is True
    assert evaluate_condition(condition, {"signal_mimir": False}) is False
