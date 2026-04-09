"""Tests for horizon length and time-step derivation in build_and_solve.

These tests confirm that the horizon (number of time steps) and the derived
time-step duration (dt) are correctly inferred from the length of
``bundle.horizon_prices``. They also confirm that the resulting schedule has
the correct number of entries.
"""

from datetime import UTC, datetime

import pytest

from mimirheim.config.schema import GridConfig, MimirheimConfig, MqttConfig, OutputsConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.model_builder import _dt_from_horizon, build_and_solve


def _minimal_bundle(n: int) -> SolveBundle:
    """Return a SolveBundle with ``n`` steps and no devices."""
    return SolveBundle(
        strategy="minimize_cost",
        solve_time_utc=datetime.now(UTC),
        horizon_prices=[0.20] * n,
        horizon_export_prices=[0.05] * n,
        horizon_confidence=[1.0] * n,
        pv_forecast=[0.0] * n,
        base_load_forecast=[0.0] * n,
    )


def _minimal_config() -> MimirheimConfig:
    """Return a MimirheimConfig with no devices."""
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        outputs=OutputsConfig(
            schedule="mimir/schedule",
            current="mimir/current",
            last_solve="mimir/status",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
    )


def test_quarter_hourly_horizon_dt() -> None:
    """96 steps maps to a 15-minute (0.25 hour) time step."""
    assert _dt_from_horizon(96) == pytest.approx(0.25)


def test_non_standard_horizon_dt() -> None:
    """Any number of steps always returns 0.25: time step is fixed at 15 minutes."""
    assert _dt_from_horizon(24) == pytest.approx(0.25)
    assert _dt_from_horizon(48) == pytest.approx(0.25)


def test_horizon_length_matches_prices() -> None:
    """The schedule produced by build_and_solve has one step per price entry."""
    n = 96
    bundle = _minimal_bundle(n)
    config = _minimal_config()
    result = build_and_solve(bundle, config)
    assert len(result.schedule) == n
