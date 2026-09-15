"""Builds mimirheim core's Config Service Descriptor and handles validate_and_write.

Mimirheim core shares its one existing MQTT connection for the Config
Service protocol (see mimirheim_shared/docs/adr/0005):
``mimirheim.io.mqtt_client.MqttClient`` owns that connection and calls into
this module for the Descriptor's well-known topic and serialised payload,
publishing it retained on connect and clearing it on graceful shutdown, and
for handling validate_and_write requests received on ``REQUEST_TOPIC``,
publishing the result this module returns to ``RESPONSE_TOPIC``. This module
never touches an MQTT client itself, matching
``mimirheim_shared.config_service``'s own no-owned-connection design.
"""

from __future__ import annotations

from pathlib import Path

from mimirheim_shared.config_service import (
    OperationalState,
    RestartRequest,
    RestartResponse,
    build_descriptor,
    descriptor_payload,
    descriptor_topic,
    get_current_values_request_topic,
    get_current_values_response_topic,
    operational_state_payload,
    restart_request_topic,
    restart_response_payload,
    restart_response_topic,
    state_topic,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
)
from mimirheim_shared.config_service import (
    handle_validate_and_write as _shared_handle_validate_and_write,
)

from mimirheim.config.formspec import MIMIRHEIM_CONFIG_FORM_SPEC
from mimirheim.config.schema import MimirheimConfig

# Stable regardless of user configuration (mqtt.client_id may vary per
# deployment or be auto-generated); the Config Editor needs a fixed identity
# for mimirheim core across restarts and reconfiguration.
OWNER_ID = "mimirheim-core"
DISPLAY_NAME = "Mimirheim"

# The well-known, retained topic mimirheim core's Descriptor is published to.
TOPIC = descriptor_topic(OWNER_ID)

# The well-known topics a Config Editor submits Candidate Values to, and
# reads the outcome from. Neither is retained (see config_service.py's
# module docstring in mimirheim_shared).
REQUEST_TOPIC = validate_and_write_request_topic(OWNER_ID)
RESPONSE_TOPIC = validate_and_write_response_topic(OWNER_ID)

# The well-known topics a Config Editor requests mimirheim core's current
# on-disk configuration on, and reads the answer from. Neither is retained,
# same reason as REQUEST_TOPIC/RESPONSE_TOPIC above. Handled directly via
# mimirheim_shared.config_service.handle_get_current_values in mqtt_client.py
# rather than a wrapper here: unlike handle_validate_and_write, it needs no
# MimirheimConfig-specific parameterisation.
GET_CURRENT_VALUES_REQUEST_TOPIC = get_current_values_request_topic(OWNER_ID)
GET_CURRENT_VALUES_RESPONSE_TOPIC = get_current_values_response_topic(OWNER_ID)

# The well-known, retained topic mimirheim core's Operational State is
# published to (see mimirheim_shared/CONTEXT.md's Operational State entry
# and ADR-0010).
STATE_TOPIC = state_topic(OWNER_ID)

# The well-known topics a Config Editor sends a Restart Request to, and
# reads the acknowledgement from. Neither is retained, same reason as
# REQUEST_TOPIC/RESPONSE_TOPIC above (see ADR-0012).
RESTART_REQUEST_TOPIC = restart_request_topic(OWNER_ID)
RESTART_RESPONSE_TOPIC = restart_response_topic(OWNER_ID)


def payload_bytes() -> bytes:
    """Serialise mimirheim core's Config Service Descriptor to publish retained.

    A plain function rather than a module-level constant: it is called once,
    at ``MqttClient`` construction, so there is no benefit to computing it
    before any caller actually needs it.

    Returns:
        UTF-8 encoded JSON bytes ready to publish to ``TOPIC``.
    """
    return descriptor_payload(
        build_descriptor(OWNER_ID, DISPLAY_NAME, MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC)
    )


def handle_validate_and_write(payload: bytes, config_path: Path) -> bytes:
    """Validate submitted Candidate Values against MimirheimConfig and, only on success, write them to disk.

    Thin wrapper around
    ``mimirheim_shared.config_service.handle_validate_and_write``,
    parameterised with ``MimirheimConfig``: mimirheim core's own validation
    model. See that function's docstring for the full validate-merge-write
    sequence.

    Args:
        payload: The raw MQTT message payload received on ``REQUEST_TOPIC``.
        config_path: The path to mimirheim core's own YAML configuration
            file, the one it was started with.

    Returns:
        UTF-8 encoded JSON bytes to publish to ``RESPONSE_TOPIC``.

    Raises:
        ValidationError: If ``payload`` is not a well-formed
            ``ValidateAndWriteRequest`` envelope. See
            ``mimirheim_shared.config_service.handle_validate_and_write``.
    """
    return _shared_handle_validate_and_write(payload, config_path, MimirheimConfig)


def operational_state_payload_bytes() -> bytes:
    """Serialise the Operational payload mimirheim core publishes retained to ``STATE_TOPIC``.

    Published once mimirheim core's configuration validates in full and it is
    running its own function (the solve loop). See
    ``mimirheim_shared/CONTEXT.md``'s Operational entry and ADR-0010.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return operational_state_payload(OperationalState(state="operational"))


def awaiting_configuration_state_payload(detail: str) -> bytes:
    """Serialise the Awaiting Configuration payload mimirheim core publishes retained to ``STATE_TOPIC``.

    Published while mimirheim core's configuration fails full validation (or
    does not exist yet), alongside the Descriptor. See
    ``mimirheim_shared/CONTEXT.md``'s Awaiting Configuration entry and
    ADR-0009/ADR-0011.

    Args:
        detail: A human-readable summary of why the configuration does not
            currently validate, e.g. the ``str()`` of the Pydantic
            ``ValidationError``.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return operational_state_payload(OperationalState(state="awaiting_configuration", detail=detail))


def handle_restart_request(payload: bytes) -> bytes:
    """Parse a Restart Request and build its acknowledgement.

    Unlike ``handle_validate_and_write``, acting on the request (clearing the
    Descriptor and Operational State, disconnecting, exiting) is orchestration
    that only the caller can perform (see ADR-0012); this function only builds
    the response bytes the caller publishes before doing so.

    Args:
        payload: The raw MQTT message payload received on
            ``RESTART_REQUEST_TOPIC``.

    Returns:
        UTF-8 encoded JSON bytes to publish to ``RESTART_RESPONSE_TOPIC``.

    Raises:
        ValidationError: If ``payload`` is not a well-formed
            ``RestartRequest`` envelope. The caller cannot correlate a
            response to a request it could not parse, so this is left to
            propagate rather than published.
    """
    request = RestartRequest.model_validate_json(payload)
    return restart_response_payload(RestartResponse(request_id=request.request_id))
