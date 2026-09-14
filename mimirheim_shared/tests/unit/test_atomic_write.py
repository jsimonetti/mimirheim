"""Tests for the atomic, comment-preserving YAML write utility."""

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

from mimirheim_shared.atomic_write import overlay_values, write_yaml_preserving_comments

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_config.yaml"


class _ToyBattery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float
    note: str = ""


class _ToyMqtt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str


class _ToyConfigWithNamedCollection(BaseModel):
    """A Named Collection (``batteries``) alongside a plain nested object
    (``mqtt``), so a test can assert the two are overlaid differently."""

    model_config = ConfigDict(extra="forbid")

    mqtt: _ToyMqtt
    batteries: dict[str, _ToyBattery]


def _read_raw(path: Path) -> str:
    return path.read_text()


def test_preserves_comments_on_update(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(FIXTURE_PATH.read_text())

    write_yaml_preserving_comments(config_file, {"battery": {"capacity_kwh": 15.0}})

    raw = _read_raw(config_file)
    assert "# Top-level system configuration" in raw
    assert "# broker address" in raw
    assert "# usable capacity" in raw

    yaml = YAML()
    with config_file.open() as fh:
        data = yaml.load(fh)
    assert data["battery"]["capacity_kwh"] == 15.0
    assert data["mqtt"]["host"] == "localhost"


def test_updates_nested_values_without_disturbing_siblings(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(FIXTURE_PATH.read_text())

    write_yaml_preserving_comments(config_file, {"mqtt": {"port": 8883}})

    yaml = YAML()
    with config_file.open() as fh:
        data = yaml.load(fh)
    assert data["mqtt"]["port"] == 8883
    assert data["mqtt"]["host"] == "localhost"
    assert data["battery"]["capacity_kwh"] == 10.0


def test_creates_new_file_when_absent(tmp_path: Path) -> None:
    config_file = tmp_path / "new_config.yaml"

    write_yaml_preserving_comments(config_file, {"battery": {"capacity_kwh": 5.0}})

    yaml = YAML()
    with config_file.open() as fh:
        data = yaml.load(fh)
    assert data["battery"]["capacity_kwh"] == 5.0


def test_write_is_atomic_no_temp_file_left_on_success(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(FIXTURE_PATH.read_text())

    write_yaml_preserving_comments(config_file, {"battery": {"capacity_kwh": 20.0}})

    leftover_tmp_files = [p for p in tmp_path.iterdir() if p.name != "config.yaml"]
    assert leftover_tmp_files == []


def test_overlay_values_merges_nested_dicts_in_place() -> None:
    document = {"mqtt": {"host": "localhost", "port": 1883}, "battery": {"capacity_kwh": 10.0}}

    overlay_values(document, {"battery": {"capacity_kwh": 15.0}})

    assert document == {
        "mqtt": {"host": "localhost", "port": 1883},
        "battery": {"capacity_kwh": 15.0},
    }


def test_overlay_values_replaces_non_dict_values_outright() -> None:
    document = {"deferrable_loads": {"a": {"x": 1}}}

    overlay_values(document, {"deferrable_loads": ["not", "a", "dict", "anymore"]})

    assert document == {"deferrable_loads": ["not", "a", "dict", "anymore"]}


def test_overlay_values_still_merges_dicts_key_by_key_when_no_model_is_given() -> None:
    """Backward compatibility: a caller with no model context (the pre-ticket-06
    behaviour) gets the old key-by-key dict merge for every dict-valued field,
    Named Collection or not."""
    document = {"batteries": {"battery_main": {"capacity_kwh": 10.0}, "battery_old": {"capacity_kwh": 1.0}}}

    overlay_values(document, {"batteries": {"battery_main": {"capacity_kwh": 15.0}}})

    assert document["batteries"]["battery_old"] == {"capacity_kwh": 1.0}


def test_overlay_values_drops_stale_entries_from_a_named_collection_field_when_model_is_given() -> None:
    """A Named Collection field (a Dict[str, Model] field on ``model``) gets
    every on-disk entry key absent from the submission dropped, so a
    submission with a different key set (an entry removed or renamed)
    actually drops the stale key instead of leaving it behind forever. See
    ADR-0008."""
    document = {
        "mqtt": {"host": "localhost"},
        "batteries": {"battery_main": {"capacity_kwh": 10.0}, "battery_old": {"capacity_kwh": 1.0}},
    }

    overlay_values(
        document,
        {"batteries": {"battery_main": {"capacity_kwh": 15.0}, "battery_new": {"capacity_kwh": 5.0}}},
        _ToyConfigWithNamedCollection,
    )

    assert document["batteries"] == {
        "battery_main": {"capacity_kwh": 15.0},
        "battery_new": {"capacity_kwh": 5.0},
    }


def test_overlay_values_merges_a_surviving_named_collection_entrys_own_fields() -> None:
    """An entry key present both on disk and in the submission is merged
    recursively, not replaced outright, so an untouched sibling field on that
    surviving entry (and, for a ruamel document, its comment) is preserved --
    only a genuinely stale entry key is dropped."""
    document = {"batteries": {"battery_main": {"capacity_kwh": 10.0, "note": "keep me"}}}

    overlay_values(
        document, {"batteries": {"battery_main": {"capacity_kwh": 15.0}}}, _ToyConfigWithNamedCollection
    )

    assert document["batteries"]["battery_main"] == {"capacity_kwh": 15.0, "note": "keep me"}


def test_overlay_values_still_merges_a_plain_nested_object_field_when_model_is_given() -> None:
    """A field that is a nested object rather than a Named Collection (``mqtt``
    here) still merges key by key even when a model is supplied, so an update
    to one of its fields leaves untouched sibling fields alone."""
    document = {
        "mqtt": {"host": "localhost", "port": 1883},
        "batteries": {"battery_main": {"capacity_kwh": 10.0}},
    }

    overlay_values(document, {"mqtt": {"host": "otherhost"}}, _ToyConfigWithNamedCollection)

    assert document["mqtt"] == {"host": "otherhost", "port": 1883}


def test_original_file_untouched_if_dump_fails(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(FIXTURE_PATH.read_text())
    original = _read_raw(config_file)

    with patch("mimirheim_shared.atomic_write._yaml.dump", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            write_yaml_preserving_comments(config_file, {"battery": {"capacity_kwh": 999.0}})

    assert _read_raw(config_file) == original
    leftover_tmp_files = [p for p in tmp_path.iterdir() if p.name != "config.yaml"]
    assert leftover_tmp_files == []
