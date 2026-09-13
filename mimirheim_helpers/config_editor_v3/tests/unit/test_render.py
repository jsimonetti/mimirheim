"""Unit tests for config_editor_v3.render: grouping, Tier collapse, Conditional Visibility."""

from __future__ import annotations

from mimirheim_shared.config_service import Descriptor
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

from config_editor_v3.render import UNGROUPED_LABEL, build_groups, schema_default_values


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
