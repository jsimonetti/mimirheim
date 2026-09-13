"""Tests for the FormSpec authored for MimirheimConfig.

Verifies the FormSpec stays aligned with MimirheimConfig's top-level fields
(mimirheim_shared.alignment.assert_form_spec_complete) and that it can be
combined with the model into a Config Service Descriptor.
"""

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.config_service import build_descriptor

from mimirheim.config.formspec import MIMIRHEIM_CONFIG_FORM_SPEC
from mimirheim.config.schema import MimirheimConfig


def test_form_spec_is_aligned_with_mimirheim_config() -> None:
    assert_form_spec_complete(MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC)


def test_form_spec_builds_into_a_descriptor() -> None:
    descriptor = build_descriptor(
        "mimirheim-core", "Mimirheim", MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC
    )

    assert descriptor.owner_id == "mimirheim-core"
    assert descriptor.form_spec == MIMIRHEIM_CONFIG_FORM_SPEC
    assert descriptor.json_schema == MimirheimConfig.model_json_schema()
