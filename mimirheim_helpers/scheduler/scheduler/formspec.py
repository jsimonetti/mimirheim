"""FormSpec authored for SchedulerConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``scheduler.config`` per ADR-0002
(``mimirheim_shared/docs/adr``): the config models carry no FormSpec-level
presentation metadata of their own. This module is what this helper's
``describe()`` step (wired via ``helper_common.config_owner.ConfigOwnerSupport``
in ``scheduler.__main__``) combines with ``SchedulerConfig`` to build its
Config Service Descriptor.

``SchedulerConfig``'s ``mqtt`` field nests ``helper_common``'s shared
``MqttConfig`` model; per ADR-0007, this FormSpec references
``helper_common.formspec``'s FormSpec for that field rather than
re-authoring its labels.

``schedules`` is ``list[dict[str, str]]`` rather than a list of a Pydantic
model, so ``mimirheim_shared.field_shape.derive_field_shape`` resolves it to
``FieldShape.SCALAR_LIST``: each entry is presented as an opaque value (a
single-key ``{cron_expression: mqtt_topic}`` mapping) rather than as a
structured sub-form. This is the field shape the framework derives for any
scalar-valued list, not a limitation specific to this helper.
"""

from __future__ import annotations

from helper_common.formspec import MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier

SCHEDULER_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "mqtt": FieldSpec(
            label="MQTT",
            description="MQTT broker connection parameters.",
            tier=Tier.BASIC,
            nested_form_spec=MQTT_CONFIG_FORM_SPEC,
        ),
        "schedules": FieldSpec(
            label="Schedules",
            description=(
                "List of schedule entries. Each entry is a single-key dict: "
                "{cron_expression: mqtt_topic}."
            ),
            tier=Tier.BASIC,
        ),
    }
)
