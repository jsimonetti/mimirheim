"""Tests for the atomic, comment-preserving YAML write utility."""

from pathlib import Path
from unittest.mock import patch

import pytest
from ruamel.yaml import YAML

from mimirheim_shared.atomic_write import write_yaml_preserving_comments

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_config.yaml"


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
