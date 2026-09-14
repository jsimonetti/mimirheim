"""Unit tests for config_editor.server.ConfigEditorServer.

Tests call `handle_request` directly for every assertion, never issuing a
real HTTP request: discovery (listing registered Config Owners, including a
real Descriptor built from mimirheim core's own Config Service `describe()`
step), Tier collapse (Expert fields rendered behind an Advanced disclosure),
and Conditional Visibility (a field hidden or shown depending on another
field's schema default). This is the same directly-call-`handle_request`
pattern the ticket's prior prototype, config_editor_v2, used (see git history
on branch `feat/config-editor-v2`; that prototype is not part of this
working tree).

`ConfigEditorServer.__init__` still binds a real listening socket (it only
ever `serve_forever()`s it once): the `server` fixture below closes that
socket on teardown so no test leaks a file descriptor, even though no test
here ever accepts a connection on it.

The `allowed_ip` restriction (do_GET/do_POST rejecting a source IP that does
not match) is exercised through `handle_request`'s own `client_ip` parameter,
not a live socket connection, keeping it on the same no-live-socket seam as
every other test here: `do_GET`/`do_POST` just forward `self.client_address[0]`
into that parameter.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from mimirheim_shared.config_service import (
    Descriptor,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    handle_validate_and_write,
)
from mimirheim_shared.field_shape import FieldShape
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

from config_editor.registry import ConfigOwnerRegistry
from config_editor.render import RenderedGroup, build_groups
from config_editor.server import ConfigEditorServer

_BATTERIES_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "sample_mimirheim_config_with_batteries.yaml"
)


@pytest.fixture
def registry() -> ConfigOwnerRegistry:
    return ConfigOwnerRegistry()


@pytest.fixture
def config_service_client() -> MagicMock:
    """Fakes ConfigEditorMqttClient; submit_validate_and_write defaults to success,
    get_current_values defaults to no current values (schema defaults alone)."""
    client = MagicMock()
    client.submit_validate_and_write.return_value = ValidateAndWriteResult(request_id="req-1", success=True)
    client.get_current_values.return_value = {}
    return client


@pytest.fixture
def server(registry: ConfigOwnerRegistry, config_service_client: MagicMock) -> Iterator[ConfigEditorServer]:
    instance = ConfigEditorServer(registry, config_service_client)
    yield instance
    instance._httpd.server_close()


def _get(server: ConfigEditorServer, path: str) -> tuple[int, str]:
    status, _headers, body = server.handle_request("GET", path, body=b"")
    return status, body.decode("utf-8")


def _post(server: ConfigEditorServer, path: str, form: dict[str, str]) -> tuple[int, str]:
    body = urllib.parse.urlencode(form).encode("utf-8")
    status, _headers, response_body = server.handle_request("POST", path, body=body)
    return status, response_body.decode("utf-8")


def test_index_lists_no_owners_when_registry_is_empty(server: ConfigEditorServer) -> None:
    status, body = _get(server, "/")

    assert status == 200
    assert "No Config Owners discovered yet." in body
    assert "/owners/" not in body


def test_index_lists_mimirheim_core_from_its_real_descriptor(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    """Registers a real Descriptor built via mimirheim core's own describe() step."""
    from mimirheim.io import config_service as core_config_service

    descriptor = Descriptor.model_validate_json(core_config_service.payload_bytes())
    registry.update(descriptor)

    status, body = _get(server, "/")

    assert status == 200
    assert "Mimirheim" in body
    assert "/owners/mimirheim-core" in body


def test_unknown_owner_page_returns_404(server: ConfigEditorServer) -> None:
    status, _body = _get(server, "/owners/does-not-exist")

    assert status == 404


def test_unknown_path_returns_404(server: ConfigEditorServer) -> None:
    status, _body = _get(server, "/nonsense")

    assert status == 404


