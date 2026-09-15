"""Configuration schema for the mimirheim scheduler tool.

This module defines the Pydantic models that represent the scheduler YAML
configuration file. It is the single source of truth for field names, types,
constraints, and defaults.

What this module does not do:
- It does not import from mimirheim or any other tool.
- It does not perform any MQTT operations.
- It does not perform any scheduling logic.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from helper_common.config import MqttConfig

from scheduler.cron import build_trigger


# MQTT 3.1.1 encodes a topic name with a two-byte length prefix, so a topic
# cannot exceed 65535 bytes of UTF-8.
_MAX_TOPIC_BYTES = 65535


def _validate_topic(index: int, topic: str) -> None:
    """Reject a schedule topic that MQTT cannot publish to.

    Without this the daemon starts happily and the fault only appears when the
    schedule first fires, as a recurring error inside the job thread, while the
    process still looks healthy. A typo in a topic is the likeliest mistake in
    this file, so it is worth catching at startup where it stops the process
    with a message naming the entry.

    Args:
        index: Position of the entry in the schedules list, used in the message.
        topic: The configured MQTT topic.

    Raises:
        ValueError: If the topic is empty or blank, contains an MQTT wildcard
            or a null character, or exceeds the MQTT length limit.
    """
    if not topic.strip():
        raise ValueError(f"schedules[{index}] topic must not be empty")
    if "+" in topic or "#" in topic:
        raise ValueError(
            f"schedules[{index}] topic {topic!r} contains an MQTT wildcard; "
            f"wildcards are for subscribing, and the scheduler publishes"
        )
    if "\x00" in topic:
        raise ValueError(
            f"schedules[{index}] topic contains a null character"
        )
    encoded = len(topic.encode("utf-8"))
    if encoded > _MAX_TOPIC_BYTES:
        raise ValueError(
            f"schedules[{index}] topic is too long: {encoded} bytes of UTF-8, "
            f"maximum is {_MAX_TOPIC_BYTES}"
        )


class SchedulerConfig(BaseModel):
    """Top-level configuration for the mimirheim scheduler daemon.

    Attributes:
        mqtt: MQTT broker connection parameters.
        schedules: List of schedule entries. Each entry is a single-key dict
            where the key is a five-field cron expression and the value is the
            MQTT topic to publish an empty trigger message to when the
            expression fires.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(description="MQTT broker connection parameters.")
    schedules: list[dict[str, str]] = Field(
        min_length=1,
        description=(
            "List of schedule entries. Each entry is a single-key dict: "
            "{cron_expression: mqtt_topic}."
        )
    )

    @field_validator("schedules")
    @classmethod
    def _validate_schedules(cls, v: list[dict[str, str]]) -> list[dict[str, str]]:
        """Validate that the schedules list is non-empty and each entry is well-formed.

        Both halves of an entry are checked, because a fault in either one is
        equally fatal and equally invisible until the schedule first fires.

        The list being non-empty is enforced by ``min_length`` on the field, so
        it is not re-checked here.

        Raises:
            ValueError: If any entry has more than one key, if any key is not a
                valid five-field cron expression, or if any value is a topic
                MQTT cannot publish to.
        """
        for i, entry in enumerate(v):
            if len(entry) != 1:
                raise ValueError(
                    f"schedules[{i}] must have exactly one key (a cron expression), "
                    f"got {len(entry)} keys: {list(entry.keys())}"
                )
            cron_expr, topic = next(iter(entry.items()))
            try:
                build_trigger(cron_expr)
            except ValueError as exc:
                raise ValueError(
                    f"schedules[{i}] has invalid cron expression "
                    f"{cron_expr!r}: {exc}"
                ) from exc
            _validate_topic(i, topic)
        return v

    @model_validator(mode="after")
    def _set_client_id_default(self) -> SchedulerConfig:
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-scheduler"
        return self

    def parsed_schedules(self) -> list[tuple[str, str]]:
        """Return the schedules as a flat list of (cron_expr, topic) tuples.

        This converts the list[dict[str, str]] storage format into pairs that
        the scheduling loop can consume directly.

        Returns:
            A list of (cron_expression, mqtt_topic) pairs in config order.
        """
        return [(next(iter(d)), next(iter(d.values()))) for d in self.schedules]
