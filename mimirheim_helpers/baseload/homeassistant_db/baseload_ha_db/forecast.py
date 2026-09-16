"""Build a base load forecast from Home Assistant historical statistics.

This module transforms hourly kWh/h readings (already unit-converted by
``fetch_statistics``) into a timestamped forecast covering a configurable
horizon. It has no HTTP, config, or MQTT dependencies.

The forecasting method is a same-hour-of-day average with optional recency
weighting:

1. Merge all entities into one net series per timestamp:
   net(t) = sum(sum_entities at t) - sum(subtract_entities at t). An entity
   without a reading at t contributes zero at t. Timestamps where no sum
   entity has a reading are skipped.
2. Group the net series by hour-of-day (0-23) and compute the weighted mean
   kWh/h at each hour, giving more recent days a higher weight when
   ``lookback_decay > 1.0``.
3. Clamp each hourly mean to zero.
4. Tile the resulting 24-hour profile to fill horizon_hours steps starting from now.

Merging before averaging is what lets two entities that hand over to each
other (a renamed or replaced sensor) read as one continuous series. Averaging
each entity separately and then summing would count both at full strength
even though they never overlapped in time.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def _compute_day_weights(
    *,
    now: datetime,
    lookback_days: int,
    decay: float,
) -> dict[date, float]:
    """Compute a weight for each calendar day in the lookback window.

    Weights express how much influence each day's readings have on the
    per-hour averages. The oldest day is always assigned weight 1.0; the most
    recent day is assigned weight ``decay``; intermediate days are interpolated
    exponentially:

        weight(i) = decay ** (i / (lookback_days - 1))

    where i = 0 is the oldest day and i = lookback_days - 1 is the newest.
    This is the same family of weighting used in exponentially weighted moving
    averages (EWMA): the importance of older observations decays relative to
    more recent ones.

    When ``decay == 1.0`` or ``lookback_days <= 1``, all weights are 1.0,
    which reproduces the plain (un-weighted) same-hour average.

    Args:
        now: Reference time. The lookback window ends here and starts
            ``lookback_days`` days earlier at midnight UTC.
        lookback_days: Number of days in the lookback window.
        decay: Total weight ratio of the newest day to the oldest. Must be
            >= 1.0. ``1.0`` disables weighting (all days equal). ``2.0`` means
            the most recent day counts twice as much as the oldest day.

    Returns:
        Dict mapping each calendar date in the lookback window to its weight.
    """
    start = (now - timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if lookback_days <= 1 or decay == 1.0:
        return {
            (start + timedelta(days=i)).date(): 1.0
            for i in range(lookback_days)
        }
    return {
        (start + timedelta(days=i)).date(): decay ** (i / (lookback_days - 1))
        for i in range(lookback_days)
    }


@dataclass
class HourlyProfile:
    """A 24-element per-hour-of-day average load profile in kilowatts.

    Each slot h (0-23) holds the mean observed kW at that hour across the
    lookback window. If a particular hour has no observations, it falls back to
    the global mean across all available readings.

    This class is an internal data structure used by build_forecast. It is not
    part of the public MQTT output.
    """

    # _kw_by_hour maps hour-of-day (0-23) to the computed mean kW.
    _kw_by_hour: dict[int, float] = field(default_factory=dict)
    # _global_mean is the average across all readings, used as a fallback when
    # data is absent for a specific hour.
    _global_mean: float = 0.0

    def kw_for_hour(self, hour: int) -> float:
        """Return the forecast kW for the given hour-of-day (0-23).

        Falls back to the global mean if no observations exist for this hour.

        Args:
            hour: Hour of day (0-23).

        Returns:
            Forecast power in kilowatts.
        """
        return self._kw_by_hour.get(hour, self._global_mean)

    @classmethod
    def from_readings(
        cls,
        readings: list[dict[str, Any]],
        day_weights: dict[date, float] | None = None,
    ) -> "HourlyProfile":
        """Build an HourlyProfile from a list of hourly kWh/h readings.

        Readings must have already been converted to kWh/h by the fetcher layer
        before being passed here. This method performs only hour-of-day grouping
        and weighted averaging — no unit conversion.

        Args:
            readings: List of reading dicts. Each must have a ``"start"`` key
                (ISO 8601 timestamp) and a ``"mean"`` key (numeric, in kWh/h).
            day_weights: Optional mapping from calendar date to a weight factor.
                When provided, readings on days with a higher weight contribute
                proportionally more to each hour's weighted average. When None,
                all readings are weighted equally (equivalent to decay = 1.0).

        Returns:
            A populated HourlyProfile.
        """
        # Accumulate readings grouped by hour-of-day.
        # Each bucket stores (kw, weight) pairs for the weighted average.
        hour_buckets: dict[int, list[tuple[float, float]]] = defaultdict(list)

        for reading in readings:
            try:
                ts = datetime.fromisoformat(reading["start"])
                kw = float(reading["mean"])
            except (KeyError, ValueError, TypeError):
                logger.warning(
                    "Skipping malformed HA reading: %r", reading
                )
                continue
            weight = day_weights.get(ts.date(), 1.0) if day_weights is not None else 1.0
            hour_buckets[ts.hour].append((kw, weight))

        if not hour_buckets:
            return cls(_kw_by_hour={}, _global_mean=0.0)

        # Compute the weighted mean kW for each hour-of-day.
        kw_by_hour: dict[int, float] = {}
        for h, pairs in hour_buckets.items():
            total_w = sum(w for _, w in pairs)
            kw_by_hour[h] = sum(v * w for v, w in pairs) / total_w

        # The global fallback is the weighted mean across all readings.
        all_pairs = [(v, w) for pairs in hour_buckets.values() for v, w in pairs]
        total_w_all = sum(w for _, w in all_pairs)
        global_mean = sum(v * w for v, w in all_pairs) / total_w_all

        return cls(_kw_by_hour=kw_by_hour, _global_mean=global_mean)


def _merge_net_readings(
    sum_readings: dict[str, list[dict[str, Any]]],
    subtract_readings: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Combine per-entity readings into one net kWh/h series keyed by timestamp.

    For every timestamp at which at least one sum entity has a reading:

        net = sum(sum entity values) - sum(subtract entity values)

    where an entity with no reading at that timestamp contributes zero.
    Timestamps carried only by subtract entities are dropped: there is nothing
    to subtract from, and keeping them would inject purely negative hours into
    the average.

    The merge is keyed on the parsed instant, not the raw ``"start"`` string,
    so two spellings of the same moment collapse into one timestamp.

    Args:
        sum_readings: Per-entity hourly kWh/h readings for entities to sum.
        subtract_readings: Per-entity hourly kWh/h readings for entities to
            subtract.

    Returns:
        List of reading dicts with ``"start"`` (UTC ISO 8601 string) and
        ``"mean"`` (net kWh/h, may be negative), sorted by timestamp.
    """
    net_by_ts: dict[datetime, float] = {}
    seen_in_sum: set[datetime] = set()

    def _iter(readings: list[dict[str, Any]]) -> Iterator[tuple[datetime, float]]:
        for reading in readings:
            try:
                ts = datetime.fromisoformat(reading["start"])
                kw = float(reading["mean"])
            except (KeyError, ValueError, TypeError):
                logger.warning("Skipping malformed HA reading: %r", reading)
                continue
            yield ts, kw

    for readings in sum_readings.values():
        for ts, kw in _iter(readings):
            net_by_ts[ts] = net_by_ts.get(ts, 0.0) + kw
            seen_in_sum.add(ts)

    for readings in subtract_readings.values():
        for ts, kw in _iter(readings):
            if ts not in seen_in_sum:
                continue
            net_by_ts[ts] -= kw

    return [
        {"start": ts.isoformat(), "mean": kw}
        for ts, kw in sorted(net_by_ts.items())
    ]


