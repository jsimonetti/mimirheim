"""Tests for the FormSpec authored for EpexPredictorPricesConfig.

Verifies the FormSpec stays aligned with EpexPredictorPricesConfig's fields
(mimirheim_shared.alignment.assert_form_spec_complete), that it can be
combined with the model into a Config Service Descriptor, and that its one
Conditional Visibility rule evaluates as expected.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes
from mimirheim_shared.visibility import evaluate_condition

from epexpredictor_prices.config import EpexPredictorPricesConfig
from epexpredictor_prices.formspec import EPEXPREDICTOR_PRICES_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_epexpredictor_prices_config() -> None:
    assert_form_spec_complete(EpexPredictorPricesConfig, EPEXPREDICTOR_PRICES_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "epexpredictor_prices",
        "EpexPredictor day-ahead prices",
        EpexPredictorPricesConfig,
        EPEXPREDICTOR_PRICES_CONFIG_FORM_SPEC,
    )

    assert descriptor.owner_id == "epexpredictor_prices"
    assert descriptor.form_spec == resolve_field_shapes(
        EpexPredictorPricesConfig, EPEXPREDICTOR_PRICES_CONFIG_FORM_SPEC
    )
    assert descriptor.json_schema == EpexPredictorPricesConfig.model_json_schema()


def test_form_spec_has_both_tiers_represented() -> None:
    tiers = {field.tier for field in EPEXPREDICTOR_PRICES_CONFIG_FORM_SPEC.fields.values()}
    assert len(tiers) == 2


def test_mimir_trigger_topic_is_only_visible_when_signal_mimir_is_enabled() -> None:
    condition = EPEXPREDICTOR_PRICES_CONFIG_FORM_SPEC.fields["mimir_trigger_topic"].visible_if
    assert condition is not None

    assert evaluate_condition(condition, {"signal_mimir": True}) is True
    assert evaluate_condition(condition, {"signal_mimir": False}) is False
