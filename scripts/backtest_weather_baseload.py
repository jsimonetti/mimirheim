"""backtest_weather_baseload.py: compare the weather-dependent base load forecast with the same-hour average.

Runs a rolling-origin backtest on a Home Assistant recorder database. For each
forecast origin in the test period it fits both forecasts on history before
the origin only, forecasts the next ``--horizon`` hours, and compares them with
the load that actually occurred:

- ``weather``: the ``baseload_weather`` model with the helper's configuration;
- ``same-hour``: the homeassistant_db helper's same-hour-of-day average, with
  ``--baseline-lookback-days`` and ``--baseline-decay``.

Both use the entities of the weather helper's configuration. The temperature
"forecast" is the temperature that was actually recorded, because the recorder
holds no archive of past forecasts. The weather result is therefore an upper
bound on what the live helper achieves; forecast error adds to it.

This is a local analysis tool. It reads the database (SQLite is opened
read-only) and the configuration file, and prints a report; it publishes
nothing and needs no MQTT broker.

Usage::

    uv run python scripts/backtest_weather_baseload.py \\
        --config baseload-weather.yaml --test-days 28 --origin-hour 22
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from baseload_ha_db.forecast import build_forecast as same_hour_forecast
from baseload_weather.config import WeatherBaseloadConfig
from baseload_weather.model import ModelError, build_forecast, history_window, training_targets
from baseload_weather.recorder import RecorderHistory, fetch_history

_H = 3600


@dataclass(frozen=True)
class OriginResult:
    """Outcome of one forecast origin.

    Attributes:
        origin: Forecast start (UTC).
        hours: Hours in the horizon with a complete actual reading.
        actual_kwh: Actual load summed over those hours.
        weather_kwh: Weather forecast summed over those hours.
        same_hour_kwh: Same-hour forecast summed over those hours.
        weather_hourly_mae: Mean absolute hourly error of the weather forecast.
        same_hour_hourly_mae: Mean absolute hourly error of the same-hour forecast.
        temp_change_c: Mean temperature of the horizon minus that of the
            preceding 24 hours, each over the hours with a recorded value;
            large values mark weather changes. ``None`` when either window
            has no recorded temperature.
    """

    origin: datetime
    hours: int
    actual_kwh: float
    weather_kwh: float
    same_hour_kwh: float
    weather_hourly_mae: float
    same_hour_hourly_mae: float
    temp_change_c: float | None


def _origins(end: datetime, test_days: int, origin_hour: int, horizon_hours: int) -> list[datetime]:
    """Daily origins at ``origin_hour`` UTC.

    The last origin is chosen so its horizon endpoint (origin plus
    ``horizon_hours``) lies strictly before ``end``, the exclusive cutoff of
    the database read, because the model needs a temperature for that
    endpoint.
    """
    last = end.replace(hour=origin_hour, minute=0, second=0, microsecond=0)
    while last + timedelta(hours=horizon_hours) >= end:
        last -= timedelta(days=1)
    return [last - timedelta(days=i) for i in range(test_days - 1, -1, -1)]


def _baseline_since(origin: datetime, lookback_days: int) -> int:
    """Start of the same-hour baseline's history, as the live helper computes it.

    The homeassistant_db helper starts its window at midnight UTC
    ``lookback_days`` before the current time.
    """
    start = (origin - timedelta(days=lookback_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _as_readings(series: dict[int, float], before: int, since: int) -> list[dict[str, Any]]:
    """Convert hourly kWh to the homeassistant_db helper's reading format."""
    return [
        {"start": datetime.fromtimestamp(ts, UTC).isoformat(), "mean": v}
        for ts, v in sorted(series.items())
        if since <= ts < before
    ]


