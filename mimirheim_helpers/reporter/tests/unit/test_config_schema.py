"""Unit tests for reporter.config — ReporterReportingSection and ReporterConfig.

These tests confirm that the Pydantic schema enforces required fields,
rejects unknown fields, and applies correct defaults.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from reporter.config import ReporterConfig, ReporterReportingSection


# ---------------------------------------------------------------------------
# ReporterReportingSection
# ---------------------------------------------------------------------------

def test_reporter_section_required_fields_accepted(tmp_path) -> None:
    """A section with only the required fields should validate without error."""
    sec = ReporterReportingSection(
        dump_dir=tmp_path / "dumps",
        output_dir=tmp_path / "reports",
    )
    assert sec.dump_dir == tmp_path / "dumps"
    assert sec.output_dir == tmp_path / "reports"


def test_reporter_section_defaults() -> None:
    """Defaults for max_reports should match the documented values; notify_topic is None."""
    sec = ReporterReportingSection(
        dump_dir="/tmp/dumps",
        output_dir="/tmp/reports",
    )
    assert sec.max_reports == 100
    assert sec.notify_topic is None


def test_reporter_section_dump_dir_required() -> None:
    """Omitting dump_dir must raise a ValidationError."""
    with pytest.raises(ValidationError):
        ReporterReportingSection(output_dir="/tmp/reports")


def test_reporter_section_output_dir_required() -> None:
    """Omitting output_dir must raise a ValidationError."""
    with pytest.raises(ValidationError):
        ReporterReportingSection(dump_dir="/tmp/dumps")


def test_reporter_section_extra_field_rejected() -> None:
    """An unknown field must raise a ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        ReporterReportingSection(
            dump_dir="/tmp/dumps",
            output_dir="/tmp/reports",
            unknown_key="oops",
        )


def test_reporter_section_max_reports_ge_zero() -> None:
    """max_reports must be >= 0; negative values must raise ValidationError."""
    with pytest.raises(ValidationError):
        ReporterReportingSection(
            dump_dir="/tmp/dumps",
            output_dir="/tmp/reports",
            max_reports=-1,
        )


def test_reporter_section_max_reports_zero_allowed() -> None:
    """max_reports == 0 (unlimited) is a valid value."""
    sec = ReporterReportingSection(
        dump_dir="/tmp/dumps",
        output_dir="/tmp/reports",
        max_reports=0,
    )
    assert sec.max_reports == 0


# ---------------------------------------------------------------------------
# ReporterConfig
# ---------------------------------------------------------------------------

_MINIMAL_RAW = {
    "mqtt": {"host": "localhost", "client_id": "test-reporter"},
    "reporting": {
        "dump_dir": "/data/dumps",
        "output_dir": "/data/reports",
    },
}


def test_reporter_config_validates_minimal_raw() -> None:
    """A minimal raw dict with only required fields should produce a valid config."""
    cfg = ReporterConfig.model_validate(_MINIMAL_RAW)
    assert cfg.mqtt.host == "localhost"
    assert cfg.reporting.dump_dir.as_posix() == "/data/dumps"


def test_reporter_config_has_no_trigger_topic() -> None:
    """ReporterConfig must not have a trigger_topic field."""
    assert not hasattr(ReporterConfig.model_fields, "trigger_topic")


def test_reporter_config_extra_field_rejected() -> None:
    """An extra top-level field must raise a ValidationError."""
    raw = {**_MINIMAL_RAW, "surprise": "field"}
    with pytest.raises(ValidationError):
        ReporterConfig.model_validate(raw)


def test_reporter_config_reporting_required() -> None:
    """Omitting reporting must raise a ValidationError."""
    raw = {"mqtt": {"host": "localhost"}}
    with pytest.raises(ValidationError):
        ReporterConfig.model_validate(raw)


def test_reporter_config_notify_topic_derived_from_default_prefix() -> None:
    """When notify_topic is not set, it is derived from the default mimir_topic_prefix."""
    cfg = ReporterConfig.model_validate(_MINIMAL_RAW)
    assert cfg.reporting.notify_topic == "mimir/status/dump_available"


def test_reporter_config_notify_topic_derived_from_custom_prefix() -> None:
    """When mimir_topic_prefix is customised, notify_topic reflects the custom prefix."""
    raw = {**_MINIMAL_RAW, "mimir_topic_prefix": "mymimir"}
    cfg = ReporterConfig.model_validate(raw)
    assert cfg.reporting.notify_topic == "mymimir/status/dump_available"


def test_reporter_config_notify_topic_explicit_overrides_derivation() -> None:
    """An explicit notify_topic in the reporting section is not overwritten."""
    raw = {
        **_MINIMAL_RAW,
        "reporting": {
            **_MINIMAL_RAW["reporting"],
            "notify_topic": "custom/notify",
        },
    }
    cfg = ReporterConfig.model_validate(raw)
    assert cfg.reporting.notify_topic == "custom/notify"