def test_owner_page_renders_basic_fields_directly(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    registry.update(
        Descriptor(
            owner_id="nordpool",
            display_name="Nordpool prices",
            json_schema={"properties": {}},
            form_spec=FormSpec(
                fields={"area": FieldSpec(label="Price area", description="Nordpool bidding area.")}
            ),
        )
    )

    status, body = _get(server, "/owners/nordpool")

    assert status == 200
    assert "Price area" in body
    assert "Nordpool bidding area." in body


def test_owner_page_collapses_expert_fields_behind_advanced_disclosure(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    registry.update(
        Descriptor(
            owner_id="nordpool",
            display_name="Nordpool prices",
            json_schema={"properties": {}},
            form_spec=FormSpec(
                fields={
                    "area": FieldSpec(label="Price area", description="Nordpool bidding area.", tier=Tier.BASIC),
                    "poll_interval": FieldSpec(
                        label="Poll interval", description="Seconds between polls.", tier=Tier.EXPERT
                    ),
                }
            ),
        )
    )

    status, body = _get(server, "/owners/nordpool")

    assert status == 200
    # Basic field renders outside any <details> disclosure.
    assert body.index("Price area") < body.index("<details")
    # Expert field is inside the <details>...</details> Advanced disclosure.
    details_start = body.index("<details")
    details_end = body.index("</details>")
    assert details_start < body.index("Poll interval") < details_end
    assert "Advanced" in body[details_start:details_end]


def test_owner_page_applies_conditional_visibility(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    registry.update(
        Descriptor(
            owner_id="homeassistant-helper",
            display_name="HA helper",
            json_schema={
                "properties": {
                    "enabled": {"type": "boolean", "default": False},
                    "discovery_prefix": {"type": "string", "default": "homeassistant"},
                }
            },
            form_spec=FormSpec(
                fields={
                    "enabled": FieldSpec(label="Enable HA discovery", description="Turn on autodiscovery."),
                    "discovery_prefix": FieldSpec(
                        label="Discovery prefix",
                        description="HA discovery topic prefix.",
                        visible_if=Comparison(field="enabled", operator=ComparisonOperator.EQ, value=True),
                    ),
                }
            ),
        )
    )

    status, body = _get(server, "/owners/homeassistant-helper")

    assert status == 200
    assert "Enable HA discovery" in body
    assert "Discovery prefix" not in body


def test_owner_page_shows_conditionally_visible_field_when_condition_holds(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    registry.update(
        Descriptor(
            owner_id="homeassistant-helper",
            display_name="HA helper",
            json_schema={
                "properties": {
                    "enabled": {"type": "boolean", "default": True},
                    "discovery_prefix": {"type": "string", "default": "homeassistant"},
                }
            },
            form_spec=FormSpec(
                fields={
                    "enabled": FieldSpec(label="Enable HA discovery", description="Turn on autodiscovery."),
                    "discovery_prefix": FieldSpec(
                        label="Discovery prefix",
                        description="HA discovery topic prefix.",
                        visible_if=Comparison(field="enabled", operator=ComparisonOperator.EQ, value=True),
                    ),
                }
            ),
        )
    )

    status, body = _get(server, "/owners/homeassistant-helper")

    assert status == 200
    assert "Discovery prefix" in body


def _register_optional_object_owner(registry: ConfigOwnerRegistry) -> None:
    registry.update(
        Descriptor(
            owner_id="owner-with-optional",
            display_name="Owner with optional section",
            json_schema={"properties": {}},
            form_spec=FormSpec(
                fields={
                    "balanced_weights": FieldSpec(
                        label="Balanced weights",
                        description="Optional weighting.",
                        shape=FieldShape.OPTIONAL_OBJECT,
                        nested_form_spec=FormSpec(
                            fields={
                                "cost_weight": FieldSpec(label="Cost weight", description="Weight.")
                            }
                        ),
                    )
                }
            ),
        )
    )


def test_presence_toggle_renders_a_disabled_hidden_fieldset_when_currently_absent(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_optional_object_owner(registry)
    config_service_client.get_current_values.return_value = {"balanced_weights": None}

    status, body = _get(server, "/owners/owner-with-optional")

    assert status == 200
    assert 'data-presence-toggle' in body
    assert 'name="balanced_weights"' in body
    assert "checked" not in body.split("data-presence-toggle", 1)[1].split(">", 1)[0]
    assert "hidden disabled>" in body


def test_presence_toggle_renders_an_enabled_visible_fieldset_when_currently_present(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_optional_object_owner(registry)
    config_service_client.get_current_values.return_value = {
        "balanced_weights": {"cost_weight": 0.5}
    }

    status, body = _get(server, "/owners/owner-with-optional")

    assert status == 200
    assert "hidden disabled>" not in body
    assert 'name="balanced_weights.cost_weight"' in body


def test_presence_toggle_javascript_is_present_exactly_once(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_optional_object_owner(registry)
    config_service_client.get_current_values.return_value = {"balanced_weights": None}

    status, body = _get(server, "/owners/owner-with-optional")

    assert status == 200
    assert body.count("data-presence-toggle") == 2  # the <input> attribute and the JS selector
    assert "addEventListener(\"change\"" in body


def test_presence_toggle_renders_as_a_bootstrap_switch(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_optional_object_owner(registry)
    config_service_client.get_current_values.return_value = {"balanced_weights": None}

    status, body = _get(server, "/owners/owner-with-optional")

    assert status == 200
    assert "form-switch" in body
    assert 'role="switch"' in body


def _register_boolean_owner(registry: ConfigOwnerRegistry) -> None:
    registry.update(
        Descriptor(
            owner_id="ha-helper",
            display_name="HA helper",
            json_schema={"properties": {"enabled": {"type": "boolean"}}},
            form_spec=FormSpec(fields={"enabled": FieldSpec(label="Enabled", description="Turn it on.")}),
        )
    )


def test_boolean_field_renders_as_a_bootstrap_switch_not_a_plain_checkbox(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_boolean_owner(registry)
    config_service_client.get_current_values.return_value = {"enabled": True}

    status, body = _get(server, "/owners/ha-helper")

    assert status == 200
    assert 'type="checkbox"' in body
    assert "form-switch" in body
    assert 'role="switch"' in body
    assert 'checked' in body


def test_boolean_field_renders_a_hidden_false_fallback_after_the_checkbox(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_boolean_owner(registry)
    config_service_client.get_current_values.return_value = {"enabled": True}

    status, body = _get(server, "/owners/ha-helper")

    assert status == 200
    assert '<input type="hidden" name="enabled" value="false">' in body
    # The checkbox must precede its hidden fallback in document order: a
    # browser submits same-named fields in document order, and
    # _parse_form_body keeps only the first value, so a checked box's "on"
    # has to arrive before the fallback's "false" for the checked state to
    # win.
    assert body.index('type="checkbox"') < body.index('type="hidden" name="enabled"')


def test_unchecking_a_boolean_field_submits_false_rather_than_omitting_it(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_boolean_owner(registry)
    # An unchecked switch sends nothing at all; only its hidden fallback is
    # submitted, matching what a real browser would send.
    status, _body = _post(server, "/owners/ha-helper", {"enabled": "false"})

    assert status == 200
    config_service_client.submit_validate_and_write.assert_called_once_with("ha-helper", {"enabled": "false"})


def test_checking_a_boolean_field_submits_on_even_with_the_hidden_fallback_present(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_boolean_owner(registry)
    # Mimics a real browser submitting a *checked* switch: both the checkbox
    # ("on") and its hidden false-fallback are sent, in the document order
    # they are rendered in (checkbox first).
    body = urllib.parse.urlencode([("enabled", "on"), ("enabled", "false")]).encode("utf-8")

    status, _headers, _response_body = server.handle_request("POST", "/owners/ha-helper", body=body)

    assert status == 200
    config_service_client.submit_validate_and_write.assert_called_once_with("ha-helper", {"enabled": "on"})


def _register_numeric_owner(registry: ConfigOwnerRegistry, *, field_schema: dict) -> None:
    registry.update(
        Descriptor(
            owner_id="numeric-owner",
            display_name="Numeric owner",
            json_schema={"properties": {"value": field_schema}},
            form_spec=FormSpec(fields={"value": FieldSpec(label="Value", description="A number.")}),
        )
    )


def test_integer_field_renders_number_input_with_step_one(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_numeric_owner(registry, field_schema={"type": "integer"})

    status, body = _get(server, "/owners/numeric-owner")

    assert status == 200
    assert 'type="number"' in body
    assert 'step="1"' in body


def test_float_field_renders_number_input_with_step_any(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_numeric_owner(registry, field_schema={"type": "number"})

    status, body = _get(server, "/owners/numeric-owner")

    assert status == 200
    assert 'type="number"' in body
    assert 'step="any"' in body


def test_float_field_with_multiple_of_uses_it_as_step(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_numeric_owner(registry, field_schema={"type": "number", "multipleOf": 0.05})

    status, body = _get(server, "/owners/numeric-owner")

    assert status == 200
    assert 'step="0.05"' in body


def test_numeric_field_with_ge_le_renders_matching_min_max(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_numeric_owner(registry, field_schema={"type": "integer", "minimum": 1, "maximum": 65535})

    status, body = _get(server, "/owners/numeric-owner")

    assert status == 200
    assert 'min="1"' in body
    assert 'max="65535"' in body


def test_numeric_field_with_no_declared_bound_has_no_min_or_max_attribute(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_numeric_owner(registry, field_schema={"type": "number"})

    status, body = _get(server, "/owners/numeric-owner")

    assert status == 200
    assert "min=" not in body
    assert "max=" not in body


def _register_suggested_value_owner(registry: ConfigOwnerRegistry) -> None:
    registry.update(
        Descriptor(
            owner_id="suggested-owner",
            display_name="Suggested owner",
            json_schema={"properties": {"import_limit_kw": {"type": "number"}}},
            form_spec=FormSpec(
                fields={
                    "import_limit_kw": FieldSpec(
                        label="Import limit", description="Grid import limit in kW.", suggested_value=5000
                    )
                }
            ),
        )
    )


def test_scalar_field_with_suggested_value_renders_it_as_help_text(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_suggested_value_owner(registry)

    status, body = _get(server, "/owners/suggested-owner")

    assert status == 200
    assert "Suggested: 5000" in body


def test_suggested_value_never_prefills_the_input(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_suggested_value_owner(registry)

    status, body = _get(server, "/owners/suggested-owner")

    assert status == 200
    assert 'value="5000"' not in body


def test_field_with_on_disk_value_and_suggested_value_shows_on_disk_value_not_suggestion(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_suggested_value_owner(registry)
    config_service_client.get_current_values.return_value = {"import_limit_kw": 7500}

    status, body = _get(server, "/owners/suggested-owner")

    assert status == 200
    assert 'value="7500"' in body
    assert 'value="5000"' not in body
    assert "Suggested: 5000" in body


def test_suggested_value_is_not_rendered_for_nested_object_field(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    registry.update(
        Descriptor(
            owner_id="nested-suggested",
            display_name="Nested suggested",
            json_schema={"properties": {}},
            form_spec=FormSpec(
                fields={
                    "mqtt": FieldSpec(
                        label="MQTT",
                        description="Broker connection.",
                        shape=FieldShape.NESTED_OBJECT,
                        suggested_value="ignored",
                        nested_form_spec=FormSpec(
                            fields={"host": FieldSpec(label="Host", description="Broker host.")}
                        ),
                    )
                }
            ),
        )
    )

    status, body = _get(server, "/owners/nested-suggested")

    assert status == 200
    assert "Suggested:" not in body


def test_suggested_value_is_not_rendered_for_named_collection_field(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    registry.update(
        Descriptor(
            owner_id="named-collection-suggested",
            display_name="Named collection suggested",
            json_schema={
                "properties": {
                    "batteries": {"type": "object", "additionalProperties": {"$ref": "#/$defs/Battery"}}
                },
                "$defs": {"Battery": {"type": "object", "properties": {"capacity_kwh": {"type": "number"}}}},
            },
            form_spec=FormSpec(
                fields={
                    "batteries": FieldSpec(
                        label="Batteries",
                        description="Named battery devices.",
                        shape=FieldShape.NAMED_COLLECTION,
                        suggested_value="ignored",
                        nested_form_spec=FormSpec(
                            fields={
                                "capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh.")
                            }
                        ),
                    )
                }
            ),
        )
    )

    status, body = _get(server, "/owners/named-collection-suggested")

    assert status == 200
    assert "Suggested:" not in body


def test_suggested_value_is_not_rendered_for_ordered_collection_field(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    registry.update(
        Descriptor(
            owner_id="ordered-collection-suggested",
            display_name="Ordered collection suggested",
            json_schema={
                "properties": {"charge_segments": {"type": "array", "items": {"$ref": "#/$defs/Segment"}}},
                "$defs": {"Segment": {"type": "object", "properties": {"power_max_kw": {"type": "number"}}}},
            },
            form_spec=FormSpec(
                fields={
                    "charge_segments": FieldSpec(
                        label="Charge segments",
                        description="Piecewise charge efficiency.",
                        shape=FieldShape.ORDERED_COLLECTION,
                        suggested_value="ignored",
                        nested_form_spec=FormSpec(
                            fields={
                                "power_max_kw": FieldSpec(label="Max power", description="Max power in kW.")
                            }
                        ),
                    )
                }
            ),
        )
    )

    status, body = _get(server, "/owners/ordered-collection-suggested")

    assert status == 200
    assert "Suggested:" not in body


def test_suggested_value_is_not_rendered_for_optional_object_field(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    registry.update(
        Descriptor(
            owner_id="optional-object-suggested",
            display_name="Optional object suggested",
            json_schema={"properties": {}},
            form_spec=FormSpec(
                fields={
                    "balanced_weights": FieldSpec(
                        label="Balanced weights",
                        description="Optional weighting.",
                        shape=FieldShape.OPTIONAL_OBJECT,
                        suggested_value="ignored",
                        nested_form_spec=FormSpec(
                            fields={"cost_weight": FieldSpec(label="Cost weight", description="Weight.")}
                        ),
                    )
                }
            ),
        )
    )

    status, body = _get(server, "/owners/optional-object-suggested")

    assert status == 200
    assert "Suggested:" not in body


def _register_nordpool(registry: ConfigOwnerRegistry) -> None:
    registry.update(
        Descriptor(
            owner_id="nordpool",
            display_name="Nordpool prices",
            json_schema={"properties": {"area": {"type": "string", "default": "SE1"}}},
            form_spec=FormSpec(
                fields={"area": FieldSpec(label="Price area", description="Nordpool bidding area.")}
            ),
        )
    )


def test_post_unknown_owner_returns_404(server: ConfigEditorServer) -> None:
    status, _body = _post(server, "/owners/does-not-exist", {"area": "SE3"})

    assert status == 404


def test_post_success_shows_confirmation_and_forwards_values(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_nordpool(registry)

    status, body = _post(server, "/owners/nordpool", {"area": "SE3"})

    assert status == 200
    assert "Saved" in body
    config_service_client.submit_validate_and_write.assert_called_once_with("nordpool", {"area": "SE3"})


def test_post_validation_failure_shows_errors_and_keeps_submitted_values(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_nordpool(registry)
    config_service_client.submit_validate_and_write.return_value = ValidateAndWriteResult(
        request_id="req-1", success=False, errors=["area: not a valid bidding area"]
    )

    status, body = _post(server, "/owners/nordpool", {"area": "NOT-AN-AREA"})

    assert status == 200
    assert "area: not a valid bidding area" in body
    assert 'value="NOT-AN-AREA"' in body


def test_get_fetches_and_renders_current_values(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_nordpool(registry)
    config_service_client.get_current_values.return_value = {"area": "SE1"}

    status, body = _get(server, "/owners/nordpool")

    assert status == 200
    config_service_client.get_current_values.assert_called_once_with("nordpool")
    assert 'value="SE1"' in body


def test_get_falls_back_to_schema_defaults_when_get_current_values_times_out(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer, config_service_client: MagicMock
) -> None:
    _register_nordpool(registry)
    config_service_client.get_current_values.side_effect = TimeoutError("no response")

    status, body = _get(server, "/owners/nordpool")

    assert status == 200
    assert 'value="SE1"' in body  # the Descriptor's own schema default for "area"


def _flatten_leaf_values(groups: list[RenderedGroup]) -> dict[str, str]:
    """Collects every leaf field's dotted name/value from a rendered tree.

    Mirrors exactly what a real browser submits for the same rendered page
    (every `<input>`/`<select>`, at every nesting level), without scraping
    the rendered HTML itself.
    """
    values: dict[str, str] = {}
    for group in groups:
        for rendered_field in (*group.basic_fields, *group.expert_fields):
            if rendered_field.entries is not None:
                for entry in rendered_field.entries:
                    values.update(_flatten_leaf_values(entry.groups))
            elif rendered_field.nested_groups is not None:
                values.update(_flatten_leaf_values(rendered_field.nested_groups))
            elif rendered_field.value is not None:
                values[rendered_field.name] = str(rendered_field.value)
    return values


def test_e2e_editing_a_nested_named_collection_field_round_trips(
    registry: ConfigOwnerRegistry, tmp_path: Path
) -> None:
    """Ticket 08's own end-to-end criterion: editing one battery's
    capacity_kwh round-trips through discovery, render, submit, and
    validate_and_write, leaving the sibling battery and unrelated fields
    (mqtt.host, its comment) untouched."""
    from mimirheim.config.schema import MimirheimConfig
    from mimirheim.io import config_service as core_config_service

    config_path = tmp_path / "mimirheim.yaml"
    config_path.write_text(_BATTERIES_FIXTURE_PATH.read_text())

    descriptor = Descriptor.model_validate_json(core_config_service.payload_bytes())
    registry.update(descriptor)

    config_service_client = MagicMock()
    config_service_client.get_current_values.side_effect = lambda owner_id, timeout=10.0: (
        yaml.safe_load(config_path.read_text()) or {}
    )

    def _submit(owner_id: str, values: dict, timeout: float = 10.0) -> ValidateAndWriteResult:
        request = ValidateAndWriteRequest(request_id="req-1", values=values)
        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path, MimirheimConfig
        )
        return ValidateAndWriteResult.model_validate_json(response)

    config_service_client.submit_validate_and_write.side_effect = _submit

    server = ConfigEditorServer(registry, config_service_client)
    try:
        # Discovery + render.
        status, body = _get(server, "/owners/mimirheim-core")
        assert status == 200
        assert "battery_main" in body
        assert "battery_sos2_example" in body
        assert 'name="batteries.battery_main.capacity_kwh"' in body

        # Build the full submission exactly as the rendered page would, then
        # change only one nested field.
        current_values = config_service_client.get_current_values("mimirheim-core")
        form = _flatten_leaf_values(build_groups(descriptor, values=current_values))
        form["batteries.battery_main.capacity_kwh"] = "6.0"

        status, body = _post(server, "/owners/mimirheim-core", form)

        assert status == 200
        assert "Saved" in body
        config_service_client.submit_validate_and_write.assert_called_once()

        written = yaml.safe_load(config_path.read_text())
        assert written["batteries"]["battery_main"]["capacity_kwh"] == 6.0
        # Sibling entry and unrelated top-level field survive untouched.
        assert written["batteries"]["battery_sos2_example"]["capacity_kwh"] == 10.0
        assert written["mqtt"]["host"] == "localhost"
        raw = config_path.read_text()
        assert "# usable capacity" in raw
    finally:
        server._httpd.server_close()


# ---------------------------------------------------------------------------
# allowed_ip restriction
# ---------------------------------------------------------------------------

@pytest.fixture
def server_with_allowed_ip(
    registry: ConfigOwnerRegistry, config_service_client: MagicMock
) -> Iterator[ConfigEditorServer]:
    instance = ConfigEditorServer(registry, config_service_client, allowed_ip="172.30.32.1")
    yield instance
    instance._httpd.server_close()


def test_get_from_disallowed_ip_is_rejected(server_with_allowed_ip: ConfigEditorServer) -> None:
    status, _headers, _body = server_with_allowed_ip.handle_request(
        "GET", "/", body=b"", client_ip="10.0.0.5"
    )
    assert status == 403


def test_get_from_allowed_ip_is_served(server_with_allowed_ip: ConfigEditorServer) -> None:
    status, _headers, body = server_with_allowed_ip.handle_request(
        "GET", "/", body=b"", client_ip="172.30.32.1"
    )
    assert status == 200
    assert "No Config Owners discovered yet." in body.decode("utf-8")


def test_post_from_disallowed_ip_is_rejected(server_with_allowed_ip: ConfigEditorServer) -> None:
    status, _headers, _body = server_with_allowed_ip.handle_request(
        "POST", "/owners/mimirheim-core", body=b"", client_ip="10.0.0.5"
    )
    assert status == 403


def test_no_allowed_ip_configured_accepts_any_client_ip(server: ConfigEditorServer) -> None:
    """When allowed_ip is unset (the default), the client_ip parameter is ignored."""
    status, _headers, _body = server.handle_request("GET", "/", body=b"", client_ip="10.0.0.5")
    assert status == 200


def test_client_ip_omitted_is_not_restricted(server_with_allowed_ip: ConfigEditorServer) -> None:
    """Existing callers that never pass client_ip (e.g. every other test in this
    module) must keep working unrestricted, even when allowed_ip is configured:
    the restriction only applies once the real HTTP layer supplies a client_ip."""
    status, _headers, _body = server_with_allowed_ip.handle_request("GET", "/", body=b"")
    assert status == 200


# ---------------------------------------------------------------------------
# Static asset serving (vendored Bootstrap5)
# ---------------------------------------------------------------------------


def test_static_asset_served_for_real_vendored_file(server: ConfigEditorServer) -> None:
    status, headers, body = server.handle_request(
        "GET", "/static/vendor/bootstrap/bootstrap.min.css", body=b""
    )

    assert status == 200
    assert headers["Content-Type"] == "text/css"
    assert len(body) > 1000


def test_static_asset_path_traversal_is_rejected(server: ConfigEditorServer) -> None:
    status, _headers, _body = server.handle_request(
        "GET", "/static/../config.py", body=b""
    )

    assert status in (403, 404)


def test_static_asset_disallowed_extension_is_rejected(server: ConfigEditorServer) -> None:
    """LICENSE is a real vendored file, but has no allowed extension."""
    status, _headers, _body = server.handle_request(
        "GET", "/static/vendor/bootstrap/LICENSE", body=b""
    )

    assert status == 403


def test_static_asset_missing_file_returns_404(server: ConfigEditorServer) -> None:
    status, _headers, _body = server.handle_request(
        "GET", "/static/vendor/bootstrap/does-not-exist.css", body=b""
    )

    assert status == 404


def test_index_page_references_vendored_bootstrap_css_and_uses_bootstrap_classes(
    server: ConfigEditorServer,
) -> None:
    status, body = _get(server, "/")

    assert status == 200
    assert '/static/vendor/bootstrap/bootstrap.min.css' in body
    assert "navbar" in body


def test_owner_page_references_vendored_bootstrap_assets_and_uses_bootstrap_classes(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_nordpool(registry)

    status, body = _get(server, "/owners/nordpool")

    assert status == 200
    assert '/static/vendor/bootstrap/bootstrap.min.css' in body
    assert "form-control" in body
    assert "btn btn-primary" in body


def test_no_page_loads_assets_from_a_network_location(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    """Every asset a page references must be vendored, not fetched from a CDN."""
    _register_nordpool(registry)

    for path in ("/", "/owners/nordpool"):
        _status, body = _get(server, path)
        assert "https://" not in body
        assert "http://" not in body


# ---------------------------------------------------------------------------
# Dark/light theme (ticket 12)
# ---------------------------------------------------------------------------


def test_index_omits_data_bs_theme_when_no_theme_cookie_is_set(server: ConfigEditorServer) -> None:
    """No cookie: the OS/browser preference decides, via CSS alone, so the
    server must not force a light or dark theme onto the page."""
    status, _headers, body = server.handle_request("GET", "/", body=b"")

    assert status == 200
    assert "data-bs-theme" not in body.decode("utf-8")


def test_index_renders_dark_theme_when_dark_cookie_is_set(server: ConfigEditorServer) -> None:
    status, _headers, body = server.handle_request(
        "GET", "/", body=b"", cookie="theme=dark"
    )

    assert status == 200
    assert 'data-bs-theme="dark"' in body.decode("utf-8")


def test_index_renders_light_theme_when_light_cookie_is_set(server: ConfigEditorServer) -> None:
    status, _headers, body = server.handle_request(
        "GET", "/", body=b"", cookie="theme=light"
    )

    assert status == 200
    assert 'data-bs-theme="light"' in body.decode("utf-8")


def test_owner_page_honours_theme_cookie(
    registry: ConfigOwnerRegistry, server: ConfigEditorServer
) -> None:
    _register_nordpool(registry)

    status, _headers, body = server.handle_request(
        "GET", "/owners/nordpool", body=b"", cookie="theme=dark"
    )

    assert status == 200
    assert 'data-bs-theme="dark"' in body.decode("utf-8")


def test_an_invalid_theme_cookie_value_is_ignored(server: ConfigEditorServer) -> None:
    status, _headers, body = server.handle_request(
        "GET", "/", body=b"", cookie="theme=purple"
    )

    assert status == 200
    assert "data-bs-theme" not in body.decode("utf-8")


def test_index_shows_a_theme_toggle_control(server: ConfigEditorServer) -> None:
    status, _headers, body = server.handle_request("GET", "/", body=b"")
    decoded = body.decode("utf-8")

    assert status == 200
    assert "/theme/dark" in decoded


def test_setting_theme_to_dark_sets_cookie_and_redirects(server: ConfigEditorServer) -> None:
    status, headers, _body = server.handle_request(
        "GET", "/theme/dark?next=/owners/nordpool", body=b""
    )

    assert status == 302
    assert headers["Location"] == "/owners/nordpool"
    assert "theme=dark" in headers["Set-Cookie"]


def test_setting_theme_to_light_sets_cookie_and_redirects(server: ConfigEditorServer) -> None:
    status, headers, _body = server.handle_request("GET", "/theme/light", body=b"")

    assert status == 302
    assert headers["Location"] == "/"
    assert "theme=light" in headers["Set-Cookie"]


def test_setting_theme_rejects_an_unrecognised_theme_name(server: ConfigEditorServer) -> None:
    status, _headers, _body = server.handle_request("GET", "/theme/purple", body=b"")

    assert status == 404


def test_setting_theme_rejects_an_off_site_next_redirect(server: ConfigEditorServer) -> None:
    """`next` must stay same-origin: a protocol-relative URL like `//evil.example`
    would otherwise let a crafted link redirect the user off-site."""
    status, headers, _body = server.handle_request(
        "GET", "/theme/dark?next=//evil.example", body=b""
    )

    assert status == 302
    assert headers["Location"] == "/"


def test_theme_css_is_served_and_reacts_to_prefers_color_scheme(server: ConfigEditorServer) -> None:
    status, headers, body = server.handle_request("GET", "/static/theme.css", body=b"")

    assert status == 200
    assert headers["Content-Type"] == "text/css"
    assert "prefers-color-scheme" in body.decode("utf-8")


def test_index_references_theme_css(server: ConfigEditorServer) -> None:
    status, _headers, body = server.handle_request("GET", "/", body=b"")

    assert status == 200
    assert "/static/theme.css" in body.decode("utf-8")
