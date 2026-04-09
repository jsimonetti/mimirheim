"""Unit tests for the chart publish step in ReporterDaemon.

Tests verify that:
- Chart and summary payloads are published to configured topics after a
  dump-available notification.
- Publication is skipped when topics are not configured.
- Oversized payloads are dropped with a warning rather than published.
- HA discovery payloads are published on connect and on HA birth message.

All tests use synthetic dump dicts and MagicMock clients. No file I/O is
required and no real dump files need to be present.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from reporter.config import ChartPublishingConfig, ReporterConfig, ReporterDiscoveryConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reporter_config(
    tmp_path: Path,
    *,
    chart_topic: str | None = None,
    summary_topic: str | None = None,
    max_payload_bytes: int = 65536,
    ha_discovery: dict | None = None,
) -> ReporterConfig:
    """Build a minimal ReporterConfig for testing."""
    raw: dict[str, Any] = {
        "mqtt": {"host": "localhost", "client_id": "test-reporter"},
        "reporting": {
            "dump_dir": str(tmp_path / "dumps"),
            "output_dir": str(tmp_path / "reports"),
        },
        "chart_publishing": {
            "chart_topic": chart_topic,
            "summary_topic": summary_topic,
            "max_payload_bytes": max_payload_bytes,
        },
    }
    if ha_discovery is not None:
        raw["ha_discovery"] = ha_discovery
    return ReporterConfig.model_validate(raw)


def _make_inp() -> dict:
    """Return a minimal synthetic SolveBundle JSON dict."""
    return {
        "solve_time_utc": "2026-04-02T14:00:00Z",
        "horizon_prices": [0.20, 0.21],
        "horizon_export_prices": [0.05, 0.05],
        "config": {},
    }


def _make_out() -> dict:
    """Return a minimal synthetic SolveResult JSON dict."""
    schedule = [
        {
            "t": "2026-04-02T14:00:00Z",
            "grid_import_kw": 1.0,
            "grid_export_kw": 0.0,
            "devices": {},
        },
        {
            "t": "2026-04-02T14:15:00Z",
            "grid_import_kw": 0.5,
            "grid_export_kw": 0.0,
            "devices": {},
        },
    ]
    return {
        "solve_time_utc": "2026-04-02T14:00:00Z",
        "strategy": "minimize_cost",
        "solve_status": "ok",
        "naive_cost_eur": 1.00,
        "optimised_cost_eur": 0.70,
        "soc_credit_eur": 0.05,
        "schedule": schedule,
    }


def _make_notification_message(inp: dict, out: dict, tmp_path: Path) -> MagicMock:
    """Write dump files to tmp_path and return a mock MQTT message for them."""
    dumps_dir = tmp_path / "dumps"
    dumps_dir.mkdir(parents=True, exist_ok=True)
    inp_path = dumps_dir / "2026-04-02T14-00-00Z_input.json"
    out_path = dumps_dir / "2026-04-02T14-00-00Z_output.json"
    inp_path.write_text(json.dumps(inp))
    out_path.write_text(json.dumps(out))
    payload = json.dumps({
        "ts": "2026-04-02T14:00:00Z",
        "input_path": str(inp_path),
        "output_path": str(out_path),
    }).encode()
    msg = MagicMock()
    msg.topic = "mimir/status/dump_available"
    msg.payload = payload
    return msg


def _make_daemon(config: ReporterConfig) -> Any:
    """Instantiate a ReporterDaemon with a mock MQTT client."""
    from reporter.daemon import ReporterDaemon
    daemon = ReporterDaemon.__new__(ReporterDaemon)
    daemon._reporter_config = config.reporting
    daemon._chart_config = config.chart_publishing
    daemon._discovery_config = config.ha_discovery
    # MqttDaemon._on_connect accesses _config and _logger set by __init__.
    daemon._config = config
    daemon._logger = logging.getLogger("test-reporter")
    daemon._client = MagicMock()
    return daemon


# ---------------------------------------------------------------------------
# Chart and summary publish step
# ---------------------------------------------------------------------------


def test_chart_payload_published_when_chart_topic_configured(tmp_path: Path) -> None:
    """Processing a dump notification calls client.publish(chart_topic, ...) with JSON."""
    inp = _make_inp()
    out = _make_out()
    cfg = _make_reporter_config(tmp_path, chart_topic="mimir/reporter/chart")
    daemon = _make_daemon(cfg)
    msg = _make_notification_message(inp, out, tmp_path)

    daemon._on_notification(msg)

    chart_calls = [
        c for c in daemon._client.publish.call_args_list
        if c.args[0] == "mimir/reporter/chart"
    ]
    assert len(chart_calls) == 1, "Expected exactly one publish to chart_topic"
    payload_str = chart_calls[0].args[1]
    parsed = json.loads(payload_str)
    assert "import_price" in parsed


def test_summary_payload_published_when_summary_topic_configured(tmp_path: Path) -> None:
    """Processing a dump notification calls client.publish(summary_topic, ...) with JSON."""
    inp = _make_inp()
    out = _make_out()
    cfg = _make_reporter_config(tmp_path, summary_topic="mimir/reporter/summary")
    daemon = _make_daemon(cfg)
    msg = _make_notification_message(inp, out, tmp_path)

    daemon._on_notification(msg)

    summary_calls = [
        c for c in daemon._client.publish.call_args_list
        if c.args[0] == "mimir/reporter/summary"
    ]
    assert len(summary_calls) == 1, "Expected exactly one publish to summary_topic"
    payload_str = summary_calls[0].args[1]
    parsed = json.loads(payload_str)
    assert "saving_eur" in parsed


def test_chart_not_published_when_chart_topic_is_none(tmp_path: Path) -> None:
    """When chart_topic is None (default), no publish call is made for chart data."""
    inp = _make_inp()
    out = _make_out()
    cfg = _make_reporter_config(tmp_path)  # all topics None
    daemon = _make_daemon(cfg)
    msg = _make_notification_message(inp, out, tmp_path)

    daemon._on_notification(msg)

    # The daemon may publish nothing at all (or at most a report), but not chart data.
    chart_calls = [
        c for c in daemon._client.publish.call_args_list
        if c.args[0] is not None and "mimir/reporter" in str(c.args[0])
    ]
    assert chart_calls == [], f"Unexpected publish calls: {chart_calls}"


def test_chart_publish_uses_retain_true_qos1(tmp_path: Path) -> None:
    """Chart and summary payloads are published with retain=True and qos=1."""
    inp = _make_inp()
    out = _make_out()
    cfg = _make_reporter_config(
        tmp_path,
        chart_topic="mimir/reporter/chart",
        summary_topic="mimir/reporter/summary",
    )
    daemon = _make_daemon(cfg)
    msg = _make_notification_message(inp, out, tmp_path)

    daemon._on_notification(msg)

    for topic in ("mimir/reporter/chart", "mimir/reporter/summary"):
        calls = [c for c in daemon._client.publish.call_args_list if c.args[0] == topic]
        assert len(calls) == 1
        c = calls[0]
        assert c.kwargs.get("qos") == 1, f"{topic}: expected qos=1"
        assert c.kwargs.get("retain") is True, f"{topic}: expected retain=True"


def test_chart_payload_truncated_when_exceeds_max_bytes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the serialised chart payload exceeds max_payload_bytes, it is not published
    and a WARNING is logged."""
    inp = _make_inp()
    out = _make_out()
    cfg = _make_reporter_config(
        tmp_path,
        chart_topic="mimir/reporter/chart",
        max_payload_bytes=10,  # absurdly small to force truncation
    )
    daemon = _make_daemon(cfg)
    msg = _make_notification_message(inp, out, tmp_path)

    with caplog.at_level(logging.WARNING):
        daemon._on_notification(msg)

    chart_calls = [
        c for c in daemon._client.publish.call_args_list
        if c.args[0] == "mimir/reporter/chart"
    ]
    assert chart_calls == [], "Oversized payload must not be published"
    assert any("max_payload_bytes" in r.message for r in caplog.records), (
        "Expected a WARNING log about max_payload_bytes"
    )


