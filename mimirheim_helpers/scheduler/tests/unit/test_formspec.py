"""Tests for the FormSpec authored for SchedulerConfig.

Verifies the FormSpec stays aligned with SchedulerConfig's fields
(mimirheim_shared.alignment.assert_form_spec_complete) and that it can be
combined with the model into a Config Service Descriptor.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor
from mimirheim_shared.formspec import resolve_field_shapes

from scheduler.config import SchedulerConfig
from scheduler.formspec import SCHEDULER_CONFIG_FORM_SPEC


def test_form_spec_is_aligned_with_scheduler_config() -> None:
    assert_form_spec_complete(SchedulerConfig, SCHEDULER_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "scheduler", "Scheduler", SchedulerConfig, SCHEDULER_CONFIG_FORM_SPEC
    )

    assert descriptor.owner_id == "scheduler"
    assert descriptor.form_spec == resolve_field_shapes(
        SchedulerConfig, SCHEDULER_CONFIG_FORM_SPEC
    )
    assert descriptor.json_schema == SchedulerConfig.model_json_schema()
