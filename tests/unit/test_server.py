"""Unit tests for config-editor-v2's HTTP server.

This module lives at the repo root, not under
mimirheim_helpers/config_editor_v2/tests/, for the same reason v1's
equivalent server tests do (see mimirheim_helpers/config_editor/AGENTS.md
and tests/unit/test_config_editor_server.py): ConfigEditorV2Server imports
across package boundaries, unlike the isolated unit tests for the earlier
71_1-71_4 steps.

Tests instantiate ConfigEditorV2Server directly using a temp directory as
config_dir and a small set of fixture Pydantic models (defined below) as its
registry, dispatched in-process via `handle_request` -- no live socket.
Fixture models are used instead of the real production registry
(config_editor_v2.registry.REGISTRY) because, as of this step, none of the
real production models have a valid no-argument default (every one requires
at least an `mqtt` block with no default); see test_registry_real_entries.py
and this step's final report. Using deterministic fixture models here keeps
these tests focused on server/API behaviour rather than on that separate,
already-reported finding.

What this module does not cover:
- The real production registry's contents and defaults: see
  test_registry_real_entries.py.
- Full HTTP round-trips with a live socket or frontend JavaScript rendering.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from config_editor_v2.registry import RegistryEntry
from config_editor_v2.server import ConfigEditorV2Server

# ---------------------------------------------------------------------------
# Fixture models
#
# `_WidgetConfig` and `_GadgetConfig` carry full defaults so that `Model()`
# succeeds, unlike every entry in the real production registry.
# `_WidgetConfig.tags` exercises the `nullable-list` adapter transform end
# to end through the HTTP layer. `_RequiredConfig` deliberately does *not*
# have a valid all-defaults state, to mirror the real production registry
# and exercise the "untouched entry with no defaults is excluded, not
# blocking" save path.
# ---------------------------------------------------------------------------


class _WidgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="widget", json_schema_extra={"x-mimir-label": "Name"})
    tags: list[str] | None = Field(
        default=None,
        min_length=1,
        json_schema_extra={"x-mimir-adapter": "nullable-list"},
    )


class _GadgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    power_kw: float = Field(default=1.0, ge=0.0)


class _RequiredConfig(BaseModel):
    """A fixture model with a required field and no default.

    Mirrors every real production model in config_editor_v2.registry, all
    of which require at least an `mqtt` block with no default. Used by
    test_post_save_untouched_entry_without_defaults_is_excluded_not_blocking
    to prove that an entry absent from the POST /api/save body -- as a real
    never-configured helper would be -- is excluded from the save (not
    written) without blocking the save of the entries the user did submit.
    """

    model_config = ConfigDict(extra="forbid")

    name: str


class _ExtraSectionConfig(BaseModel):
    """A nested object-typed section, used only via `_SectionedConfig.extra`."""

    model_config = ConfigDict(extra="forbid")

    label: str = "default-label"


class _SectionedConfig(BaseModel):
    """A fixture model with a `default_factory` nested object field.

    `extra` mirrors the shape of real, unconfigured MimirheimConfig sections
    such as `objectives` or `constraints`: `model_json_schema()` never emits
    a literal `default` for a `default_factory` field, so a partial on-disk
    YAML that omits the key must be filled in from the model's actual
    default by GET /api/entries/{name}/data, not left absent -- Jedison
    cannot construct a value for an object-typed field with no default and
    no data. See tests below for the two cases this must handle: a valid
    partial file (filled in) and an invalid one (rejected, not passed
    through).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = "sectioned"
    extra: _ExtraSectionConfig = Field(default_factory=_ExtraSectionConfig)
    power_kw: float = Field(default=1.0, ge=0.0)


def _widget_entry() -> RegistryEntry:
    return RegistryEntry(
        name="Widget",
        filename="widget.yaml",
        model_path=f"{_WidgetConfig.__module__}.{_WidgetConfig.__qualname__}",
    )


def _gadget_entry() -> RegistryEntry:
    return RegistryEntry(
        name="Gadget",
        filename="gadget.yaml",
        model_path=f"{_GadgetConfig.__module__}.{_GadgetConfig.__qualname__}",
    )


