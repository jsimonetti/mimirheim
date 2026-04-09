"""Build a base load forecast from a static per-hour-of-day power profile.

This module tiles a caller-supplied kilowatt profile across a configurable
horizon of hours, producing a timestamped list of steps ready for MQTT
publication. It has no HTTP, config, or MQTT dependencies.

The profile can be any length from 1 to 168 elements. The wall-clock UTC hour
of each step selects a profile entry via modulo, so a 24-element profile
always maps profile[h] to hour h regardless of when the tool is triggered.
A single-element profile produces a flat constant load.

An optional ``weekly_profiles_kw`` dict (keyed 0=Monday … 6=Sunday) provides
per-weekday overrides. When a step's UTC weekday has an entry, that profile is
used instead of the fallback ``profile_kw``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def build_forecast(
    profile_kw: list[float] | None,
    horizon_hours: int,
    now: datetime,
    weekly_profiles_kw: dict[int, list[float]] | None = None,
) -> list[dict[str, Any]]:
    """Build a timestamped base load forecast by tiling a static profile.

    Each step selects its power value from the most specific profile available:

    1. If ``weekly_profiles_kw`` contains an entry for the step's UTC weekday
       (0=Monday … 6=Sunday), that profile is used.
    2. Otherwise ``profile_kw`` is used as the fallback.

    Within the selected profile, the value is chosen by:

        kw = profile[ts.hour % len(profile)]

    where ``ts.hour`` is the UTC wall-clock hour. Profile[0] is always served at
    midnight, profile[12] at noon, and so on — independent of trigger time.

    Args:
        profile_kw: Fallback repeating kilowatt profile. May be None only when
            ``weekly_profiles_kw`` covers all 7 weekdays.
        horizon_hours: Number of hourly steps to produce.
        now: Current UTC time. The first step is aligned to the current hour
            (minutes, seconds, and microseconds are discarded).
        weekly_profiles_kw: Optional per-weekday profiles keyed by Python
            weekday number (0=Monday, 6=Sunday). When provided, matching steps
            use the weekday profile instead of ``profile_kw``.

    Returns:
        List of step dicts with "ts" (UTC ISO 8601 string) and "kw" (float).
    """
    start = now.replace(minute=0, second=0, microsecond=0)

    steps: list[dict[str, Any]] = []
    for offset in range(horizon_hours):
        ts = start + timedelta(hours=offset)
        weekday = ts.weekday()  # 0=Monday, 6=Sunday
        if weekly_profiles_kw is not None and weekday in weekly_profiles_kw:
            active_profile = weekly_profiles_kw[weekday]
        elif profile_kw is not None:
            active_profile = profile_kw
        else:
            raise ValueError(
                f"No profile available for weekday {weekday} and profile_kw is None."
            )
        kw = active_profile[ts.hour % len(active_profile)]
        steps.append(
            {
                "ts": ts.isoformat(),
                "kw": round(kw, 4),
            }
        )

    return steps
