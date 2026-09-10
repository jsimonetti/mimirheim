"""Forecast resampling and horizon computation for mimirheim.

This module converts timestamped forecast data — arriving from MQTT at arbitrary
resolution as lists of ``PriceStep`` or ``PowerForecastStep`` objects — into the
flat per-step arrays that the MILP solver consumes.

The solver always works with a fixed 15-minute time step (``_STEP_MINUTES = 15``).
The horizon is not fixed at 24 hours; it is computed dynamically as the shortest
coverage window across all required forecast series. See ``compute_horizon_steps``
for the precise definition.

Resampling strategies:
    - **Prices** (``merge_price_sources``): step function per source. Within
      a source, a price quoted for a given timestamp applies until that
      source's next known timestamp — this matches how day-ahead market
      prices work. Across sources (mimirheim accepts more than one price
      topic), each step of the merged result takes the source with the
      highest reported confidence, restricted to sources with real
      timestamp coverage of that step; see ``merge_price_sources`` for the
      full tier rule.
    - **Power forecasts** (``resample_power``): step function (hold-previous). Each
      forecast.solar hourly value represents the average power for the interval
      starting at that timestamp (e.g. ``watts[09:00]`` = average power during
      09:00–10:00). All 15-minute slots within the interval therefore receive the
      same value. Linear interpolation across hour boundaries would incorrectly
      blend adjacent hourly averages and produce artefacts — most visibly a
      gradual ramp from zero at sunrise rather than an abrupt step.

This module has no I/O, no logging, and no global state. All functions are pure.
It does not import from ``mimirheim.io`` or ``mimirheim.config``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from mimirheim.core.bundle import PowerForecastStep, PriceStep

_STEP_MINUTES: int = 15
_STEP_DURATION: timedelta = timedelta(minutes=_STEP_MINUTES)


def floor_to_15min(dt: datetime) -> datetime:
    """Round a datetime down to the nearest 15-minute boundary.

    Args:
        dt: Any UTC datetime.

    Returns:
        The largest 15-minute-aligned datetime that is <= dt.
    """
    discard = timedelta(
        minutes=dt.minute % _STEP_MINUTES,
        seconds=dt.second,
        microseconds=dt.microsecond,
    )
    return dt - discard


def _last_ts_at_or_after(series: list[Any], threshold: datetime) -> datetime | None:
    """Return the latest ``.ts`` in series that is at or after threshold.

    Each element of series must have a ``.ts`` attribute of type ``datetime``.

    Args:
        series: List of step objects with a ``.ts`` attribute.
        threshold: Lower bound for the timestamp.

    Returns:
        The maximum qualifying timestamp, or ``None`` if no element qualifies.
    """
    candidates = [s.ts for s in series if s.ts >= threshold]
    return max(candidates) if candidates else None


def compute_horizon_steps(solve_start: datetime, *series: list[Any]) -> int:
    """Compute the number of available 15-minute steps from solve_start.

    The horizon is bounded by the shortest forecast coverage across all
    provided series. Specifically:

    1. For each series, find the latest timestamp at or after solve_start.
       Call this ``last_ts``. If any series has no such timestamp, return 0.
    2. ``horizon_end = min(last_ts for all series)``.
    3. ``n_steps = floor((horizon_end - solve_start) / 15 minutes)``.

    No extrapolation is performed beyond the last known data point of any
    series: resampling is only well defined within
    ``[solve_start, horizon_end]``.

    Args:
        solve_start: The 15-minute-aligned start of the solve horizon.
        *series: One or more lists of step objects, each with a ``.ts``
            attribute. Mixes of ``PriceStep`` and ``PowerForecastStep`` are
            accepted. Empty lists immediately short-circuit to 0.

    Returns:
        The number of available 15-minute steps. Zero means not enough
        data is available to run even a single step.
    """
    ends: list[datetime] = []
    for s in series:
        last = _last_ts_at_or_after(s, solve_start)
        if last is None:
            return 0
        ends.append(last)

    if not ends:
        return 0

    horizon_end = min(ends)
    steps = int((horizon_end - solve_start).total_seconds() / (60 * _STEP_MINUTES))
    return max(0, steps)


def find_gaps(
    series: list[Any],
    solve_start: datetime,
    horizon_end: datetime,
    max_gap_hours: float,
) -> list[tuple[datetime, datetime]]:
    """Find consecutive timestamp pairs within the horizon whose gap exceeds max_gap_hours.

    Only timestamps inside ``[solve_start, horizon_end]`` are considered, with
    both bounds inclusive. The gap before the first data point at or after
    solve_start is ignored — it is a horizon boundary, not an internal data
    gap.

    A gap of exactly ``max_gap_hours`` is not flagged; only strictly greater
    gaps are returned. This prevents false alarms when data arrives at
    regular hourly intervals and max_gap_hours is set to 2.0.

    Args:
        series: List of step objects with a ``.ts`` attribute.
        solve_start: Start of the horizon window.
        horizon_end: End of the horizon window.
        max_gap_hours: Maximum allowed gap in hours. Gaps strictly larger
            than this value are returned.

    Returns:
        A list of ``(gap_start, gap_end)`` pairs for each flagged gap. Empty
        list means no gaps exceed the threshold.
    """
    max_gap = timedelta(hours=max_gap_hours)
    ts_in_range = sorted(
        s.ts for s in series if solve_start <= s.ts <= horizon_end
    )

    gaps: list[tuple[datetime, datetime]] = []
    for i in range(1, len(ts_in_range)):
        gap = ts_in_range[i] - ts_in_range[i - 1]
        if gap > max_gap:
            gaps.append((ts_in_range[i - 1], ts_in_range[i]))
    return gaps


def compute_price_horizon_steps(
    solve_start: datetime,
    sources: list[list[PriceStep]],
) -> int:
    """Compute the number of available 15-minute price steps, unioned across sources.

    Unlike ``compute_horizon_steps`` (which takes the *minimum* coverage
    because every series it is given is mandatory), price sources are
    optional alternatives: the whole point of accepting more than one is that
    a longer-horizon source should extend the usable horizon rather than be
    capped by a shorter, higher-confidence one. This function therefore takes
    the *maximum* of the per-source coverage instead of the minimum.

    Args:
        solve_start: The 15-minute-aligned start of the horizon.
        sources: One list of ``PriceStep`` per configured price topic. A
            source with no data at all is skipped rather than short-circuiting
            the whole computation to 0.

    Returns:
        The number of available 15-minute steps. Zero if no source has any
        data at or after solve_start.
    """
    ends: list[datetime] = []
    for source in sources:
        last = _last_ts_at_or_after(source, solve_start)
        if last is not None:
            ends.append(last)

    if not ends:
        return 0

    horizon_end = max(ends)
    steps = int((horizon_end - solve_start).total_seconds() / (60 * _STEP_MINUTES))
    return max(0, steps)


def merge_price_sources(
    sources: list[list[PriceStep]],
    solve_start: datetime,
    n_steps: int,
) -> tuple[list[float], list[float], list[float]]:
    """Merge multiple price sources onto the 15-minute grid, highest confidence wins.

    Each source is resampled independently using the same step (hold-previous)
    function as a single price series: the price quoted for a given timestamp
    applies until that source's next known timestamp. The sources are then
    merged per output step in two tiers:

    1. **Real coverage.** A source is a candidate for step ``t`` only if it has
       an active (hold-previous) value AND ``t`` falls within that source's own
       real timestamp range, i.e. ``t <= last_ts(source)``. This is what stops
       a short-horizon source from being held forward at its own confidence
       past the end of its actual data — without it, a short-coverage
       confidence-1.0 source (e.g. day-ahead prices covering only 24 hours)
       would outrank a longer-horizon predictive source at every step beyond
       its own last timestamp, permanently defeating the point of merging in
       a longer-horizon source at all.
    2. **Leading-edge fallback.** Used only for a step where every source's
       tier-1 candidacy is empty (``t`` is before every source's first
       timestamp). Each source with any data at all then contributes its own
       first step, exactly like the single-source backward-fill behaviour.

    Within whichever tier applies, the candidate with the highest confidence
    wins; ties are broken by source index (earlier entries in ``sources``,
    i.e. higher configured priority, win).

    Args:
        sources: One list of ``PriceStep`` per configured price topic, in
            priority order (index 0 = highest priority on confidence ties).
            Each list need not be sorted.
        solve_start: The 15-minute-aligned start of the horizon.
        n_steps: Number of 15-minute steps to produce. Must be >= 1.

    Returns:
        A 3-tuple of ``(import_eur_per_kwh, export_eur_per_kwh, confidence)``,
        each a ``list[float]`` of length ``n_steps``, taken from the winning
        source's own step at each position.
    """
    sorted_sources = [sorted(source, key=lambda s: s.ts) for source in sources]
    last_ts_per_source = [s[-1].ts if s else None for s in sorted_sources]

    imports: list[float] = []
    exports: list[float] = []
    confidences: list[float] = []

    for i in range(n_steps):
        t = solve_start + i * _STEP_DURATION

        tier1: list[tuple[int, PriceStep]] = []
        for idx, sorted_source in enumerate(sorted_sources):
            active = None
            for step in sorted_source:
                if step.ts <= t:
                    active = step
                else:
                    break
            if active is not None and t <= last_ts_per_source[idx]:  # type: ignore[operator]
                tier1.append((idx, active))

        if tier1:
            candidates = tier1
        else:
            # Leading-edge fallback: every source's real coverage starts
            # after t, so extend the earliest known price of each source
            # backwards, exactly as the single-source case does.
            candidates = [
                (idx, sorted_source[0])
                for idx, sorted_source in enumerate(sorted_sources)
                if sorted_source
            ]

        # Highest confidence wins; ties broken by lowest source index (config
        # priority order). See IMPLEMENTATION_DETAILS.md §7, subsection
        # "Multi-source price merge".
        _, winner = min(candidates, key=lambda pair: (-pair[1].confidence, pair[0]))

        imports.append(winner.import_eur_per_kwh)
        exports.append(winner.export_eur_per_kwh)
        confidences.append(winner.confidence)

    return imports, exports, confidences


def resample_power(
    steps: list[PowerForecastStep],
    solve_start: datetime,
    n_steps: int,
) -> list[float]:
    """Resample power forecast steps to the 15-minute grid using a step function.

    The forecast.solar API returns ``watts[T]`` as the *average* power during
    the interval ``[T, T + duration)``.  Every 15-minute output slot whose
    start time ``t_i`` falls within the same source interval therefore receives
    the same value as the interval's declared average.  Blending adjacent
    intervals across their boundary (linear interpolation) would be physically
    incorrect: it would produce an artificial ramp between hours rather than
    the abrupt step that the data semantics require.

    If ``t_i`` is at or beyond the last known point, the final value is held
    constant (nearest-neighbour extrapolation).  In practice, ``n_steps``
    should never require extrapolation beyond the last point because
    ``compute_horizon_steps`` ensures all requested steps fall within
    ``[solve_start, last_known_ts]``.

    Args:
        steps: Raw power forecast steps, not necessarily sorted.
        solve_start: The 15-minute-aligned start of the horizon.
        n_steps: Number of 15-minute steps to produce. Must be >= 1.

    Returns:
        A ``list[float]`` of length ``n_steps`` with kW values.
    """
    sorted_steps = sorted(steps, key=lambda s: s.ts)

    result: list[float] = []

    for i in range(n_steps):
        t = solve_start + i * _STEP_DURATION

        # Find the pair of steps bracketing t.
        before: PowerForecastStep | None = None
        after: PowerForecastStep | None = None
        for step in sorted_steps:
            if step.ts <= t:
                before = step
            elif after is None:
                after = step
                break

        if before is None and after is None:
            # No data at all — should not happen if compute_horizon_steps
            # was consulted first, but guard defensively.
            result.append(0.0)
        elif before is not None and after is None:
            # At or beyond the last known point: use the last value.
            result.append(before.kw)
        elif before is None:
            # Before any known point: use the first value.
            result.append(after.kw)  # type: ignore[union-attr]
        else:
            # Step function: hold the value of the interval that contains t.
            # Whether t coincides exactly with before.ts or falls somewhere
            # between before.ts and after.ts, the correct value is before.kw —
            # the declared average for the source interval beginning at before.ts.
            result.append(before.kw)

    return result