def _required_entry() -> RegistryEntry:
    return RegistryEntry(
        name="Required",
        filename="required.yaml",
        model_path=f"{_RequiredConfig.__module__}.{_RequiredConfig.__qualname__}",
    )


def _sectioned_entry() -> RegistryEntry:
    return RegistryEntry(
        name="Sectioned",
        filename="sectioned.yaml",
        model_path=f"{_SectionedConfig.__module__}.{_SectionedConfig.__qualname__}",
    )


def _make_server(tmp_path: Path, entries: list[RegistryEntry] | None = None) -> ConfigEditorV2Server:
    """Returns a ConfigEditorV2Server wired to a temp config directory.

    Defaults to the two fixture entries above when `entries` is not given.
    """
    if entries is None:
        entries = [_widget_entry(), _gadget_entry()]
    return ConfigEditorV2Server(config_dir=tmp_path, port=0, entries=entries)


def _get(server: ConfigEditorV2Server, path: str) -> tuple[int, dict[str, str], bytes]:
    return server.handle_request("GET", path, body=b"")


def _post(server: ConfigEditorV2Server, path: str, data: Any) -> tuple[int, dict[str, str], bytes]:
    return server.handle_request("POST", path, body=json.dumps(data).encode())


# ---------------------------------------------------------------------------
# GET /api/entries
# ---------------------------------------------------------------------------


def test_get_entries_returns_registered_schemas(tmp_path: Path) -> None:
    """GET /api/entries includes an entry for MimirheimConfig with a Jedison-shaped schema.

    Uses the real production registry here specifically because this test
    only inspects schema shape (title, properties) -- it never instantiates
    a model -- so the "no valid no-argument default" finding that motivates
    using fixture models elsewhere in this file does not apply.
    """
    from config_editor_v2.registry import REGISTRY

    server = ConfigEditorV2Server(config_dir=tmp_path, port=0, entries=REGISTRY)
    status, _headers, body = _get(server, "/api/entries")
    assert status == 200
    data = json.loads(body)

    mimirheim_entries = [item for item in data if item["name"] == "Mimirheim"]
    assert len(mimirheim_entries) == 1
    entry = mimirheim_entries[0]
    assert entry["filename"] == "mimirheim.yaml"
    assert entry["schema"]["title"] == "Mimirheim Configuration"
    assert "properties" in entry["schema"]


def test_get_entries_applies_nullable_list_transform(tmp_path: Path) -> None:
    """The fixture Widget entry's `tags` field is rendered as a plain array.

    Proves GET /api/entries actually chains adapter.transform_schema (which
    dispatches nullable-list) rather than serving the raw model schema.
    """
    server = _make_server(tmp_path)
    status, _headers, body = _get(server, "/api/entries")
    assert status == 200
    data = json.loads(body)

    widget = next(item for item in data if item["name"] == "Widget")
    tags_schema = widget["schema"]["properties"]["tags"]
    assert tags_schema["type"] == "array"
    assert "anyOf" not in tags_schema
    assert tags_schema["x-mimir-min-length-hint"] == 1


# ---------------------------------------------------------------------------
# GET /api/entries/{name}/data
# ---------------------------------------------------------------------------


def test_get_entry_data_when_file_absent_returns_defaults(tmp_path: Path) -> None:
    """No widget.yaml on disk; the response reflects the model's defaults."""
    server = _make_server(tmp_path)
    status, _headers, body = _get(server, "/api/entries/Widget/data")
    assert status == 200
    data = json.loads(body)
    assert data == {"name": "widget", "tags": None}


def test_get_entry_data_when_file_present_returns_file_contents(tmp_path: Path) -> None:
    (tmp_path / "widget.yaml").write_text(yaml.safe_dump({"name": "from-disk", "tags": ["a"]}))
    server = _make_server(tmp_path)
    status, _headers, body = _get(server, "/api/entries/Widget/data")
    assert status == 200
    data = json.loads(body)
    assert data == {"name": "from-disk", "tags": ["a"]}


