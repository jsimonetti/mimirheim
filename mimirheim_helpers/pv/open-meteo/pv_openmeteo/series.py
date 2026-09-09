"""Series shaping for the Open-Meteo PV forecast pipeline.

This module turns the power series returned by the fetcher into the payload
mimirheim expects: trimmed to the retention window, ordered, in UTC, and with a
confidence value attached to every step.

Confidence decays with the forecast horizon. The defaults are the same envelope
the forecast.solar helper uses, because they describe how much a day-ahead
irradiance forecast can be trusted rather than anything specific to a provider:

    0-6 h ahead:   0.90  (very recent forecast, high confidence)
    6-24 h ahead:  0.75  (same-day forecast, good confidence)
    24-48 h ahead: 0.55  (tomorrow's forecast, moderate confidence)
    48+ h ahead:   0.35  (day after tomorrow, speculative)

Unlike the forecast.solar pipeline, there is no night-gap filling to do here.
Open-Meteo returns a dense 15-minute series with explicit zeros overnight, so
the resampler in mimirheim never has to bridge a hole.

What this module does not do:
- It does not call the Open-Meteo API.
- It does not publish to MQTT.
- It does not import from mimirheim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class ConfidenceDecay:
    """Confidence values to assign per horizon band.

    Attributes:
        hours_0_to_6: Confidence for steps 0-6 hours ahead.
        hours_6_to_24: Confidence for steps 6-24 hours ahead.
        hours_24_to_48: Confidence for steps 24-48 hours ahead.
        hours_48_plus: Confidence for steps more than 48 hours ahead.
    """

    hours_0_to_6: float
    hours_6_to_24: float
    hours_24_to_48: float
    hours_48_plus: float

    def confidence_for_step(self, step_ts: datetime, fetch_time: datetime) -> float:
        """Return the confidence value for a forecast step.

        Selects the band from how many hours ahead ``step_ts`` is relative to
        ``fetch_time``. Steps in the recent past fall in the nearest band: they
        are the part of the forecast that has already been observed, so they
        are at least as reliable as the next hour.

        Args:
            step_ts: The timestamp of the forecast step (UTC-aware).
            fetch_time: The time at which the forecast was fetched (UTC-aware).

        Returns:
            A confidence value in [0.0, 1.0].
        """
        hours_ahead = (step_ts - fetch_time).total_seconds() / 3600
        if hours_ahead < 6:
            return self.hours_0_to_6
        if hours_ahead < 24:
            return self.hours_6_to_24
        if hours_ahead < 48:
            return self.hours_24_to_48
        return self.hours_48_plus


def trim_history(
    series: dict[datetime, float],
    *,
    now: datetime,
    past_hours: float,
) -> dict[datetime, float]:
    """Drop steps that are further in the past than the retention window.

    Open-Meteo returns whole days, so a fetch at 18:00 carries eighteen hours of
    elapsed forecast that the solver has no use for. Keeping a small amount of
    it is still worthwhile: mimirheim resamples onto a 15-minute grid starting
    at the current time, and a step at or before that instant means the first
    grid slot is interpolated rather than extrapolated.

    Args:
        series: Power series keyed by UTC-aware timestamp.
        now: Reference instant, normally the start of the fetch cycle.
        past_hours: Hours of elapsed forecast to keep. 0 keeps only steps at or
            after ``now``.

    Returns:
        A new dict containing the steps at or after ``now - past_hours``.
    """
    cutoff = now - timedelta(hours=past_hours)
    return {ts: kw for ts, kw in series.items() if ts >= cutoff}


def apply_confidence(
    series: dict[datetime, float],
    fetch_time: datetime,
    decay: ConfidenceDecay,
) -> list[dict]:
    """Convert a kW series into the list of steps mimirheim expects.

    Args:
        series: Average power in kW keyed by timestamp. Timestamps may carry
            any UTC offset; naive timestamps are treated as UTC.
        fetch_time: The time at which the forecast was fetched. Used as the
            reference point for the confidence bands.
        decay: Confidence values per horizon band.

    Returns:
        A list of dicts, each with keys ``ts`` (ISO 8601 UTC string), ``kw``
        (float rounded to three decimals) and ``confidence``. Ordered by
        ascending timestamp.
    """
    if fetch_time.tzinfo is None:
        fetch_time = fetch_time.replace(tzinfo=timezone.utc)

    steps: list[dict] = []
    for ts in sorted(series, key=_as_utc):
        ts_utc = _as_utc(ts)
        steps.append({
            "ts": ts_utc.isoformat(),
            "kw": round(series[ts], 3),
            "confidence": decay.confidence_for_step(ts_utc, fetch_time),
        })
    return steps


def _as_utc(ts: datetime) -> datetime:
    """Return ``ts`` as a UTC-aware datetime, treating a naive value as UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)
