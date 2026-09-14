"""Tests for the FormSpec authored for PvLearnerConfig (PV ML learner).

Verifies the FormSpec stays aligned with PvLearnerConfig's fields
(mimirheim_shared.alignment.assert_form_spec_complete), that it can be
combined with the model into a Config Service Descriptor, and that its one
Conditional Visibility rule evaluates as expected.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes
from mimirheim_shared.visibility import evaluate_condition

from pv_ml_learner.config import PvLearnerConfig
from pv_ml_learner.formspec import PV_LEARNER_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_pv_learner_config() -> None:
    assert_form_spec_complete(PvLearnerConfig, PV_LEARNER_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "pv_ml_learner", "PV forecast (ML learner)", PvLearnerConfig, PV_LEARNER_CONFIG_FORM_SPEC
    )

    assert descriptor.owner_id == "pv_ml_learner"
    assert descriptor.form_spec == resolve_field_shapes(
        PvLearnerConfig, PV_LEARNER_CONFIG_FORM_SPEC
    )
    assert descriptor.json_schema == PvLearnerConfig.model_json_schema()


def test_form_spec_has_both_tiers_represented() -> None:
    tiers = {field.tier for field in PV_LEARNER_CONFIG_FORM_SPEC.fields.values()}
    assert len(tiers) == 2


def test_mimir_trigger_topic_is_only_visible_when_signal_mimir_is_enabled() -> None:
    condition = PV_LEARNER_CONFIG_FORM_SPEC.fields["mimir_trigger_topic"].visible_if
    assert condition is not None

    assert evaluate_condition(condition, {"signal_mimir": True}) is True
    assert evaluate_condition(condition, {"signal_mimir": False}) is False
