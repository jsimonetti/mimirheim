"""Tests for config_editor.__main__._watch_for_restart_request.

Mirrors scheduler's own TestWatchForRestartRequest
(mimirheim_helpers/scheduler/scheduler/tests/unit/test_main.py):
config_editor drives its own run loop rather than using
helper_common.daemon.MqttDaemon.run, so this poll is implemented directly in
__main__.py instead of being inherited (config-owner-startup-resilience
ticket 03).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from config_editor.__main__ import _watch_for_restart_request


def _make_mqtt_client(restart_requested: threading.Event) -> MagicMock:
    client = MagicMock()
    client.restart_requested = restart_requested
    return client


class TestWatchForRestartRequest:
    def test_sets_stop_event_once_a_restart_is_requested(self) -> None:
        restart_requested = threading.Event()
        restart_requested.set()
        mqtt_client = _make_mqtt_client(restart_requested)
        stop_event = threading.Event()

        watcher = threading.Thread(
            target=_watch_for_restart_request, args=(mqtt_client, stop_event)
        )
        watcher.start()
        watcher.join(timeout=2)

        assert not watcher.is_alive()
        assert stop_event.is_set()

    def test_returns_promptly_if_stop_event_is_set_some_other_way(self) -> None:
        mqtt_client = _make_mqtt_client(threading.Event())
        stop_event = threading.Event()
        stop_event.set()

        watcher = threading.Thread(
            target=_watch_for_restart_request, args=(mqtt_client, stop_event)
        )
        watcher.start()
        watcher.join(timeout=2)

        assert not watcher.is_alive()
