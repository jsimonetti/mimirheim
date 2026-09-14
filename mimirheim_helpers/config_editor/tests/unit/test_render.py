"""Unit tests for config_editor.render: grouping, Tier collapse, Conditional Visibility,
and (ticket 08) recursive rendering of every Field Shape."""

from __future__ import annotations

import pytest

from mimirheim_shared.config_service import Descriptor
from mimirheim_shared.field_shape import FieldShape
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

from config_editor.render import RenderedField, RenderedGroup, UNGROUPED_LABEL, build_groups, schema_default_values


def _find_field(groups: list[RenderedGroup], name: str) -> RenderedField:
    for group in groups:
        for candidate in (*group.basic_fields, *group.expert_fields):
            if candidate.name == name:
                return candidate
    raise AssertionError(f"No rendered field named {name!r} in {groups!r}")


def test_schema_default_values_reads_each_propertys_default() -> None:
    json_schema = {
        "properties": {
            "enabled": {"type": "boolean", "default": False},
            "prefix": {"type": "string", "default": "homeassistant"},
            "host": {"type": "string"},
        }
    }

    values = schema_default_values(json_schema)

    assert values == {"enabled": False, "prefix": "homeassistant"}


def test_schema_default_values_handles_missing_properties_key() -> None:
    assert schema_default_values({}) == {}


def _descriptor(form_spec: FormSpec, json_schema: dict | None = None) -> Descriptor:
    return Descriptor(
        owner_id="owner",
        display_name="Owner",
        json_schema=json_schema or {"properties": {}},
        form_spec=form_spec,
    )


def test_fields_with_no_group_fall_under_ungrouped_label() -> None:
    descriptor = _descriptor(
        FormSpec(fields={"host": FieldSpec(label="Host", description="Broker host.")})
    )

    groups = build_groups(descriptor)

    assert [g.label for g in groups] == [UNGROUPED_LABEL]
    assert [f.name for f in groups[0].basic_fields] == ["host"]


def test_fields_are_grouped_by_their_declared_group() -> None:
    descriptor = _descriptor(
        FormSpec(
            fields={
                "host": FieldSpec(label="Host", description="Broker host.", group="MQTT"),
                "port": FieldSpec(label="Port", description="Broker port.", group="MQTT"),
                "batteries": FieldSpec(label="Batteries", description="Battery devices.", group="Devices"),
            }
        )
    )

    groups = build_groups(descriptor)

    assert [g.label for g in groups] == ["MQTT", "Devices"]
    assert [f.name for f in groups[0].basic_fields] == ["host", "port"]
    assert [f.name for f in groups[1].basic_fields] == ["batteries"]


def test_expert_tier_fields_are_separated_from_basic_fields() -> None:
    descriptor = _descriptor(
        FormSpec(
            fields={
                "host": FieldSpec(label="Host", description="Broker host.", group="MQTT", tier=Tier.BASIC),
                "tls": FieldSpec(label="TLS", description="Enable TLS.", group="MQTT", tier=Tier.EXPERT),
            }
        )
    )

    (group,) = build_groups(descriptor)

    assert [f.name for f in group.basic_fields] == ["host"]
    assert [f.name for f in group.expert_fields] == ["tls"]
    assert group.has_expert_fields is True


def test_group_with_no_expert_fields_reports_has_expert_fields_false() -> None:
    descriptor = _descriptor(
        FormSpec(fields={"host": FieldSpec(label="Host", description="Broker host.")})
    )

    (group,) = build_groups(descriptor)

    assert group.has_expert_fields is False


def test_hidden_field_is_excluded_entirely() -> None:
    descriptor = _descriptor(
        FormSpec(
            fields={
                "host": FieldSpec(label="Host", description="Broker host."),
                "internal": FieldSpec(label="Internal", description="Not shown.", hidden=True),
            }
        )
    )

    (group,) = build_groups(descriptor)

    assert [f.name for f in group.basic_fields] == ["host"]


def test_conditional_visibility_hides_field_when_condition_is_false() -> None:
    descriptor = _descriptor(
        FormSpec(
            fields={
                "enabled": FieldSpec(label="Enabled", description="Enable autodiscovery."),
                "prefix": FieldSpec(
                    label="Prefix",
                    description="Autodiscovery prefix.",
                    visible_if=Comparison(field="enabled", operator=ComparisonOperator.EQ, value=True),
                ),
            }
        ),
        json_schema={
            "properties": {
                "enabled": {"type": "boolean", "default": False},
                "prefix": {"type": "string", "default": "homeassistant"},
            }
        },
    )

    (group,) = build_groups(descriptor)

    assert [f.name for f in group.basic_fields] == ["enabled"]


