"""Unit tests for mimirheim/devices/derating.py: the shared derating ceiling lines.

These tests check the line arithmetic alone. The solver constraints built from
the lines are covered by the battery and hybrid inverter constraint tests, not
here.
"""

from __future__ import annotations

import pytest

from mimirheim.devices.derating import charge_ceiling_line, discharge_ceiling_line


def test_charge_line_runs_from_max_power_at_the_threshold_to_min_power_at_capacity() -> None:
    """The charge ceiling passes through both configured points and between them."""
    line = charge_ceiling_line(max_kw=5.0, capacity_kwh=10.0, above_kwh=8.0, min_kw=1.0)
    assert line is not None
    slope, intercept = line
    assert slope * 8.0 + intercept == pytest.approx(5.0)
    assert slope * 10.0 + intercept == pytest.approx(1.0)
    assert slope * 9.5 + intercept == pytest.approx(2.0)


def test_discharge_line_runs_from_min_power_at_the_floor_to_max_power_at_the_threshold() -> None:
    """The discharge ceiling has a positive slope through both configured points."""
    line = discharge_ceiling_line(max_kw=4.0, min_soc_kwh=0.5, below_kwh=2.0, min_kw=0.7)
    assert line is not None
    slope, intercept = line
    assert slope > 0.0
    assert slope * 2.0 + intercept == pytest.approx(4.0)
    assert slope * 0.5 + intercept == pytest.approx(0.7)


@pytest.mark.parametrize("above,min_kw", [(None, 1.0), (8.0, None), (None, None)])
def test_charge_line_is_none_without_derating(above: float | None, min_kw: float | None) -> None:
    """Without both charge derating fields there is no line."""
    assert charge_ceiling_line(max_kw=5.0, capacity_kwh=10.0, above_kwh=above, min_kw=min_kw) is None


@pytest.mark.parametrize("below,min_kw", [(None, 1.0), (2.0, None), (None, None)])
def test_discharge_line_is_none_without_derating(below: float | None, min_kw: float | None) -> None:
    """Without both discharge derating fields there is no line."""
    assert discharge_ceiling_line(max_kw=4.0, min_soc_kwh=0.5, below_kwh=below, min_kw=min_kw) is None
