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

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mimirheim_shared.atomic_write import overlay_values, write_yaml_preserving_comments
from mimirheim_shared.formspec import FormSpec, resolve_field_shapes

# The MQTT idiom for clearing a retained message: an empty payload published
# retained removes it from the broker. Used both as the explicit clearing
# publish on graceful shutdown and as the last-will payload registered for
# ungraceful disconnects.
CLEARING_PAYLOAD: bytes = b""


def _topic_root() -> str:
    """Return the Config Service topic root, e.g. ``"mimir/config-service"``.

    Read from ``MQTT_PREFIX`` on every call rather than cached at import
    time, so a process (or a test using ``monkeypatch.setenv``) that sets
    the environment variable before calling a topic-naming function below
    sees the override without needing to reload this module. Independent of
    any Config Owner's own parsed configuration (e.g. mimirheim core's
    ``mqtt.topic_prefix``): see ``mimirheim_shared/docs/adr/0014`` for why
    every Config Owner in a deployment must agree on this root by
    construction rather than by each owner's own settings.
    """
    return f"{os.environ.get('MQTT_PREFIX', 'mimir')}/config-service"


def descriptor_topic(owner_id: str) -> str:
    """Return the well-known retained topic a Config Owner's Descriptor is published to.

    Args:
        owner_id: The Config Owner's stable identifier (e.g. ``"mimirheim-core"``).

    Returns:
        The topic string, e.g. ``"mimir/config-service/mimirheim-core/descriptor"``.
    """
    return f"{_topic_root()}/{owner_id}/descriptor"


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
        ``"mimir/config-service/mimirheim-core/get_current_values/request"``.
    """
    return f"{_topic_root()}/{owner_id}/get_current_values/request"


def get_current_values_response_topic(owner_id: str) -> str:
    """Return the well-known topic a Config Owner publishes get_current_values results to.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g.
        ``"mimir/config-service/mimirheim-core/get_current_values/response"``.
    """
    return f"{_topic_root()}/{owner_id}/get_current_values/response"


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
        ``"mimir/config-service/mimirheim-core/validate_and_write/request"``.
    """
    return f"{_topic_root()}/{owner_id}/validate_and_write/request"


def validate_and_write_response_topic(owner_id: str) -> str:
    """Return the well-known topic a Config Owner publishes validate_and_write results to.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g.
        ``"mimir/config-service/mimirheim-core/validate_and_write/response"``.
    """
    return f"{_topic_root()}/{owner_id}/validate_and_write/response"


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


def coerced_submission(dumped: dict[str, Any], submitted: dict[str, Any]) -> dict[str, Any]:
    """Restrict a validated model's dumped values to the keys actually submitted.

    ``model.model_validate(merged)`` coerces types and applies defaults
    across the whole merged configuration, but Candidate Values may be a
    partial submission (see ``handle_validate_and_write``), so only the keys
    present in ``submitted`` should be written back. Recurses into nested
    dicts so a partial update to one field of a nested section pulls out
    only that field, coerced, not the rest of the section.

    Args:
        dumped: ``model.model_dump(mode="json")`` of the validated, merged
            configuration.
        submitted: The Candidate Values as submitted, before validation.

    Returns:
        A dict shaped like ``submitted``, with each leaf replaced by its
        validated/coerced counterpart from ``dumped``.
    """
    result: dict[str, Any] = {}
    for key, value in submitted.items():
        dumped_value = dumped.get(key)
        if isinstance(value, dict) and isinstance(dumped_value, dict):
            result[key] = coerced_submission(dumped_value, value)
        else:
            result[key] = dumped_value
    return result