def test_values_override_replaces_schema_defaults() -> None:
    descriptor = _descriptor(
        FormSpec(fields={"area": FieldSpec(label="Price area", description="Bidding area.")}),
        json_schema={"properties": {"area": {"type": "string", "default": "SE1"}}},
    )

    (group,) = build_groups(descriptor, values={"area": "SE3"})

    assert group.basic_fields[0].value == "SE3"


def test_values_override_also_drives_conditional_visibility() -> None:
    descriptor = _descriptor(
        FormSpec(
            fields={
                "enabled": FieldSpec(label="Enabled", description="Enable autodiscovery."),
                "prefix": FieldSpec(
                    label="Prefix",
                    description="Autodiscovery prefix.",
                    visible_if=Comparison(field="enabled", operator=ComparisonOperator.EQ, value=True),
                ),
            }
        ),
        json_schema={
            "properties": {
                "enabled": {"type": "boolean", "default": False},
                "prefix": {"type": "string", "default": "homeassistant"},
            }
        },
    )

    (group,) = build_groups(descriptor, values={"enabled": True, "prefix": "homeassistant"})

    assert [f.name for f in group.basic_fields] == ["enabled", "prefix"]


def test_conditional_visibility_shows_field_when_condition_is_true() -> None:
    descriptor = _descriptor(
        FormSpec(
            fields={
                "enabled": FieldSpec(label="Enabled", description="Enable autodiscovery."),
                "prefix": FieldSpec(
                    label="Prefix",
                    description="Autodiscovery prefix.",
                    visible_if=Comparison(field="enabled", operator=ComparisonOperator.EQ, value=True),
                ),
            }
        ),
        json_schema={
            "properties": {
                "enabled": {"type": "boolean", "default": True},
                "prefix": {"type": "string", "default": "homeassistant"},
            }
        },
    )

    (group,) = build_groups(descriptor)

    assert [f.name for f in group.basic_fields] == ["enabled", "prefix"]


class TestNestedObjectShape:
    def test_nested_object_field_recurses_into_its_own_groups(self) -> None:
        nested_spec = FormSpec(
            fields={"host": FieldSpec(label="Host", description="Broker host.", shape=FieldShape.SCALAR)}
        )
        descriptor = _descriptor(
            FormSpec(
                fields={
                    "mqtt": FieldSpec(
                        label="MQTT",
                        description="Broker connection.",
                        shape=FieldShape.NESTED_OBJECT,
                        nested_form_spec=nested_spec,
                    )
                }
            )
        )

        (group,) = build_groups(descriptor, values={"mqtt": {"host": "localhost"}})

        field = group.basic_fields[0]
        assert field.nested_groups is not None
        assert field.entries is None
        (nested_group,) = field.nested_groups
        assert nested_group.basic_fields[0].name == "mqtt.host"
        assert nested_group.basic_fields[0].value == "localhost"


class TestNamedCollectionShape:
    _NESTED_SPEC = FormSpec(
        fields={
            "capacity_kwh": FieldSpec(
                label="Capacity", description="Usable capacity in kWh.", shape=FieldShape.SCALAR
            )
        }
    )
    _JSON_SCHEMA = {
        "properties": {
            "batteries": {"type": "object", "additionalProperties": {"$ref": "#/$defs/Battery"}}
        },
        "$defs": {"Battery": {"type": "object", "properties": {"capacity_kwh": {"type": "number"}}}},
    }

    def _descriptor(self) -> Descriptor:
        return _descriptor(
            FormSpec(
                fields={
                    "batteries": FieldSpec(
                        label="Batteries",
                        description="Named battery devices.",
                        shape=FieldShape.NAMED_COLLECTION,
                        nested_form_spec=self._NESTED_SPEC,
                    )
                }
            ),
            json_schema=self._JSON_SCHEMA,
        )

    def test_renders_one_entry_per_existing_named_entry(self) -> None:
        descriptor = self._descriptor()
        values = {
            "batteries": {
                "battery_main": {"capacity_kwh": 5.4},
                "battery_sos2": {"capacity_kwh": 10.0},
            }
        }

        (group,) = build_groups(descriptor, values=values)

        field = group.basic_fields[0]
        assert field.nested_groups is None
        assert [entry.key for entry in field.entries] == ["battery_main", "battery_sos2"]
        first_entry_field = field.entries[0].groups[0].basic_fields[0]
        assert first_entry_field.name == "batteries.battery_main.capacity_kwh"
        assert first_entry_field.value == 5.4

    def test_renders_no_entries_when_collection_is_empty(self) -> None:
        descriptor = self._descriptor()

        (group,) = build_groups(descriptor, values={"batteries": {}})

        assert group.basic_fields[0].entries == []