# ---------------------------------------------------------------------------
# HA discovery
# ---------------------------------------------------------------------------


def test_discovery_published_on_connect_when_enabled(tmp_path: Path) -> None:
    """When ha_discovery.enabled=True, _on_connect publishes a device JSON
    discovery payload to homeassistant/device/{device_id}/config."""
    cfg = _make_reporter_config(
        tmp_path,
        chart_topic="mimir/reporter/chart",
        summary_topic="mimir/reporter/summary",
        ha_discovery={"enabled": True, "device_id": "my-reporter", "device_name": "My Reporter"},
    )
    daemon = _make_daemon(cfg)
    mock_client = MagicMock()
    daemon._client = mock_client

    # Simulate _on_connect call (reason_code 0 = success).
    daemon._on_connect(mock_client, None, None, 0, None)

    discovery_calls = [
        c for c in mock_client.publish.call_args_list
        if "homeassistant/device" in str(c.args[0])
    ]
    assert len(discovery_calls) == 1, (
        f"Expected one discovery publish, got {len(discovery_calls)}"
    )
    topic = discovery_calls[0].args[0]
    assert topic == "homeassistant/device/my-reporter/config"
    # Payload must be valid JSON with device + components keys.
    payload = json.loads(discovery_calls[0].args[1])
    assert "device" in payload
    assert "components" in payload


def test_discovery_republished_on_ha_birth_message(tmp_path: Path) -> None:
    """When homeassistant/status 'online' arrives, discovery is re-published."""
    cfg = _make_reporter_config(
        tmp_path,
        chart_topic="mimir/reporter/chart",
        ha_discovery={"enabled": True, "device_name": "My Reporter"},
    )
    daemon = _make_daemon(cfg)
    mock_client = MagicMock()
    daemon._client = mock_client

    birth_msg = MagicMock()
    birth_msg.topic = "homeassistant/status"
    birth_msg.payload = b"online"
    daemon._on_message(mock_client, None, birth_msg)

    discovery_calls = [
        c for c in mock_client.publish.call_args_list
        if "homeassistant/device" in str(c.args[0])
    ]
    assert len(discovery_calls) >= 1, "Expected discovery re-publish on HA birth"


def test_discovery_not_published_when_disabled(tmp_path: Path) -> None:
    """When ha_discovery is None or enabled=False, no discovery is published on connect."""
    for ha_disc in (None, {"enabled": False}):
        cfg = _make_reporter_config(tmp_path, ha_discovery=ha_disc)
        daemon = _make_daemon(cfg)
        mock_client = MagicMock()
        daemon._client = mock_client

        daemon._on_connect(mock_client, None, None, 0, None)

        discovery_calls = [
            c for c in mock_client.publish.call_args_list
            if "homeassistant/device" in str(c.args[0])
        ]
        assert discovery_calls == [], (
            f"Expected no discovery publish when ha_discovery={ha_disc!r}"
        )
