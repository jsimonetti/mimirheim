"""Descriptor and validate_and_write construction for the Config Service protocol.

A Config Owner's ``describe()`` step builds a Descriptor (its pydantic validation
schema plus its FormSpec) and publishes it retained to a well-known,
per-Config-Owner MQTT topic, cleared via last-will when the owner disconnects
(see ADR-0001, ADR-0005). Its ``validate_and_write()`` step accepts Candidate
Values submitted by a Config Editor over a second well-known topic, validates
them against its own pydantic model, and, only on success, writes them to its
configuration file, publishing a result either way. This module provides the
pure pieces of both steps: topic naming, the Descriptor/request/result shapes,
and the bytes to publish them with.

It deliberately does not touch an MQTT client. mimirheim_shared never owns or
constructs a connection (see ``mimirheim_shared/docs/adr/0005``): a Config
Owner's own connection-management code (mimirheim core's ``MqttClient``, or a
helper's own MQTT setup) is responsible for calling ``client.will_set(...)``
with ``descriptor_topic(...)`` and ``CLEARING_PAYLOAD`` before connecting,
``client.publish(...)`` with ``descriptor_payload(...)`` from its own
``on_connect`` handler, and subscribing to ``validate_and_write_request_topic(...)``
to receive Candidate Values and publish a ``validate_and_write_result_payload(...)``
to ``validate_and_write_response_topic(...)`` in reply. Neither request nor
response is retained: this is a point-in-time request/response exchange, not
a state a late subscriber needs to see.

A Config Editor correlates a response to its request by ``request_id``
(a caller-chosen opaque string, e.g. a UUID) rather than any native MQTT
request/response feature: mimirheim core's connection negotiates MQTT
3.1.1, which has no such feature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mimirheim_shared.atomic_write import overlay_values, write_yaml_preserving_comments
from mimirheim_shared.formspec import FormSpec, resolve_field_shapes

# Fixed regardless of any Config Owner's own business MQTT topic prefix (e.g.
# mimirheim core's mqtt.topic_prefix): the Config Editor must be able to
# discover every Config Owner from one well-known namespace without prior
# knowledge of each owner's own topic layout.
_TOPIC_ROOT = "mimirheim/config-service"

# The MQTT idiom for clearing a retained message: an empty payload published
# retained removes it from the broker. Used both as the explicit clearing
# publish on graceful shutdown and as the last-will payload registered for
# ungraceful disconnects.
CLEARING_PAYLOAD: bytes = b""


def descriptor_topic(owner_id: str) -> str:
    """Return the well-known retained topic a Config Owner's Descriptor is published to.

    Args:
        owner_id: The Config Owner's stable identifier (e.g. ``"mimirheim-core"``).

    Returns:
        The topic string, e.g. ``"mimirheim/config-service/mimirheim-core/descriptor"``.
    """
    return f"{_TOPIC_ROOT}/{owner_id}/descriptor"


class Descriptor(BaseModel):
    """The payload a Config Owner publishes retained, describing its configuration.

    Attributes:
        owner_id: The Config Owner's stable identifier.
        display_name: Human-readable name shown by the Config Editor.
        json_schema: The Config Owner's validation model, as a JSON Schema
            (``model.model_json_schema()``).
        form_spec: The presentation-only FormSpec paired with the model.
    """

    model_config = ConfigDict(extra="forbid")

    owner_id: str
    display_name: str
    json_schema: dict[str, Any]
    form_spec: FormSpec


def build_descriptor(
    owner_id: str,
    display_name: str,
    model: type[BaseModel],
    form_spec: FormSpec,
) -> Descriptor:
    """Build a Descriptor from a Config Owner's validation model and FormSpec.

    Args:
        owner_id: The Config Owner's stable identifier.
        display_name: Human-readable name shown by the Config Editor.
        model: The Config Owner's validation model.
        form_spec: The FormSpec paired with ``model``. Not checked for
            alignment here; each Config Owner's own test suite calls
            ``mimirheim_shared.alignment.assert_form_spec_complete`` for that.

    Returns:
        The assembled Descriptor. ``form_spec`` is resolved through
        ``resolve_field_shapes`` first, so every field (at every depth) that
        crosses the wire carries its effective Field Shape: the Config
        Editor never imports ``model`` itself, so this is its only way to
        know it.
    """
    return Descriptor(
        owner_id=owner_id,
        display_name=display_name,
        json_schema=model.model_json_schema(),
        form_spec=resolve_field_shapes(model, form_spec),
    )


def descriptor_payload(descriptor: Descriptor) -> bytes:
    """Serialise a Descriptor to the bytes a Config Owner publishes retained.

    Args:
        descriptor: The Descriptor to serialise.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return descriptor.model_dump_json().encode("utf-8")


def get_current_values_request_topic(owner_id: str) -> str:
    """Return the well-known topic a Config Owner accepts get_current_values requests on.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g.
        ``"mimirheim/config-service/mimirheim-core/get_current_values/request"``.
    """
    return f"{_TOPIC_ROOT}/{owner_id}/get_current_values/request"


def get_current_values_response_topic(owner_id: str) -> str:
    """Return the well-known topic a Config Owner publishes get_current_values results to.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g.
        ``"mimirheim/config-service/mimirheim-core/get_current_values/response"``.
    """
    return f"{_TOPIC_ROOT}/{owner_id}/get_current_values/response"


