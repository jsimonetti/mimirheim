"""Unit tests for config_editor_v3.server.ConfigEditorServer.

Tests call `handle_request` directly for every assertion, never issuing a
real HTTP request: discovery (listing registered Config Owners, including a
real Descriptor built from mimirheim core's own Config Service `describe()`
step), Tier collapse (Expert fields rendered behind an Advanced disclosure),
and Conditional Visibility (a field hidden or shown depending on another
field's schema default). This is the same directly-call-`handle_request`
pattern the ticket's prior prototype, config_editor_v2, used (see git history
on branch `feat/config-editor-v2`; that prototype is not part of this
working tree).

`ConfigEditorServer.__init__` still binds a real listening socket (matching
`config_editor`/`config_editor_v2`'s own constructor, which only ever
`serve_forever()`s it once): the `server` fixture below closes that socket on
teardown so no test leaks a file descriptor, even though no test here ever
accepts a connection on it.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from mimirheim_shared.config_service import Descriptor, ValidateAndWriteResult
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

from config_editor_v3.registry import ConfigOwnerRegistry
from config_editor_v3.server import ConfigEditorServer


@pytest.fixture
def registry() -> ConfigOwnerRegistry:
    return ConfigOwnerRegistry()


@pytest.fixture
def config_service_client() -> MagicMock:
    """Fakes ConfigEditorMqttClient.submit_validate_and_write; defaults to success."""
    client = MagicMock()
    client.submit_validate_and_write.return_value = ValidateAndWriteResult(request_id="req-1", success=True)
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
