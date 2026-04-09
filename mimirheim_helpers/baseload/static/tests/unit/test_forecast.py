"""Unit tests for baseload_static.forecast.

Covers step generation, profile tiling, and timestamp alignment.
"""
from __future__ import annotations

from datetime import datetime, timezone

from baseload_static.forecast import build_forecast


_NOW = datetime(2026, 3, 31, 14, 37, 12, tzinfo=timezone.utc)


class TestBuildForecast:
    def test_step_count_matches_horizon_hours(self) -> None:
        steps = build_forecast(profile_kw=[0.5] * 24, horizon_hours=48, now=_NOW)
        assert len(steps) == 48

    def test_first_step_aligned_to_current_hour(self) -> None:
        steps = build_forecast(profile_kw=[0.5] * 24, horizon_hours=1, now=_NOW)
        # Minutes and seconds must be stripped; hour is preserved.
        assert steps[0]["ts"] == "2026-03-31T14:00:00+00:00"

    def test_steps_are_one_hour_apart(self) -> None:
        steps = build_forecast(profile_kw=[0.5] * 24, horizon_hours=4, now=_NOW)
        ts0 = datetime.fromisoformat(steps[0]["ts"])
        ts1 = datetime.fromisoformat(steps[1]["ts"])
        assert (ts1 - ts0).total_seconds() == 3600

    def test_profile_indexed_by_wall_clock_hour(self) -> None:
        # _NOW has hour=14. A 24-element profile [0, 1, ..., 23] should
        # serve profile[14]=14.0 at the first step (14:00), profile[15]=15.0
        # at 15:00, etc. — independent of the trigger time.
        profile = list(range(24))  # [0, 1, 2, ..., 23]
        steps = build_forecast(profile_kw=profile, horizon_hours=24, now=_NOW)
        assert steps[0]["kw"] == 14.0   # 14:00 → profile[14]
        assert steps[1]["kw"] == 15.0   # 15:00 → profile[15]
        assert steps[10]["kw"] == 0.0   # 00:00 next day → profile[0]

    def test_profile_values_stable_regardless_of_trigger_time(self) -> None:
        # Triggering at 09:00 or 14:00 must produce the same value at 12:00.
        profile = [float(h) for h in range(24)]
        steps_a = build_forecast(
            profile_kw=profile,
            horizon_hours=24,
            now=_NOW.replace(hour=9),
        )
        steps_b = build_forecast(
            profile_kw=profile,
            horizon_hours=24,
            now=_NOW.replace(hour=14),
        )
        # Find the 12:00 step in each result and confirm both give profile[12].
        kw_a = next(s["kw"] for s in steps_a if "T12:" in s["ts"])
        kw_b = next(s["kw"] for s in steps_b if "T12:" in s["ts"])
        assert kw_a == 12.0
        assert kw_b == 12.0

    def test_short_profile_wraps_by_hour(self) -> None:
        # A 2-element profile: even hours get profile[0]=1.0, odd hours get
        # profile[1]=2.0, because ts.hour % 2 == 0 or 1.
        # _NOW has hour=14 (even), so first step → profile[0]=1.0.
        steps = build_forecast(profile_kw=[1.0, 2.0], horizon_hours=4, now=_NOW)
        assert steps[0]["kw"] == 1.0   # 14:00, even
        assert steps[1]["kw"] == 2.0   # 15:00, odd
        assert steps[2]["kw"] == 1.0   # 16:00, even
        assert steps[3]["kw"] == 2.0   # 17:00, odd

    def test_single_element_profile_produces_flat_constant(self) -> None:
        steps = build_forecast(profile_kw=[0.75], horizon_hours=10, now=_NOW)
        assert all(s["kw"] == 0.75 for s in steps)

    def test_kw_values_rounded_to_four_decimal_places(self) -> None:
        steps = build_forecast(profile_kw=[0.123456789], horizon_hours=1, now=_NOW)
        assert steps[0]["kw"] == 0.1235

    def test_step_dict_has_ts_and_kw_keys_only(self) -> None:
        steps = build_forecast(profile_kw=[0.5], horizon_hours=1, now=_NOW)
        assert set(steps[0].keys()) == {"ts", "kw"}


class TestBuildForecastWeekly:
    """Tests for the weekly_profiles_kw selection logic."""

    def test_weekly_profile_used_on_matching_weekday(self) -> None:
        # _NOW is 2026-03-31 14:xx UTC — a Tuesday (weekday=1).
        # Provide a distinct profile for Tuesday; fallback is all-zeros.
        weekly = {1: [9.0] * 24}
        steps = build_forecast(
            profile_kw=[0.0] * 24,
            horizon_hours=1,
            now=_NOW,
            weekly_profiles_kw=weekly,
        )
        # 14:00 on Tuesday should use weekly[1][14] = 9.0
        assert steps[0]["kw"] == 9.0

    def test_fallback_to_profile_kw_on_unmatched_weekday(self) -> None:
        # Only Wednesday (2) has a weekly profile; Tuesday falls back.
        weekly = {2: [9.0] * 24}
        steps = build_forecast(
            profile_kw=[7.0] * 24,
            horizon_hours=1,
            now=_NOW,
            weekly_profiles_kw=weekly,
        )
        assert steps[0]["kw"] == 7.0

    def test_weekly_profile_without_fallback(self) -> None:
        # All 7 days covered; profile_kw is None.
        weekly = {i: [float(i)] * 24 for i in range(7)}
        steps = build_forecast(
            profile_kw=None,
            horizon_hours=1,
            now=_NOW,  # Tuesday (1) at 14:00
            weekly_profiles_kw=weekly,
        )
        assert steps[0]["kw"] == 1.0  # profile for weekday 1

    def test_weekly_profile_transitions_across_midnight(self) -> None:
        # Start at 23:00 on a Monday (weekday=0). Second step is 00:00 Tuesday (1).
        monday_23 = _NOW.replace(hour=23, day=30)  # 2026-03-30 is a Monday
        weekly = {0: [0.0] * 24, 1: [1.0] * 24}
        steps = build_forecast(
            profile_kw=[0.0] * 24,
            horizon_hours=2,
            now=monday_23,
            weekly_profiles_kw=weekly,
        )
        assert steps[0]["kw"] == 0.0  # 23:00 Monday
        assert steps[1]["kw"] == 1.0  # 00:00 Tuesday
