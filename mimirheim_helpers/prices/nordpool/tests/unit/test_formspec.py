"""Tests for the FormSpec authored for NordpoolConfig.

Verifies the FormSpec stays aligned with NordpoolConfig's top-level fields
(mimirheim_shared.alignment.assert_form_spec_complete), that it can be
combined with the model into a Config Service Descriptor, and that its one
Conditional Visibility rule evaluates as expected.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes
from mimirheim_shared.visibility import evaluate_condition

from nordpool.config import NordpoolConfig
from nordpool.formspec import NORDPOOL_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_nordpool_config() -> None:
    assert_form_spec_complete(NordpoolConfig, NORDPOOL_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "nordpool", "Nordpool day-ahead prices", NordpoolConfig, NORDPOOL_CONFIG_FORM_SPEC
    )

    assert descriptor.owner_id == "nordpool"
    assert descriptor.form_spec == resolve_field_shapes(NordpoolConfig, NORDPOOL_CONFIG_FORM_SPEC)
    assert descriptor.json_schema == NordpoolConfig.model_json_schema()


def test_form_spec_has_both_tiers_represented() -> None:
    tiers = {field.tier for field in NORDPOOL_CONFIG_FORM_SPEC.fields.values()}
    assert len(tiers) == 2


def test_mimir_trigger_topic_is_only_visible_when_signal_mimir_is_enabled() -> None:
    condition = NORDPOOL_CONFIG_FORM_SPEC.fields["mimir_trigger_topic"].visible_if
    assert condition is not None

    assert evaluate_condition(condition, {"signal_mimir": True}) is True
    assert evaluate_condition(condition, {"signal_mimir": False}) is False