def build_forecast(
    *,
    sum_readings: dict[str, list[dict[str, Any]]],
    subtract_readings: dict[str, list[dict[str, Any]]],
    now: datetime,
    horizon_hours: int,
    lookback_days: int,
    lookback_decay: float = 1.0,
) -> list[dict[str, Any]]:
    """Build a timestamped base load forecast covering horizon_hours steps.

    Entities are first merged into one net kWh/h series per timestamp (see
    ``_merge_net_readings``), that series is averaged per hour-of-day with the
    recency weights, and each hourly mean is clamped to zero:

        net_kw[h] = max(0, weighted_mean_over_days(net(t) for t at hour h))

    The 24-hour day profile is tiled to fill horizon_hours steps. If
    horizon_hours = 48, each hour-of-day appears twice.

    Readings must already be in kWh/h (as returned by ``fetch_statistics``).
    No unit conversion is performed here.

    Args:
        sum_readings: Per-entity hourly kWh/h readings for entities to sum.
            Keys are entity IDs; values are lists of reading dicts.
        subtract_readings: Per-entity hourly kWh/h readings for entities to subtract.
        now: The current time (UTC). The first forecast step starts at this hour.
        horizon_hours: Number of steps to produce.
        lookback_days: Number of historical days covered by the readings. Used
            to compute per-day recency weights.
        lookback_decay: Total weight ratio of the newest day to the oldest day
            in the lookback window. ``1.0`` (default) assigns equal weight to
            all days. Values greater than 1.0 apply exponential recency
            weighting so that more recent days contribute more to each hour's
            average. For example, ``2.0`` means the most recent day counts
            twice as much as the oldest day, with intermediate days
            interpolated exponentially between those two extremes.

    Returns:
        List of step dicts with ``"ts"`` (UTC ISO 8601) and ``"kw"`` (non-negative float).
    """
    # Compute per-day weights for the lookback window. When lookback_decay is
    # 1.0 all weights are equal and the result is the plain same-hour average.
    day_weights = _compute_day_weights(
        now=now, lookback_days=lookback_days, decay=lookback_decay
    )

    # Merge every entity into one net series first, then build a single
    # hour-of-day profile from it. Averaging per entity and summing afterwards
    # would double count a sensor that was renamed or replaced mid-window.
    # Readings are already in kWh/h so no unit argument is needed.
    net_profile = HourlyProfile.from_readings(
        _merge_net_readings(sum_readings, subtract_readings), day_weights
    )

    # Round down to the current hour so the first step is aligned.
    start = now.replace(minute=0, second=0, microsecond=0)

    steps: list[dict[str, Any]] = []
    for offset in range(horizon_hours):
        ts = start + timedelta(hours=offset)
        hour = ts.hour

        # Clamp after averaging: an hour where subtract entities exceeded the
        # sum entities lowers the mean but the published load is never negative.
        net_kw = max(0.0, net_profile.kw_for_hour(hour))

        steps.append(
            {
                "ts": ts.isoformat(),
                "kw": round(net_kw, 4),
            }
        )

    return steps