def handle_validate_and_write(
    payload: bytes, config_path: Path, model: type[BaseModel]
) -> bytes:
    """Validate submitted Candidate Values against ``model`` and, only on success, write them.

    This is the generic form of a Config Owner's ``validate_and_write`` step,
    parameterised so any Config Owner's validation model can reuse it rather
    than reimplementing the sequence. Mimirheim core's own
    ``mimirheim.io.config_service.handle_validate_and_write`` is a thin
    wrapper around this function, parameterised with ``MimirheimConfig``. It
    never touches an MQTT client (see this module's own docstring); the
    caller publishes the returned bytes to its ``validate_and_write``
    response topic.

    Parses ``payload`` as a ``ValidateAndWriteRequest``. Candidate Values may
    be a partial update (e.g. just one changed section), matching
    ``write_yaml_preserving_comments``'s own overlay semantics, so they are
    validated against the *merged* result of overlaying them onto the current
    on-disk configuration, not in isolation: validating a partial submission
    by itself would reject an update to one field of an otherwise-required
    nested section. On failure, returns a result carrying the validation
    errors and performs no write. On success, calls
    ``write_yaml_preserving_comments`` to atomically overlay the *validated,
    coerced* counterpart of ``values`` (via ``model.model_dump``) onto
    ``config_path`` and returns a success result: this writes the type the
    Config Owner's model actually stores (e.g. a float), not whatever
    JSON-compatible value the Config Editor happened to submit (e.g. a
    string).

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
    overlay_values(merged, request.values, model)

    try:
        validated = model.model_validate(merged)
    except ValidationError as exc:
        result = ValidateAndWriteResult(
            request_id=request.request_id,
            success=False,
            errors=[_format_validation_error(error) for error in exc.errors()],
        )
        return validate_and_write_result_payload(result)

    coerced_values = coerced_submission(validated.model_dump(mode="json"), request.values)
    write_yaml_preserving_comments(config_path, coerced_values, model)
    result = ValidateAndWriteResult(request_id=request.request_id, success=True)
    return validate_and_write_result_payload(result)


def state_topic(owner_id: str) -> str:
    """Return the well-known retained topic a Config Owner's Operational State is published to.

    Published and cleared in lockstep with the Descriptor (see
    ``mimirheim_shared/CONTEXT.md``'s Operational State entry and
    ``mimirheim_shared/docs/adr/0010``), but on its own topic rather than a
    field of the Descriptor: the Descriptor describes what a Config Owner
    *can* be configured as, never whether it is currently running.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g. ``"mimir/config-service/mimirheim-core/state"``.
    """
    return f"{_topic_root()}/{owner_id}/state"


class OperationalState(BaseModel):
    """The payload a Config Owner publishes retained, describing its Operational State.

    See ``mimirheim_shared/CONTEXT.md``'s Operational State, Awaiting
    Configuration, and Operational entries, and ADR-0010 and ADR-0011.

    Attributes:
        state: ``"awaiting_configuration"`` if the Config Owner's
            configuration does not yet fully validate (or does not exist),
            ``"operational"`` once it does and the Config Owner is running
            its own function.
        detail: An optional human-readable elaboration, e.g. a summary of
            why validation is failing. ``None`` when there is nothing to add.
    """

    model_config = ConfigDict(extra="forbid")

    state: Literal["awaiting_configuration", "operational"]
    detail: str | None = None


def operational_state_payload(state: OperationalState) -> bytes:
    """Serialise an OperationalState to the bytes a Config Owner publishes retained.

    Args:
        state: The OperationalState to serialise.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return state.model_dump_json().encode("utf-8")


def restart_request_topic(owner_id: str) -> str:
    """Return the well-known topic a Config Owner accepts a Restart Request on.

    See ``mimirheim_shared/CONTEXT.md``'s Restart Request entry and
    ADR-0012: a Config Owner acts on this request by clearing its Descriptor
    and Operational State, disconnecting gracefully, and exiting, relying on
    its container supervisor to start a fresh process.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g.
        ``"mimir/config-service/mimirheim-core/restart/request"``.
    """
    return f"{_topic_root()}/{owner_id}/restart/request"


def restart_response_topic(owner_id: str) -> str:
    """Return the well-known topic a Config Owner publishes a Restart Request's response to.

    Args:
        owner_id: The Config Owner's stable identifier.

    Returns:
        The topic string, e.g.
        ``"mimir/config-service/mimirheim-core/restart/response"``.
    """
    return f"{_topic_root()}/{owner_id}/restart/response"


class RestartRequest(BaseModel):
    """A Config Editor's request that a Config Owner exit for its supervisor to restart it.

    Attributes:
        request_id: Opaque string chosen by the Config Editor (e.g. a UUID),
            echoed back on the matching ``RestartResponse`` so the Editor can
            correlate a response to its request, matching
            ``ValidateAndWriteRequest``'s existing correlation shape.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str


class RestartResponse(BaseModel):
    """A Config Owner's acknowledgement of a Restart Request, published before it exits.

    Attributes:
        request_id: Echoed from the ``RestartRequest`` this response
            acknowledges.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str


def restart_response_payload(response: RestartResponse) -> bytes:
    """Serialise a RestartResponse to the bytes a Config Owner publishes.

    Args:
        response: The response to serialise.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return response.model_dump_json().encode("utf-8")
