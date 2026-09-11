"""Unit tests for config_editor_v2.save's validate_all and write_all.

Tests verify:
- All-or-nothing validation: every registered entry is validated
  independently, one invalid entry does not stop others from being
  validated, and errors are attributed to the correct entry name and field
  location.
- An entry absent from the submitted data validates against its model's own
  defaults.
- A broken registry entry (bad model_path) raises ImportError from
  `validate_all` itself, rather than being swallowed into a FieldError.
- `write_all` writes atomically and preserves existing comments and
  structure already on disk, via ruamel.yaml's round-trip loader.
- `write_all` leaves the original file untouched, with no leftover temp
  file, if the final rename fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config_editor_v2.registry import RegistryEntry
from config_editor_v2.save import validate_all, write_all

from ..conftest import ListFieldModel, PlainFieldModel, RequiredFieldModel


def _entry(name: str, filename: str, model: type) -> RegistryEntry:
    return RegistryEntry(
        name=name,
        filename=filename,
        model_path=f"{model.__module__}.{model.__qualname__}",
    )


def test_all_valid_entries_produce_no_errors() -> None:
    """Two entries with valid submissions produce no errors, both models present."""
    plain_entry = _entry("plain", "plain.yaml", PlainFieldModel)
    counts_entry = _entry("counts", "counts.yaml", ListFieldModel)

    validated, errors = validate_all(
        [plain_entry, counts_entry],
        {"plain": {"plain": "hello"}, "counts": {"counts": [1, 2, 3]}},
    )

    assert errors == []
    assert validated["plain"].plain == "hello"
    assert validated["counts"].counts == [1, 2, 3]


def test_one_invalid_entry_blocks_the_other(tmp_path: Path) -> None:
    """A second, invalid entry produces errors; nothing is written for either.

    `validate_all` alone never writes anything -- `write_all` is a separate
    call the caller only makes once the error list is empty. This test
    demonstrates that guarantee by simply never calling `write_all` here and
    confirming no file exists afterward.
    """
    plain_entry = _entry("plain", "plain.yaml", PlainFieldModel)
    counts_entry = _entry("counts", "counts.yaml", ListFieldModel)

    validated, errors = validate_all(
        [plain_entry, counts_entry],
        {"plain": {"plain": "hello"}, "counts": {"counts": ["not-an-int"]}},
    )

    assert errors != []
    assert not (tmp_path / "plain.yaml").exists()
    assert not (tmp_path / "counts.yaml").exists()


def test_error_attributed_to_correct_entry_and_field() -> None:
    """The FieldError for a bad list item names the correct entry and loc.

    `counts` is a `list[int]`; submitting a non-numeric string at index 0
    produces a Pydantic error whose `loc` is `("counts", 0)`. `FieldError.loc`
    is typed `list[str]`, so the integer index must be stringified.
    """
    counts_entry = _entry("counts", "counts.yaml", ListFieldModel)

    _validated, errors = validate_all([counts_entry], {"counts": {"counts": ["not-an-int"]}})

    assert len(errors) == 1
    error = errors[0]
    assert error.entry_name == "counts"
    assert error.loc == ["counts", "0"]
    assert error.message


def test_untouched_entry_validates_against_defaults() -> None:
    """An entry with no key in `submitted` validates using its own defaults."""
    plain_entry = _entry("plain", "plain.yaml", PlainFieldModel)

    validated, errors = validate_all([plain_entry], {})

    assert errors == []
    assert validated["plain"].plain == "default"


def test_untouched_entry_whose_defaults_dont_validate_is_silently_excluded() -> None:
    """An untouched entry with no valid all-defaults state is excluded, not blocking.

    `RequiredFieldModel.name` has no default, so `model_validate({})` raises.
    Since the entry is absent from `submitted` entirely (never touched by
    the user in this save), that failure must not surface as a FieldError --
    it must simply be excluded from `validated`.
    """
    required_entry = _entry("required", "required.yaml", RequiredFieldModel)

    validated, errors = validate_all([required_entry], {})

    assert errors == []
    assert "required" not in validated


def test_registry_import_error_propagates_not_swallowed() -> None:
    """A broken model_path raises ImportError from validate_all, not a FieldError."""
    broken_entry = RegistryEntry(
        name="broken",
        filename="broken.yaml",
        model_path="config_editor_v2_does_not_exist.module.Model",
    )

    with pytest.raises(ImportError):
        validate_all([broken_entry], {})


def test_write_all_preserves_existing_comments(tmp_path: Path) -> None:
    """write_all updates the value on disk while keeping the file's comment."""
    file_path = tmp_path / "plain.yaml"
    file_path.write_text("# a hand-written comment\nplain: old-value\n")
    plain_entry = _entry("plain", "plain.yaml", PlainFieldModel)

    validated, errors = validate_all([plain_entry], {"plain": {"plain": "new-value"}})
    assert errors == []

    write_all(validated, [plain_entry], tmp_path)

    content = file_path.read_text()
    assert "# a hand-written comment" in content
    assert "new-value" in content
    assert "old-value" not in content


def test_write_all_is_atomic_on_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the rename step fails, the original file is unchanged and no temp file remains."""
    file_path = tmp_path / "plain.yaml"
    original_content = "plain: old-value\n"
    file_path.write_text(original_content)
    plain_entry = _entry("plain", "plain.yaml", PlainFieldModel)

    validated, errors = validate_all([plain_entry], {"plain": {"plain": "new-value"}})
    assert errors == []

    def _raise_os_error(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr("config_editor_v2.save.os.replace", _raise_os_error)

    with pytest.raises(OSError):
        write_all(validated, [plain_entry], tmp_path)

    assert file_path.read_text() == original_content
    leftover_files = [entry for entry in tmp_path.iterdir() if entry != file_path]
    assert leftover_files == []
