"""Unit tests for scheduler.__main__.

Tests verify:
- Logging setup leaves the scheduler's own loggers inheriting the root level.
- APScheduler's loggers are quietened to WARNING, so that its per-fire
  duplicates of the scheduler's own messages do not reach the log while its
  misfire warnings still do.
"""

import logging

from scheduler.__main__ import _configure_logging


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
