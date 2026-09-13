"""Tests for the FormSpecs authored for helper_common's shared nested config models.

MqttConfig and HomeAssistantConfig are nested by every helper's own config
model (ADR-0007, mimirheim_shared/docs/adr): their FormSpec is authored once
here, in the package that defines them, rather than by every helper that
nests them.
"""

from mimirheim_shared.alignment import assert_form_spec_complete

from helper_common.config import HomeAssistantConfig, MqttConfig
from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC


def test_mqtt_config_form_spec_is_aligned() -> None:
    assert_form_spec_complete(MqttConfig, MQTT_CONFIG_FORM_SPEC)


def test_home_assistant_config_form_spec_is_aligned() -> None:
    assert_form_spec_complete(HomeAssistantConfig, HOME_ASSISTANT_CONFIG_FORM_SPEC)