def test_get_entry_data_fills_in_missing_defaulted_sections(tmp_path: Path) -> None:
    """A partial on-disk file gets its default_factory sections filled in.

    Before this fix, GET /api/entries/{name}/data returned the raw on-disk
    dict unchanged, so `extra` was simply absent from the response. Jedison
    has no schema-level default to fall back on for a `default_factory`
    field (Pydantic never emits one), so the section silently failed to
    render in the browser. See mimirheim's config_editor_v2 investigation.
    """
    (tmp_path / "sectioned.yaml").write_text(yaml.safe_dump({"name": "from-disk"}))
    server = _make_server(tmp_path, entries=[_sectioned_entry()])
    status, _headers, body = _get(server, "/api/entries/Sectioned/data")
    assert status == 200
    data = json.loads(body)
    assert data == {
        "name": "from-disk",
        "extra": {"label": "default-label"},
        "power_kw": 1.0,
    }


def test_get_entry_data_invalid_on_disk_file_returns_422(tmp_path: Path) -> None:
    """An on-disk file that fails validation is rejected, not passed through.

    Fixing a hand-edited or stale config file is the user's job, not this
    editor's: a config that fails validation because it predates a model
    change is a migration the owning helper or mimirheim must perform, not
    something GET /api/entries/{name}/data papers over.
    """
    (tmp_path / "sectioned.yaml").write_text(
        yaml.safe_dump({"name": "from-disk", "power_kw": -5.0})
    )
    server = _make_server(tmp_path, entries=[_sectioned_entry()])
    status, _headers, body = _get(server, "/api/entries/Sectioned/data")
    assert status == 422
    data = json.loads(body)
    sectioned_errors = data["errors"]["Sectioned"]
    assert len(sectioned_errors) == 1
    assert sectioned_errors[0]["entry_name"] == "Sectioned"
    assert sectioned_errors[0]["loc"] == ["power_kw"]