class TestOrderedCollectionShape:
    _NESTED_SPEC = FormSpec(
        fields={
            "power_max_kw": FieldSpec(
                label="Max power", description="Max power in kW.", shape=FieldShape.SCALAR
            )
        }
    )
    _JSON_SCHEMA = {
        "properties": {
            "charge_segments": {"type": "array", "items": {"$ref": "#/$defs/Segment"}}
        },
        "$defs": {"Segment": {"type": "object", "properties": {"power_max_kw": {"type": "number"}}}},
    }

    def test_renders_one_entry_per_existing_row_in_index_order(self) -> None:
        descriptor = _descriptor(
            FormSpec(
                fields={
                    "charge_segments": FieldSpec(
                        label="Charge segments",
                        description="Piecewise charge efficiency.",
                        shape=FieldShape.ORDERED_COLLECTION,
                        nested_form_spec=self._NESTED_SPEC,
                    )
                }
            ),
            json_schema=self._JSON_SCHEMA,
        )
        values = {"charge_segments": [{"power_max_kw": 2.5}, {"power_max_kw": 1.4}]}

        (group,) = build_groups(descriptor, values=values)

        field = group.basic_fields[0]
        assert [entry.key for entry in field.entries] == ["0", "1"]
        assert field.entries[0].groups[0].basic_fields[0].name == "charge_segments.0.power_max_kw"
        assert field.entries[0].groups[0].basic_fields[0].value == 2.5
        assert field.entries[1].groups[0].basic_fields[0].value == 1.4


class TestOptionalObjectShape:
    _NESTED_SPEC = FormSpec(
        fields={
            "grid_price_weight": FieldSpec(
                label="Grid price weight", description="Weight.", shape=FieldShape.SCALAR
            )
        }
    )

    def _descriptor(self) -> Descriptor:
        return _descriptor(
            FormSpec(
                fields={
                    "balanced_weights": FieldSpec(
                        label="Balanced weights",
                        description="Optional weighting.",
                        shape=FieldShape.OPTIONAL_OBJECT,
                        nested_form_spec=self._NESTED_SPEC,
                    )
                }
            )
        )

    def test_currently_absent_field_reports_unchecked_presence_toggle_but_still_builds_nested_groups(
        self,
    ) -> None:
        # Nested groups are always built, even when currently absent:
        # owner.html renders them inside a <fieldset disabled> its Presence
        # Toggle checkbox enables live via JS, so a user can populate a
        # currently-absent optional section without a page reload.
        (group,) = build_groups(self._descriptor(), values={"balanced_weights": None})

        field = group.basic_fields[0]
        assert field.present is False
        assert field.nested_groups is not None
        assert field.nested_groups[0].basic_fields[0].value is None

    def test_currently_present_field_reports_checked_presence_toggle_and_renders_nested_fields(
        self,
    ) -> None:
        (group,) = build_groups(
            self._descriptor(), values={"balanced_weights": {"grid_price_weight": 0.5}}
        )

        field = group.basic_fields[0]
        assert field.present is True
        assert field.nested_groups is not None
        assert field.nested_groups[0].basic_fields[0].value == 0.5


class TestEnumSelectShape:
    def test_renders_options_from_json_schema_and_current_value(self) -> None:
        descriptor = _descriptor(
            FormSpec(
                fields={
                    "unit": FieldSpec(
                        label="SOC unit",
                        description="Unit of the published value.",
                        shape=FieldShape.ENUM_SELECT,
                        option_labels={"kwh": "kWh", "percent": "Percent"},
                    )
                }
            ),
            json_schema={"properties": {"unit": {"type": "string", "enum": ["kwh", "percent"]}}},
        )

        (group,) = build_groups(descriptor, values={"unit": "percent"})

        field = group.basic_fields[0]
        assert field.options == ["kwh", "percent"]
        assert field.value == "percent"


