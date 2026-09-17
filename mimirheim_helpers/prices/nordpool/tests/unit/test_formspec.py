"""Tests for the FormSpec authored for NordpoolConfig.

Verifies the FormSpec stays aligned with NordpoolConfig's top-level fields
(mimirheim_shared.alignment.assert_form_spec_complete), that it can be
combined with the model into a Config Service Descriptor, and that its one
Conditional Visibility rule evaluates as expected.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.field_shape import FieldShape
from mimirheim_shared.formspec import option_label, resolve_field_shapes
from mimirheim_shared.visibility import evaluate_condition

from nordpool.config import NordpoolApiConfig, NordpoolConfig
from nordpool.formspec import NORDPOOL_API_CONFIG_FORM_SPEC, NORDPOOL_CONFIG_FORM_SPEC


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


def test_area_resolves_to_an_enum_select_of_pynordpools_own_area_codes() -> None:
    resolved = resolve_field_shapes(NordpoolApiConfig, NORDPOOL_API_CONFIG_FORM_SPEC)
    area_spec = resolved.fields["area"]

    assert area_spec.shape is FieldShape.ENUM_SELECT
    assert "NO2" in NordpoolApiConfig.model_fields["area"].annotation.__args__
    assert "SYS" not in NordpoolApiConfig.model_fields["area"].annotation.__args__


def test_area_option_labels_show_the_country_name_alongside_the_code() -> None:
    area_spec = NORDPOOL_API_CONFIG_FORM_SPEC.fields["area"]

    assert option_label(area_spec, "NO2") == "Norway 2 (NO2)"
