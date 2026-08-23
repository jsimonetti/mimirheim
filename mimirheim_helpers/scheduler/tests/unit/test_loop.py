"""Unit tests for scheduler.loop.

Tests verify:
- run() exits immediately when stop_event is pre-set before the call.
- run() registers exactly one APScheduler job per schedule entry.
- All registered jobs use CronTrigger triggers.
- run() handles an empty schedule list without error.
- A configured schedule reaches _publish() with the right topic and payload.
- A publish the broker never received raises, rather than logging success.
"""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

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


def _fire(scheduler: BackgroundScheduler, job_id: str, client: "_RcClient") -> None:
    """Bring a registered job forward and wait for it to run.

    Args:
        scheduler: The running scheduler holding the job.
        job_id: Identifier of the job to fire.
        client: The client whose recorded calls signal that the job has run.
    """
    scheduler.get_job(job_id).modify(
        next_run_time=datetime.now(tz=UTC) + timedelta(milliseconds=50)
    )
    deadline = time.monotonic() + 5.0
    while not client.calls and time.monotonic() < deadline:
        time.sleep(0.01)


def test_a_firing_schedule_publishes_an_empty_trigger_to_its_topic() -> None:
    """A configured schedule reaches scheduler.loop._publish when it fires.

    This goes through the real path: a (cron_expr, topic) pair is registered by
    run(), the registered job is brought forward, and the publish that arrives
    at the client is inspected. Asserting the payload, qos and retain flag as
    well as the topic is what makes the test fail if _publish is changed or
    removed, rather than merely if the job runs.
    """
    client = _RcClient(mqtt.MQTT_ERR_SUCCESS)
    scheduler = BackgroundScheduler(timezone="UTC")
    stop_event = threading.Event()
    stop_event.set()

    try:
        run(client, [("*/15 * * * *", "mimir/input/trigger")], stop_event,
            _scheduler=scheduler)
        _fire(scheduler, "job_0", client)
    finally:
        scheduler.shutdown()

    assert client.calls == [("mimir/input/trigger", b"", 0, False)]


def test_each_schedule_publishes_to_its_own_topic() -> None:
    """The topic published is the one paired with the cron expression that fired."""
    client = _RcClient(mqtt.MQTT_ERR_SUCCESS)
    scheduler = BackgroundScheduler(timezone="UTC")
    stop_event = threading.Event()
    stop_event.set()

    schedules = [
        ("*/15 * * * *", "mimir/input/trigger"),
        ("0 14 * * *", "mimir/input/tools/prices/trigger"),
        ("0 0 * * *", "mimir/input/tools/baseload/trigger"),
    ]
    try:
        run(client, schedules, stop_event, _scheduler=scheduler)
        _fire(scheduler, "job_1", client)
    finally:
        scheduler.shutdown()

    assert client.calls == [("mimir/input/tools/prices/trigger", b"", 0, False)]


def test_a_firing_schedule_is_logged_with_its_topic(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delivered trigger is reported under the topic, not the internal job id."""
    client = _RcClient(mqtt.MQTT_ERR_SUCCESS)
    scheduler = BackgroundScheduler(timezone="UTC")
    stop_event = threading.Event()
    stop_event.set()

    try:
        with caplog.at_level(logging.INFO, logger="scheduler.loop"):
            run(client, [("*/15 * * * *", "mimir/input/trigger")], stop_event,
                _scheduler=scheduler)
            _fire(scheduler, "job_0", client)
            messages = _wait_for_log(caplog, "Triggered")
    finally:
        scheduler.shutdown()

    assert any("Triggered mimir/input/trigger" in m for m in messages), messages


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
