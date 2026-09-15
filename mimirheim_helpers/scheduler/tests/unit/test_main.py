"""Unit tests for scheduler.__main__.

Tests verify:
- Logging setup leaves the scheduler's own loggers inheriting the root level.
- APScheduler's loggers are quietened to WARNING, so that its per-fire
  duplicates of the scheduler's own messages do not reach the log while its
  misfire warnings still do.
- ``_watch_for_restart_request`` sets ``stop_event`` once a Restart Request
  is acknowledged, and exits promptly if ``stop_event`` is set some other
  way first (config-owner-startup-resilience ticket 03).
"""

import logging
import threading

from helper_common.config_owner import ConfigOwnerSupport

from scheduler.__main__ import _configure_logging, _watch_for_restart_request
from scheduler.config import SchedulerConfig
from scheduler.formspec import SCHEDULER_CONFIG_FORM_SPEC


def test_scheduler_loggers_keep_inheriting_the_root_level() -> None:
    """The tool's own loggers are left alone, so its messages still appear.

    Asserting a concrete level here would be testing basicConfig, which does
    nothing once the root logger has handlers, as it does under pytest. The
    contract is that no explicit level is pinned on these loggers.
    """
    _configure_logging()
    assert logging.getLogger("scheduler").level == logging.NOTSET
    assert logging.getLogger("scheduler.loop").level == logging.NOTSET


def test_apscheduler_is_quietened_to_warning() -> None:
    """APScheduler restates every fire twice under its own loggers.

    Each trigger produced three log lines, two of them APScheduler repeating
    what scheduler.loop had already reported, each carrying a long trigger
    repr. Eight of the twelve lines from a startup plus one fire came from
    apscheduler.
    """
    _configure_logging()
    for name in ("apscheduler", "apscheduler.scheduler", "apscheduler.executors.default"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


def test_misfire_warnings_survive() -> None:
    """A skipped trigger is only ever reported by APScheduler, at WARNING.

    Nothing in this package logs a misfire, so quietening apscheduler below
    WARNING would make a lost trigger completely silent.
    """
    _configure_logging()
    assert logging.getLogger("apscheduler.executors.default").isEnabledFor(logging.WARNING)


def _make_config_owner() -> ConfigOwnerSupport:
    return ConfigOwnerSupport(
        owner_id="scheduler",
        display_name="Scheduler",
        model=SchedulerConfig,
        form_spec=SCHEDULER_CONFIG_FORM_SPEC,
        config_path=None,
    )


class TestWatchForRestartRequest:
    def test_sets_stop_event_once_a_restart_is_requested(self) -> None:
        """The scheduler has no MqttDaemon base class to poll
        restart_requested for it (ADR-0012), so this function does it in its
        own thread; a Restart Request must still result in stop_event being
        set."""
        config_owner = _make_config_owner()
        stop_event = threading.Event()
        config_owner.restart_requested.set()

        watcher = threading.Thread(
            target=_watch_for_restart_request, args=(config_owner, stop_event)
        )
        watcher.start()
        watcher.join(timeout=2)

        assert not watcher.is_alive()
        assert stop_event.is_set()

    def test_returns_promptly_if_stop_event_is_set_some_other_way(self) -> None:
        """A SIGTERM/SIGINT handler sets stop_event directly; this watcher
        must not keep the process alive waiting on a Restart Request that
        will never come."""
        config_owner = _make_config_owner()
        stop_event = threading.Event()
        stop_event.set()

        watcher = threading.Thread(
            target=_watch_for_restart_request, args=(config_owner, stop_event)
        )
        watcher.start()
        watcher.join(timeout=2)

        assert not watcher.is_alive()
