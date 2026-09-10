"""Unit tests for epexpredictor_prices.series.

Covers the fetch-time confidence bands (identical boundary semantics to
pv_openmeteo.series.ConfidenceDecay), formula application, output shape, and
the known_until_confidence override.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from epexpredictor_prices.series import ConfidenceDecay, build_price_steps


_DECAY = ConfidenceDecay(
    hours_0_to_6=0.90,
    hours_6_to_24=0.75,
    hours_24_to_48=0.55,
    hours_48_plus=0.35,
)
_FETCH_TIME = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
_FAR_KNOWN_UNTIL = _FETCH_TIME + timedelta(days=30)


class TestConfidenceForStep:
    def test_zero_hours_ahead(self) -> None:
        assert _DECAY.confidence_for_step(_FETCH_TIME, _FETCH_TIME) == 0.90

    def test_just_under_6_hours(self) -> None:
        assert (
            _DECAY.confidence_for_step(_FETCH_TIME + timedelta(hours=5, minutes=59), _FETCH_TIME)
            == 0.90
        )

    def test_exactly_6_hours(self) -> None:
        assert _DECAY.confidence_for_step(_FETCH_TIME + timedelta(hours=6), _FETCH_TIME) == 0.75

    def test_just_under_24_hours(self) -> None:
        assert (
            _DECAY.confidence_for_step(_FETCH_TIME + timedelta(hours=23, minutes=59), _FETCH_TIME)
            == 0.75
        )

    def test_exactly_24_hours(self) -> None:
        assert _DECAY.confidence_for_step(_FETCH_TIME + timedelta(hours=24), _FETCH_TIME) == 0.55

    def test_just_under_48_hours(self) -> None:
        assert (
            _DECAY.confidence_for_step(_FETCH_TIME + timedelta(hours=47, minutes=59), _FETCH_TIME)
            == 0.55
        )

    def test_exactly_48_hours(self) -> None:
        assert _DECAY.confidence_for_step(_FETCH_TIME + timedelta(hours=48), _FETCH_TIME) == 0.35

    def test_200_hours(self) -> None:
        assert _DECAY.confidence_for_step(_FETCH_TIME + timedelta(hours=200), _FETCH_TIME) == 0.35

    def test_custom_decay_values_honoured(self) -> None:
        decay = ConfidenceDecay(
            hours_0_to_6=0.5, hours_6_to_24=0.4, hours_24_to_48=0.3, hours_48_plus=0.2
        )
        assert decay.confidence_for_step(_FETCH_TIME, _FETCH_TIME) == 0.5
        assert decay.confidence_for_step(_FETCH_TIME + timedelta(hours=6), _FETCH_TIME) == 0.4
        assert decay.confidence_for_step(_FETCH_TIME + timedelta(hours=24), _FETCH_TIME) == 0.3
        assert decay.confidence_for_step(_FETCH_TIME + timedelta(hours=48), _FETCH_TIME) == 0.2


class TestBuildPriceSteps:
    def test_import_and_export_formulas_applied_independently(self) -> None:
        raw = [(_FETCH_TIME, 0.10)]
        steps = build_price_steps(
            raw,
            fetch_time=_FETCH_TIME,
            decay=_DECAY,
            import_formula="price * 1.1",
            export_formula="0.0",
            known_until=_FAR_KNOWN_UNTIL,
        )
        assert steps[0]["import_eur_per_kwh"] == round(0.10 * 1.1, 6)
        assert steps[0]["export_eur_per_kwh"] == 0.0

    def test_ts_format_matches_nordpool_isoformat(self) -> None:
        raw = [(_FETCH_TIME, 0.10)]
        steps = build_price_steps(
            raw,
            fetch_time=_FETCH_TIME,
            decay=_DECAY,
            import_formula="price",
            export_formula="price",
            known_until=_FAR_KNOWN_UNTIL,
        )
        assert steps[0]["ts"] == _FETCH_TIME.isoformat()
        assert steps[0]["ts"] == "2026-09-10T11:00:00+00:00"

    def test_prices_rounded_to_six_decimals(self) -> None:
        raw = [(_FETCH_TIME, 0.123456789)]
        steps = build_price_steps(
            raw,
            fetch_time=_FETCH_TIME,
            decay=_DECAY,
            import_formula="price",
            export_formula="price",
            known_until=_FAR_KNOWN_UNTIL,
        )
        assert steps[0]["import_eur_per_kwh"] == round(0.123456789, 6)
        assert steps[0]["export_eur_per_kwh"] == round(0.123456789, 6)

    def test_default_none_override_anchors_confidence_purely_to_fetch_time(self) -> None:
        # known_until is far in the future, yet the step's own horizon distance
        # (200h) still drives the confidence — the fetch-time anchor, not the
        # real/predicted boundary, decides.
        far_step = _FETCH_TIME + timedelta(hours=200)
        raw = [(far_step, 0.10)]
        steps = build_price_steps(
            raw,
            fetch_time=_FETCH_TIME,
            decay=_DECAY,
            import_formula="price",
            export_formula="price",
            known_until=_FAR_KNOWN_UNTIL,
            known_until_confidence=None,
        )
        assert steps[0]["confidence"] == 0.35

    def test_known_until_confidence_overrides_steps_at_or_before_known_until(self) -> None:
        known_until = _FETCH_TIME + timedelta(hours=10)
        # This step is 200h ahead of fetch time (would normally be 0.35) but
        # is at known_until, so it gets the override value instead.
        raw = [(known_until, 0.10)]
        steps = build_price_steps(
            raw,
            fetch_time=_FETCH_TIME,
            decay=_DECAY,
            import_formula="price",
            export_formula="price",
            known_until=known_until,
            known_until_confidence=1.0,
        )
        assert steps[0]["confidence"] == 1.0

    def test_known_until_confidence_boundary_is_inclusive(self) -> None:
        known_until = _FETCH_TIME + timedelta(hours=10)
        raw = [(known_until, 0.10)]
        steps = build_price_steps(
            raw,
            fetch_time=_FETCH_TIME,
            decay=_DECAY,
            import_formula="price",
            export_formula="price",
            known_until=known_until,
            known_until_confidence=0.42,
        )
        assert steps[0]["confidence"] == 0.42

    def test_known_until_confidence_does_not_affect_steps_after_known_until(self) -> None:
        known_until = _FETCH_TIME + timedelta(hours=10)
        step_after = known_until + timedelta(seconds=1)
        raw = [(step_after, 0.10)]
        steps = build_price_steps(
            raw,
            fetch_time=_FETCH_TIME,
            decay=_DECAY,
            import_formula="price",
            export_formula="price",
            known_until=known_until,
            known_until_confidence=1.0,
        )
        # step_after is 10h+ ahead -> falls in the 6-24h band (0.75), not the override.
        assert steps[0]["confidence"] == 0.75

    def test_empty_input_returns_empty_list(self) -> None:
        steps = build_price_steps(
            [],
            fetch_time=_FETCH_TIME,
            decay=_DECAY,
            import_formula="price",
            export_formula="price",
            known_until=_FAR_KNOWN_UNTIL,
        )
        assert steps == []
