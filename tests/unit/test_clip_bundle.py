"""Unit tests for the _clip_bundle helper in mimirheim.__main__.

_clip_bundle is a pure function that trims all per-step arrays in a SolveBundle
to at most max_steps entries. These tests verify the trimming behaviour for each
array that scales with the horizon length.
"""

from datetime import UTC, datetime

import pytest

from mimirheim.__main__ import _clip_bundle
from mimirheim.core.bundle import SolveBundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOLVE_TIME = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _make_bundle(n: int, **overrides: object) -> SolveBundle:
    """Return a minimal valid SolveBundle with ``n`` horizon steps."""
    data: dict = {
        "solve_time_utc": _SOLVE_TIME,
        "horizon_prices": [0.20] * n,
        "horizon_export_prices": [0.10] * n,
        "horizon_confidence": [1.0] * n,
        "pv_forecast": [3.0] * n,
        "base_load_forecast": [1.0] * n,
    }
    data.update(overrides)
    return SolveBundle.model_validate(data)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_clip_bundle_noop_when_short() -> None:
    """A bundle shorter than max_steps passes through unchanged."""
    bundle = _make_bundle(48)
    result = _clip_bundle(bundle, 96)
    assert len(result.horizon_prices) == 48


def test_clip_bundle_noop_at_exact_max() -> None:
    """A bundle whose length equals max_steps passes through unchanged."""
    bundle = _make_bundle(96)
    result = _clip_bundle(bundle, 96)
    assert len(result.horizon_prices) == 96


def test_clip_bundle_clips_all_top_level_arrays() -> None:
    """All five top-level per-step arrays are trimmed to exactly max_steps."""
    bundle = _make_bundle(200)
    result = _clip_bundle(bundle, 96)
    assert len(result.horizon_prices) == 96
    assert len(result.horizon_export_prices) == 96
    assert len(result.horizon_confidence) == 96
    assert len(result.pv_forecast) == 96
    assert len(result.base_load_forecast) == 96


def test_clip_bundle_clips_hybrid_inverter_pv_forecast() -> None:
    """HybridInverterInputs.pv_forecast_kw is trimmed to max_steps."""
    bundle = _make_bundle(
        200,
        hybrid_inverter_inputs={
            "inv1": {"soc_kwh": 5.0, "pv_forecast_kw": [2.0] * 200},
        },
    )
    result = _clip_bundle(bundle, 96)
    assert len(result.hybrid_inverter_inputs["inv1"].pv_forecast_kw) == 96


def test_clip_bundle_clips_space_heating_outdoor_temp() -> None:
    """SpaceHeatingInputs.outdoor_temp_forecast_c is trimmed to max_steps."""
    bundle = _make_bundle(
        200,
        space_heating_inputs={
            "sh1": {
                "heat_needed_kwh": 10.0,
                "outdoor_temp_forecast_c": [5.0] * 200,
            },
        },
    )
    result = _clip_bundle(bundle, 96)
    assert len(result.space_heating_inputs["sh1"].outdoor_temp_forecast_c) == 96


def test_clip_bundle_clips_combi_hp_outdoor_temp() -> None:
    """CombiHeatPumpInputs.outdoor_temp_forecast_c is trimmed to max_steps."""
    bundle = _make_bundle(
        200,
        combi_hp_inputs={
            "chp1": {
                "current_temp_c": 45.0,
                "heat_needed_kwh": 5.0,
                "outdoor_temp_forecast_c": [8.0] * 200,
            },
        },
    )
    result = _clip_bundle(bundle, 96)
    assert len(result.combi_hp_inputs["chp1"].outdoor_temp_forecast_c) == 96


def test_clip_bundle_leaves_optional_none_outdoor_temp_untouched() -> None:
    """None outdoor_temp_forecast_c is preserved as None after clipping."""
    bundle = _make_bundle(
        200,
        space_heating_inputs={
            "sh1": {
                "heat_needed_kwh": 0.0,
                "outdoor_temp_forecast_c": None,
            },
        },
    )
    result = _clip_bundle(bundle, 96)
    assert result.space_heating_inputs["sh1"].outdoor_temp_forecast_c is None
