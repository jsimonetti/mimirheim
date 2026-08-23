"""Unit tests for scheduler.loop.

Tests verify:
- run() exits immediately when stop_event is pre-set before the call.
- run() registers exactly one APScheduler job per schedule entry.
- All registered jobs use CronTrigger triggers.
- run() handles an empty schedule list without error.
- run() publishes to the correct topic when a job fires.
- A publish the broker never received raises, rather than logging success.
"""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from scheduler.loop import _publish, run


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

# ---------------------------------------------------------------------------
# publish failure
# ---------------------------------------------------------------------------


class _RcClient:
    """A client whose publish() reports a paho result code without connecting."""

    def __init__(self, rc: int) -> None:
        self._rc = rc
        self.calls: list[tuple] = []

    def publish(self, topic, payload=None, qos=0, retain=False):  # noqa: ANN001, ANN201
        self.calls.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self._rc)


def test_publish_is_silent_when_the_broker_accepts_the_message() -> None:
    """A successful publish raises nothing and logs nothing at warning or above."""
    client = _RcClient(mqtt.MQTT_ERR_SUCCESS)
    _publish(client, "mimir/input/trigger")
    assert client.calls == [("mimir/input/trigger", b"", 0, False)]


def test_publish_raises_when_the_client_is_not_connected() -> None:
    """paho drops a qos=0 publish while disconnected and only reports it in the rc."""
    client = _RcClient(mqtt.MQTT_ERR_NO_CONN)
    with pytest.raises(RuntimeError, match="not currently connected"):
        _publish(client, "mimir/input/trigger")


def test_publish_error_names_the_topic() -> None:
    """The failure message identifies which trigger was lost."""
    client = _RcClient(mqtt.MQTT_ERR_QUEUE_SIZE)
    with pytest.raises(RuntimeError, match="mimir/input/tools/prices/trigger"):
        _publish(client, "mimir/input/tools/prices/trigger")


def _wait_for_log(
    caplog: pytest.LogCaptureFixture, needle: str, timeout: float = 10.0
) -> list[str]:
    """Wait for a log record containing ``needle`` and return every message seen.

    The event listener that logs a fire runs after the job function returns, on
    APScheduler's executor thread. Sleeping a fixed interval instead of waiting
    for the record is a race that a loaded CI runner loses.

    Args:
        caplog: The pytest log capture fixture.
        needle: Substring identifying the record to wait for.
        timeout: Seconds to wait before giving up.

    Returns:
        The messages captured so far, whether or not the needle appeared.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = [r.getMessage() for r in caplog.records]
        if any(needle in m for m in messages):
            return messages
        time.sleep(0.01)
    return [r.getMessage() for r in caplog.records]


def test_lost_trigger_is_not_logged_as_a_successful_trigger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A trigger lost to a broker outage must not produce a success line.

    Before this behaviour existed, publish() returned rc=4, the message went
    nowhere, and the job still completed normally, so the scheduler logged
    "Triggered <topic>" every cycle for the duration of the outage.
    """
    client = _RcClient(mqtt.MQTT_ERR_NO_CONN)
    scheduler = BackgroundScheduler(timezone="UTC")
    stop_event = threading.Event()
    # Pre-set, so run() registers the job and returns without blocking. The
    # injected scheduler keeps running, which lets the job be forced below.
    stop_event.set()

    with caplog.at_level(logging.DEBUG, logger="scheduler.loop"):
        run(client, [("*/15 * * * *", "mimir/input/trigger")], stop_event,
            _scheduler=scheduler)
        job = scheduler.get_jobs()[0]
        job.modify(next_run_time=datetime.now(tz=UTC) + timedelta(milliseconds=50))
        messages = _wait_for_log(caplog, "raised an exception")
        scheduler.shutdown()

    assert any("raised an exception" in m for m in messages), messages
    assert not any("Triggered mimir/input/trigger" in m for m in messages), messages
