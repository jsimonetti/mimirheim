"""Tests for the FormSpec authored for ReporterConfig.

Verifies the FormSpec stays aligned with ReporterConfig's fields
(mimirheim_shared.alignment.assert_form_spec_complete) and that it can be
combined with the model into a Config Service Descriptor.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes

from reporter.config import ReporterConfig
from reporter.formspec import REPORTER_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_reporter_config() -> None:
    assert_form_spec_complete(ReporterConfig, REPORTER_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "reporter", "Reporter", ReporterConfig, REPORTER_CONFIG_FORM_SPEC
    )

    assert descriptor.owner_id == "reporter"
    assert descriptor.form_spec == resolve_field_shapes(ReporterConfig, REPORTER_CONFIG_FORM_SPEC)
    assert descriptor.json_schema == ReporterConfig.model_json_schema()


def test_form_spec_has_both_tiers_represented() -> None:
    tiers = {field.tier for field in REPORTER_CONFIG_FORM_SPEC.fields.values()}
    assert len(tiers) == 2
