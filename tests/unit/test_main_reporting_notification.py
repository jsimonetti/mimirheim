"""Unit tests for the reporting notification function in mimirheim.__main__.

Tests confirm that ``_publish_reporting_notification`` writes a dump via
``debug_dump`` and publishes a JSON notification to the configured MQTT topic
when reporting is enabled, and that it is suppressed when disabled or when
the solve was infeasible.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mimirheim.config.schema import MimirheimConfig


def _minimal_hioo_config_with_reporting(dump_dir: Path) -> MimirheimConfig:
    """Return a minimal MimirheimConfig with reporting enabled and dump_dir set."""
    raw = {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "mqtt": {"host": "localhost", "client_id": "mimirheim"},
        "outputs": {
            "schedule": "mimir/strategy/schedule",
            "current": "mimir/strategy/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "static_loads": {
            "base_load": {"topic_forecast": "mimir/input/base_load_forecast"},
        },
        "reporting": {
            "enabled": True,
            "dump_dir": str(dump_dir),
        },
    }
    return MimirheimConfig.model_validate(raw)


def _minimal_hioo_config_reporting_disabled() -> MimirheimConfig:
    """Return a minimal MimirheimConfig with reporting disabled (the default)."""
    raw = {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "mqtt": {"host": "localhost", "client_id": "mimirheim"},
        "outputs": {
            "schedule": "mimir/strategy/schedule",
            "current": "mimir/strategy/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "static_loads": {
            "base_load": {"topic_forecast": "mimir/input/base_load_forecast"},
        },
    }
    return MimirheimConfig.model_validate(raw)


def test_notification_published_after_successful_solve(tmp_path: Path) -> None:
    """_publish_reporting_notification publishes to notify_topic when enabled.

    Verifies that:
    - ``debug_dump`` is called with the reporting dump_dir and max_dumps.
    - ``mqtt_client.publish`` is called exactly once.
    - The payload is valid JSON with keys 'ts', 'input_path', 'output_path'.
    - The message is published QoS 0, not retained.
    """
    from mimirheim.__main__ import _publish_reporting_notification

    config = _minimal_hioo_config_with_reporting(tmp_path)
    bundle = MagicMock()
    result = MagicMock()
    mqtt_client = MagicMock()

    fake_input = tmp_path / "2026-04-02T14-00-00Z_input.json"
    fake_output = tmp_path / "2026-04-02T14-00-00Z_output.json"

    with patch(
        "mimirheim.__main__.debug_dump", return_value=(fake_input, fake_output)
    ) as mock_dump:
        _publish_reporting_notification(bundle, result, config, mqtt_client)

    mock_dump.assert_called_once_with(
        bundle,
        result,
        config,
        config.reporting.dump_dir,
        config.reporting.max_dumps,
    )

    mqtt_client.publish.assert_called_once()
    call_args = mqtt_client.publish.call_args
    topic = call_args[0][0]
    payload_str = call_args[0][1]
    kwargs = call_args[1]

    assert topic == config.reporting.notify_topic
    assert kwargs.get("qos") == 0
    assert kwargs.get("retain") is False

    payload = json.loads(payload_str)
    assert "ts" in payload
    assert payload["input_path"] == str(fake_input)
    assert payload["output_path"] == str(fake_output)


def test_notification_not_published_when_reporting_disabled() -> None:
    """_publish_reporting_notification is a no-op when reporting.enabled=False.

    Verifies that neither debug_dump nor mqtt_client.publish is called.
    """
    from mimirheim.__main__ import _publish_reporting_notification

    config = _minimal_hioo_config_reporting_disabled()
    bundle = MagicMock()
    result = MagicMock()
    mqtt_client = MagicMock()

    with patch("mimirheim.__main__.debug_dump") as mock_dump:
        _publish_reporting_notification(bundle, result, config, mqtt_client)

    mock_dump.assert_not_called()
    mqtt_client.publish.assert_not_called()


def test_notification_not_published_when_dump_returns_none(tmp_path: Path) -> None:
    """_publish_reporting_notification skips publish when debug_dump returns None.

    This can happen if dump_dir is None at call time, which should not occur
    in practice when reporting.enabled is True, but the function must be robust.
    """
    from mimirheim.__main__ import _publish_reporting_notification

    config = _minimal_hioo_config_with_reporting(tmp_path)
    bundle = MagicMock()
    result = MagicMock()
    mqtt_client = MagicMock()

    with patch("mimirheim.__main__.debug_dump", return_value=None):
        _publish_reporting_notification(bundle, result, config, mqtt_client)

    mqtt_client.publish.assert_not_called()


# ---------------------------------------------------------------------------
# Solve error formatting for the last_solve status topic
# ---------------------------------------------------------------------------


def test_solve_error_summary_omits_traceback() -> None:
    """The status topic must carry a summary, never a raw traceback.

    ``outputs.last_solve`` is retained, so whatever is published stays on the
    broker until the next solve overwrites it. A formatted traceback exposes
    filesystem paths and internal structure to every subscriber, and
    ``publish_last_solve_status`` documents its ``error`` argument as excluding
    them. The full traceback belongs in the process log instead.
    """
    from mimirheim.__main__ import _format_solve_error

    try:
        raise ValueError("battery 'bat' has no entry in bundle.battery_inputs")
    except ValueError as exc:
        summary = _format_solve_error(exc)

    assert "Traceback" not in summary
    assert "File \"" not in summary
    assert "\n" not in summary


def test_solve_error_summary_identifies_the_failure() -> None:
    """The summary must still say what went wrong, and of what type."""
    from mimirheim.__main__ import _format_solve_error

    summary = _format_solve_error(ValueError("horizon_prices is empty"))

    assert summary == "ValueError: horizon_prices is empty"


def test_solve_error_summary_handles_exception_without_message() -> None:
    """An exception carrying no message must still produce a usable summary."""
    from mimirheim.__main__ import _format_solve_error

    assert _format_solve_error(KeyError()) == "KeyError"


def test_solve_error_summary_collapses_multiline_messages() -> None:
    """Pydantic errors span many lines; the status payload takes one line.

    A ValidationError message is a multi-line block. Publishing it verbatim
    makes the retained JSON payload awkward to read in any MQTT client, so the
    summary is flattened to a single line.
    """
    from mimirheim.__main__ import _format_solve_error

    summary = _format_solve_error(ValueError("line one\nline two\nline three"))

    assert "\n" not in summary
    assert "line one" in summary
    assert "line three" in summary
