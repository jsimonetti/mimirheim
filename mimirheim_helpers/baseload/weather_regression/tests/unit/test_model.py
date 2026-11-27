"""Unit tests for baseload_weather.model.

The model is pure: every test builds hourly inputs in memory (keyed by the
UTC epoch second of each hour's start, as the recorder and the Open-Meteo
client return them) and checks the fitted forecast.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from baseload_weather.config import ModelConfig
from baseload_weather.model import (
    ForecastResult,
    ModelError,
    build_forecast,
    history_window,
    smooth_temperatures,
    temperature_series,
    training_targets,
)

H = 3600
AMS = ZoneInfo("Europe/Amsterdam")
S = datetime(2026, 1, 20, 12, tzinfo=UTC)
S_TS = int(S.timestamp())


def _cfg(**kw: object) -> ModelConfig:
    base: dict[str, object] = {"timezone": "Europe/Amsterdam", "lookback_days": 28, "smoothing_hours": 6.0}
    base.update(kw)
    return ModelConfig(**base)


def _temps(start: int, end: int, fn: Callable[[int], float]) -> dict[int, float]:
    """Hourly temperatures for [start, end] inclusive from ``fn(hour_ts)``."""
    return {ts: fn(ts) for ts in range(start, end + H, H)}


def _wavy(ts: int) -> float:
    """A temperature that varies within every hour-of-day cell across days.

    Spans roughly -1 to 21 degrees, so both sides of the balance temperature
    are exercised and the kink is identifiable.
    """
    day = (ts - S_TS) // 86400
    return 10.0 + 9.0 * math.sin(day * 0.9) + 2.0 * math.sin(ts / 86400 * 2 * math.pi)


Energy = dict[str, dict[int, float]]


def _scenario(
    cfg: ModelConfig,
    load_fn: Callable[[int, float], float],
    temp_fn: Callable[[int], float] = _wavy,
    horizon: int = 24,
) -> tuple[Energy, dict[int, float], dict[int, float]]:
    """Build recorder/forecast inputs whose load follows ``load_fn(ts, t_smooth)``."""
    grid_start, window_start = history_window(S, cfg)
    end = S_TS + horizon * H
    all_temps = _temps(grid_start, end, temp_fn)
    series = temperature_series(all_temps, {}, grid_start, end)
    t_s, _ = smooth_temperatures(series, cfg.smoothing_hours)
    recorder_temps = {ts: v for ts, v in all_temps.items() if ts < S_TS}
    forecast_temps = {ts: v for ts, v in all_temps.items() if ts >= S_TS - 24 * H}
    energy = {
        "sensor.load": {
            ts: load_fn(ts, t_s[(ts - grid_start) // H]) for ts in range(window_start, S_TS, H)
        }
    }
    return energy, recorder_temps, forecast_temps


def _run(
    cfg: ModelConfig,
    energy: Energy,
    rec: dict[int, float],
    fc: dict[int, float],
    horizon: int = 24,
    **kw: list[str],
) -> ForecastResult:
    return build_forecast(
        energy=energy,
        temperature_recorder=rec,
        temperature_forecast=fc,
        sum_ids=kw.get("sum_ids", ["sensor.load"]),
        subtract_ids=kw.get("subtract_ids", []),
        config=cfg,
        start=S,
        horizon_hours=horizon,
    )


class TestTrainingTargets:
    def test_requires_every_constituent(self) -> None:
        energy = {
            "a": {0: 2.0, H: 2.0, 2 * H: 2.0},
            "b": {0: 1.0, 2 * H: 1.0},
            "x": {0: 0.5, H: 0.5},
        }
        targets = training_targets(energy, ["a", "b"], ["x"])
        assert targets == {0: 2.5}

    def test_missing_entity_gives_no_targets(self) -> None:
        assert training_targets({"a": {0: 1.0}}, ["a", "b"], []) == {}


class TestTemperatureSeries:
    def test_recorder_wins_over_forecast(self) -> None:
        series = temperature_series({0: 1.0}, {0: 9.0, H: 2.0}, 0, H)
        assert series == [1.0, 2.0]

    def test_short_gap_interpolated(self) -> None:
        series = temperature_series({0: 0.0, 6 * H: 6.0}, {}, 0, 6 * H)
        assert series == pytest.approx([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    def test_long_gap_left_empty(self) -> None:
        series = temperature_series({0: 0.0, 8 * H: 8.0}, {}, 0, 8 * H)
        assert series[0] == 0.0 and series[8] == 8.0
        assert all(v is None for v in series[1:8])

    def test_leading_gap_left_empty(self) -> None:
        assert temperature_series({2 * H: 5.0}, {}, 0, 2 * H) == [None, None, 5.0]


class TestSmoothing:
    def test_ema_coefficient_and_seed(self) -> None:
        smoothed, _ = smooth_temperatures([10.0, 0.0, 0.0], 2.0)
        alpha = 1 - math.exp(-1 / 2.0)
        assert smoothed[0] == 10.0
        assert smoothed[1] == pytest.approx(10.0 * (1 - alpha))
        assert smoothed[2] == pytest.approx(10.0 * (1 - alpha) ** 2)

    def test_warm_up_after_seed_and_reseed(self) -> None:
        series: list[float | None] = [5.0] * 10 + [None] * 7 + [1.0] * 10
        smoothed, warm = smooth_temperatures(series, 1.0)
        # 3 * smoothing_hours = 3 warm-up hours after each (re)seed.
        assert warm[:3] == [False, False, False] and warm[3] is True
        assert all(s is None for s in smoothed[10:17])
        assert smoothed[17] == 1.0  # reseeded, not blended with the old state
        assert warm[17:20] == [False, False, False] and warm[20] is True


class TestFit:
    def test_recovers_synthetic_model(self) -> None:
        # 28 days give 8 weekend samples per cell, so min_samples is lowered
        # to let every cell fit at level 1.
        cfg = _cfg(min_samples=5)
        t_base, b = 15.0, 0.4

        def load(ts: int, t_s: float) -> float:
            local_hour = datetime.fromtimestamp(ts, AMS).hour
            return 0.3 + 0.05 * local_hour + b * max(0.0, t_base - t_s)

        energy, rec, fc = _scenario(cfg, load)
        result = _run(cfg, energy, rec, fc)
        assert result.stats["t_base_c"] == pytest.approx(t_base)
        assert result.stats["levels"]["1"] == 48
        assert result.stats["r2"] == pytest.approx(1.0, abs=1e-9)
        assert result.stats["mae_kwh"] == pytest.approx(0.0, abs=1e-9)

    def test_forecast_follows_fitted_model(self) -> None:
        cfg = _cfg()

        def load(ts: int, t_s: float) -> float:
            return 0.5 + 0.3 * max(0.0, 15.0 - t_s)

        energy, rec, fc = _scenario(cfg, load)
        result = _run(cfg, energy, rec, fc)
        series = temperature_series({**rec}, fc, history_window(S, cfg)[0], S_TS + 24 * H)
        t_s, _ = smooth_temperatures(series, cfg.smoothing_hours)
        grid_start = history_window(S, cfg)[0]
        for point in result.points:
            ts = int(datetime.fromisoformat(point["ts"]).timestamp())
            expected = 0.5 + 0.3 * max(0.0, 15.0 - t_s[(ts - grid_start) // H])
            assert point["kw"] == pytest.approx(expected, abs=1e-4)  # published at 4 decimals

    def test_negative_slope_refit_with_zero(self) -> None:
        cfg = _cfg()

        def load(ts: int, t_s: float) -> float:
            return 5.0 - 0.2 * max(0.0, 15.0 - t_s)  # less load when colder

        energy, rec, fc = _scenario(cfg, load)
        result = _run(cfg, energy, rec, fc)
        for cell in result.cells.values():
            assert cell.b >= 0.0

    def test_level_2_when_weekend_cells_are_sparse(self) -> None:
        cfg = _cfg(min_samples=10)

        def load(ts: int, t_s: float) -> float:
            return 1.0 + 0.3 * max(0.0, 15.0 - t_s)

        energy, rec, fc = _scenario(cfg, load)
        # Keep only two weekend days: weekend cells fall below min_samples.
        weekend_days = sorted(
            {datetime.fromtimestamp(ts, AMS).date() for ts in energy["sensor.load"]
             if datetime.fromtimestamp(ts, AMS).weekday() >= 5}
        )
        keep_weekend = set(weekend_days[-2:])
        energy["sensor.load"] = {
            ts: v for ts, v in energy["sensor.load"].items()
            if datetime.fromtimestamp(ts, AMS).weekday() < 5
            or datetime.fromtimestamp(ts, AMS).date() in keep_weekend
        }
        result = _run(cfg, energy, rec, fc)
        assert result.stats["levels"]["2"] == 24
        assert result.stats["levels"]["1"] == 24

    def test_level_3_within_cell_estimator(self) -> None:
        cfg = _cfg(min_samples=10, lookback_days=7)

        def load(ts: int, t_s: float) -> float:
            return 1.0 + 0.3 * max(0.0, 15.0 - t_s)

        # 7 days: each cell has at most 5 samples, each hour 7: levels 1-2 fail.
        energy, rec, fc = _scenario(cfg, load)
        result = _run(cfg, energy, rec, fc)
        assert result.stats["levels"]["3"] == 48
        assert {round(c.b, 6) for c in result.cells.values()} == {0.3}

    def test_between_cell_differences_do_not_create_a_slope(self) -> None:
        cfg = _cfg(min_samples=3)

        # Temperature is constant per local hour (no day-to-day variation), and
        # load differs per hour for reasons unrelated to temperature.
        def temp(ts: int) -> float:
            return 2.0 + datetime.fromtimestamp(ts, AMS).hour * 0.5

        def load(ts: int, t_s: float) -> float:
            return 0.2 * datetime.fromtimestamp(ts, AMS).hour

        energy, rec, fc = _scenario(cfg, load, temp_fn=temp)
        result = _run(cfg, energy, rec, fc)
        assert result.stats["t_base_c"] is None
        assert result.stats["levels"]["4"] == 48
        assert all(c.b == 0.0 for c in result.cells.values())

    def test_all_warm_history_falls_back_to_means(self) -> None:
        cfg = _cfg()
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 0.7, temp_fn=lambda ts: 21.0 + (ts // H) % 5)
        result = _run(cfg, energy, rec, fc)
        assert result.stats["t_base_c"] is None
        assert result.stats["levels"]["4"] == 48
        assert all(p["kw"] == pytest.approx(0.7) for p in result.points)

    def test_t_base_tie_goes_to_lowest(self) -> None:
        # Temperatures always above 20 C except a cold tail that only the
        # range [10, 12] distinguishes poorly: a flat load makes every candidate
        # with enough variance fit perfectly (SSE 0), so the lowest wins.
        cfg = _cfg(min_hdd_variance=0.01)
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 1.0)
        result = _run(cfg, energy, rec, fc)
        assert result.stats["t_base_c"] == 10.0

    def test_t_base_numerical_tie_goes_to_lowest_at_large_sse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # At a large SSE, candidates that tie mathematically can differ by far
        # more than an absolute 1e-12 through rounding. The selection must
        # treat them as tied (keep the lowest) yet still take a real gain.
        from baseload_weather import model

        sse_by_candidate = {10.0: 1.0e9, 10.5: 1.0e9 - 1.0e-4, 11.0: 1.0e9 - 2.0e-4, 11.5: 0.9e9}

        def fake_within_cell(rows: list[model._Row], t_base: float) -> tuple[float, float, float]:
            return 0.1, 10.0, sse_by_candidate.get(t_base, 2.0e9)

        monkeypatch.setattr(model, "_within_cell", fake_within_cell)
        cfg = _cfg(t_base_min_c=10.0, t_base_max_c=12.0)
        rows = [model._Row((0, False), 5.0, 1.0, 1.0)] * 20
        assert model._select_t_base(rows, cfg) == 11.5
        sse_by_candidate[11.5] = 1.0e9 - 3.0e-4  # now also a rounding-level tie
        assert model._select_t_base(rows, cfg) == 10.0

    def test_constant_target_has_undefined_r2(self) -> None:
        cfg = _cfg(lookback_days=28, lookback_decay=3.0)
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 0.1 + 0.2)
        result = _run(cfg, energy, rec, fc)
        assert result.stats["r2"] is None

    def test_empty_history_raises(self) -> None:
        cfg = _cfg()
        _, rec, fc = _scenario(cfg, lambda ts, t_s: 1.0)
        with pytest.raises(ModelError):
            _run(cfg, {"sensor.load": {}}, rec, fc)

    def test_incomplete_constituent_hours_are_dropped_not_zero_filled(self) -> None:
        cfg = _cfg(lookback_days=7)
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 2.0)
        # The subtract entity misses half of the hours; treating them as zero
        # would raise those hours to 2.0 and the mean above 1.0.
        sub = {ts: 1.0 for i, ts in enumerate(sorted(energy["sensor.load"])) if i % 2 == 0}
        energy["sensor.sub"] = sub
        result = _run(cfg, energy, rec, fc, subtract_ids=["sensor.sub"])
        assert all(p["kw"] == pytest.approx(1.0) for p in result.points)
        assert result.stats["training_coverage_pct"] == pytest.approx(50.0, abs=1.0)

    def test_exclude_dates_drop_targets_only(self) -> None:
        cfg = _cfg(lookback_days=7)
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 1.0)
        bad_day = (S - timedelta(days=3)).astimezone(AMS).date()
        for ts in list(energy["sensor.load"]):
            if datetime.fromtimestamp(ts, AMS).date() == bad_day:
                energy["sensor.load"][ts] = 99.0
        excluded = _run(_cfg(lookback_days=7, exclude_dates=[bad_day]), energy, rec, fc)
        assert all(p["kw"] == pytest.approx(1.0) for p in excluded.points)

    def test_recency_decay_weights_recent_days_more(self) -> None:
        def load(ts: int, t_s: float) -> float:
            return 2.0 if ts >= S_TS - 7 * 86400 else 1.0

        flat_cfg = _cfg(lookback_days=28)
        energy, rec, fc = _scenario(flat_cfg, load, temp_fn=lambda ts: 22.0)
        flat = _run(flat_cfg, energy, rec, fc).points[0]["kw"]
        decayed = _run(_cfg(lookback_days=28, lookback_decay=10.0), energy, rec, fc).points[0]["kw"]
        assert 1.0 < flat < decayed < 2.0


class TestHorizonAndLocalTime:
    def test_horizon_points_include_endpoint_marker(self) -> None:
        cfg = _cfg(lookback_days=7)
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 1.0, horizon=96)
        result = _run(cfg, energy, rec, fc, horizon=96)
        assert len(result.points) == 97
        assert result.points[0]["ts"] == S.isoformat()
        assert result.points[-1]["ts"] == (S + timedelta(hours=96)).isoformat()

    def test_late_day_and_maximum_horizon(self) -> None:
        cfg = _cfg(lookback_days=7)
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 1.0, horizon=168)
        result = _run(cfg, energy, rec, fc, horizon=168)
        assert len(result.points) == 169

    def test_missing_forecast_hour_raises(self) -> None:
        cfg = _cfg(lookback_days=7)
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 1.0)
        del fc[S_TS + 5 * H]
        del fc[S_TS + 6 * H]
        del fc[S_TS + 7 * H]
        del fc[S_TS + 8 * H]
        del fc[S_TS + 9 * H]
        del fc[S_TS + 10 * H]
        del fc[S_TS + 11 * H]
        with pytest.raises(ModelError):
            _run(cfg, energy, rec, fc)

    def test_forecast_without_heating_degrees_is_baseline(self) -> None:
        cfg = _cfg()

        def load(ts: int, t_s: float) -> float:
            return 0.4 + 0.3 * max(0.0, 15.0 - t_s)

        energy, rec, fc = _scenario(cfg, load)
        warm_fc = {ts: (25.0 if ts >= S_TS - 24 * H else v) for ts, v in fc.items()}
        result = _run(cfg, energy, rec, warm_fc)
        assert result.points[-1]["kw"] == pytest.approx(0.4, abs=1e-4)

    def test_local_hours_stay_aligned_across_dst(self) -> None:
        # Start the forecast in the week after the spring DST change and give
        # the house a morning peak at local 07:00 in history spanning the change.
        start = datetime(2026, 4, 1, 0, tzinfo=UTC)
        cfg = _cfg(lookback_days=21)
        grid_start, window_start = history_window(start, cfg)
        end = int(start.timestamp()) + 48 * H
        temps = {ts: 22.0 for ts in range(grid_start, end + H, H)}

        def load(ts: int) -> float:
            return 3.0 if datetime.fromtimestamp(ts, AMS).hour == 7 else 0.5

        energy = {"sensor.load": {ts: load(ts) for ts in range(window_start, int(start.timestamp()), H)}}
        result = build_forecast(
            energy=energy,
            temperature_recorder={ts: v for ts, v in temps.items() if ts < start.timestamp()},
            temperature_forecast=temps,
            sum_ids=["sensor.load"],
            subtract_ids=[],
            config=cfg,
            start=start,
            horizon_hours=48,
        )
        for point in result.points:
            local_hour = datetime.fromisoformat(point["ts"]).astimezone(AMS).hour
            assert point["kw"] == pytest.approx(3.0 if local_hour == 7 else 0.5)


def test_history_window() -> None:
    cfg = _cfg(lookback_days=7, smoothing_hours=4.5)
    grid_start, window_start = history_window(S, cfg)
    assert window_start == S_TS - 7 * 86400
    assert grid_start == window_start - 14 * H  # ceil(3 * 4.5) hours of warm-up
    assert isinstance(date.today(), date)


class TestPreciseRules:
    """Tests that pin each specified rule exactly, not only its direction."""

    def test_interpolation_boundary_six_versus_seven_hours(self) -> None:
        six = temperature_series({0: 0.0, 7 * H: 7.0}, {}, 0, 7 * H)
        assert six == pytest.approx([float(i) for i in range(8)])
        seven = temperature_series({0: 0.0, 8 * H: 8.0}, {}, 0, 8 * H)
        assert all(v is None for v in seven[1:8])

    def test_warm_up_hours_after_a_long_gap_are_not_trained_on(self) -> None:
        cfg = _cfg(lookback_days=7, smoothing_hours=2.0, min_samples=3)

        def load(ts: int, t_s: float) -> float:
            return 0.5 + 0.3 * max(0.0, 15.0 - t_s)

        energy, rec, fc = _scenario(cfg, load)
        _, window_start = history_window(S, cfg)
        gap_start = window_start + 60 * H
        for ts in range(gap_start, gap_start + 8 * H, H):  # 8 h: not interpolated
            del rec[ts]
        clean = _run(cfg, energy, rec, fc)
        warm_up = [gap_start + (8 + k) * H for k in range(6)]  # ceil(3 * 2) hours
        polluted = {**energy["sensor.load"], **{ts: 999.0 for ts in warm_up}}
        dirty = _run(cfg, {"sensor.load": polluted}, rec, fc)
        assert clean.stats["training_hours"] == 7 * 24 - 8 - 6
        assert dirty.points == clean.points

    def test_excluded_dates_keep_their_temperatures(self) -> None:
        cfg = _cfg(lookback_days=14, min_samples=3)

        def load(ts: int, t_s: float) -> float:
            return 0.5 + 0.3 * max(0.0, 15.0 - t_s)

        energy, rec, fc = _scenario(cfg, load)
        bad_day = (S - timedelta(days=5)).astimezone(AMS).date()
        bad = [ts for ts in energy["sensor.load"] if datetime.fromtimestamp(ts, AMS).date() == bad_day]
        for ts in bad:
            energy["sensor.load"][ts] = 999.0
        result = _run(_cfg(lookback_days=14, min_samples=3, exclude_dates=[bad_day]), energy, rec, fc)
        # Only the day itself is dropped; no warm-up loss follows it, because
        # its temperatures stay in the smoothed series.
        assert result.stats["training_hours"] == 14 * 24 - len(bad)
        assert result.stats["mae_kwh"] == pytest.approx(0.0, abs=1e-9)

    def test_negative_slope_refit_uses_weighted_mean_and_keeps_level(self) -> None:
        cfg = _cfg(min_samples=5)

        def load(ts: int, t_s: float) -> float:
            return 5.0 - 0.2 * max(0.0, 15.0 - t_s)

        energy, rec, fc = _scenario(cfg, load)
        result = _run(cfg, energy, rec, fc)
        by_cell: dict[tuple[int, bool], list[float]] = {}
        for ts, y in energy["sensor.load"].items():
            local = datetime.fromtimestamp(ts, AMS)
            by_cell.setdefault((local.hour, local.weekday() >= 5), []).append(y)
        for cell, fit in result.cells.items():
            assert fit.b == 0.0
            assert fit.level == 1
            assert fit.a == pytest.approx(sum(by_cell[cell]) / len(by_cell[cell]))

    def test_level_3_intercepts_cell_then_hour_then_global(self) -> None:
        from baseload_weather import model

        cfg = _cfg(min_samples=10, min_hdd_variance=0.5)
        rows: list[model._Row] = []
        # Hour 1: weekday and weekend cells with different baselines; hour 0:
        # weekday only. Every cell has 4 rows, so levels 1 and 2 fail on
        # min_samples while level 3 (all 12 rows) is eligible.
        for cell, base in (((1, False), 10.0), ((1, True), 20.0), ((0, False), 30.0)):
            for t_s in (0.0, 2.0, 4.0, 6.0):
                rows.append(model._Row(cell, t_s, base + 0.3 * (15.0 - t_s), 1.0))
        fits = model._fit_cells(rows, 15.0, cfg)
        assert {f.level for f in fits.values()} == {3}
        assert fits[(1, False)].b == pytest.approx(0.3)
        assert fits[(1, False)].a == pytest.approx(10.0)  # own cell
        assert fits[(1, True)].a == pytest.approx(20.0)  # own cell
        assert fits[(0, True)].a == pytest.approx(30.0)  # hour 0 pooled
        mean_y = sum(r.y for r in rows) / len(rows)
        mean_x = sum(15.0 - r.t_s for r in rows) / len(rows)
        assert fits[(5, False)].a == pytest.approx(mean_y - 0.3 * mean_x)  # global

    def test_level_4_has_no_sample_minimum(self) -> None:
        cfg = _cfg(lookback_days=7)
        energy, rec, fc = _scenario(cfg, lambda ts, t_s: 1.0)
        only = max(energy["sensor.load"])
        result = _run(cfg, {"sensor.load": {only: 0.42}}, rec, fc)
        assert result.stats["training_hours"] == 1
        assert result.stats["levels"]["4"] == 48
        assert all(p["kw"] == pytest.approx(0.42) for p in result.points)

    def test_recency_weights_follow_the_specified_formula(self) -> None:
        lookback, decay = 7, 4.0
        cfg = _cfg(lookback_days=lookback, lookback_decay=decay)
        _, window_start = history_window(S, cfg)

        def load(ts: int, t_s: float) -> float:
            return float((ts - window_start) // 86400)  # the day index itself

        energy, rec, fc = _scenario(cfg, load, temp_fn=lambda ts: 22.0)
        result = _run(cfg, energy, rec, fc)
        for point in result.points:
            ts = int(datetime.fromisoformat(point["ts"]).timestamp())
            local = datetime.fromtimestamp(ts, AMS)
            cell = (local.hour, local.weekday() >= 5)
            num = den = 0.0
            for hts, y in energy["sensor.load"].items():
                hl = datetime.fromtimestamp(hts, AMS)
                if (hl.hour, hl.weekday() >= 5) != cell:
                    continue
                w = decay ** (((hts - window_start) // 86400) / (lookback - 1))
                num += w * y
                den += w
            assert point["kw"] == pytest.approx(num / den, abs=1e-4)

    def test_eligibility_uses_kish_effective_sample_size(self) -> None:
        def load(ts: int, t_s: float) -> float:
            return 1.0 + 0.3 * max(0.0, 15.0 - t_s)

        flat_cfg = _cfg(lookback_days=7, min_samples=4)
        energy, rec, fc = _scenario(flat_cfg, load)
        flat = _run(flat_cfg, energy, rec, fc)
        skewed = _run(_cfg(lookback_days=7, min_samples=4, lookback_decay=1e6), energy, rec, fc)
        # Weekday cells hold 5 raw samples (>= 4) in both runs; with extreme
        # recency weighting their Kish n_eff drops below 4.
        assert flat.stats["levels"]["1"] == 24
        assert skewed.stats["levels"]["1"] == 0
