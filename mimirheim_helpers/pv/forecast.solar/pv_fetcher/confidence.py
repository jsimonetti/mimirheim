"""Confidence decay logic for the PV forecast pipeline.

This module applies per-step confidence values to a forecast series based on
how far ahead each step is from the time of the fetch. Steps further in the
future are less reliable and receive lower confidence values.

The confidence values are configurable in ConfidenceDecayConfig. The defaults
represent a reasonable envelope for the forecast.solar service:

    0–6 h ahead:   0.90  (very recent forecast, high confidence)
    6–24 h ahead:  0.75  (same-day forecast, good confidence)
    24–48 h ahead: 0.55  (tomorrow's forecast, moderate confidence)
    48+ h ahead:   0.35  (day-after-tomorrow, speculative)

What this module does not do:
- It does not call the forecast.solar API.
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
        hours_0_to_6: Confidence for steps 0–6 hours ahead.
        hours_6_to_24: Confidence for steps 6–24 hours ahead.
        hours_24_to_48: Confidence for steps 24–48 hours ahead.
        hours_48_plus: Confidence for steps more than 48 hours ahead.
    """

    hours_0_to_6: float
    hours_6_to_24: float
    hours_24_to_48: float
    hours_48_plus: float

    def confidence_for_step(self, step_ts: datetime, fetch_time: datetime) -> float:
        """Return the confidence value for a forecast step.

        Selects the appropriate band based on how many hours ahead ``step_ts``
        is relative to ``fetch_time``. Both arguments must be UTC-aware.

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


def fill_night_gaps(
    watts: dict[datetime, int],
    *,
    step: timedelta = timedelta(hours=1),
) -> dict[datetime, int]:
    """Fill missing timesteps in a sparse watts dict with zero-watt entries.

    forecast.solar only returns timestamps where power is predicted to be
    non-zero. Night-time hours between the last daytime step of one day and the
    first daytime step of the next day are absent from the response entirely.

    Without gap-filling, the step-function resampler in mimirheim would hold the
    last non-zero evening value through the entire night, producing spuriously
    positive kW values. Inserting zeros at every missing hourly slot within the
    forecast range prevents this.

    The API also includes sub-hourly "boundary" timestamps at exact sunrise and
    sunset times (e.g. ``05:07:58Z``, ``18:11:24Z``) with a value of zero.
    Starting the fill walk from one of these sub-hourly markers would generate
    offsets like ``06:07:58``, ``07:07:58`` etc. — zero entries that land
    *inside* hourly data windows. The step-function resampler would then pick
    ``07:07:58=0`` as the ``before`` value for ``t=07:15``, ``t=07:30``, and
    ``t=07:45``, returning zero for three out of four quarter-hour slots.

    To avoid this, the fill walk is floor-snapped to the nearest step boundary
    at or before the first timestamp. For ``step=1h`` and a sunrise marker at
    ``05:07:58``, the walk starts at ``05:00:00`` and proceeds on clean UTC
    hour boundaries (``06:00``, ``07:00``, …), which already exist in the data
    and are therefore skipped. No zero entries are injected inside day-light
    windows.

    Args:
        watts: Raw ``Estimate.watts`` dict from the forecast_solar library,
            mapping UTC-aware datetimes to integer watt values. Keys are a mix
            of exact hourly entries and sub-hourly sunrise/sunset markers.
        step: Slot size to fill. Defaults to one hour, which matches the
            forecast.solar API data resolution. Override only in tests.

    Returns:
        A new dict containing all original entries plus zero-watt entries for
        every missing step-aligned slot between the first and last timestamps.
    """
    if len(watts) < 2:
        return dict(watts)

    sorted_ts = sorted(watts)

    # Seed the output with every original entry so the sub-hourly boundary
    # markers and all hourly data entries survive unconditionally.
    filled: dict[datetime, int] = dict(watts)

    # Floor-snap the walk start to the nearest step boundary at or before
    # the first timestamp.  sorted_ts[0] may be a sub-hourly sunrise or
    # sunset marker (e.g. 05:07:58Z from the forecast.solar API).  Starting
    # the walk from that sub-hourly offset would generate 06:07:58, 07:07:58,
    # … — zeros that land inside hourly data windows and corrupt the step-
    # function resampler output for the :15, :30, :45 quarter-hour slots.
    first = sorted_ts[0]
    end = sorted_ts[-1]
    step_secs = step.total_seconds()
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc) if first.tzinfo is not None else datetime(1970, 1, 1)
    elapsed_secs = (first - epoch).total_seconds()
    ts = epoch + timedelta(seconds=(elapsed_secs // step_secs) * step_secs)

    while ts <= end:
        if ts not in filled:
            filled[ts] = 0
        ts += step
    return filled


def apply_confidence(
    watts: dict[datetime, int],
    fetch_time: datetime,
    decay: ConfidenceDecay,
) -> list[dict]:
    """Convert a raw watts dict to a list of mimirheim-format forecast steps.

    Takes the ``estimate.watts`` dict returned by ``fetch_array``
    (``{UTC-aware datetime: watts_int}``), converts watts to kilowatts,
    attaches a confidence value to each step, and returns the result as a
    list of dicts in the format expected by the mimirheim PV forecast input topic.

    Steps are returned in ascending timestamp order.

    Args:
        watts: A mapping from UTC-aware datetimes to integer watts, as
            returned by ``fetch_array``. All timestamps must be UTC-aware.
        fetch_time: The time at which the forecast was fetched. Used as the
            reference point for computing confidence band membership.
        decay: Confidence values per horizon band.

    Returns:
        A list of dicts, each with keys ``ts`` (ISO 8601 UTC string),
        ``kw`` (float, rounded to 3 decimal places), and ``confidence``
        (float from ``decay``). Ordered by ascending timestamp.
    """
    # Ensure the fetch_time is UTC-aware for comparison with the step timestamps.
    if fetch_time.tzinfo is None:
        fetch_time = fetch_time.replace(tzinfo=timezone.utc)

    # Fill night-time gaps with zeros before attaching confidence values.
    # forecast.solar omits hours with no predicted power; without this step,
    # resample_power in mimirheim would linearly interpolate across the gaps and
    # produce non-zero kW values during the night.
    watts = fill_night_gaps(watts)

    steps = []
    for ts in sorted(watts):
        ts_utc = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
        kw = round(watts[ts] / 1000.0, 3)
        confidence = decay.confidence_for_step(ts_utc, fetch_time)
        steps.append({
            "ts": ts_utc.isoformat(),
            "kw": kw,
            "confidence": confidence,
        })
    return steps
