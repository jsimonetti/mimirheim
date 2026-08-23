"""Unit tests for scheduler.config.

Tests verify:
- A valid configuration loads correctly.
- parsed_schedules() returns the expected list of (cron_expr, topic) tuples.
- A schedule entry with an invalid cron expression is rejected.
- A schedule entry with more than one key is rejected.
- An empty schedules list is rejected.
- Unknown top-level and mqtt fields are rejected (extra="forbid").
- Multiple entries targeting the same topic are valid.
- Day-of-week follows standard cron, so 7 is accepted as Sunday.
- Restricting day-of-month and day-of-week together is rejected.
- A schedule entry whose topic MQTT cannot publish to is rejected.
- Default values for optional mqtt fields are applied correctly.
"""

import pytest
from pydantic import ValidationError

from scheduler.config import SchedulerConfig


def _base_raw(**overrides: object) -> dict:
    """Return a minimal valid raw config dict, with optional field overrides."""
    raw: dict = {
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "test-scheduler",
        },
        "schedules": [
            {"*/15 * * * *": "mimir/input/trigger"},
        ],
    }
    raw.update(overrides)
    return raw


def test_valid_config_loads() -> None:
    """A minimal valid config loads without error."""
    config = SchedulerConfig.model_validate(_base_raw())
    assert config.mqtt.host == "localhost"
    assert config.mqtt.port == 1883
    assert config.mqtt.client_id == "test-scheduler"
    assert len(config.schedules) == 1


def test_parsed_schedules_returns_tuples() -> None:
    """parsed_schedules() converts the list[dict] format to (cron, topic) pairs."""
    config = SchedulerConfig.model_validate(
        _base_raw(
            schedules=[
                {"*/15 * * * *": "mimir/input/trigger"},
                {"0 14 * * *": "mimir/input/tools/prices/trigger"},
            ]
        )
    )
    assert config.parsed_schedules() == [
        ("*/15 * * * *", "mimir/input/trigger"),
        ("0 14 * * *", "mimir/input/tools/prices/trigger"),
    ]


def test_invalid_cron_expression_rejected() -> None:
    """A schedule entry whose key is not a valid cron expression raises ValidationError."""
    with pytest.raises(ValidationError, match="invalid cron expression"):
        SchedulerConfig.model_validate(
            _base_raw(schedules=[{"not-a-cron": "some/topic"}])
        )


def test_multi_key_dict_rejected() -> None:
    """A schedule entry with more than one key raises ValidationError."""
    with pytest.raises(ValidationError, match="exactly one key"):
        SchedulerConfig.model_validate(
            _base_raw(
                schedules=[{"*/15 * * * *": "topic/a", "0 * * * *": "topic/b"}]
            )
        )


def test_empty_schedules_list_rejected() -> None:
    """An empty schedules list raises ValidationError.

    The rule lives in ``min_length`` on the field rather than in the validator,
    so that it also reaches the generated JSON schema as ``minItems`` and the
    config editor can report it before the daemon is started.
    """
    with pytest.raises(ValidationError, match="at least 1 item"):
        SchedulerConfig.model_validate(_base_raw(schedules=[]))


def test_non_empty_constraint_reaches_the_json_schema() -> None:
    """The config editor builds its UI from the schema, not from the validator."""
    schema = SchedulerConfig.model_json_schema()
    assert schema["properties"]["schedules"]["minItems"] == 1


def test_unknown_top_level_field_rejected() -> None:
    """An unrecognised top-level field raises ValidationError (extra='forbid')."""
    raw = _base_raw()
    raw["unexpected_field"] = "value"
    with pytest.raises(ValidationError):
        SchedulerConfig.model_validate(raw)


def test_unknown_mqtt_field_rejected() -> None:
    """An unrecognised field inside mqtt raises ValidationError (extra='forbid')."""
    raw = _base_raw()
    raw["mqtt"]["unexpected"] = "value"
    with pytest.raises(ValidationError):
        SchedulerConfig.model_validate(raw)


def test_multiple_entries_same_topic_valid() -> None:
    """Two entries targeting the same topic with different cron expressions are valid."""
    config = SchedulerConfig.model_validate(
        _base_raw(
            schedules=[
                {"0 14 * * *": "mimir/input/tools/prices/trigger"},
                {"5 0 * * *": "mimir/input/tools/prices/trigger"},
            ]
        )
    )
    pairs = config.parsed_schedules()
    assert len(pairs) == 2
    assert pairs[0][1] == pairs[1][1] == "mimir/input/tools/prices/trigger"