class GetCurrentValuesRequest(BaseModel):
    """A Config Editor's request for a Config Owner's current on-disk values.

    Carries no field selection: a Config Owner always returns its full
    current configuration, matching the shape ``validate_and_write`` accepts
    Candidate Values in.

    Attributes:
        request_id: Opaque string chosen by the Config Editor (e.g. a UUID),
            echoed back on the matching ``GetCurrentValuesResult`` so the
            Editor can correlate a response to its request.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str


class GetCurrentValuesResult(BaseModel):
    """A Config Owner's current on-disk configuration values, as requested.

    Attributes:
        request_id: Echoed from the ``GetCurrentValuesRequest`` this result
            answers.
        values: The Config Owner's current configuration, as parsed from its
            on-disk YAML file (``{}`` if the file does not exist yet).
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    values: dict[str, Any]


def get_current_values_result_payload(result: GetCurrentValuesResult) -> bytes:
    """Serialise a GetCurrentValuesResult to the bytes a Config Owner publishes.

    Args:
        result: The result to serialise.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return result.model_dump_json().encode("utf-8")


def handle_get_current_values(payload: bytes, config_path: Path) -> bytes:
    """Read a Config Owner's current on-disk values and build its response payload.

    This is the generic form of a Config Owner's ``get_current_values`` step,
    parameterised the same way ``handle_validate_and_write`` is so any Config
    Owner can reuse it rather than reimplementing the read. It never touches
    an MQTT client (see this module's own docstring); the caller publishes
    the returned bytes to its ``get_current_values`` response topic.

    Args:
        payload: The raw MQTT message payload received on the Config Owner's
            ``get_current_values`` request topic.
        config_path: The path to the Config Owner's own YAML configuration
            file.

    Returns:
        UTF-8 encoded JSON bytes to publish to the Config Owner's
        ``get_current_values`` response topic.

    Raises:
        ValidationError: If ``payload`` is not a well-formed
            ``GetCurrentValuesRequest`` envelope. The caller cannot
            correlate a response to a request it could not parse, so this is
            left to propagate rather than published.
    """
    request = GetCurrentValuesRequest.model_validate_json(payload)

    current: dict[str, Any] = {}
    if config_path.exists():
        current = yaml.safe_load(config_path.read_text()) or {}

    result = GetCurrentValuesResult(request_id=request.request_id, values=current)
    return get_current_values_result_payload(result)


def validate_and_write_request_topic(owner_id: str) -> str:
    """Return the well-known topic a Config Owner accepts validate_and_write requests on.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g.
        ``"mimirheim/config-service/mimirheim-core/validate_and_write/request"``.
    """
    return f"{_TOPIC_ROOT}/{owner_id}/validate_and_write/request"


def validate_and_write_response_topic(owner_id: str) -> str:
    """Return the well-known topic a Config Owner publishes validate_and_write results to.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g.
        ``"mimirheim/config-service/mimirheim-core/validate_and_write/response"``.
    """
    return f"{_TOPIC_ROOT}/{owner_id}/validate_and_write/response"


class ValidateAndWriteRequest(BaseModel):
    """Candidate Values a Config Editor submits for validation and writing.

    Attributes:
        request_id: Opaque string chosen by the Config Editor (e.g. a UUID).
            Echoed back on the matching ``ValidateAndWriteResult`` so the
            Editor can correlate a response to its request; neither the
            request nor the response topic is otherwise scoped per-request.
        values: The proposed field values, as a JSON-compatible dict shaped
            like the Config Owner's own validation model.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    values: dict[str, Any]


class ValidateAndWriteResult(BaseModel):
    """The outcome of a validate_and_write request, published by the Config Owner.

    Attributes:
        request_id: Echoed from the ``ValidateAndWriteRequest`` this result
            answers.
        success: True if ``values`` validated against the Config Owner's
            model and were written to its configuration file.
        errors: Human-readable validation error messages. Empty on success.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str
    success: bool
    errors: list[str] = Field(default_factory=list)


def validate_and_write_result_payload(result: ValidateAndWriteResult) -> bytes:
    """Serialise a ValidateAndWriteResult to the bytes a Config Owner publishes.

    Args:
        result: The result to serialise.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return result.model_dump_json().encode("utf-8")


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


def handle_validate_and_write(
    payload: bytes, config_path: Path, model: type[BaseModel]
) -> bytes:
    """Validate submitted Candidate Values against ``model`` and, only on success, write them.

    This is the generic form of a Config Owner's ``validate_and_write`` step:
    it is the same sequence mimirheim core's own
    ``mimirheim.io.config_service.handle_validate_and_write`` performs for
    ``MimirheimConfig``, parameterised so any Config Owner's validation model
    can reuse it rather than reimplementing the sequence. It never touches an
    MQTT client (see this module's own docstring); the caller publishes the
    returned bytes to its ``validate_and_write`` response topic.

    Parses ``payload`` as a ``ValidateAndWriteRequest``. Candidate Values may
    be a partial update (e.g. just one changed section), matching
    ``write_yaml_preserving_comments``'s own overlay semantics, so they are
    validated against the *merged* result of overlaying them onto the current
    on-disk configuration, not in isolation: validating a partial submission
    by itself would reject an update to one field of an otherwise-required
    nested section. On failure, returns a result carrying the validation
    errors and performs no write. On success, calls
    ``write_yaml_preserving_comments`` to atomically overlay ``values`` onto
    ``config_path`` and returns a success result.

    Args:
        payload: The raw MQTT message payload received on the Config Owner's
            ``validate_and_write`` request topic.
        config_path: The path to the Config Owner's own YAML configuration
            file, the one it was started with.
        model: The Config Owner's validation model. ``model.model_validate``
            is called on the merged Candidate Values.

    Returns:
        UTF-8 encoded JSON bytes to publish to the Config Owner's
        ``validate_and_write`` response topic.

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
        model.model_validate(merged)
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
