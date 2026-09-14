"""Tests for the FormSpec authored for MimirheimConfig.

Verifies the FormSpec stays aligned with MimirheimConfig's top-level fields
(mimirheim_shared.alignment.assert_form_spec_complete) and that it can be
combined with the model into a Config Service Descriptor.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.field_shape import FieldShape, derive_field_shape
from mimirheim_shared.formspec import resolve_field_shapes

from mimirheim.config.formspec import MIMIRHEIM_CONFIG_FORM_SPEC
from mimirheim.config.schema import MimirheimConfig


def test_form_spec_is_aligned_with_mimirheim_config() -> None:
    assert_form_spec_complete(MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "mimirheim-core", "Mimirheim", MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC
    )

    assert descriptor.owner_id == "mimirheim-core"
    assert descriptor.form_spec == resolve_field_shapes(MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC)
    assert descriptor.json_schema == MimirheimConfig.model_json_schema()


def test_grid_is_a_real_nested_object() -> None:
    field_spec = MIMIRHEIM_CONFIG_FORM_SPEC.fields["grid"]
    annotation = MimirheimConfig.model_fields["grid"].annotation

    assert derive_field_shape(annotation) is FieldShape.NESTED_OBJECT
    assert field_spec.nested_form_spec is not None


def test_batteries_is_a_real_named_collection() -> None:
    field_spec = MIMIRHEIM_CONFIG_FORM_SPEC.fields["batteries"]
    annotation = MimirheimConfig.model_fields["batteries"].annotation

    assert derive_field_shape(annotation) is FieldShape.NAMED_COLLECTION
    assert field_spec.nested_form_spec is not None


def test_battery_charge_segments_is_a_real_ordered_collection() -> None:
    from mimirheim.config.formspec import BATTERY_CONFIG_FORM_SPEC
    from mimirheim.config.schema import BatteryConfig

    field_spec = BATTERY_CONFIG_FORM_SPEC.fields["charge_segments"]
    annotation = BatteryConfig.model_fields["charge_segments"].annotation

    assert derive_field_shape(annotation) is FieldShape.ORDERED_COLLECTION
    assert field_spec.nested_form_spec is not None


_DEVICE_FIELD_NAMES = (
    "batteries",
    "pv_arrays",
    "ev_chargers",
    "deferrable_loads",
    "static_loads",
    "hybrid_inverters",
    "thermal_boilers",
    "space_heating_hps",
    "combi_heat_pumps",
)


def test_every_device_type_has_its_own_tab() -> None:
    tabs = {name: MIMIRHEIM_CONFIG_FORM_SPEC.fields[name].tab for name in _DEVICE_FIELD_NAMES}

    assert len(set(tabs.values())) == len(tabs), tabs
    assert all(tab is not None for tab in tabs.values())


def test_grid_has_its_own_tab_distinct_from_devices_and_internals() -> None:
    grid_tab = MIMIRHEIM_CONFIG_FORM_SPEC.fields["grid"].tab

    assert grid_tab is not None
    assert grid_tab not in {MIMIRHEIM_CONFIG_FORM_SPEC.fields[name].tab for name in _DEVICE_FIELD_NAMES}


def test_non_device_non_grid_sections_share_one_internals_tab() -> None:
    internals_field_names = (
        "objectives",
        "constraints",
        "solver",
        "readiness",
        "mqtt",
        "outputs",
        "inputs",
        "homeassistant",
        "debug",
        "control",
        "reporting",
    )

    internals_tabs = {MIMIRHEIM_CONFIG_FORM_SPEC.fields[name].tab for name in internals_field_names}
    device_and_grid_tabs = {
        MIMIRHEIM_CONFIG_FORM_SPEC.fields[name].tab for name in (*_DEVICE_FIELD_NAMES, "grid")
    }

    assert len(internals_tabs) == 1
    assert internals_tabs.isdisjoint(device_and_grid_tabs)


def test_objectives_balanced_weights_is_a_real_optional_object() -> None:
    from mimirheim.config.formspec import OBJECTIVES_CONFIG_FORM_SPEC
    from mimirheim.config.schema import ObjectivesConfig

    field_spec = OBJECTIVES_CONFIG_FORM_SPEC.fields["balanced_weights"]
    annotation = ObjectivesConfig.model_fields["balanced_weights"].annotation

    assert derive_field_shape(annotation) is FieldShape.OPTIONAL_OBJECT
    assert field_spec.nested_form_spec is not None


def test_battery_charge_segments_and_curve_are_mutually_visible() -> None:
    from mimirheim_shared.visibility import evaluate_condition

    from mimirheim.config.formspec import BATTERY_CONFIG_FORM_SPEC

    segments_condition = BATTERY_CONFIG_FORM_SPEC.fields["charge_segments"].visible_if
    curve_condition = BATTERY_CONFIG_FORM_SPEC.fields["charge_efficiency_curve"].visible_if
    assert segments_condition is not None
    assert curve_condition is not None

    assert evaluate_condition(segments_condition, {"charge_efficiency_curve": None}) is True
    assert evaluate_condition(segments_condition, {"charge_efficiency_curve": []}) is False
    assert evaluate_condition(curve_condition, {"charge_segments": None}) is True
    assert evaluate_condition(curve_condition, {"charge_segments": []}) is False


def test_battery_discharge_segments_and_curve_are_mutually_visible() -> None:
    from mimirheim_shared.visibility import evaluate_condition

    from mimirheim.config.formspec import BATTERY_CONFIG_FORM_SPEC

    segments_condition = BATTERY_CONFIG_FORM_SPEC.fields["discharge_segments"].visible_if
    curve_condition = BATTERY_CONFIG_FORM_SPEC.fields["discharge_efficiency_curve"].visible_if
    assert segments_condition is not None
    assert curve_condition is not None

    assert evaluate_condition(segments_condition, {"discharge_efficiency_curve": None}) is True
    assert evaluate_condition(segments_condition, {"discharge_efficiency_curve": []}) is False
    assert evaluate_condition(curve_condition, {"discharge_segments": None}) is True
    assert evaluate_condition(curve_condition, {"discharge_segments": []}) is False
