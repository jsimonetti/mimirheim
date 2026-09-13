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
from typing import Any

import yaml
from pydantic import ValidationError

from mimirheim_shared.atomic_write import overlay_values, write_yaml_preserving_comments
from mimirheim_shared.config_service import (
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    build_descriptor,
    descriptor_payload,
    descriptor_topic,
    get_current_values_request_topic,
    get_current_values_response_topic,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
    validate_and_write_result_payload,
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


def _format_validation_error(error: dict[str, Any]) -> str:
    """Render one pydantic error dict as a single human-readable line.

    Args:
        error: One entry of ``ValidationError.errors()``.

    Returns:
        ``"<dotted.field.path>: <message>"``, or just the message when the
        error is not attached to a specific field (an empty ``loc``).
    """
    loc = ".".join(str(part) for part in error["loc"])
    return f"{loc}: {error['msg']}" if loc else error["msg"]


def handle_validate_and_write(payload: bytes, config_path: Path) -> bytes:
    """Validate submitted Candidate Values and, only on success, write them to disk.

    Parses ``payload`` as a ``ValidateAndWriteRequest``. Candidate Values may
    be a partial update (e.g. just one changed section), matching
    ``write_yaml_preserving_comments``'s own overlay semantics (ticket 01), so
    they are validated against the *merged* result of overlaying them onto
    the current on-disk configuration, not in isolation: validating a partial
    submission by itself would reject an update to one field of an
    otherwise-required nested section. On failure, returns a result carrying
    the validation errors and performs no write. On success, calls
    ``write_yaml_preserving_comments`` to atomically overlay ``values`` onto
    ``config_path`` and returns a success result.

    Args:
        payload: The raw MQTT message payload received on ``REQUEST_TOPIC``.
        config_path: The path to mimirheim core's own YAML configuration
            file, the one it was started with.

    Returns:
        UTF-8 encoded JSON bytes to publish to ``RESPONSE_TOPIC``.

    Raises:
        ValidationError: If ``payload`` is not a well-formed
            ``ValidateAndWriteRequest`` envelope (as opposed to a
            well-formed envelope carrying invalid Candidate Values, which is
            reported in the returned result instead). The caller cannot
            correlate a response to a request it could not parse, so this is
            left to propagate rather than published.
    """
    request = ValidateAndWriteRequest.model_validate_json(payload)

    current: dict[str, Any] = {}
    if config_path.exists():
        current = yaml.safe_load(config_path.read_text()) or {}
    merged = dict(current)
    overlay_values(merged, request.values)

    try:
        MimirheimConfig.model_validate(merged)
    except ValidationError as exc:
        result = ValidateAndWriteResult(
            request_id=request.request_id,
            success=False,
            errors=[_format_validation_error(error) for error in exc.errors()],
        )
        return validate_and_write_result_payload(result)

    write_yaml_preserving_comments(config_path, request.values)
    result = ValidateAndWriteResult(request_id=request.request_id, success=True)
    return validate_and_write_result_payload(result)
