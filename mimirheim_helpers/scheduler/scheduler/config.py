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

import sys
from pathlib import Path

import yaml
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from helper_common.config import MqttConfig, apply_mqtt_env_overrides


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

    mqtt: MqttConfig = Field(description="MQTT broker connection parameters.", json_schema_extra={"ui_label": "MQTT", "ui_group": "basic"})
    schedules: list[dict[str, str]] = Field(
        description=(
            "List of schedule entries. Each entry is a single-key dict: "
            "{cron_expression: mqtt_topic}."
        ),
        json_schema_extra={"ui_label": "Schedules", "ui_group": "basic"},
    )

    @field_validator("schedules")
    @classmethod
    def _validate_schedules(cls, v: list[dict[str, str]]) -> list[dict[str, str]]:
        """Validate that the schedules list is non-empty and each entry is well-formed.

        Raises:
            ValueError: If the list is empty, if any entry has more than one
                key, or if any key is not a valid five-field cron expression.
        """
        if not v:
            raise ValueError("schedules must not be empty")
        for i, entry in enumerate(v):
            if len(entry) != 1:
                raise ValueError(
                    f"schedules[{i}] must have exactly one key (a cron expression), "
                    f"got {len(entry)} keys: {list(entry.keys())}"
                )
            cron_expr = next(iter(entry))
            try:
                CronTrigger.from_crontab(cron_expr, timezone="UTC")
            except ValueError:
                raise ValueError(
                    f"schedules[{i}] has invalid cron expression: {cron_expr!r}"
                )
        return v

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "SchedulerConfig":
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


def load_config(path: str) -> SchedulerConfig:
    """Load and validate the YAML configuration file.

    Reads the YAML file at ``path``, parses it, and validates it against
    ``SchedulerConfig``. On failure, prints a human-readable error and exits.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        The validated ``SchedulerConfig`` instance.

    Raises:
        SystemExit: With exit code 1 if the file cannot be read or the
            configuration fails Pydantic validation.
    """
    try:
        with Path(path).open() as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        print(f"ERROR: Cannot read config file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    apply_mqtt_env_overrides(raw)

    try:
        return SchedulerConfig.model_validate(raw)
    except PydanticValidationError as exc:
        print(f"ERROR: Invalid configuration in {path!r}:\n{exc}", file=sys.stderr)
        sys.exit(1)