def test_mqtt_optional_fields_default_to_none() -> None:
    """username and password default to None when omitted."""
    config = SchedulerConfig.model_validate(_base_raw())
    assert config.mqtt.username is None
    assert config.mqtt.password is None


def test_mqtt_port_defaults_to_1883() -> None:
    """mqtt.port defaults to 1883 when omitted."""
    raw = _base_raw()
    del raw["mqtt"]["port"]
    config = SchedulerConfig.model_validate(raw)
    assert config.mqtt.port == 1883


def test_mqtt_credentials_accepted() -> None:
    """username and password are accepted when provided."""
    raw = _base_raw()
    raw["mqtt"]["username"] = "user"
    raw["mqtt"]["password"] = "secret"
    config = SchedulerConfig.model_validate(raw)
    assert config.mqtt.username == "user"
    assert config.mqtt.password == "secret"


def test_multiple_valid_cron_formats() -> None:
    """A range of syntactically valid cron expressions all pass validation."""
    valid_exprs = [
        "* * * * *",
        "*/15 * * * *",
        "0 14 * * *",
        "0 14 * * 1-5",
        "5 0,12 * * *",
        "0 3,6,9,12,15,18 * * *",
    ]
    config = SchedulerConfig.model_validate(
        _base_raw(schedules=[{expr: "t"} for expr in valid_exprs])
    )
    assert len(config.parsed_schedules()) == len(valid_exprs)


def test_day_seven_is_accepted_as_sunday() -> None:
    """Standard cron spells Sunday as 0 or 7; APScheduler alone rejects 7."""
    config = SchedulerConfig.model_validate(
        _base_raw(schedules=[{"0 2 * * 7": "mimir/input/trigger"}])
    )
    assert config.parsed_schedules() == [("0 2 * * 7", "mimir/input/trigger")]


def test_both_day_fields_restricted_is_rejected() -> None:
    """Standard cron ORs the two day fields, which one trigger cannot express."""
    with pytest.raises(ValidationError, match="day-of-month and day-of-week"):
        SchedulerConfig.model_validate(
            _base_raw(schedules=[{"0 0 13 * fri": "some/topic"}])
        )


def test_invalid_cron_error_names_the_entry_and_the_reason() -> None:
    """The validation message identifies both the bad entry and what is wrong."""
    with pytest.raises(ValidationError, match=r"schedules\[0\].*out of range"):
        SchedulerConfig.model_validate(
            _base_raw(schedules=[{"0 14 * * 9": "some/topic"}])
        )


# ---------------------------------------------------------------------------
# Topic validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "reason"),
    [
        ("", "must not be empty"),
        ("   ", "must not be empty"),
        ("mimir/input/#", "wildcard"),
        ("mimir/+/trigger", "wildcard"),
        ("mimir/\x00/trigger", "null"),
        ("a" * 65536, "too long"),
    ],
)
def test_unpublishable_topic_rejected(topic: str, reason: str) -> None:
    """A topic paho would refuse is rejected at startup, not at fire time."""
    with pytest.raises(ValidationError, match=reason):
        SchedulerConfig.model_validate(
            _base_raw(schedules=[{"*/15 * * * *": topic}])
        )


def test_topic_error_names_the_entry() -> None:
    """The message identifies which schedule entry carries the bad topic."""
    with pytest.raises(ValidationError, match=r"schedules\[1\]"):
        SchedulerConfig.model_validate(
            _base_raw(
                schedules=[
                    {"*/15 * * * *": "mimir/input/trigger"},
                    {"0 14 * * *": "mimir/input/#"},
                ]
            )
        )


@pytest.mark.parametrize(
    "topic",
    [
        "mimir/input/trigger",
        "a",
        "a" * 65535,
        "mimir/input/tools/pv_ml_learner/infer",
        "with spaces/inside",
    ],
)
def test_valid_topics_accepted(topic: str) -> None:
    """Anything MQTT can publish to stays acceptable, including odd but legal names."""
    config = SchedulerConfig.model_validate(
        _base_raw(schedules=[{"*/15 * * * *": topic}])
    )
    assert config.parsed_schedules()[0][1] == topic