def test_get_entry_data_unknown_name_returns_404(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, _body = _get(server, "/api/entries/Nonexistent/data")
    assert status == 404


def test_get_entry_data_url_decodes_the_name(tmp_path: Path) -> None:
    """A name containing a space (as real Baseload entries do) round-trips."""
    entry = RegistryEntry(
        name="Baseload (static)",
        filename="baseload-static.yaml",
        model_path=f"{_GadgetConfig.__module__}.{_GadgetConfig.__qualname__}",
    )
    server = _make_server(tmp_path, entries=[entry])
    status, _headers, body = _get(server, "/api/entries/Baseload%20%28static%29/data")
    assert status == 200
    assert json.loads(body) == {"power_kw": 1.0}


# ---------------------------------------------------------------------------
# POST /api/save
# ---------------------------------------------------------------------------


def test_post_save_all_valid_writes_every_file(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    payload = {
        "Widget": {"name": "living-room", "tags": ["a", "b"]},
        "Gadget": {"power_kw": 3.5},
    }
    status, _headers, body = _post(server, "/api/save", payload)
    assert status == 200
    assert json.loads(body) == {"ok": True}

    widget_data = yaml.safe_load((tmp_path / "widget.yaml").read_text())
    gadget_data = yaml.safe_load((tmp_path / "gadget.yaml").read_text())
    assert widget_data == {"name": "living-room", "tags": ["a", "b"]}
    assert gadget_data == {"power_kw": 3.5}


def test_post_save_converts_empty_submitted_list_to_none(tmp_path: Path) -> None:
    """An empty tags list submitted by the frontend is saved as null, not []."""
    server = _make_server(tmp_path)
    payload = {
        "Widget": {"name": "empty-tags", "tags": []},
        "Gadget": {"power_kw": 1.0},
    }
    status, _headers, _body = _post(server, "/api/save", payload)
    assert status == 200
    widget_data = yaml.safe_load((tmp_path / "widget.yaml").read_text())
    assert widget_data["tags"] is None


def test_post_save_one_invalid_entry_writes_nothing(tmp_path: Path) -> None:
    """One entry invalid: HTTP 422, and no file changes for any entry.

    Mirrors the save semantics acceptance criterion: even the entry that
    individually validated (Widget) must not be written.
    """
    server = _make_server(tmp_path)
    payload = {
        "Widget": {"name": "living-room", "tags": ["a"]},
        "Gadget": {"power_kw": -5.0},  # violates ge=0.0
    }
    status, _headers, _body = _post(server, "/api/save", payload)
    assert status == 422
    assert not (tmp_path / "widget.yaml").exists()
    assert not (tmp_path / "gadget.yaml").exists()


def test_post_save_error_response_grouped_by_entry_name(tmp_path: Path) -> None:
    """The 422 body groups FieldErrors under their entry_name.

    Chosen shape: {"errors": {<entry_name>: [<FieldError dict>, ...]}}. Each
    error dict still carries its own "entry_name" key so a client can also
    work from the flattened per-error view if it prefers.
    """
    server = _make_server(tmp_path)
    payload = {
        "Widget": {"name": "ok", "tags": ["a"]},
        "Gadget": {"power_kw": -1.0},
    }
    status, _headers, body = _post(server, "/api/save", payload)
    assert status == 422
    data = json.loads(body)
    assert set(data["errors"].keys()) == {"Gadget"}
    gadget_errors = data["errors"]["Gadget"]
    assert len(gadget_errors) == 1
    assert gadget_errors[0]["entry_name"] == "Gadget"
    assert gadget_errors[0]["loc"] == ["power_kw"]


def test_post_save_missing_entry_in_body_validates_against_defaults(tmp_path: Path) -> None:
    """An entry the frontend never fetched is validated against its own defaults."""
    server = _make_server(tmp_path)
    payload = {"Widget": {"name": "living-room", "tags": None}}
    status, _headers, _body = _post(server, "/api/save", payload)
    assert status == 200
    gadget_data = yaml.safe_load((tmp_path / "gadget.yaml").read_text())
    assert gadget_data == {"power_kw": 1.0}


def test_post_save_untouched_entry_without_defaults_is_excluded_not_blocking(
    tmp_path: Path,
) -> None:
    """The real-world scenario this fix targets: saving one entry works even
    when another registered entry, entirely absent from the body, has no
    valid all-defaults state -- as every real production model does (each
    requires at least an `mqtt` block with no default).

    Before this fix, `Required`'s absence from the body still caused
    `validate_all` to try `model_validate({})` and treat the resulting
    `ValidationError` as a blocking `FieldError`, so this save would have
    failed with 422 even though the user never touched `Required` at all.
    """
    entries = [_widget_entry(), _required_entry()]
    server = _make_server(tmp_path, entries=entries)
    payload = {"Widget": {"name": "living-room", "tags": ["a"]}}

    status, _headers, _body = _post(server, "/api/save", payload)

    assert status == 200
    assert (tmp_path / "widget.yaml").exists()
    assert not (tmp_path / "required.yaml").exists()
    assert [entry.name for entry in tmp_path.iterdir()] == ["widget.yaml"]


def test_post_save_malformed_json_returns_400(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, _body = server.handle_request("POST", "/api/save", body=b"not json")
    assert status == 400


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------


def test_static_path_traversal_returns_403(tmp_path: Path) -> None:
    """A request path that resolves outside static/ via an absolute-path trick.

    ``/static//etc/passwd.html`` strips the ``/static/`` prefix down to
    ``/etc/passwd.html``: an *absolute* relative-path component, which makes
    ``os.path.join`` discard the static base directory entirely rather than
    joining onto it. No literal ``..`` segment is present, so this exercises
    `_safe_join`'s containment check specifically, not the earlier
    ``".." in path`` fast-reject (which returns 400, see
    test_config_editor_server.py's equivalent v1 test).
    """
    server = _make_server(tmp_path)
    status, _headers, _body = _get(server, "/static//etc/passwd.html")
    assert status == 403


def test_static_disallowed_extension_returns_403(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, _body = _get(server, "/static/vendor/jedison/LICENSE")
    assert status == 403


def test_static_serves_vendored_bootstrap_source_map(tmp_path: Path) -> None:
    """The .map extension, added beyond v1's allowlist, is actually reachable."""
    server = _make_server(tmp_path)
    status, headers, _body = _get(server, "/static/vendor/bootstrap/bootstrap.min.css.map")
    assert status == 200
    assert headers["Content-Type"] == "application/octet-stream"


def test_static_serves_app_js(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, headers, body = _get(server, "/static/app.js")
    assert status == 200
    assert headers["Content-Type"] == "text/javascript"
    assert len(body) > 0


def test_index_html_is_served_at_root(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, headers, body = _get(server, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()


def test_unknown_route_returns_404(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, _body = _get(server, "/api/does-not-exist")
    assert status == 404
