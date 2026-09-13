"""Unit tests for mimirheim.io.config_service.

This module only builds mimirheim core's Config Service Descriptor topic and
payload; it never touches an MQTT client. See tests/unit/test_mqtt_client.py
for coverage of MqttClient publishing/clearing the Descriptor on its one
connection, and tests/integration/test_config_service_roundtrip.py for the
real-broker round trip.
"""

from __future__ import annotations

from mimirheim.config.formspec import MIMIRHEIM_CONFIG_FORM_SPEC
from mimirheim.config.schema import MimirheimConfig
from mimirheim.io.config_service import DISPLAY_NAME, OWNER_ID, TOPIC, payload_bytes
from mimirheim_shared.config_service import Descriptor, descriptor_topic


def test_topic_is_the_well_known_descriptor_topic_for_this_owner() -> None:
    assert TOPIC == descriptor_topic(OWNER_ID)
    assert OWNER_ID == "mimirheim-core"


def test_payload_bytes_is_a_valid_descriptor_for_mimirheim_config() -> None:
    payload = payload_bytes()

    descriptor = Descriptor.model_validate_json(payload)
    assert descriptor.owner_id == OWNER_ID
    assert descriptor.display_name == DISPLAY_NAME
    assert descriptor.form_spec == MIMIRHEIM_CONFIG_FORM_SPEC
    assert descriptor.json_schema == MimirheimConfig.model_json_schema()
