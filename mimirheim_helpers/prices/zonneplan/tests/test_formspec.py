"""Tests for the FormSpec authored for ZonneplanPricesConfig.

Verifies the FormSpec stays aligned with ZonneplanPricesConfig's fields
(mimirheim_shared.alignment.assert_form_spec_complete), that it can be
combined with the model into a Config Service Descriptor, and that its one
Conditional Visibility rule evaluates as expected.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes
from mimirheim_shared.visibility import evaluate_condition

from zonneplan_prices.config import ZonneplanPricesConfig
from zonneplan_prices.formspec import ZONNEPLAN_PRICES_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_zonneplan_prices_config() -> None:
    assert_form_spec_complete(ZonneplanPricesConfig, ZONNEPLAN_PRICES_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "zonneplan_prices",
        "Zonneplan electricity prices",
        ZonneplanPricesConfig,
        ZONNEPLAN_PRICES_CONFIG_FORM_SPEC,
    )

    assert descriptor.owner_id == "zonneplan_prices"
    assert descriptor.form_spec == resolve_field_shapes(
        ZonneplanPricesConfig, ZONNEPLAN_PRICES_CONFIG_FORM_SPEC
    )
    assert descriptor.json_schema == ZonneplanPricesConfig.model_json_schema()


def test_form_spec_has_both_tiers_represented() -> None:
    tiers = {field.tier for field in ZONNEPLAN_PRICES_CONFIG_FORM_SPEC.fields.values()}
    assert len(tiers) == 2


def test_mimir_trigger_topic_is_only_visible_when_signal_mimir_is_enabled() -> None:
    condition = ZONNEPLAN_PRICES_CONFIG_FORM_SPEC.fields["mimir_trigger_topic"].visible_if
    assert condition is not None

    assert evaluate_condition(condition, {"signal_mimir": True}) is True
    assert evaluate_condition(condition, {"signal_mimir": False}) is False