def evaluate_origin(
    weather_history: RecorderHistory,
    baseline_history: RecorderHistory,
    scoring: RecorderHistory,
    config: WeatherBaseloadConfig,
    origin: datetime,
    horizon_hours: int,
    baseline_lookback_days: int,
    baseline_decay: float,
) -> OriginResult | None:
    """Forecast from ``origin`` with both methods and score them.

    Each method gets its own history, read up to the origin over its own
    window, because the recorder's outlier filter works over the whole read:
    sharing one read would let one method's window decide which hours the
    other keeps.

    Args:
        weather_history: Recorder history for the weather model, from its
            smoothing warm-up start to the origin.
        baseline_history: Recorder history for the same-hour baseline, from
            midnight UTC ``baseline_lookback_days`` before the origin to the
            origin.
        scoring: Actual load and recorded temperatures from 24 hours before
            the origin through the horizon end.
        config: The weather helper's configuration.
        origin: Forecast start (UTC, hour aligned).
        horizon_hours: Hours to forecast and score.
        baseline_lookback_days: Lookback of the same-hour baseline.
        baseline_decay: Recency decay of the same-hour baseline.

    Returns:
        The scores, or ``None`` when the weather model cannot fit or no hour
        of the horizon has a complete actual reading.
    """
    origin_ts = int(origin.timestamp())
    end_ts = origin_ts + horizon_hours * _H
    sum_ids = [e.entity_id for e in config.homeassistant.sum_entities]
    sub_ids = [e.entity_id for e in config.homeassistant.subtract_entities]

    past_energy = {
        eid: {ts: v for ts, v in hours.items() if ts < origin_ts}
        for eid, hours in weather_history.energy.items()
    }
    past_temps = {ts: v for ts, v in weather_history.temperature_c.items() if ts < origin_ts}
    future_temps = {
        ts: v for ts, v in scoring.temperature_c.items() if origin_ts - 24 * _H <= ts <= end_ts
    }
    try:
        weather = build_forecast(
            energy=past_energy,
            temperature_recorder=past_temps,
            temperature_forecast=future_temps,
            sum_ids=sum_ids,
            subtract_ids=sub_ids,
            config=config.model,
            start=origin,
            horizon_hours=horizon_hours,
        )
    except ModelError:
        return None
    weather_kw = {int(datetime.fromisoformat(p["ts"]).timestamp()): p["kw"] for p in weather.points}

    since = _baseline_since(origin, baseline_lookback_days)
    baseline = same_hour_forecast(
        sum_readings={e: _as_readings(baseline_history.energy.get(e, {}), origin_ts, since) for e in sum_ids},
        subtract_readings={
            e: _as_readings(baseline_history.energy.get(e, {}), origin_ts, since) for e in sub_ids
        },
        now=origin,
        horizon_hours=horizon_hours,
        lookback_days=baseline_lookback_days,
        lookback_decay=baseline_decay,
    )
    baseline_kw = {int(datetime.fromisoformat(s["ts"]).timestamp()): s["kw"] for s in baseline}

    actual = training_targets(scoring.energy, sum_ids, sub_ids)
    hours = [ts for ts in range(origin_ts, end_ts, _H) if ts in actual]
    if not hours:
        return None
    before = [v for ts, v in scoring.temperature_c.items() if origin_ts - 24 * _H <= ts < origin_ts]
    during = [v for ts, v in scoring.temperature_c.items() if origin_ts <= ts < end_ts]
    change = statistics.fmean(during) - statistics.fmean(before) if before and during else None
    return OriginResult(
        origin=origin,
        hours=len(hours),
        actual_kwh=sum(actual[h] for h in hours),
        weather_kwh=sum(weather_kw[h] for h in hours),
        same_hour_kwh=sum(baseline_kw[h] for h in hours),
        weather_hourly_mae=statistics.fmean(abs(weather_kw[h] - actual[h]) for h in hours),
        same_hour_hourly_mae=statistics.fmean(abs(baseline_kw[h] - actual[h]) for h in hours),
        temp_change_c=change,
    )


