"""Tests for the FormSpec authored for WeatherBaseloadConfig.

Verifies the FormSpec stays aligned with the config model at every depth
(ADR-0006), builds into a Config Service Descriptor, represents both tiers,
and that its Conditional Visibility rule evaluates as expected.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes
from mimirheim_shared.visibility import evaluate_condition

from baseload_weather.config import WeatherBaseloadConfig
from baseload_weather.formspec import WEATHER_BASELOAD_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_config() -> None:
    assert_form_spec_complete(WeatherBaseloadConfig, WEATHER_BASELOAD_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "baseload_weather",
        "Baseload (weather-dependent)",
        WeatherBaseloadConfig,
        WEATHER_BASELOAD_CONFIG_FORM_SPEC,
    )

    assert descriptor.owner_id == "baseload_weather"
    assert descriptor.form_spec == resolve_field_shapes(
        WeatherBaseloadConfig, WEATHER_BASELOAD_CONFIG_FORM_SPEC
    )
    assert descriptor.json_schema == WeatherBaseloadConfig.model_json_schema()


def test_form_spec_has_both_tiers_represented() -> None:
    tiers = {field.tier for field in WEATHER_BASELOAD_CONFIG_FORM_SPEC.fields.values()}
    assert len(tiers) == 2


def test_mimir_trigger_topic_is_only_visible_when_signal_mimir_is_enabled() -> None:
    condition = WEATHER_BASELOAD_CONFIG_FORM_SPEC.fields["mimir_trigger_topic"].visible_if
    assert condition is not None

    assert evaluate_condition(condition, {"signal_mimir": True}) is True
    assert evaluate_condition(condition, {"signal_mimir": False}) is False
