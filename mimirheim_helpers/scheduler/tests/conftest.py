"""Pytest configuration for the scheduler test suite."""

from collections.abc import Iterator

import pytest
from apscheduler.schedulers.background import BackgroundScheduler


@pytest.fixture
def scheduler() -> Iterator[BackgroundScheduler]:
    """Yield a scheduler that is shut down when the test ends.

    ``run()`` only shuts down a scheduler it created itself, because
    ``shutdown()`` empties the job store and several tests inspect
    ``get_jobs()`` after ``run()`` returns. A test that injects its own
    scheduler is therefore responsible for stopping it, and a test that
    forgets leaves a live thread with armed cron jobs behind for the rest of
    the session. Those jobs do fire: a ``*/15 * * * *`` entry registered by a
    finished test will publish to its mock partway through an unrelated test
    whenever a run crosses a quarter-hour boundary.

    Yields:
        A scheduler in the UTC timezone, not yet started.
    """
    instance = BackgroundScheduler(timezone="UTC")
    yield instance
    if instance.running:
        instance.shutdown()
