"""Unit tests for config_editor.submission.parse_submission."""

from __future__ import annotations

from mimirheim_shared.field_shape import FieldShape
from mimirheim_shared.formspec import FieldSpec, FormSpec

from config_editor.submission import parse_submission


def test_scalar_field_present_in_the_submission_is_included() -> None:
    form_spec = FormSpec(fields={"area": FieldSpec(label="Area", description="Bidding area.")})

    values = parse_submission(form_spec, {"area": "SE3"})

    assert values == {"area": "SE3"}


def test_scalar_field_absent_from_the_submission_is_omitted() -> None:
    form_spec = FormSpec(
        fields={
            "area": FieldSpec(label="Area", description="Bidding area."),
            "poll_interval": FieldSpec(label="Poll interval", description="Seconds."),
        }
    )

    values = parse_submission(form_spec, {"area": "SE3"})

    assert values == {"area": "SE3"}


def test_blank_scalar_field_is_omitted_rather_than_passed_through_as_empty_string() -> None:
    form_spec = FormSpec(
        fields={"min_charge_kw": FieldSpec(label="Minimum charge power", description="kW.")}
    )

    values = parse_submission(form_spec, {"min_charge_kw": ""})

    assert values == {}


def test_hidden_field_is_never_included_even_if_present_in_the_raw_submission() -> None:
    form_spec = FormSpec(
        fields={"internal": FieldSpec(label="Internal", description="Not shown.", hidden=True)}
    )

    values = parse_submission(form_spec, {"internal": "sneaky"})

    assert values == {}


class TestNestedObject:
    _NESTED_SPEC = FormSpec(fields={"host": FieldSpec(label="Host", description="Broker host.")})
    _FORM_SPEC = FormSpec(
        fields={
            "mqtt": FieldSpec(
                label="MQTT",
                description="Broker connection.",
                shape=FieldShape.NESTED_OBJECT,
                nested_form_spec=_NESTED_SPEC,
            )
        }
    )

    def test_builds_a_nested_dict_from_dotted_children(self) -> None:
        values = parse_submission(self._FORM_SPEC, {"mqtt.host": "localhost"})

        assert values == {"mqtt": {"host": "localhost"}}

    def test_omitted_entirely_when_no_dotted_children_are_present(self) -> None:
        values = parse_submission(self._FORM_SPEC, {})

        assert values == {}


class TestNamedCollection:
    _NESTED_SPEC = FormSpec(
        fields={"capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh.")}
    )
    _FORM_SPEC = FormSpec(
        fields={
            "batteries": FieldSpec(
                label="Batteries",
                description="Named battery devices.",
                shape=FieldShape.NAMED_COLLECTION,
                nested_form_spec=_NESTED_SPEC,
            )
        }
    )

    def test_builds_one_entry_per_distinct_key_found_in_the_submission(self) -> None:
        raw = {
            "batteries.battery_main.capacity_kwh": "15.0",
            "batteries.battery_sos2_example.capacity_kwh": "10.0",
        }

        values = parse_submission(self._FORM_SPEC, raw)

        assert values == {
            "batteries": {
                "battery_main": {"capacity_kwh": "15.0"},
                "battery_sos2_example": {"capacity_kwh": "10.0"},
            }
        }

    def test_editing_one_entry_leaves_a_sibling_entrys_own_submitted_fields_untouched(self) -> None:
        raw = {
            "batteries.battery_main.capacity_kwh": "99.0",
            "batteries.battery_sos2_example.capacity_kwh": "10.0",
        }

        values = parse_submission(self._FORM_SPEC, raw)

        assert values["batteries"]["battery_sos2_example"] == {"capacity_kwh": "10.0"}

    def test_omitted_entirely_when_the_collection_has_no_entries_in_the_submission(self) -> None:
        values = parse_submission(self._FORM_SPEC, {})

        assert values == {}


class TestOrderedCollection:
    _NESTED_SPEC = FormSpec(
        fields={"power_max_kw": FieldSpec(label="Max power", description="Max power in kW.")}
    )
    _FORM_SPEC = FormSpec(
        fields={
            "charge_segments": FieldSpec(
                label="Charge segments",
                description="Piecewise charge efficiency.",
                shape=FieldShape.ORDERED_COLLECTION,
                nested_form_spec=_NESTED_SPEC,
            )
        }
    )

    def test_builds_a_list_ordered_by_index_regardless_of_raw_key_order(self) -> None:
        raw = {
            "charge_segments.1.power_max_kw": "1.4",
            "charge_segments.0.power_max_kw": "2.5",
        }

        values = parse_submission(self._FORM_SPEC, raw)

        assert values == {"charge_segments": [{"power_max_kw": "2.5"}, {"power_max_kw": "1.4"}]}

    def test_omitted_entirely_when_the_collection_has_no_rows_in_the_submission(self) -> None:
        values = parse_submission(self._FORM_SPEC, {})

        assert values == {}


class TestOptionalObject:
    _NESTED_SPEC = FormSpec(
        fields={"grid_price_weight": FieldSpec(label="Grid price weight", description="Weight.")}
    )
    _FORM_SPEC = FormSpec(
        fields={
            "balanced_weights": FieldSpec(
                label="Balanced weights",
                description="Optional weighting.",
                shape=FieldShape.OPTIONAL_OBJECT,
                nested_form_spec=_NESTED_SPEC,
            )
        }
    )

    def test_builds_a_nested_dict_when_its_fields_were_rendered_and_submitted(self) -> None:
        values = parse_submission(self._FORM_SPEC, {"balanced_weights.grid_price_weight": "0.5"})

        assert values == {"balanced_weights": {"grid_price_weight": "0.5"}}

    def test_omitted_entirely_when_it_was_not_rendered(self) -> None:
        values = parse_submission(self._FORM_SPEC, {})

        assert values == {}


class TestScalarList:
    _FORM_SPEC = FormSpec(
        fields={
            "production_stages": FieldSpec(
                label="Production stages",
                description="Discrete power levels.",
                shape=FieldShape.SCALAR_LIST,
            )
        }
    )

    def test_builds_a_list_ordered_by_index_regardless_of_raw_key_order(self) -> None:
        raw = {
            "production_stages.1": "1.5",
            "production_stages.0": "0.0",
            "production_stages.2": "3.0",
        }

        values = parse_submission(self._FORM_SPEC, raw)

        assert values == {"production_stages": ["0.0", "1.5", "3.0"]}

    def test_a_blank_entry_is_dropped_rather_than_passed_through_as_empty_string(self) -> None:
        raw = {"production_stages.0": "0.0", "production_stages.1": ""}

        values = parse_submission(self._FORM_SPEC, raw)

        assert values == {"production_stages": ["0.0"]}

    def test_omitted_entirely_when_the_list_has_no_rows_in_the_submission(self) -> None:
        values = parse_submission(self._FORM_SPEC, {})

        assert values == {}
