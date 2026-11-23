"""Weather-dependent base load model: training data, fit and forecast.

The house load in each hour is modelled as

    kWh = a + b * max(0, T_base - T_s)

per *cell*, where a cell is a (local hour of day, weekday/weekend) pair:

- ``T_s`` is the outdoor temperature smoothed by an exponential moving
  average. The smoothing stands in for the thermal mass of the building and
  its heat emitters: a house reacts to the last half day of weather, not to
  the current hour.
- ``x = max(0, T_base - T_s)`` is the heating-degree regressor. It is zero
  above the balance temperature ``T_base``, where no heating is needed.
- ``a`` is the weather-independent load of the cell and ``b >= 0`` its
  heating slope in kWh per degree below ``T_base``.

Each cell is fitted at the most specific level its data supports (own cell,
hour pooled over day types, shared slope with per-cell intercepts, plain
weighted mean). ``T_base`` is chosen once from a 0.5-degree grid.

Everything here is a pure function of in-memory inputs keyed by the UTC
epoch second of an hour's start. This module performs no I/O and does not
know about MQTT, the recorder or Open-Meteo.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from baseload_weather.config import ModelConfig

_H = 3600
_DAY = 86400
MAX_INTERPOLATED_GAP_HOURS = 6
"""Gaps of at most this many missing hours are linearly interpolated."""
T_BASE_STEP_C = 0.5

Cell = tuple[int, bool]
"""(local hour 0-23, is_weekend)."""


class ModelError(Exception):
    """Raised when no forecast can be produced from the inputs."""


@dataclass(frozen=True)
class CellFit:
    """Fitted coefficients of one cell.

    Attributes:
        a: Weather-independent load in kWh per hour (equal to the average kW).
        b: Heating slope in kWh per hour per degree below ``T_base``; >= 0.
        level: Fit level that produced the coefficients (1-4).
    """

    a: float
    b: float
    level: int


@dataclass(frozen=True)
class ForecastResult:
    """Model output.

    Attributes:
        points: Forecast steps ``{"ts": ISO 8601 UTC, "kw": float >= 0}`` for
            every hour from the start through start + horizon inclusive.
        cells: Fitted coefficients per (local hour, is_weekend) cell.
        stats: Fit statistics for the model statistics topic.
    """

    points: list[dict[str, Any]]
    cells: dict[Cell, CellFit]
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Row:
    """One usable training hour."""

    cell: Cell
    t_s: float
    y: float
    w: float


def warm_up_hours(smoothing_hours: float) -> int:
    """Hours after an EMA (re)seed before its value is trusted for training."""
    return math.ceil(3.0 * smoothing_hours)


def history_window(start: datetime, config: ModelConfig) -> tuple[int, int]:
    """Return ``(grid_start, window_start)`` as UTC epoch seconds.

    ``window_start`` is the first training hour, ``lookback_days`` before
    ``start``. ``grid_start`` lies one EMA warm-up period earlier, so the
    smoothed temperature is settled when training begins. Recorder history
    must be read from ``grid_start`` to ``start``.

    Args:
        start: Forecast start (UTC, hour aligned).
        config: Model settings.
    """
    window_start = int(start.timestamp()) - config.lookback_days * _DAY
    return window_start - warm_up_hours(config.smoothing_hours) * _H, window_start


def training_targets(
    energy: dict[str, dict[int, float]], sum_ids: list[str], subtract_ids: list[str]
) -> dict[int, float]:
    """Return the net load per hour where every constituent has a value.

    An hour is kept only when every sum and subtract entity has a valid
    delta for it. A missing reading is never treated as zero: zero for a
    missing PV or export counter would turn into artificial house load.

    Args:
        energy: Hourly kWh per entity (as returned by the recorder).
        sum_ids: Entities added to the load.
        subtract_ids: Entities subtracted from the load.

    Returns:
        Net kWh per hour (sum minus subtract).
    """
    ids = [*sum_ids, *subtract_ids]
    if not ids or any(i not in energy for i in ids):
        return {}
    hours = set.intersection(*(set(energy[i]) for i in ids))
    return {
        h: sum(energy[i][h] for i in sum_ids) - sum(energy[i][h] for i in subtract_ids)
        for h in hours
    }


def temperature_series(
    recorder: dict[int, float], forecast: dict[int, float], grid_start: int, grid_end: int
) -> list[float | None]:
    """Build the hourly temperature series on ``[grid_start, grid_end]``.

    Recorder values win where both sources have an hour. Interior gaps of at
    most ``MAX_INTERPOLATED_GAP_HOURS`` hours are linearly interpolated;
    longer gaps and leading or trailing gaps stay ``None``.
    """
    n = (grid_end - grid_start) // _H + 1
    series: list[float | None] = []
    for i in range(n):
        ts = grid_start + i * _H
        series.append(recorder.get(ts, forecast.get(ts)))

    known = [i for i, v in enumerate(series) if v is not None]
    for left, right in zip(known, known[1:], strict=False):
        gap = right - left - 1
        if 0 < gap <= MAX_INTERPOLATED_GAP_HOURS:
            lv, rv = series[left], series[right]
            if lv is None or rv is None:  # unreachable: both indices are known values
                continue
            for k in range(1, gap + 1):
                series[left + k] = lv + (rv - lv) * k / (gap + 1)
    return series


def smooth_temperatures(
    series: list[float | None], smoothing_hours: float
) -> tuple[list[float | None], list[bool]]:
    """Apply the exponential moving average and mark settled hours.

    ``alpha = 1 - exp(-1 / smoothing_hours)``. The first value seeds the
    average. A ``None`` (an uninterpolated gap) breaks the average; the next
    value reseeds it. After every (re)seed, the following
    ``ceil(3 * smoothing_hours)`` hours are marked not warm, because the
    average has not yet forgotten its seed.

    Returns:
        The smoothed series (``None`` where there is no state) and a parallel
        list of flags telling whether each hour may be used for training.
    """
    alpha = 1.0 - math.exp(-1.0 / smoothing_hours)
    warm_up = warm_up_hours(smoothing_hours)
    smoothed: list[float | None] = []
    warm: list[bool] = []
    state: float | None = None
    since_seed = 0
    for value in series:
        if value is None:
            state = None
            smoothed.append(None)
            warm.append(False)
            continue
        if state is None:
            state = value
            since_seed = 0
        else:
            state = state + alpha * (value - state)
            since_seed += 1
        smoothed.append(state)
        warm.append(since_seed >= warm_up)
    return smoothed, warm


def build_forecast(
    *,
    energy: dict[str, dict[int, float]],
    temperature_recorder: dict[int, float],
    temperature_forecast: dict[int, float],
    sum_ids: list[str],
    subtract_ids: list[str],
    config: ModelConfig,
    start: datetime,
    horizon_hours: int,
) -> ForecastResult:
    """Fit the model on history and forecast ``horizon_hours`` ahead.

    Args:
        energy: Hourly kWh per entity for ``[window_start, start)``.
        temperature_recorder: Hourly recorder temperatures (degrees Celsius)
            from ``grid_start`` (see ``history_window``).
        temperature_forecast: Hourly forecast temperatures covering at least
            ``[start, start + horizon_hours]``.
        sum_ids: Entities added to the load.
        subtract_ids: Entities subtracted from the load.
        config: Model settings.
        start: Forecast start (UTC, timezone-aware, hour aligned).
        horizon_hours: Hours after ``start`` to forecast; ``horizon_hours + 1``
            points are produced so the last one marks the horizon end.

    Returns:
        The forecast, the per-cell coefficients and fit statistics.

    Raises:
        ModelError: If there is no usable training hour, or the temperature
            series has no smoothed value for a forecast hour.
    """
    tz = ZoneInfo(config.timezone)
    start_ts = int(start.timestamp())
    end_ts = start_ts + horizon_hours * _H
    grid_start, window_start = history_window(start, config)

    series = temperature_series(temperature_recorder, temperature_forecast, grid_start, end_ts)
    t_s, warm = smooth_temperatures(series, config.smoothing_hours)

    def cell_of(ts: int) -> Cell:
        local = datetime.fromtimestamp(ts, tz)
        return local.hour, local.weekday() >= 5

    excluded = set(config.exclude_dates)
    targets = training_targets(energy, sum_ids, subtract_ids)
    rows: list[_Row] = []
    for ts in range(window_start, start_ts, _H):
        idx = (ts - grid_start) // _H
        value = targets.get(ts)
        smoothed = t_s[idx]
        if value is None or smoothed is None or not warm[idx]:
            continue
        if datetime.fromtimestamp(ts, tz).date() in excluded:
            continue
        day = (ts - window_start) // _DAY
        weight = (
            1.0
            if config.lookback_days <= 1 or config.lookback_decay == 1.0
            else config.lookback_decay ** (day / (config.lookback_days - 1))
        )
        rows.append(_Row(cell_of(ts), smoothed, value, weight))
    if not rows:
        raise ModelError("No usable training hours: every hour lacks a complete reading.")

    t_base = _select_t_base(rows, config)
    cells = _fit_cells(rows, t_base, config)

    points: list[dict[str, Any]] = []
    for ts in range(start_ts, end_ts + _H, _H):
        smoothed = t_s[(ts - grid_start) // _H]
        if smoothed is None:
            raise ModelError(
                f"No temperature for forecast hour {datetime.fromtimestamp(ts, UTC).isoformat()}."
            )
        fit = cells[cell_of(ts)]
        x = 0.0 if t_base is None else max(0.0, t_base - smoothed)
        points.append(
            {
                "ts": datetime.fromtimestamp(ts, UTC).isoformat(),
                "kw": round(max(0.0, fit.a + fit.b * x), 4),
            }
        )

    total_hours = (start_ts - window_start) // _H
    stats = _stats(rows, cells, t_base, total_hours, len(points))
    return ForecastResult(points=points, cells=cells, stats=stats)


def _x(row: _Row, t_base: float) -> float:
    """Heating-degree regressor of a row."""
    return max(0.0, t_base - row.t_s)


def _n_eff(weights: list[float]) -> float:
    """Kish effective sample size of a weighted sample."""
    s = sum(weights)
    s2 = sum(w * w for w in weights)
    return s * s / s2 if s2 > 0 else 0.0


def _wls(rows: list[_Row], t_base: float) -> tuple[float, float, float]:
    """Weighted least squares of ``y = a + b x``; returns ``(a, b, var_x)``.

    A fitted ``b < 0`` (less load when colder, unphysical for heating) is
    replaced by ``b = 0`` with ``a`` the weighted mean.
    """
    sw = sum(r.w for r in rows)
    mx = sum(r.w * _x(r, t_base) for r in rows) / sw
    my = sum(r.w * r.y for r in rows) / sw
    sxx = sum(r.w * (_x(r, t_base) - mx) ** 2 for r in rows)
    var_x = sxx / sw
    if sxx <= 0.0:
        return my, 0.0, var_x
    b = sum(r.w * (_x(r, t_base) - mx) * (r.y - my) for r in rows) / sxx
    if b < 0.0:
        return my, 0.0, var_x
    return my - b * mx, b, var_x


def _within_cell(rows: list[_Row], t_base: float) -> tuple[float, float, float]:
    """Shared-slope (fixed-effects) estimator.

    Centres ``x`` and ``y`` on each cell's weighted mean, so differences
    between cells cannot appear as slope. Returns ``(b, within_var_x, sse)``
    where ``within_var_x`` is the weighted variance of the centred ``x``.
    """
    groups: dict[Cell, list[_Row]] = defaultdict(list)
    for r in rows:
        groups[r.cell].append(r)
    sxx = sxy = sw = 0.0
    centred: list[tuple[float, float, float]] = []
    for members in groups.values():
        gw = sum(m.w for m in members)
        mx = sum(m.w * _x(m, t_base) for m in members) / gw
        my = sum(m.w * m.y for m in members) / gw
        for m in members:
            dx, dy = _x(m, t_base) - mx, m.y - my
            sxx += m.w * dx * dx
            sxy += m.w * dx * dy
            sw += m.w
            centred.append((m.w, dx, dy))
    within_var = sxx / sw if sw > 0 else 0.0
    b = max(0.0, sxy / sxx) if sxx > 0 else 0.0
    sse = sum(w * (dy - b * dx) ** 2 for w, dx, dy in centred)
    return b, within_var, sse


def _level3_eligible(rows: list[_Row], t_base: float, config: ModelConfig) -> bool:
    _, within_var, _ = _within_cell(rows, t_base)
    return _n_eff([r.w for r in rows]) >= config.min_samples and within_var >= config.min_hdd_variance


def _select_t_base(rows: list[_Row], config: ModelConfig) -> float | None:
    """Pick the balance temperature with the lowest within-cell weighted SSE.

    Candidates run from ``t_base_min_c`` in 0.5-degree steps up to
    ``t_base_max_c``. Candidates whose within-cell regressor variance is
    below ``min_hdd_variance`` are skipped. Ties go to the lowest candidate.
    Returns ``None`` when no candidate is eligible.
    """
    steps = int((config.t_base_max_c - config.t_base_min_c) / T_BASE_STEP_C + 1e-9)
    best: tuple[float, float] | None = None
    for i in range(steps + 1):
        candidate = config.t_base_min_c + i * T_BASE_STEP_C
        if not _level3_eligible(rows, candidate, config):
            continue
        _, _, sse = _within_cell(rows, candidate)
        # Keep the earlier (lower) candidate on numerical ties. The tolerance
        # is relative because SSE scales with the data: candidates below the
        # coldest training hour give mathematically identical fits whose SSE
        # differs only by rounding.
        if best is None or (
            sse < best[1] and not math.isclose(sse, best[1], rel_tol=1e-9, abs_tol=1e-12)
        ):
            best = (candidate, sse)
    return None if best is None else best[0]


def _fit_cells(rows: list[_Row], t_base: float | None, config: ModelConfig) -> dict[Cell, CellFit]:
    """Fit every cell at the most specific eligible level."""
    by_cell: dict[Cell, list[_Row]] = defaultdict(list)
    by_hour: dict[int, list[_Row]] = defaultdict(list)
    for r in rows:
        by_cell[r.cell].append(r)
        by_hour[r.cell[0]].append(r)

    def eligible(members: list[_Row]) -> bool:
        if t_base is None or _n_eff([m.w for m in members]) < config.min_samples:
            return False
        _, _, var_x = _wls(members, t_base)
        return var_x >= config.min_hdd_variance

    def wmean(members: list[_Row], attr: str) -> float:
        sw = sum(m.w for m in members)
        return sum(m.w * getattr(m, attr) for m in members) / sw

    level3_b: float | None = None
    if t_base is not None and _level3_eligible(rows, t_base, config):
        level3_b, _, _ = _within_cell(rows, t_base)

    def intercept_level3(cell: Cell, b: float, balance_c: float) -> float:
        """``a = mean_y - b * mean_x`` over the cell, else its hour, else all rows."""
        group = by_cell.get(cell) or by_hour.get(cell[0]) or rows
        mean_x = sum(m.w * _x(m, balance_c) for m in group) / sum(m.w for m in group)
        return wmean(group, "y") - b * mean_x

    fits: dict[Cell, CellFit] = {}
    for hour in range(24):
        for weekend in (False, True):
            cell = (hour, weekend)
            own = by_cell.get(cell, [])
            pooled = by_hour.get(hour, [])
            if t_base is not None and own and eligible(own):
                a, b, _ = _wls(own, t_base)
                fits[cell] = CellFit(a, b, 1)
            elif t_base is not None and pooled and eligible(pooled):
                a, b, _ = _wls(pooled, t_base)
                fits[cell] = CellFit(a, b, 2)
            elif t_base is not None and level3_b is not None:
                fits[cell] = CellFit(intercept_level3(cell, level3_b, t_base), level3_b, 3)
            else:
                group = own or pooled or rows
                fits[cell] = CellFit(wmean(group, "y"), 0.0, 4)
    return fits


def _stats(
    rows: list[_Row],
    cells: dict[Cell, CellFit],
    t_base: float | None,
    total_hours: int,
    forecast_points: int,
) -> dict[str, Any]:
    """In-sample fit statistics for the model statistics topic."""
    sw = sum(r.w for r in rows)
    # Offsets from a reference target keep a constant target exactly
    # constant, so its total sum of squares is exactly zero (R2 undefined).
    ref = rows[0].y
    mean_offset = sum(r.w * (r.y - ref) for r in rows) / sw
    residuals = []
    for r in rows:
        fit = cells[r.cell]
        x = 0.0 if t_base is None else _x(r, t_base)
        residuals.append(r.y - (fit.a + fit.b * x))
    ss_res = sum(r.w * e * e for r, e in zip(rows, residuals, strict=True))
    constant = all(r.y == ref for r in rows)
    ss_tot = 0.0 if constant else sum(r.w * ((r.y - ref) - mean_offset) ** 2 for r in rows)
    levels = {str(level): 0 for level in (1, 2, 3, 4)}
    for fit in cells.values():
        levels[str(fit.level)] += 1
    return {
        "t_base_c": t_base,
        "levels": levels,
        "r2": (1.0 - ss_res / ss_tot) if ss_tot > 0 else None,
        "mae_kwh": sum(abs(e) for e in residuals) / len(residuals),
        "training_hours": len(rows),
        "training_coverage_pct": 100.0 * len(rows) / total_hours if total_hours else 0.0,
        "forecast_points": forecast_points,
    }

