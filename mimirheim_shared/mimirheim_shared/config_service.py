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

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mimirheim_shared.formspec import FormSpec

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
        The assembled Descriptor.
    """
    return Descriptor(
        owner_id=owner_id,
        display_name=display_name,
        json_schema=model.model_json_schema(),
        form_spec=form_spec,
    )


def descriptor_payload(descriptor: Descriptor) -> bytes:
    """Serialise a Descriptor to the bytes a Config Owner publishes retained.

    Args:
        descriptor: The Descriptor to serialise.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return descriptor.model_dump_json().encode("utf-8")


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
