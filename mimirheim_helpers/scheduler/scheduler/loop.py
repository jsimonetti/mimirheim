"""Schedule loop — registers cron jobs with APScheduler and publishes MQTT trigger messages.

This module owns the scheduling lifecycle. It registers one APScheduler cron job
per schedule entry, starts a BackgroundScheduler, then blocks on the stop_event
until the caller requests shutdown. On stop, it shuts down the scheduler cleanly
and waits for any in-flight job to complete.

APScheduler handles all timer, drift, and coalescing logic. The custom min-heap
and threading.Event sleep loop that previously existed here have been removed.

What this module does not do:
- It does not parse configuration — that is config.py's responsibility.
- It does not connect to MQTT — that is __main__.py's responsibility.
- It does not replay missed triggers: APScheduler's default misfire_grace_time
  of 1 second means any job that fires more than 1 second late is discarded,
  consistent with standard cron semantics where a skipped job is not re-run.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger("scheduler.loop")


def _publish(client: Any, topic: str) -> None:
    """Publish an empty MQTT trigger message to ``topic``.

    This is the callable registered as the APScheduler job function. It is
    intentionally thin: all scheduling logic lives in APScheduler, and all
    MQTT connection management lives in __main__.py.

    Args:
        client: A paho-mqtt Client instance with loop_start() already called.
        topic: The MQTT topic to publish to.
    """
    client.publish(topic, payload=b"", qos=0, retain=False)


def run(
    client: Any,
    schedules: list[tuple[str, str]],
    stop_event: threading.Event,
    *,
    _scheduler: BackgroundScheduler | None = None,
) -> None:
    """Run the scheduler until ``stop_event`` is set.

    Registers one APScheduler cron job per schedule entry, starts the
    BackgroundScheduler in a background thread, then blocks until stop_event
    is set. On stop, shuts down the scheduler cleanly and waits for any
    currently executing job to finish.

    APScheduler's default ``misfire_grace_time`` of 1 second means any job
    whose scheduled time is more than 1 second in the past when the scheduler
    wakes up is silently discarded. There is no catch-up behaviour.

    ``coalesce=True`` means that if multiple instances of the same job have
    accumulated (e.g. after a GC pause or a brief system sleep), only one
    instance is run and the rest are discarded.

    Args:
        client: A paho-mqtt Client instance with loop_start() already called.
            The scheduler calls client.publish() with qos=0, retain=False for
            each trigger that fires.
        schedules: List of (cron_expr, topic) pairs from the config. Typically
            obtained via SchedulerConfig.parsed_schedules().
        stop_event: Setting this event causes the loop to exit after the
            current sleep completes. SIGTERM and SIGINT handlers in __main__
            set this event.
        _scheduler: Optional BackgroundScheduler to use instead of creating a
            new one. Pass a controlled instance in tests to add pre-configured
            jobs or inspect registered jobs after run() returns.
    """
    owned = _scheduler is None
    scheduler = _scheduler if _scheduler is not None else BackgroundScheduler(timezone="UTC")

    job_topic: dict[str, str] = {}
    for i, (cron_expr, topic) in enumerate(schedules):
        job_id = f"job_{i}"
        scheduler.add_job(
            _publish,
            CronTrigger.from_crontab(cron_expr, timezone="UTC"),
            args=[client, topic],
            id=job_id,
            name=topic,
            coalesce=True,
            max_instances=1,
        )
        job_topic[job_id] = topic

    def _on_event(event: JobExecutionEvent) -> None:
        topic = job_topic.get(event.job_id, event.job_id)  # type: ignore[arg-type]
        if event.exception:
            logger.error(
                "Job for %s raised an exception: %s",
                topic,
                event.exception,
            )
        else:
            logger.info(
                "Triggered %s (scheduled %s)",
                topic,
                event.scheduled_run_time.isoformat(),
            )

    scheduler.add_listener(_on_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.start()
    logger.info("Scheduler running with %d schedule entries.", len(schedules))

    stop_event.wait()
    if owned:
        scheduler.shutdown()
    logger.info("Scheduler loop stopped.")