def summarise(results: list[OriginResult], swing_c: float) -> str:
    """Render the per-origin table and the summary.

    Args:
        results: Scored origins in chronological order.
        swing_c: Absolute temperature change in degrees Celsius from which an
            origin counts as a weather swing. Origins without a known change
            are never counted as swings.

    Returns:
        The report as plain text.
    """
    lines = [
        f"{'origin (UTC)':<17} {'h':>3} {'actual':>8} {'weather':>8} {'same-hr':>8} "
        f"{'|err| w':>8} {'|err| s':>8} {'dT':>6}"
    ]
    for r in results:
        lines.append(
            f"{r.origin:%Y-%m-%d %H:%M} {r.hours:>3} {r.actual_kwh:>8.1f} {r.weather_kwh:>8.1f} "
            f"{r.same_hour_kwh:>8.1f} {abs(r.weather_kwh - r.actual_kwh):>8.1f} "
            f"{abs(r.same_hour_kwh - r.actual_kwh):>8.1f} "
            f"{'n/a' if r.temp_change_c is None else f'{r.temp_change_c:+.1f}':>6}"
        )

    def block(name: str, rows: list[OriginResult]) -> list[str]:
        if not rows:
            return [f"{name}: no origins"]
        w = statistics.fmean(abs(r.weather_kwh - r.actual_kwh) for r in rows)
        s = statistics.fmean(abs(r.same_hour_kwh - r.actual_kwh) for r in rows)
        wh = statistics.fmean(r.weather_hourly_mae for r in rows)
        sh = statistics.fmean(r.same_hour_hourly_mae for r in rows)
        return [
            f"{name} ({len(rows)} origins): horizon-energy MAE weather {w:.2f} kWh, "
            f"same-hour {s:.2f} kWh; hourly MAE weather {wh:.3f}, same-hour {sh:.3f} kWh"
        ]

    swings = [r for r in results if r.temp_change_c is not None and abs(r.temp_change_c) >= swing_c]
    lines += ["", *block("All", results), *block(f"|dT| >= {swing_c:g} C", swings)]
    return "\n".join(lines)


def main() -> int:
    """Parse arguments, run the backtest and print the report.

    Returns:
        The process exit status (0 on success; argument errors exit through
        ``argparse``).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, type=Path, help="baseload_weather config.yaml")
    parser.add_argument("--db-url", help="Override homeassistant.db_url from the config")
    parser.add_argument("--test-days", type=int, default=28)
    parser.add_argument("--horizon", type=int, default=24, help="Hours scored per origin")
    parser.add_argument("--origin-hour", type=int, default=22, help="UTC hour of each origin")
    parser.add_argument("--baseline-lookback-days", type=int, default=14)
    parser.add_argument("--baseline-decay", type=float, default=1.0)
    parser.add_argument("--swing", type=float, default=3.0, help="|dT| threshold in degrees C")
    args = parser.parse_args()
    if args.test_days < 1:
        parser.error("--test-days must be at least 1")
    if args.horizon < 1:
        parser.error("--horizon must be at least 1")
    if not 0 <= args.origin_hour <= 23:
        parser.error("--origin-hour must be between 0 and 23")
    if args.baseline_lookback_days < 1:
        parser.error("--baseline-lookback-days must be at least 1")
    if args.baseline_decay < 1.0:
        parser.error("--baseline-decay must be at least 1.0")

    raw = yaml.safe_load(args.config.read_text())
    if args.db_url:
        raw.setdefault("homeassistant", {})["db_url"] = args.db_url
    config = WeatherBaseloadConfig.model_validate(raw)

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    origins = _origins(now, args.test_days, args.origin_hour, args.horizon)
    first_ts = int(origins[0].timestamp())
    scoring = fetch_history(
        config.homeassistant,
        start_ts=first_ts - 24 * _H,
        end_ts=int(origins[-1].timestamp()) + (args.horizon + 1) * _H,
    )

    results: list[OriginResult] = []
    for origin in origins:
        # Read each origin's history separately, and separately per method:
        # the recorder's outlier filter works over the whole requested period,
        # so a shared read would let later load, or the other method's window,
        # decide which hours survive.
        origin_ts = int(origin.timestamp())
        grid_start, _ = history_window(origin, config.model)
        weather_history = fetch_history(config.homeassistant, start_ts=grid_start, end_ts=origin_ts)
        baseline_history = fetch_history(
            config.homeassistant,
            start_ts=_baseline_since(origin, args.baseline_lookback_days),
            end_ts=origin_ts,
        )
        result = evaluate_origin(
            weather_history, baseline_history, scoring, config, origin, args.horizon,
            args.baseline_lookback_days, args.baseline_decay,
        )
        if result is not None:
            results.append(result)
    print(summarise(results, args.swing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
