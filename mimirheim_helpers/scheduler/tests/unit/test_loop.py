"""Unit tests for scheduler.loop.

Tests verify:
- run() exits immediately when stop_event is pre-set before the call.
- run() registers exactly one APScheduler job per schedule entry.
- All registered jobs use CronTrigger triggers.
- run() handles an empty schedule list without error.
- run() publishes to the correct topic when a job fires.
"""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from scheduler.loop import run


# ---------------------------------------------------------------------------
# stop behaviour
# ---------------------------------------------------------------------------


def test_run_exits_when_stop_event_is_preset() -> None:
    """Setting stop_event before calling run() causes immediate return without publishing."""
    client = MagicMock()
    stop_event = threading.Event()
    stop_event.set()

    run(client, [("*/15 * * * *", "test/topic")], stop_event)

    client.publish.assert_not_called()


# ---------------------------------------------------------------------------
# job registration
# ---------------------------------------------------------------------------


def test_run_registers_correct_number_of_jobs() -> None:
    """run() registers exactly one APScheduler job per schedule entry."""
    client = MagicMock()
    stop_event = threading.Event()
    stop_event.set()

    schedules = [
        ("*/15 * * * *", "mimir/input/trigger"),
        ("0 12 * * *", "mimir/input/tools/prices/trigger"),
    ]
    scheduler = BackgroundScheduler(timezone="UTC")

    run(client, schedules, stop_event, _scheduler=scheduler)

    assert len(scheduler.get_jobs()) == 2


def test_run_registers_cron_triggers() -> None:
    """Every job registered by run() uses a CronTrigger."""
    client = MagicMock()
    stop_event = threading.Event()
    stop_event.set()

    schedules = [
        ("*/15 * * * *", "mimir/input/trigger"),
        ("0 12 * * *", "mimir/input/tools/prices/trigger"),
        ("50 23 * * *", "mimir/input/tools/baseload/trigger"),
    ]
    scheduler = BackgroundScheduler(timezone="UTC")

    run(client, schedules, stop_event, _scheduler=scheduler)

    for job in scheduler.get_jobs():
        assert isinstance(job.trigger, CronTrigger)


def test_run_empty_schedules() -> None:
    """run() exits cleanly and does not publish when the schedule list is empty."""
    client = MagicMock()
    stop_event = threading.Event()
    stop_event.set()

    run(client, [], stop_event)

    client.publish.assert_not_called()


# ---------------------------------------------------------------------------
# publish path
# ---------------------------------------------------------------------------


def test_run_publishes_when_job_fires() -> None:
    """The publish callable is invoked when a pre-configured job fires.

    A DateTrigger job is injected into the BackgroundScheduler before run() is
    called. The job publishes to a topic and then sets stop_event. run() blocks
    on stop_event.wait(), so it returns only after the publish has occurred.
    """
    client = MagicMock()
    stop_event = threading.Event()

    def _job() -> None:
        client.publish("test/topic", payload=b"", qos=0, retain=False)
        stop_event.set()

    scheduler = BackgroundScheduler(timezone="UTC")
    run_date = datetime.now(tz=timezone.utc) + timedelta(milliseconds=200)
    scheduler.add_job(_job, DateTrigger(run_date=run_date), id="test_job")

    run(client, [], stop_event, _scheduler=scheduler)

    client.publish.assert_called_once_with("test/topic", payload=b"", qos=0, retain=False)