class TestTypedWidgets:
    def test_boolean_field_gets_checkbox_input_type(self) -> None:
        descriptor = _descriptor(
            FormSpec(fields={"enabled": FieldSpec(label="Enabled", description="Enable it.")}),
            json_schema={"properties": {"enabled": {"type": "boolean"}}},
        )

        (group,) = build_groups(descriptor, values={"enabled": True})

        field = group.basic_fields[0]
        assert field.input_type == "checkbox"
        assert field.value is True

    def test_integer_field_gets_number_input_type_and_step_one(self) -> None:
        descriptor = _descriptor(
            FormSpec(fields={"poll_interval": FieldSpec(label="Poll interval", description="Seconds.")}),
            json_schema={"properties": {"poll_interval": {"type": "integer"}}},
        )

        (group,) = build_groups(descriptor)

        field = group.basic_fields[0]
        assert field.input_type == "number"
        assert field.step == "1"

    def test_float_field_gets_number_input_type_and_step_any_with_no_multiple_of(self) -> None:
        descriptor = _descriptor(
            FormSpec(fields={"capacity_kwh": FieldSpec(label="Capacity", description="kWh.")}),
            json_schema={"properties": {"capacity_kwh": {"type": "number"}}},
        )

        (group,) = build_groups(descriptor)

        field = group.basic_fields[0]
        assert field.input_type == "number"
        assert field.step == "any"

    def test_float_field_with_multiple_of_uses_it_as_step(self) -> None:
        descriptor = _descriptor(
            FormSpec(fields={"efficiency": FieldSpec(label="Efficiency", description="Fraction.")}),
            json_schema={"properties": {"efficiency": {"type": "number", "multipleOf": 0.05}}},
        )

        (group,) = build_groups(descriptor)

        field = group.basic_fields[0]
        assert field.step == "0.05"

    def test_ge_le_bounds_become_min_max(self) -> None:
        descriptor = _descriptor(
            FormSpec(fields={"port": FieldSpec(label="Port", description="Port.")}),
            json_schema={"properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}}},
        )

        (group,) = build_groups(descriptor)

        field = group.basic_fields[0]
        assert field.min_value == 1
        assert field.max_value == 65535

    def test_gt_lt_bounds_become_min_max(self) -> None:
        descriptor = _descriptor(
            FormSpec(fields={"weight": FieldSpec(label="Weight", description="Weight.")}),
            json_schema={
                "properties": {"weight": {"type": "number", "exclusiveMinimum": 0.0, "exclusiveMaximum": 1.0}}
            },
        )

        (group,) = build_groups(descriptor)

        field = group.basic_fields[0]
        assert field.min_value == 0.0
        assert field.max_value == 1.0

    def test_numeric_field_with_no_declared_bound_has_no_min_or_max(self) -> None:
        descriptor = _descriptor(
            FormSpec(fields={"capacity_kwh": FieldSpec(label="Capacity", description="kWh.")}),
            json_schema={"properties": {"capacity_kwh": {"type": "number"}}},
        )

        (group,) = build_groups(descriptor)

        field = group.basic_fields[0]
        assert field.min_value is None
        assert field.max_value is None

    def test_string_field_keeps_text_input_type(self) -> None:
        descriptor = _descriptor(
            FormSpec(fields={"host": FieldSpec(label="Host", description="Broker host.")}),
            json_schema={"properties": {"host": {"type": "string"}}},
        )

        (group,) = build_groups(descriptor)

        field = group.basic_fields[0]
        assert field.input_type == "text"

    def test_enum_select_field_is_unaffected_by_widget_typing(self) -> None:
        descriptor = _descriptor(
            FormSpec(
                fields={
                    "unit": FieldSpec(
                        label="SOC unit",
                        description="Unit.",
                        shape=FieldShape.ENUM_SELECT,
                    )
                }
            ),
            json_schema={"properties": {"unit": {"type": "string", "enum": ["kwh", "percent"]}}},
        )

        (group,) = build_groups(descriptor, values={"unit": "kwh"})

        field = group.basic_fields[0]
        assert field.options == ["kwh", "percent"]
        assert field.input_type == "text"


class TestShapeOverride:
    def test_field_overridden_to_scalar_renders_as_a_leaf_despite_a_nested_form_spec(self) -> None:
        nested_spec = FormSpec(
            fields={"host": FieldSpec(label="Host", description="Broker host.", shape=FieldShape.SCALAR)}
        )
        descriptor = _descriptor(
            FormSpec(
                fields={
                    "mqtt": FieldSpec(
                        label="MQTT",
                        description="Broker connection, shown as raw text.",
                        shape_override=FieldShape.SCALAR,
                        # As resolve_field_shapes would leave it: the
                        # *effective* shape is the override, not the derived
                        # NESTED_OBJECT shape nested_form_spec implies.
                        shape=FieldShape.SCALAR,
                        nested_form_spec=nested_spec,
                    )
                }
            )
        )

        (group,) = build_groups(descriptor, values={"mqtt": {"host": "localhost"}})

        field = group.basic_fields[0]
        assert field.nested_groups is None
        assert field.entries is None
        assert field.value == {"host": "localhost"}


class TestBatterySegmentsVersusCurveConditionalVisibility:
    """BatteryConfig.charge_segments and charge_efficiency_curve are mutually
    exclusive (ticket 07's Conditional Visibility rule); ticket 08 requires
    the Config Editor to actually honour it once rendering recurses into a
    real battery entry."""

    @staticmethod
    def _battery_groups(battery_values: dict) -> list[RenderedGroup]:
        from mimirheim.io import config_service as core_config_service

        descriptor = Descriptor.model_validate_json(core_config_service.payload_bytes())
        groups = build_groups(descriptor, values={"batteries": {"bat1": battery_values}})
        batteries_field = _find_field(groups, "batteries")
        (entry,) = batteries_field.entries
        return entry.groups

    def test_charge_segments_populated_hides_charge_efficiency_curve(self) -> None:
        groups = self._battery_groups(
            {
                "capacity_kwh": 5.0,
                "charge_segments": [{"power_max_kw": 2.5, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 2.5, "efficiency": 0.95}],
            }
        )

        assert _find_field(groups, "batteries.bat1.charge_segments") is not None
        with pytest.raises(AssertionError):
            _find_field(groups, "batteries.bat1.charge_efficiency_curve")

    def test_charge_efficiency_curve_populated_hides_charge_segments(self) -> None:
        groups = self._battery_groups(
            {
                "capacity_kwh": 5.0,
                "charge_efficiency_curve": [
                    {"power_kw": 0.0, "efficiency": 0.9},
                    {"power_kw": 3.0, "efficiency": 0.95},
                ],
                "discharge_efficiency_curve": [
                    {"power_kw": 0.0, "efficiency": 0.9},
                    {"power_kw": 3.0, "efficiency": 0.95},
                ],
            }
        )

        assert _find_field(groups, "batteries.bat1.charge_efficiency_curve") is not None
        with pytest.raises(AssertionError):
            _find_field(groups, "batteries.bat1.charge_segments")

    def test_neither_populated_both_render(self) -> None:
        groups = self._battery_groups({"capacity_kwh": 5.0})

        assert _find_field(groups, "batteries.bat1.charge_segments") is not None
        assert _find_field(groups, "batteries.bat1.charge_efficiency_curve") is not None


def test_real_mimirheim_descriptor_renders_a_battery_soc_unit_through_every_nesting_level() -> None:
    """Full-depth integration check: Named Collection -> optional object ->
    nested object -> enum select, resolved against mimirheim core's real,
    just-as-published Descriptor (JSON Schema $ref/$defs included)."""
    from mimirheim.io import config_service as core_config_service

    descriptor = Descriptor.model_validate_json(core_config_service.payload_bytes())
    values = {
        "batteries": {
            "bat1": {
                "capacity_kwh": 10.0,
                "inputs": {"soc": {"unit": "kwh"}},
            }
        }
    }

    groups = build_groups(descriptor, values=values)

    batteries_field = _find_field(groups, "batteries")
    assert batteries_field.entries is not None
    (entry,) = batteries_field.entries
    assert entry.key == "bat1"

    capacity_field = _find_field(entry.groups, "batteries.bat1.capacity_kwh")
    assert capacity_field.value == 10.0

    inputs_field = _find_field(entry.groups, "batteries.bat1.inputs")
    assert inputs_field.present is True

    soc_field = _find_field(inputs_field.nested_groups, "batteries.bat1.inputs.soc")
    unit_field = _find_field(soc_field.nested_groups, "batteries.bat1.inputs.soc.unit")
    assert unit_field.options == ["kwh", "percent"]
    assert unit_field.value == "kwh"
