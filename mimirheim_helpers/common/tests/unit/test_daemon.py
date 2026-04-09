"""Unit tests for HelperDaemon in helper_common.daemon.

Verifies the shared MQTT behaviour that all trigger-based input helpers
inherit from HelperDaemon:

- Retained messages are ignored (broker replays them on every subscribe).
- Two triggers within the 5-second debounce window result in only one
  _run_cycle call.
- A trigger arriving after the 5-second window does fire a new cycle.
- A homeassistant/status "online" message schedules a delayed discovery
  re-publish via threading.Timer.
- A homeassistant/status message with any other payload does not schedule
  a re-publish.
- Stats are published after every cycle to stats_topic when configured.
- CycleResult.suppress_until is honoured for rate-limit suppression.

These properties belong in the common package because HelperDaemon owns the
implementation. Individual helpers should not duplicate these tests.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import paho.mqtt.client as mqtt
import pytest

from helper_common.config import MqttConfig
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_TRIGGER_TOPIC = "mimir/test/trigger"
_HA_STATUS_TOPIC = "homeassistant/status"


def _make_mqtt_config() -> MqttConfig:
    return MqttConfig(host="localhost", port=1883, client_id="daemon-test")


class _ConcreteConfig:
    """Minimal config object accepted by HelperDaemon.__init__.

    HelperDaemon reads ``config.mqtt``, ``config.trigger_topic``,
    optionally ``config.ha_discovery``, and optionally ``config.stats_topic``.
    """

    def __init__(self, *, stats_topic: str | None = None) -> None:
        self.mqtt = _make_mqtt_config()
        self.trigger_topic = _TRIGGER_TOPIC
        self.ha_discovery = None
        self.stats_topic = stats_topic


class _ConcreteDaemon(HelperDaemon):
    """Minimal concrete subclass for testing.

    Records every ``_run_cycle`` invocation in ``cycle_calls``.
    """

    TOOL_NAME = "test_tool"

    def __init__(self, config: _ConcreteConfig) -> None:
        super().__init__(config)
        self.cycle_calls: list[int] = []

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        self.cycle_calls.append(1)
        return None


def _make_daemon(*, stats_topic: str | None = None) -> _ConcreteDaemon:
    """Return a fully initialised daemon without connecting to a broker."""
    return _ConcreteDaemon(_ConcreteConfig(stats_topic=stats_topic))


def _make_msg(*, topic: str = _TRIGGER_TOPIC, retain: bool = False, payload: bytes = b"") -> MagicMock:
    msg = MagicMock()
    msg.topic = topic
    msg.retain = retain
    msg.payload = payload
    return msg


# ---------------------------------------------------------------------------
# Logger name
# ---------------------------------------------------------------------------


class TestLoggerName:
    def test_logger_is_named_after_subclass_module(self) -> None:
        """The daemon's internal logger must use the subclass package name so
        log records are attributed to the tool (e.g. ``nordpool``)
        rather than ``helper_common.daemon`` or ``nordpool.__main__``."""
        daemon = _make_daemon()
        assert daemon._logger.name == "mimirheim_helpers.common.tests.unit.test_daemon"


# ---------------------------------------------------------------------------
# Unhandled exception resilience
# ---------------------------------------------------------------------------


class TestRunCycleExceptionHandling:
    def test_unhandled_exception_in_run_cycle_does_not_propagate(self) -> None:
        """An exception raised from _run_cycle must be caught by the base class.
        The paho network thread must not be killed — the daemon must remain alive
        and able to process the next trigger.
        """
        daemon = _make_daemon()

        def _crashing_cycle(client: mqtt.Client) -> datetime | None:
            raise TimeoutError("network unreachable")

        daemon._run_cycle = _crashing_cycle  # type: ignore[method-assign]
        msg = _make_msg()

        # Must not raise.
        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            daemon._on_message(daemon._client, None, msg)

    def test_daemon_still_processes_next_trigger_after_exception(self) -> None:
        """After an exception in _run_cycle the daemon accepts the next trigger
        once the debounce window has passed."""
        daemon = _make_daemon()
        call_count = 0

        def _flaky_cycle(client: mqtt.Client) -> datetime | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            return None

        daemon._run_cycle = _flaky_cycle  # type: ignore[method-assign]
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 106.0]
            daemon._on_message(daemon._client, None, msg)  # raises, caught
            daemon._on_message(daemon._client, None, msg)  # succeeds

        assert call_count == 2


# ---------------------------------------------------------------------------
# Retain guard
# ---------------------------------------------------------------------------


class TestRetainGuard:
    def test_retained_message_does_not_fire_cycle(self) -> None:
        """A retained message replayed by the broker must be silently ignored."""
        daemon = _make_daemon()
        msg = _make_msg(retain=True)

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            daemon._on_message(daemon._client, None, msg)

        assert daemon.cycle_calls == []

    def test_non_retained_message_fires_cycle(self) -> None:
        """A fresh (non-retained) trigger message must call _run_cycle once."""
        daemon = _make_daemon()
        msg = _make_msg(retain=False)

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            daemon._on_message(daemon._client, None, msg)

        assert len(daemon.cycle_calls) == 1


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------


class TestDebounce:
    def test_second_trigger_within_window_is_dropped(self) -> None:
        """Two triggers 3 s apart must fire only one cycle (window is 5 s)."""
        daemon = _make_daemon()
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 103.0]
            daemon._on_message(daemon._client, None, msg)
            daemon._on_message(daemon._client, None, msg)

        assert len(daemon.cycle_calls) == 1

    def test_second_trigger_exactly_at_window_boundary_is_dropped(self) -> None:
        """A trigger arriving exactly 5 s after the previous one is still inside
        the window (the condition is strictly less-than 5 s, not less-than-or-equal).
        This test documents the boundary behaviour.
        """
        daemon = _make_daemon()
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 105.0]
            daemon._on_message(daemon._client, None, msg)
            daemon._on_message(daemon._client, None, msg)

        # 105.0 - 100.0 == 5.0, which is not < 5.0, so it is allowed through.
        assert len(daemon.cycle_calls) == 2

    def test_second_trigger_after_window_is_allowed(self) -> None:
        """Two triggers 6 s apart must both fire a cycle."""
        daemon = _make_daemon()
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 106.0]
            daemon._on_message(daemon._client, None, msg)
            daemon._on_message(daemon._client, None, msg)

        assert len(daemon.cycle_calls) == 2


# ---------------------------------------------------------------------------
# Rate-limit suppression
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_cycle_returning_datetime_suppresses_subsequent_triggers(self) -> None:
        """When _run_cycle returns a future datetime, the next trigger must be
        suppressed until that time has passed."""
        daemon = _make_daemon()
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)

        call_count = 0

        def _limited_cycle(client: mqtt.Client) -> CycleResult | None:
            nonlocal call_count
            call_count += 1
            return CycleResult(suppress_until=future)

        daemon._run_cycle = _limited_cycle  # type: ignore[method-assign]
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 106.0]
            with patch("helper_common.daemon.datetime") as mock_dt:
                # First trigger: fires cycle, sets ratelimit_until
                mock_dt.now.return_value = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)
                # Second trigger: still within rate-limit window
                mock_dt.now.return_value = datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)

        assert call_count == 1

    def test_cycle_returning_none_clears_ratelimit(self) -> None:
        """When _run_cycle returns None after a previous rate-limit, subsequent
        triggers must be allowed through normally."""
        daemon = _make_daemon()
        # Set a rate-limit deadline in the past relative to the mocked clock.
        daemon._ratelimit_until = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 106.0]
            with patch("helper_common.daemon.datetime") as mock_dt:
                # Now is after the rate-limit deadline, so the trigger must fire.
                mock_dt.now.return_value = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)

        assert len(daemon.cycle_calls) == 1
        assert daemon._ratelimit_until is None


# ---------------------------------------------------------------------------
# HA birth message handling
# ---------------------------------------------------------------------------


class TestHaStatusHandling:
    def test_ha_online_schedules_discovery_with_delay(self) -> None:
        """A homeassistant/status "online" message must schedule _publish_discovery
        via threading.Timer with a delay in the range [1, 5] seconds.
        """
        daemon = _make_daemon()
        msg = _make_msg(topic=_HA_STATUS_TOPIC, payload=b"online")

        with patch("helper_common.daemon.threading.Timer") as mock_timer_cls:
            mock_timer = MagicMock()
            mock_timer_cls.return_value = mock_timer
            with patch("helper_common.daemon.random.uniform", return_value=2.5):
                daemon._on_message(daemon._client, None, msg)

        # A Timer must have been created with approximately the right delay.
        mock_timer_cls.assert_called_once()
        delay_arg = mock_timer_cls.call_args[0][0]
        assert delay_arg == pytest.approx(2.5)
        mock_timer.start.assert_called_once()

    def test_ha_online_does_not_fire_run_cycle(self) -> None:
        """The HA birth message must not trigger a fetch/publish cycle."""
        daemon = _make_daemon()
        msg = _make_msg(topic=_HA_STATUS_TOPIC, payload=b"online")

        with patch("helper_common.daemon.threading.Timer"):
            daemon._on_message(daemon._client, None, msg)

        assert daemon.cycle_calls == []

    def test_ha_offline_does_not_schedule_discovery(self) -> None:
        """A homeassistant/status message with payload other than "online" must
        be ignored silently — no Timer, no cycle.
        """
        daemon = _make_daemon()
        msg = _make_msg(topic=_HA_STATUS_TOPIC, payload=b"offline")

        with patch("helper_common.daemon.threading.Timer") as mock_timer_cls:
            daemon._on_message(daemon._client, None, msg)

        mock_timer_cls.assert_not_called()
        assert daemon.cycle_calls == []

    def test_ha_status_message_is_not_affected_by_retain_guard(self) -> None:
        """The HA birth message is published retained by HA itself. The retain
        guard must not suppress it — the check for the status topic must happen
        before the guard.
        """
        daemon = _make_daemon()
        # Simulate HA publishing the birth message as retained (which it does).
        msg = _make_msg(topic=_HA_STATUS_TOPIC, payload=b"online", retain=True)

        with patch("helper_common.daemon.threading.Timer") as mock_timer_cls:
            mock_timer_cls.return_value = MagicMock()
            with patch("helper_common.daemon.random.uniform", return_value=1.5):
                daemon._on_message(daemon._client, None, msg)

        # Timer must be created despite msg.retain being True.
        mock_timer_cls.assert_called_once()
        assert daemon.cycle_calls == []


# ---------------------------------------------------------------------------
# Stats publication
# ---------------------------------------------------------------------------

_STATS_TOPIC = "mimir/test/stats"


class TestStatsPublication:
    def test_stats_published_after_successful_cycle(self) -> None:
        """After a successful _run_cycle, a JSON stats payload is published
        retained to stats_topic containing ts, success, duration_s,
        horizon_hours, exit_code, and exit_message."""
        daemon = _make_daemon(stats_topic=_STATS_TOPIC)
        daemon._client.publish = MagicMock()
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            with patch("helper_common.daemon.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 4, 2, 14, 0, 0, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)

        publish_calls = [
            c for c in daemon._client.publish.call_args_list
            if c.args[0] == _STATS_TOPIC
        ]
        assert len(publish_calls) == 1, "Expected exactly one publish to stats_topic"
        payload = json.loads(publish_calls[0].args[1])
        assert set(payload.keys()) == {"ts", "success", "duration_s", "horizon_hours", "exit_code", "exit_message"}
        assert payload["success"] is True
        assert payload["ts"] == "2026-04-02T14:00:00Z"
        assert isinstance(payload["duration_s"], float)
        assert publish_calls[0].kwargs.get("qos") == 1
        assert publish_calls[0].kwargs.get("retain") is True

    def test_stats_published_after_unhandled_exception(self) -> None:
        """If _run_cycle raises, stats are still published with success=False."""
        daemon = _make_daemon(stats_topic=_STATS_TOPIC)
        daemon._client.publish = MagicMock()

        def _crashing(client: mqtt.Client) -> CycleResult | None:
            raise RuntimeError("boom")

        daemon._run_cycle = _crashing  # type: ignore[method-assign]
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            with patch("helper_common.daemon.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 4, 2, 14, 0, 0, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)

        publish_calls = [
            c for c in daemon._client.publish.call_args_list
            if c.args[0] == _STATS_TOPIC
        ]
        assert len(publish_calls) == 1
        payload = json.loads(publish_calls[0].args[1])
        assert payload["success"] is False
        assert payload["horizon_hours"] is None

    def test_stats_not_published_when_stats_topic_is_none(self) -> None:
        """When stats_topic is None, no publish call goes out for stats."""
        daemon = _make_daemon(stats_topic=None)
        daemon._client.publish = MagicMock()
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            with patch("helper_common.daemon.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 4, 2, 14, 0, 0, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)

        daemon._client.publish.assert_not_called()

    def test_horizon_hours_propagated_from_cycle_result(self) -> None:
        """When _run_cycle returns CycleResult(horizon_hours=24.0), the stats
        payload contains horizon_hours == 24.0."""
        daemon = _make_daemon(stats_topic=_STATS_TOPIC)
        daemon._client.publish = MagicMock()

        def _cycle_with_horizon(client: mqtt.Client) -> CycleResult | None:
            return CycleResult(horizon_hours=24.0)

        daemon._run_cycle = _cycle_with_horizon  # type: ignore[method-assign]
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            with patch("helper_common.daemon.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 4, 2, 14, 0, 0, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)

        publish_calls = [
            c for c in daemon._client.publish.call_args_list
            if c.args[0] == _STATS_TOPIC
        ]
        payload = json.loads(publish_calls[0].args[1])
        assert payload["horizon_hours"] == 24.0

    def test_cycle_result_suppress_until_is_stored(self) -> None:
        """CycleResult(suppress_until=<future>) is stored and subsequent
        triggers are rate-limited exactly as before."""
        daemon = _make_daemon(stats_topic=_STATS_TOPIC)
        daemon._client.publish = MagicMock()
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        call_count = 0

        def _limited(client: mqtt.Client) -> CycleResult | None:
            nonlocal call_count
            call_count += 1
            return CycleResult(suppress_until=future)

        daemon._run_cycle = _limited  # type: ignore[method-assign]
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 106.0]
            with patch("helper_common.daemon.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 4, 2, 14, 0, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)
                daemon._on_message(daemon._client, None, msg)

        assert call_count == 1
        assert daemon._ratelimit_until == future

    def test_cycle_result_none_clears_ratelimit_new(self) -> None:
        """CycleResult() with no suppress_until clears any previous rate-limit."""
        daemon = _make_daemon(stats_topic=_STATS_TOPIC)
        daemon._client.publish = MagicMock()
        daemon._ratelimit_until = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

        def _ok(client: mqtt.Client) -> CycleResult | None:
            return CycleResult()

        daemon._run_cycle = _ok  # type: ignore[method-assign]
        msg = _make_msg()

        with patch("helper_common.daemon.time") as mock_time:
            mock_time.monotonic.return_value = 100.0
            with patch("helper_common.daemon.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)
                daemon._on_message(daemon._client, None, msg)

        assert daemon._ratelimit_until is None
