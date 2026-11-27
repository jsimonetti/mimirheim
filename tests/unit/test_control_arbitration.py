"""Unit tests for mimirheim.core.control_arbitration.assign_control_authority.

All tests in this file are written before the implementation exists and must
fail with ImportError or AttributeError until control_arbitration.py is created.

The tests cover:

- Non-zero-exchange steps: no enforcer is needed; all capable devices receive False.
- Single-device selection: the only eligible device becomes enforcer.
- Multi-device scoring: efficiency and headroom select the correct winner.
- EV exclusion when unplugged.
- Loadbalance behavior: always active when plugged, suppressed when battery enforces.
- EV with both zero_exchange and loadbalance: zero_exchange takes priority.
- Hysteresis: switch_delta prevents oscillation when scores are close.
- Minimum dwell: once selected, a device stays enforcer for at least
  min_enforcer_dwell_steps consecutive steps.
- Deterministic tie-break by device type priority then name.
- Passthrough: a suppressed-dispatch schedule has flags correctly set.
- SoC- and hold-aware battery eligibility: full, empty, battery-care hold,
  hold at the top, idle threshold, derating, and release mid-dwell.
- Hybrid inverters: SoC gate near full or empty, derated absorb headroom,
  carry-forward of the last known SoC.
"""

from datetime import datetime, timezone

import pytest

from mimirheim.config.schema import (
    MimirheimConfig,
)
from mimirheim.core.bundle import DeviceSetpoint, EvInputs, ScheduleStep, SolveBundle, SolveResult
from mimirheim.core.control_arbitration import (
    _TYPE_PRIORITY,
    _collect_zex_capable,
    _efficiency_at_power,
    _max_charge_kw,
    assign_control_authority,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)


def _minimal_config_dict() -> dict:
    """Return the minimal raw dict for a valid MimirheimConfig."""
    return {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "objectives": {},
        "mqtt": {"host": "localhost", "client_id": "mimirheim"},
        "outputs": {
            "schedule": "mimir/strategy/schedule",
            "current": "mimir/strategy/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "static_loads": {
            "base_load": {"topic_forecast": "mimir/input/base_load_forecast"},
        },
    }


def _config_with_battery_zex(exchange_epsilon_kw: float = 0.05) -> MimirheimConfig:
    """MimirheimConfig with a single battery that has zero_exchange enabled."""
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    raw["control"] = {"exchange_epsilon_kw": exchange_epsilon_kw}
    return MimirheimConfig.model_validate(raw)


def _config_with_battery_and_pv_zex() -> MimirheimConfig:
    """MimirheimConfig with a battery (zero_exchange) and PV (zero_export)."""
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    raw["pv_arrays"] = {
        "pv": {
            "max_power_kw": 6.0,
            "topic_forecast": "mimir/input/pv_forecast",
            "capabilities": {"zero_export": True},
            "outputs": {"zero_export_mode": "mimir/pv/zero_export_mode"},
        }
    }
    return MimirheimConfig.model_validate(raw)


def _config_with_two_batteries(
    bat1_efficiency: float = 0.95,
    bat2_efficiency: float = 0.90,
    bat1_max_kw: float = 5.0,
    bat2_max_kw: float = 5.0,
) -> MimirheimConfig:
    """MimirheimConfig with two batteries, both zero_exchange capable."""
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat1": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": bat1_max_kw, "efficiency": bat1_efficiency}],
            "discharge_segments": [{"power_max_kw": bat1_max_kw, "efficiency": bat1_efficiency}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat1/exchange_mode"},
        },
        "bat2": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": bat2_max_kw, "efficiency": bat2_efficiency}],
            "discharge_segments": [{"power_max_kw": bat2_max_kw, "efficiency": bat2_efficiency}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat2/exchange_mode"},
        },
    }
    return MimirheimConfig.model_validate(raw)


def _config_with_ev_zex_and_loadbalance() -> MimirheimConfig:
    """MimirheimConfig with an EV that has both zero_exchange and loadbalance enabled."""
    raw = _minimal_config_dict()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "capabilities": {"zero_exchange": True, "v2h": True, "loadbalance": True},
            "outputs": {
                "exchange_mode": "mimir/ev1/exchange_mode",
                "loadbalance_cmd": "mimir/ev1/loadbalance_cmd",
            },
        }
    }
    return MimirheimConfig.model_validate(raw)


def _config_with_battery_zex_and_ev_loadbalance() -> MimirheimConfig:
    """MimirheimConfig with a battery (zero_exchange) and an EV (loadbalance only)."""
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "capabilities": {"loadbalance": True},
            "outputs": {"loadbalance_cmd": "mimir/ev1/loadbalance_cmd"},
        }
    }
    return MimirheimConfig.model_validate(raw)


def _minimal_bundle(ev_available: bool = True, ev_name: str = "ev1") -> SolveBundle:
    """Build a minimal SolveBundle with one optional EV."""
    ev_inputs: dict = {}
    if ev_name:
        ev_inputs[ev_name] = EvInputs(soc_kwh=10.0, available=ev_available)
    return SolveBundle.model_validate(
        {
            "solve_time_utc": _T0.isoformat(),
            "strategy": "minimize_cost",
            "horizon_prices": [0.20],
            "horizon_export_prices": [0.10],
            "horizon_confidence": [1.0],
            "pv_forecast": [0.0],
            "base_load_forecast": [0.5],
            "battery_inputs": {},
            "ev_inputs": ev_inputs,
        }
    )


def _minimal_bundle_no_ev() -> SolveBundle:
    return _minimal_bundle(ev_available=False, ev_name="")


def _step(
    t: int,
    grid_import_kw: float,
    grid_export_kw: float,
    devices: dict[str, DeviceSetpoint],
) -> ScheduleStep:
    return ScheduleStep(
        t=t,
        grid_import_kw=grid_import_kw,
        grid_export_kw=grid_export_kw,
        devices=devices,
    )


def _result(steps: list[ScheduleStep]) -> SolveResult:
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.0,
        solve_status="optimal",
        schedule=steps,
    )


# ---------------------------------------------------------------------------
# Non-zero-exchange steps
# ---------------------------------------------------------------------------


def test_no_enforcer_on_nonzero_exchange_step() -> None:
    """All capable devices receive zero_exchange_active=False when grid exchange is nonzero."""
    config = _config_with_battery_zex()
    bundle = _minimal_bundle_no_ev()
    step = _step(
        t=0,
        grid_import_kw=2.0,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(kw=-1.0, type="battery", zero_exchange_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["bat"].zero_exchange_active is False


def test_nonzero_export_step_clears_all_capable_devices() -> None:
    """grid_export_kw above epsilon clears all capable devices (enforcer not needed)."""
    config = _config_with_battery_and_pv_zex()
    bundle = _minimal_bundle_no_ev()
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=1.0,
        devices={
            "bat": DeviceSetpoint(kw=-2.0, type="battery", zero_exchange_active=False),
            "pv": DeviceSetpoint(kw=3.0, type="pv", zero_exchange_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["bat"].zero_exchange_active is False
    assert result.schedule[0].devices["pv"].zero_exchange_active is False


# ---------------------------------------------------------------------------
# Single-battery selection
# ---------------------------------------------------------------------------


def test_single_battery_zero_exchange_selected() -> None:
    """A single battery with headroom above margin is selected as enforcer."""
    config = _config_with_battery_zex()
    bundle = _minimal_bundle_no_ev()
    # Battery at 0 kW has full 5 kW headroom (above default 0.10 margin).
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={"bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False)},
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["bat"].zero_exchange_active is True


def test_ineligible_device_below_headroom_margin_excluded() -> None:
    """A battery with headroom below headroom_margin_kw is not selected as enforcer."""
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            # Single segment: max 0.05 kW, so headroom at max discharge is 0.05 kW
            # which is below the default headroom_margin_kw of 0.10.
            "charge_segments": [{"power_max_kw": 0.05, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 0.05, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle_no_ev()
    # Battery at max discharge (kw=0.05): headroom = 0.05 - 0.0 + 0.05 = 0.10... borderline.
    # Use kw=0.05 (full discharge) → charge_kw=0.0, discharge_kw=0.05, max_charge=0.05
    # headroom = 0.05 - 0.0 + 0.05 = 0.10. Exactly at margin. Use kw > max (force low headroom).
    # Instead just configure margin above the device capacity:
    raw["control"] = {"headroom_margin_kw": 1.0}
    config = MimirheimConfig.model_validate(raw)
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={"bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False)},
    )
    result = assign_control_authority(_result([step]), bundle, config)
    # With headroom_margin_kw=1.0 and max_charge=0.05, headroom=0.05 < 1.0: not eligible.
    assert result.schedule[0].devices["bat"].zero_exchange_active is False


# ---------------------------------------------------------------------------
# Battery preferred over PV
# ---------------------------------------------------------------------------


def test_battery_preferred_over_pv_when_both_eligible() -> None:
    """Battery always outscores PV (PV score is 0.0) when both are eligible."""
    config = _config_with_battery_and_pv_zex()
    bundle = _minimal_bundle_no_ev()
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            "pv": DeviceSetpoint(kw=3.0, type="pv", zero_exchange_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["bat"].zero_exchange_active is True
    assert result.schedule[0].devices["pv"].zero_exchange_active is False


# ---------------------------------------------------------------------------
# Efficiency-aware scoring
# ---------------------------------------------------------------------------


def test_higher_efficiency_device_wins_when_headroom_similar() -> None:
    """bat1 (efficiency 0.95) beats bat2 (efficiency 0.90) when headroom is equal."""
    config = _config_with_two_batteries(
        bat1_efficiency=0.95,
        bat2_efficiency=0.90,
        bat1_max_kw=5.0,
        bat2_max_kw=5.0,
    )
    bundle = _minimal_bundle_no_ev()
    # Both at 0 kW: identical headroom of 5.0 kW. Efficiency score decides.
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "bat1": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            "bat2": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["bat1"].zero_exchange_active is True
    assert result.schedule[0].devices["bat2"].zero_exchange_active is False


# ---------------------------------------------------------------------------
# EV exclusion when unplugged
# ---------------------------------------------------------------------------


def test_ev_excluded_when_unplugged() -> None:
    """An EV with available=False is not eligible as enforcer; battery takes over."""
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "capabilities": {"zero_exchange": True, "v2h": True},
            "outputs": {"exchange_mode": "mimir/ev1/exchange_mode"},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle(ev_available=False, ev_name="ev1")
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            "ev1": DeviceSetpoint(kw=0.0, type="ev_charger", zero_exchange_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["bat"].zero_exchange_active is True
    assert result.schedule[0].devices["ev1"].zero_exchange_active is False


# ---------------------------------------------------------------------------
# Loadbalance behavior
# ---------------------------------------------------------------------------


def test_loadbalance_ev_always_active_when_plugged() -> None:
    """An EV with only loadbalance (no battery present) gets loadbalance_active=True on all steps.

    The "always active" guarantee applies when no battery zero_exchange enforcer
    is present to suppress it. When the battery is absent, both near-zero-exchange
    and non-zero-exchange steps leave the EV as the sole loadbalance device, and
    it stays active throughout.
    """
    raw = _minimal_config_dict()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "capabilities": {"loadbalance": True},
            "outputs": {"loadbalance_cmd": "mimir/ev1/loadbalance_cmd"},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle(ev_available=True, ev_name="ev1")
    # Two steps: one near-zero exchange, one with clear import.
    steps = [
        _step(
            t=0,
            grid_import_kw=0.0,
            grid_export_kw=0.0,
            devices={
                "ev1": DeviceSetpoint(kw=-3.0, type="ev_charger", loadbalance_active=False),
            },
        ),
        _step(
            t=1,
            grid_import_kw=1.5,
            grid_export_kw=0.0,
            devices={
                "ev1": DeviceSetpoint(kw=-3.0, type="ev_charger", loadbalance_active=False),
            },
        ),
    ]
    result = assign_control_authority(_result(steps), bundle, config)
    # No battery enforcer: loadbalance_active must be True on both steps.
    assert result.schedule[0].devices["ev1"].loadbalance_active is True
    assert result.schedule[1].devices["ev1"].loadbalance_active is True


def test_loadbalance_ev_suppressed_when_battery_closed_loop_enforcer() -> None:
    """EV loadbalance_active is False on steps where the battery is the zero_exchange enforcer."""
    config = _config_with_battery_zex_and_ev_loadbalance()
    bundle = _minimal_bundle(ev_available=True, ev_name="ev1")
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            "ev1": DeviceSetpoint(kw=-3.0, type="ev_charger", loadbalance_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["bat"].zero_exchange_active is True
    assert result.schedule[0].devices["ev1"].loadbalance_active is False


def test_ev_with_both_closed_loop_and_loadbalance_closed_loop_takes_priority() -> None:
    """When an EV has both zero_exchange and loadbalance, and is selected as enforcer,
    zero_exchange_active=True and loadbalance_active=False for that step."""
    config = _config_with_ev_zex_and_loadbalance()
    bundle = _minimal_bundle(ev_available=True, ev_name="ev1")
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "ev1": DeviceSetpoint(
                kw=0.0, type="ev_charger",
                zero_exchange_active=False,
                loadbalance_active=False,
            ),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["ev1"].zero_exchange_active is True
    assert result.schedule[0].devices["ev1"].loadbalance_active is False


# ---------------------------------------------------------------------------
# Hysteresis and dwell
# ---------------------------------------------------------------------------


def test_hysteresis_prevents_flapping() -> None:
    """Enforcer does not switch when challenger score exceeds current by less than switch_delta."""
    # bat1 efficiency 0.95, bat2 efficiency 0.94 — very close.
    # switch_delta=0.10 is larger than the score difference, so bat1 stays enforcer
    # even after being selected on step 0. No step-to-step switch should occur.
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat1": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat1/exchange_mode"},
        },
        "bat2": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.94}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.94}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat2/exchange_mode"},
        },
    }
    raw["control"] = {"switch_delta": 0.10}
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle_no_ev()

    def _zex_step(t: int) -> ScheduleStep:
        return _step(
            t=t,
            grid_import_kw=0.0,
            grid_export_kw=0.0,
            devices={
                "bat1": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
                "bat2": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            },
        )

    result = assign_control_authority(_result([_zex_step(0), _zex_step(1)]), bundle, config)
    # bat1 wins on step 0 (higher efficiency, similar headroom).
    # On step 1, bat2 does not exceed bat1's score by switch_delta, so bat1 stays.
    assert result.schedule[0].devices["bat1"].zero_exchange_active is True
    assert result.schedule[1].devices["bat1"].zero_exchange_active is True
    assert result.schedule[0].devices["bat2"].zero_exchange_active is False
    assert result.schedule[1].devices["bat2"].zero_exchange_active is False


def test_min_dwell_respected() -> None:
    """Once selected, enforcer persists for at least min_enforcer_dwell_steps steps."""
    raw = _minimal_config_dict()
    # bat1 has lower efficiency — it would not normally win on later steps.
    # But it becomes enforcer on step 0 via dwell, so it should stay until dwell expires.
    raw["batteries"] = {
        "bat1": {
            "capacity_kwh": 10.0,
            # Low efficiency: bat1 would not normally beat bat2.
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.80}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.80}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat1/exchange_mode"},
        },
        "bat2": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat2/exchange_mode"},
        },
    }
    # min_enforcer_dwell_steps=3, switch_delta=0. Score order: bat2 > bat1 always.
    # bat2 wins on step 0. With min_dwell=3, bat2 should remain enforcer through step 2
    # even if we somehow contrive a challenge (not possible here since bat2 always scores higher,
    # so this test verifies dwell doesn't break the normal case).
    raw["control"] = {"min_enforcer_dwell_steps": 3, "switch_delta": 0.0}
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle_no_ev()

    def _zex_step(t: int) -> ScheduleStep:
        return _step(
            t=t,
            grid_import_kw=0.0,
            grid_export_kw=0.0,
            devices={
                "bat1": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
                "bat2": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            },
        )

    result = assign_control_authority(
        _result([_zex_step(0), _zex_step(1), _zex_step(2), _zex_step(3)]),
        bundle,
        config,
    )
    # bat2 wins outright (higher efficiency). Dwell ensures it holds.
    for t in range(4):
        assert result.schedule[t].devices["bat2"].zero_exchange_active is True
        assert result.schedule[t].devices["bat1"].zero_exchange_active is False


# ---------------------------------------------------------------------------
# Deterministic tie-break
# ---------------------------------------------------------------------------


def test_deterministic_tie_break_by_name() -> None:
    """When all scoring levels tie, the device with higher type priority wins.
    Between two identical batteries, the one that sorts first alphabetically wins."""
    raw = _minimal_config_dict()
    # bat_aaa and bat_zzz: identical efficiency, capacity, and headroom.
    for name in ("bat_aaa", "bat_zzz"):
        if "batteries" not in raw:
            raw["batteries"] = {}
        raw["batteries"][name] = {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "wear_cost_eur_per_kwh": 0.05,
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": f"mimir/{name}/exchange_mode"},
        }
    raw["control"] = {"switch_delta": 0.0}
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle_no_ev()
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "bat_aaa": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            "bat_zzz": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    # Both are batteries with identical scores. Name sort: bat_aaa < bat_zzz,
    # but the tie-break prefers the device with the lower name (alphabetically first = bat_aaa).
    enforcer = [
        name for name, sp in result.schedule[0].devices.items()
        if sp.zero_exchange_active is True
    ]
    assert len(enforcer) == 1, "Exactly one enforcer must be selected."
    # The result must be deterministic regardless of dict iteration order.
    # Both calls with the same config must produce the same winner.
    result2 = assign_control_authority(_result([step]), bundle, config)
    enforcer2 = [
        name for name, sp in result2.schedule[0].devices.items()
        if sp.zero_exchange_active is True
    ]
    assert enforcer == enforcer2


# ---------------------------------------------------------------------------
# Passthrough for suppressed schedule
# ---------------------------------------------------------------------------


def test_dispatch_suppressed_schedule_passthrough() -> None:
    """assign_control_authority applies flag logic even when dispatch_suppressed=True."""
    config = _config_with_battery_zex()
    bundle = _minimal_bundle_no_ev()
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={"bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False)},
    )
    suppressed = SolveResult(
        strategy="minimize_cost",
        objective_value=0.0,
        solve_status="optimal",
        dispatch_suppressed=True,
        schedule=[step],
    )
    result = assign_control_authority(suppressed, bundle, config)
    assert result.dispatch_suppressed is True
    assert result.schedule[0].devices["bat"].zero_exchange_active is True


# ---------------------------------------------------------------------------
# Infeasible result passthrough
# ---------------------------------------------------------------------------


def test_infeasible_result_returned_unchanged() -> None:
    """An infeasible SolveResult is returned without modification."""
    config = _config_with_battery_zex()
    bundle = _minimal_bundle_no_ev()
    infeasible = SolveResult(
        strategy="minimize_cost",
        objective_value=0.0,
        solve_status="infeasible",
        schedule=[],
    )
    result = assign_control_authority(infeasible, bundle, config)
    assert result is infeasible


# ---------------------------------------------------------------------------
# Hybrid inverter arbitration (Plan 55)
# ---------------------------------------------------------------------------


def _config_with_hybrid_zex(
    max_charge_kw: float = 5.0,
    inverter_efficiency: float = 0.97,
    wear_cost: float = 0.0,
) -> MimirheimConfig:
    """MimirheimConfig with a hybrid inverter that has zero_exchange enabled."""
    raw = _minimal_config_dict()
    raw["hybrid_inverters"] = {
        "hi": {
            "capacity_kwh": 10.0,
            "max_charge_kw": max_charge_kw,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 6.0,
            "wear_cost_eur_per_kwh": wear_cost,
            "inverter_efficiency": inverter_efficiency,
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/hi/exchange_mode"},
        }
    }
    return MimirheimConfig.model_validate(raw)


def _config_with_battery_and_hybrid_zex(
    bat_wear_cost: float = 0.05,
    hi_wear_cost: float = 0.05,
) -> MimirheimConfig:
    """MimirheimConfig with both a battery and a hybrid inverter, both zero_exchange capable."""
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "wear_cost_eur_per_kwh": bat_wear_cost,
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    raw["hybrid_inverters"] = {
        "hi": {
            "capacity_kwh": 10.0,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 6.0,
            "inverter_efficiency": 0.95,
            "wear_cost_eur_per_kwh": hi_wear_cost,
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/hi/exchange_mode"},
        }
    }
    return MimirheimConfig.model_validate(raw)


def test_hybrid_inverter_included_in_zex_capable() -> None:
    """A hybrid inverter with zero_exchange=True appears in _collect_zex_capable."""
    config = _config_with_hybrid_zex()
    capable = _collect_zex_capable(config)
    assert "hi" in capable


def test_hybrid_inverter_not_in_zex_capable_when_flag_false() -> None:
    """A hybrid inverter with zero_exchange=False is absent from _collect_zex_capable."""
    raw = _minimal_config_dict()
    raw["hybrid_inverters"] = {
        "hi": {
            "capacity_kwh": 10.0,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 6.0,
            "capabilities": {"zero_exchange": False},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    capable = _collect_zex_capable(config)
    assert "hi" not in capable


def test_hybrid_inverter_max_charge_kw_uses_ac_side() -> None:
    """_max_charge_kw for a hybrid_inverter returns max_charge_kw / inverter_efficiency.

    The AC-side import limit is the DC charge limit divided by inverter efficiency.
    DeviceSetpoint.kw is net AC power, so the headroom calculation must use the
    AC limit, not the DC-bus limit.
    """
    config = _config_with_hybrid_zex(max_charge_kw=5.0, inverter_efficiency=0.97)
    expected_ac_limit = 5.0 / 0.97
    result = _max_charge_kw("hi", "hybrid_inverter", config)
    assert abs(result - expected_ac_limit) < 1e-9


def test_hybrid_inverter_max_charge_kw_unknown_name_returns_zero() -> None:
    """_max_charge_kw returns 0.0 for an unknown hybrid inverter name."""
    config = _config_with_hybrid_zex()
    assert _max_charge_kw("nonexistent", "hybrid_inverter", config) == 0.0


def test_hybrid_inverter_efficiency_uses_inverter_efficiency() -> None:
    """_efficiency_at_power for a hybrid_inverter returns config.inverter_efficiency."""
    config = _config_with_hybrid_zex(inverter_efficiency=0.97)
    result = _efficiency_at_power("hi", "hybrid_inverter", power_kw=3.0, config=config)
    assert abs(result - 0.97) < 1e-9


def test_hybrid_inverter_type_priority_equals_ev() -> None:
    """_TYPE_PRIORITY["hybrid_inverter"] == 2 (same as ev_charger, above pv)."""
    assert _TYPE_PRIORITY.get("hybrid_inverter") == 2
    assert _TYPE_PRIORITY.get("hybrid_inverter") > _TYPE_PRIORITY.get("pv", 1)


def test_hybrid_inverter_selected_as_enforcer_when_only_candidate() -> None:
    """assign_control_authority sets zero_exchange_active=True on a near-zero-exchange
    step when the hybrid inverter is the only capable device and has sufficient headroom.
    """
    config = _config_with_hybrid_zex(max_charge_kw=5.0, inverter_efficiency=0.97)
    bundle = _minimal_bundle_no_ev()
    # Hybrid inverter at 0 kW AC: headroom = max_charge_kw / η = ~5.15 kW (above margin).
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={"hi": DeviceSetpoint(kw=0.0, type="hybrid_inverter", zero_exchange_active=False)},
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["hi"].zero_exchange_active is True


def test_hybrid_inverter_not_selected_on_nonzero_exchange_step() -> None:
    """zero_exchange_active remains False on non-zero-exchange steps."""
    config = _config_with_hybrid_zex()
    bundle = _minimal_bundle_no_ev()
    step = _step(
        t=0,
        grid_import_kw=2.0,
        grid_export_kw=0.0,
        devices={"hi": DeviceSetpoint(kw=-1.0, type="hybrid_inverter", zero_exchange_active=False)},
    )
    result = assign_control_authority(_result([step]), bundle, config)
    assert result.schedule[0].devices["hi"].zero_exchange_active is False


def test_battery_preferred_over_hybrid_inverter_same_efficiency() -> None:
    """When a battery and hybrid inverter have identical efficiency and headroom,
    the battery wins because its type priority (3) > hybrid inverter (2).

    To produce a true tie at levels 1 (efficiency) and 2 (headroom), the AC-side
    charge limit for both devices must be equal. The hybrid inverter's AC limit
    is max_charge_kw / inverter_efficiency; setting that equal to the battery's
    segment sum (5.0 kW) requires max_charge_kw = 5.0 * 0.95 = 4.75 kW.
    """
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    raw["hybrid_inverters"] = {
        "hi": {
            "capacity_kwh": 10.0,
            # max_charge_kw / inverter_efficiency = 4.75 / 0.95 = 5.0 kW (AC),
            # matching the battery's 5.0 kW segment sum exactly.
            "max_charge_kw": 4.75,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 6.0,
            "inverter_efficiency": 0.95,
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/hi/exchange_mode"},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle_no_ev()
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            "hi": DeviceSetpoint(kw=0.0, type="hybrid_inverter", zero_exchange_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    # Battery type priority (3) > hybrid inverter (2): battery wins.
    assert result.schedule[0].devices["bat"].zero_exchange_active is True
    assert result.schedule[0].devices["hi"].zero_exchange_active is False


def test_hybrid_inverter_wear_cost_penalises_selection() -> None:
    """When a battery has lower wear cost than a hybrid inverter, and headroom is
    equal, the battery wins because lower wear cost produces a higher wear_penalty
    value (level 3), and the battery also has higher type priority (level 4).

    Equal headroom requires matching AC-side charge limits:
        battery: 5.0 kW (segment sum)
        hybrid:  max_charge_kw / inverter_efficiency = 4.75 / 0.95 = 5.0 kW AC

    Efficiency scores are equal (0.95 for both), so the win is decided at
    wear_penalty (level 3): battery (0.02) beats hybrid (0.10).
    """
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "wear_cost_eur_per_kwh": 0.02,
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    raw["hybrid_inverters"] = {
        "hi": {
            "capacity_kwh": 10.0,
            "max_charge_kw": 4.75,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 6.0,
            "inverter_efficiency": 0.95,
            "wear_cost_eur_per_kwh": 0.10,
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/hi/exchange_mode"},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle_no_ev()
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
            "hi": DeviceSetpoint(kw=0.0, type="hybrid_inverter", zero_exchange_active=False),
        },
    )
    result = assign_control_authority(_result([step]), bundle, config)
    # Battery wear cost (0.02) < hybrid wear cost (0.10): battery wins.
    assert result.schedule[0].devices["bat"].zero_exchange_active is True
    assert result.schedule[0].devices["hi"].zero_exchange_active is False



# ---------------------------------------------------------------------------
# EV with both zero_exchange and loadbalance, when it is NOT the enforcer
# ---------------------------------------------------------------------------


def _config_with_battery_zex_and_dual_capability_ev() -> MimirheimConfig:
    """A battery with zero_exchange plus an EV with zero_exchange and loadbalance.

    The schema documents loadbalance as orthogonal to zero_exchange, so this
    combination is valid. It is the case where a dual-capability EV loses the
    enforcer role to a battery and should fall back to load balancing.
    """
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.99}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.99}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
        }
    }
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.80}],
            "capabilities": {"zero_exchange": True, "v2h": True, "loadbalance": True},
            "outputs": {
                "exchange_mode": "mimir/ev1/exchange_mode",
                "loadbalance_cmd": "mimir/ev1/loadbalance_cmd",
            },
        }
    }
    return MimirheimConfig.model_validate(raw)


def _dual_capability_devices() -> dict[str, DeviceSetpoint]:
    return {
        "bat": DeviceSetpoint(kw=0.0, type="battery", zero_exchange_active=False),
        "ev1": DeviceSetpoint(
            kw=0.0,
            type="ev_charger",
            zero_exchange_active=False,
            loadbalance_active=False,
        ),
    }


def test_dual_capability_ev_loadbalances_on_non_zero_exchange_steps() -> None:
    """A plugged-in dual-capability EV must load balance when no enforcer is needed.

    On a step with real grid exchange no closed-loop enforcer is selected, so
    the EV's zero_exchange register is cleared. Its loadbalance capability is
    independent and the vehicle is plugged in, so load balancing applies.
    An EV declaring only loadbalance already behaves this way; declaring
    zero_exchange as well must not silently disable it.
    """
    config = _config_with_battery_zex_and_dual_capability_ev()
    bundle = _minimal_bundle(ev_available=True, ev_name="ev1")
    step = _step(
        t=0,
        grid_import_kw=3.0,
        grid_export_kw=0.0,
        devices=_dual_capability_devices(),
    )

    result = assign_control_authority(_result([step]), bundle, config)

    ev = result.schedule[0].devices["ev1"]
    assert ev.zero_exchange_active is False
    assert ev.loadbalance_active is True


def test_dual_capability_ev_loadbalances_when_a_pv_array_enforces() -> None:
    """Losing the enforcer role to a PV array must leave load balancing on.

    Load balancing is suppressed only when a battery holds the enforcer role,
    because a battery's closed-loop controller and an EVSE load balancer
    regulate the same grid current. A PV array curtailing its own production
    does not conflict with an EVSE clamping its charge power.

    The EV is pinned at full charge power so its absorption headroom is zero
    and it drops out of enforcer selection, leaving the PV array as the only
    eligible candidate.
    """
    raw = _minimal_config_dict()
    raw["pv_arrays"] = {
        "pv": {
            "topic_forecast": "mimir/input/pv/forecast",
            "max_power_kw": 5.0,
            "capabilities": {"zero_export": True},
            "outputs": {"zero_export_mode": "mimir/pv/zero_export_mode"},
        }
    }
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.80}],
            "capabilities": {"zero_exchange": True, "v2h": True, "loadbalance": True},
            "outputs": {
                "exchange_mode": "mimir/ev1/exchange_mode",
                "loadbalance_cmd": "mimir/ev1/loadbalance_cmd",
            },
        }
    }
    config = MimirheimConfig.model_validate(raw)
    bundle = _minimal_bundle(ev_available=True, ev_name="ev1")
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "pv": DeviceSetpoint(kw=2.0, type="pv", zero_exchange_active=False),
            "ev1": DeviceSetpoint(
                kw=-11.0,
                type="ev_charger",
                zero_exchange_active=False,
                loadbalance_active=False,
            ),
        },
    )

    result = assign_control_authority(_result([step]), bundle, config)

    assert result.schedule[0].devices["pv"].zero_exchange_active is True
    ev = result.schedule[0].devices["ev1"]
    assert ev.zero_exchange_active is False
    assert ev.loadbalance_active is True


def test_dual_capability_ev_loadbalance_suppressed_when_battery_enforces() -> None:
    """A battery holding the enforcer role must still suppress EV load balancing.

    Both controllers regulate the same grid current, so only one may be
    authoritative. This is the rule that makes the dual-capability case worth
    getting right rather than simply always enabling load balancing.
    """
    config = _config_with_battery_zex_and_dual_capability_ev()
    bundle = _minimal_bundle(ev_available=True, ev_name="ev1")
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices=_dual_capability_devices(),
    )

    result = assign_control_authority(_result([step]), bundle, config)

    assert result.schedule[0].devices["bat"].zero_exchange_active is True
    ev = result.schedule[0].devices["ev1"]
    assert ev.zero_exchange_active is False
    assert ev.loadbalance_active is False


def test_dual_capability_ev_not_loadbalanced_when_unplugged() -> None:
    """Load balancing is meaningless with no vehicle connected."""
    config = _config_with_battery_zex_and_dual_capability_ev()
    bundle = _minimal_bundle(ev_available=False, ev_name="ev1")
    step = _step(
        t=0,
        grid_import_kw=3.0,
        grid_export_kw=0.0,
        devices=_dual_capability_devices(),
    )

    result = assign_control_authority(_result([step]), bundle, config)

    assert result.schedule[0].devices["ev1"].loadbalance_active is False


# ---------------------------------------------------------------------------
# SoC- and hold-aware battery eligibility
#
# A battery is a zero-exchange candidate only when it can regulate in both
# directions around zero (absorb a surplus and cover a deficit of at least
# headroom_margin_kw) and the step is not inside a battery-care hold. An idle
# or charging step at the top of the range has no absorb headroom. Each test asserts
# the complete assignment of the step: zero_exchange_active and
# loadbalance_active of every device.
# ---------------------------------------------------------------------------


def _config_soc_aware(
    *,
    with_pv: bool = True,
    with_lb_ev: bool = False,
    control: dict | None = None,
    battery_extra: dict | None = None,
) -> MimirheimConfig:
    """Battery (10 kWh, min 1 kWh, 5 kW both ways) with zero_exchange, plus options.

    Args:
        with_pv: Add a PV array with zero_export.
        with_lb_ev: Add an EV with loadbalance only.
        control: Raw ``control`` block.
        battery_extra: Extra raw battery fields (derating).
    """
    raw = _minimal_config_dict()
    raw["batteries"] = {
        "bat": {
            "capacity_kwh": 10.0,
            "min_soc_kwh": 1.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/bat/exchange_mode"},
            **(battery_extra or {}),
        }
    }
    if with_pv:
        raw["pv_arrays"] = {
            "pv": {
                "max_power_kw": 6.0,
                "topic_forecast": "mimir/input/pv_forecast",
                "capabilities": {"zero_export": True},
                "outputs": {"zero_export_mode": "mimir/pv/zero_export_mode"},
            }
        }
    if with_lb_ev:
        raw["ev_chargers"] = {
            "ev1": {
                "capacity_kwh": 52.0,
                "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
                "capabilities": {"loadbalance": True},
                "outputs": {"loadbalance_cmd": "mimir/ev1/loadbalance_cmd"},
            }
        }
    if control is not None:
        raw["control"] = control
    return MimirheimConfig.model_validate(raw)


def _bundle_with_battery_soc(soc_kwh: float, ev_available: bool | None = None) -> SolveBundle:
    """Bundle carrying the measured battery SoC and optionally a plugged-in EV."""
    ev_inputs: dict = {}
    if ev_available is not None:
        ev_inputs["ev1"] = EvInputs(soc_kwh=10.0, available=ev_available)
    return SolveBundle.model_validate(
        {
            "solve_time_utc": _T0.isoformat(),
            "strategy": "minimize_cost",
            "horizon_prices": [0.20],
            "horizon_export_prices": [0.10],
            "horizon_confidence": [1.0],
            "pv_forecast": [0.0],
            "base_load_forecast": [0.5],
            "battery_inputs": {"bat": {"soc_kwh": soc_kwh}},
            "ev_inputs": ev_inputs,
        }
    )


def _zex_step(t: int, bat_kw: float, bat_soc_kwh: float | None, *, ev_kw: float | None = None) -> ScheduleStep:
    """Near-zero-exchange step with the battery at the given setpoint and PV producing 3 kW."""
    devices = {
        "bat": DeviceSetpoint(kw=bat_kw, type="battery", soc_kwh=bat_soc_kwh),
        "pv": DeviceSetpoint(kw=3.0, type="pv"),
    }
    if ev_kw is not None:
        devices["ev1"] = DeviceSetpoint(kw=ev_kw, type="ev_charger")
    return _step(t=t, grid_import_kw=0.0, grid_export_kw=0.0, devices=devices)


def _care(target_kwh: float, step: int, hold_steps: int) -> dict:
    """battery_care payload for the battery with an enforced hold."""
    from mimirheim.core.bundle import BatteryCareStatus

    return {
        "bat": BatteryCareStatus(
            floor_kwh=0.0,
            enforced_target_kwh=target_kwh,
            enforced_step=step,
            hold_steps=hold_steps,
            enforced_hold_steps=hold_steps,
        )
    }


def _flags(step: ScheduleStep) -> dict[str, tuple[bool | None, bool | None]]:
    """Map every device to its (zero_exchange_active, loadbalance_active) pair."""
    return {
        name: (sp.zero_exchange_active, sp.loadbalance_active)
        for name, sp in step.devices.items()
    }


_BAT_ENFORCES = {"bat": (True, None), "pv": (False, None)}
_PV_ENFORCES = {"bat": (False, None), "pv": (True, None)}


def test_full_idle_battery_yields_to_pv() -> None:
    """A full idle battery cannot absorb a surplus; the PV array enforces."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, 0.0, 10.0)]), _bundle_with_battery_soc(10.0), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_battery_at_min_soc_idle_not_enforcer() -> None:
    """An idle battery at min_soc_kwh cannot cover a deficit."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, 0.0, 1.0)]), _bundle_with_battery_soc(1.0), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_battery_in_care_hold_not_enforcer() -> None:
    """Steps inside the battery-care hold go to the next candidate; after it the battery returns."""
    config = _config_soc_aware()
    steps = [_zex_step(t, 0.0, 9.0) for t in range(3)]
    result = _result(steps).model_copy(update={"battery_care": _care(9.0, step=0, hold_steps=1)})
    out = assign_control_authority(result, _bundle_with_battery_soc(9.0), config)
    assert _flags(out.schedule[0]) == _PV_ENFORCES
    assert _flags(out.schedule[1]) == _PV_ENFORCES
    assert _flags(out.schedule[2]) == _BAT_ENFORCES


def test_top_hold_entered_slightly_low_not_enforcer() -> None:
    """Idle step starting at 99.5 % and ending at 100 % is a hold at the top."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, 0.02, 10.0)]), _bundle_with_battery_soc(9.95), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_charging_to_full_not_enforcer() -> None:
    """A charge that ends at 100 % is a hold at the top; the PV array takes over."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, -1.2, 10.0)]), _bundle_with_battery_soc(9.7), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_discharging_from_full_still_eligible() -> None:
    """A household discharge from 100 % absorbs a surplus by discharging less."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, 2.0, 9.5)]), _bundle_with_battery_soc(10.0), config
    )
    assert _flags(result.schedule[0]) == _BAT_ENFORCES


def test_charging_at_min_soc_still_eligible() -> None:
    """A battery charging at min_soc_kwh covers a deficit by charging less."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, -2.0, 1.5)]), _bundle_with_battery_soc(1.0), config
    )
    assert _flags(result.schedule[0]) == _BAT_ENFORCES


def test_loadbalance_not_suppressed_when_battery_ineligible() -> None:
    """With the full battery out of the running, a plugged-in EV load balances."""
    config = _config_soc_aware(with_lb_ev=True)
    result = assign_control_authority(
        _result([_zex_step(0, 0.0, 10.0, ev_kw=-3.0)]),
        _bundle_with_battery_soc(10.0, ev_available=True),
        config,
    )
    assert _flags(result.schedule[0]) == {
        "bat": (False, None),
        "pv": (True, None),
        "ev1": (None, True),
    }


def test_battery_released_when_ineligible_mid_dwell() -> None:
    """A battery that enters a care hold during its dwell is released at once."""
    config = _config_soc_aware(control={"min_enforcer_dwell_steps": 4})
    steps = [_zex_step(t, 0.0, 5.0) for t in range(4)]
    result = _result(steps).model_copy(update={"battery_care": _care(5.0, step=2, hold_steps=1)})
    out = assign_control_authority(result, _bundle_with_battery_soc(5.0), config)
    assert [_flags(s) for s in out.schedule] == [
        _BAT_ENFORCES,
        _BAT_ENFORCES,
        _PV_ENFORCES,
        _PV_ENFORCES,
    ]


def test_idle_threshold_decides_hold_or_discharge() -> None:
    """Planned power within 0.05 kW of zero is idle, so a full battery holds.

    With headroom_margin_kw 0, a 0.08 kW discharge from 100 % is a real
    discharge (eligible: it absorbs by discharging less), while 0.03 kW is
    solver noise and the battery holds at the top.
    """
    bundle = _bundle_with_battery_soc(10.0)
    config = _config_soc_aware(control={"headroom_margin_kw": 0.0})
    held = assign_control_authority(_result([_zex_step(0, 0.03, 9.9925)]), bundle, config)
    assert _flags(held.schedule[0]) == _PV_ENFORCES
    discharging = assign_control_authority(_result([_zex_step(0, 0.08, 9.98)]), bundle, config)
    assert _flags(discharging.schedule[0]) == _BAT_ENFORCES


def test_charge_derating_removes_absorb_headroom() -> None:
    """Near full, a derated charge limit below the headroom margin makes the battery ineligible."""
    step = _zex_step(0, 0.0, 9.95)
    bundle = _bundle_with_battery_soc(9.95)
    plain = assign_control_authority(_result([step]), bundle, _config_soc_aware())
    assert _flags(plain.schedule[0]) == _BAT_ENFORCES
    derated_config = _config_soc_aware(
        battery_extra={"reduce_charge_above_soc_kwh": 5.0, "reduce_charge_min_kw": 0.01}
    )
    derated = assign_control_authority(_result([step]), bundle, derated_config)
    assert _flags(derated.schedule[0]) == _PV_ENFORCES


def test_care_hold_matched_on_horizon_step_index() -> None:
    """The care hold is located by ScheduleStep.t, not by list position."""
    config = _config_soc_aware()
    steps = [_zex_step(4, 0.0, 5.0), _zex_step(5, 0.0, 5.0)]
    result = _result(steps).model_copy(update={"battery_care": _care(5.0, step=5, hold_steps=0)})
    out = assign_control_authority(result, _bundle_with_battery_soc(5.0), config)
    assert _flags(out.schedule[0]) == _BAT_ENFORCES
    assert _flags(out.schedule[1]) == _PV_ENFORCES


def test_suppressed_schedule_full_battery_ineligible_every_step() -> None:
    """Without per-step SoC the start SoC is carried forward, so a full battery stays full."""
    config = _config_soc_aware()
    steps = [_zex_step(t, 0.0, None) for t in range(3)]
    out = assign_control_authority(_result(steps), _bundle_with_battery_soc(10.0), config)
    assert [_flags(s) for s in out.schedule] == [_PV_ENFORCES] * 3


def test_full_power_discharge_has_no_supply_headroom() -> None:
    """A battery already discharging at its limit cannot cover a further deficit."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, 5.0, 3.75)]), _bundle_with_battery_soc(5.0), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_soc_aware_absorb_headroom_used_for_scoring() -> None:
    """A derated, nearly full battery scores its real absorb headroom, not its configured maximum.

    Both batteries have the same efficiency and wear. Without SoC-aware
    scoring the tie falls to the name, and bat2 (nearly full, derated to about
    0.11 kW of charge) would win over bat1 (half full, 5 kW of room).
    """
    raw = _minimal_config_dict()
    common = {
        "capacity_kwh": 10.0,
        "min_soc_kwh": 1.0,
        "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
        "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
        "capabilities": {"zero_exchange": True},
    }
    raw["batteries"] = {
        "bat1": {**common, "outputs": {"exchange_mode": "mimir/bat1/exchange_mode"}},
        "bat2": {
            **common,
            "reduce_charge_above_soc_kwh": 5.0,
            "reduce_charge_min_kw": 0.01,
            "outputs": {"exchange_mode": "mimir/bat2/exchange_mode"},
        },
    }
    config = MimirheimConfig.model_validate(raw)
    bundle = SolveBundle.model_validate(
        {
            "solve_time_utc": _T0.isoformat(),
            "strategy": "minimize_cost",
            "horizon_prices": [0.20],
            "horizon_export_prices": [0.10],
            "horizon_confidence": [1.0],
            "pv_forecast": [0.0],
            "base_load_forecast": [0.5],
            "battery_inputs": {"bat1": {"soc_kwh": 5.0}, "bat2": {"soc_kwh": 9.9}},
            "ev_inputs": {},
        }
    )
    step = _step(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "bat1": DeviceSetpoint(kw=0.0, type="battery", soc_kwh=5.0),
            "bat2": DeviceSetpoint(kw=0.0, type="battery", soc_kwh=9.9),
        },
    )
    out = assign_control_authority(_result([step]), bundle, config)
    assert _flags(out.schedule[0]) == {"bat1": (True, None), "bat2": (False, None)}


def test_zero_headroom_margin_still_excludes_empty_battery() -> None:
    """With headroom_margin_kw 0 an idle battery at min_soc_kwh still has no supply headroom."""
    config = _config_soc_aware(control={"headroom_margin_kw": 0.0})
    result = assign_control_authority(
        _result([_zex_step(0, 0.0, 1.0)]), _bundle_with_battery_soc(1.0), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_discharge_ending_at_min_soc_has_no_supply_headroom() -> None:
    """A planned discharge that empties the battery by the end of the step cannot cover more."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, 4.0, 1.0)]), _bundle_with_battery_soc(2.0), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_battery_held_at_care_floor_not_enforcer() -> None:
    """The battery-care floor counts as empty: a closed loop would drain below it."""
    from mimirheim.core.bundle import BatteryCareStatus

    config = _config_soc_aware()
    care = {"bat": BatteryCareStatus(floor_kwh=7.0)}
    at_floor = _result([_zex_step(0, 0.0, 7.0)]).model_copy(update={"battery_care": care})
    out = assign_control_authority(at_floor, _bundle_with_battery_soc(7.0), config)
    assert _flags(out.schedule[0]) == _PV_ENFORCES
    above = _result([_zex_step(0, 0.0, 8.0)]).model_copy(update={"battery_care": care})
    out = assign_control_authority(above, _bundle_with_battery_soc(8.0), config)
    assert _flags(out.schedule[0]) == _BAT_ENFORCES


def test_zero_margin_tolerates_rounding_at_min_soc() -> None:
    """With headroom_margin_kw 0 a SoC a hair above min_soc_kwh still counts as empty."""
    config = _config_soc_aware(control={"headroom_margin_kw": 0.0})
    result = assign_control_authority(
        _result([_zex_step(0, 0.0, 1.0000001)]), _bundle_with_battery_soc(1.0000001), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_soc_trajectory_carries_forward_from_power() -> None:
    """Without a published soc_kwh a battery's end SoC follows the planned power, unclamped below min."""
    from mimirheim.core.control_arbitration import _soc_trajectory

    steps = [_zex_step(0, 2.0, None), _zex_step(1, 0.0, None)]
    assert _soc_trajectory("bat", 5.0, 10.0, steps, True) == [(5.0, 4.5), (4.5, 4.5)]
    assert _soc_trajectory("bat", 0.5, 10.0, steps[1:], True) == [(0.5, 0.5)]


def test_zero_margin_ignores_charge_noise_at_min_soc() -> None:
    """Solver noise below the idle threshold does not give an empty battery supply headroom."""
    config = _config_soc_aware(control={"headroom_margin_kw": 0.0})
    result = assign_control_authority(
        _result([_zex_step(0, -0.003, 1.0007)]), _bundle_with_battery_soc(1.0), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


def test_discharge_derating_removes_supply_headroom() -> None:
    """Near empty, a derated discharge limit below the headroom margin makes the battery ineligible."""
    step = _zex_step(0, 0.0, 1.05)
    bundle = _bundle_with_battery_soc(1.05)
    plain = assign_control_authority(_result([step]), bundle, _config_soc_aware())
    assert _flags(plain.schedule[0]) == _BAT_ENFORCES
    derated_config = _config_soc_aware(
        battery_extra={"reduce_discharge_below_soc_kwh": 5.0, "reduce_discharge_min_kw": 0.01}
    )
    derated = assign_control_authority(_result([step]), bundle, derated_config)
    assert _flags(derated.schedule[0]) == _PV_ENFORCES


def test_published_end_soc_used_when_start_unknown() -> None:
    """Without a measured SoC the first step's published soc_kwh still gates the battery."""
    config = _config_soc_aware()
    result = assign_control_authority(
        _result([_zex_step(0, 0.0, 1.0)]), _minimal_bundle_no_ev(), config
    )
    assert _flags(result.schedule[0]) == _PV_ENFORCES


# ---------------------------------------------------------------------------
# Hybrid inverters: conservative SoC gate
#
# A hybrid inverter's net AC power includes its own PV, so its supply headroom
# cannot be read from the schedule. It is excluded whenever its battery is near
# full or near empty; its absorb headroom uses the derated charge ceiling.
# ---------------------------------------------------------------------------


def _config_hybrid_soc(hybrid_extra: dict | None = None) -> MimirheimConfig:
    """Hybrid inverter (10 kWh, min 1 kWh, 5 kW both ways, 97 %) plus a zero-export PV array."""
    raw = _minimal_config_dict()
    raw["hybrid_inverters"] = {
        "hi": {
            "capacity_kwh": 10.0,
            "min_soc_kwh": 1.0,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 6.0,
            "inverter_efficiency": 0.97,
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/hi/exchange_mode"},
            **(hybrid_extra or {}),
        }
    }
    raw["pv_arrays"] = {
        "pv": {
            "max_power_kw": 6.0,
            "topic_forecast": "mimir/input/pv_forecast",
            "capabilities": {"zero_export": True},
            "outputs": {"zero_export_mode": "mimir/pv/zero_export_mode"},
        }
    }
    return MimirheimConfig.model_validate(raw)


def _bundle_with_hybrid_soc(soc_kwh: float, horizon: int = 1) -> SolveBundle:
    return SolveBundle.model_validate(
        {
            "solve_time_utc": _T0.isoformat(),
            "strategy": "minimize_cost",
            "horizon_prices": [0.20] * horizon,
            "horizon_export_prices": [0.10] * horizon,
            "horizon_confidence": [1.0] * horizon,
            "pv_forecast": [0.0] * horizon,
            "base_load_forecast": [0.5] * horizon,
            "hybrid_inverter_inputs": {"hi": {"soc_kwh": soc_kwh, "pv_forecast_kw": [2.0] * horizon}},
        }
    )


def _hybrid_step(t: int, hi_kw: float, hi_soc_kwh: float | None) -> ScheduleStep:
    devices = {
        "hi": DeviceSetpoint(kw=hi_kw, type="hybrid_inverter", soc_kwh=hi_soc_kwh, zero_exchange_active=False),
        "pv": DeviceSetpoint(kw=3.0, type="pv"),
    }
    return _step(t=t, grid_import_kw=0.0, grid_export_kw=0.0, devices=devices)


_HI_ENFORCES = {"hi": (True, None), "pv": (False, None)}
_PV_OVER_HI = {"hi": (False, None), "pv": (True, None)}


@pytest.mark.parametrize(
    "soc_kwh,hi_kw,expected",
    [
        (5.0, 0.0, _HI_ENFORCES),  # mid SoC, idle
        (10.0, 0.0, _PV_OVER_HI),  # full, idle
        (10.0, 2.0, _PV_OVER_HI),  # full, exporting PV: skipped (conservative)
        (1.0, 0.0, _PV_OVER_HI),  # empty, idle
        (1.0, -2.0, _PV_OVER_HI),  # empty, importing: still gated
    ],
)
def test_hybrid_soc_gate(soc_kwh: float, hi_kw: float, expected: dict) -> None:
    out = assign_control_authority(
        _result([_hybrid_step(0, hi_kw, soc_kwh)]), _bundle_with_hybrid_soc(soc_kwh), _config_hybrid_soc()
    )
    assert _flags(out.schedule[0]) == expected


@pytest.mark.parametrize(
    "start_kwh,end_kwh,hi_kw",
    [
        (10.0, 9.5, 2.0),  # starts full, discharges away from it
        (1.0, 1.5, -2.0),  # starts empty, charges away from it
        (9.98, 9.98, 0.0),  # within the tolerance of full
        (1.02, 1.02, 0.0),  # within the tolerance of empty
    ],
)
def test_hybrid_gated_on_start_of_step_soc_and_tolerance(start_kwh: float, end_kwh: float, hi_kw: float) -> None:
    """The start SoC alone, or a SoC within one step at the margin of a bound, gates the hybrid."""
    out = assign_control_authority(
        _result([_hybrid_step(0, hi_kw, end_kwh)]), _bundle_with_hybrid_soc(start_kwh), _config_hybrid_soc()
    )
    assert _flags(out.schedule[0]) == _PV_OVER_HI


@pytest.mark.parametrize("soc_kwh,dc_kw", [(7.0, 5.0), (4.0, 4.6)])
def test_hybrid_charge_ceiling_follows_the_solvers_line(soc_kwh: float, dc_kw: float) -> None:
    """A derating end point above max_charge_kw gives a rising line, capped at max_charge_kw.

    The line ``0.4 * soc + 3`` kW is what the solver allows: 5 kW (capped) at
    7 kWh, and 4.6 kW at 4 kWh. A hybrid charging at that DC power (``dc_kw /
    0.97`` kW AC) has no absorb headroom left; clamping to the end point
    instead would credit it 0.8 or 0.4 kW DC.
    """
    config = _config_hybrid_soc({"reduce_charge_above_soc_kwh": 5.0, "reduce_charge_min_kw": 7.0})
    out = assign_control_authority(
        _result([_hybrid_step(0, -dc_kw / 0.97, soc_kwh + 0.3)]), _bundle_with_hybrid_soc(soc_kwh), config
    )
    assert _flags(out.schedule[0]) == _PV_OVER_HI


def test_hybrid_gated_on_end_of_step_soc() -> None:
    """A planned move to the top within the step gates the hybrid too."""
    out = assign_control_authority(
        _result([_hybrid_step(0, -3.0, 10.0)]), _bundle_with_hybrid_soc(9.3), _config_hybrid_soc()
    )
    assert _flags(out.schedule[0]) == _PV_OVER_HI


def test_hybrid_charge_derating_lowers_absorb_headroom() -> None:
    """At 9.95 kWh a ceiling derated to 0.01 kW at full leaves less than the 0.1 kW margin."""
    step = _hybrid_step(0, 0.0, 9.95)
    bundle = _bundle_with_hybrid_soc(9.95)
    plain = assign_control_authority(_result([step]), bundle, _config_hybrid_soc())
    assert _flags(plain.schedule[0]) == _HI_ENFORCES
    derated = assign_control_authority(
        _result([step]),
        bundle,
        _config_hybrid_soc({"reduce_charge_above_soc_kwh": 5.0, "reduce_charge_min_kw": 0.01}),
    )
    assert _flags(derated.schedule[0]) == _PV_OVER_HI


def test_hybrid_without_soc_carries_the_last_known_soc() -> None:
    """Net AC power says nothing about a hybrid's SoC (its PV may cover the
    export), so a full hybrid stays full through steps that publish no SoC."""
    steps = [_hybrid_step(t, 1.0, None) for t in range(2)]
    out = assign_control_authority(_result(steps), _bundle_with_hybrid_soc(10.0, horizon=2), _config_hybrid_soc())
    assert [_flags(s) for s in out.schedule] == [_PV_OVER_HI, _PV_OVER_HI]


def test_suppressed_schedule_reaches_arbitration_with_the_hybrid_gated() -> None:
    """A dispatch-suppressed schedule (kw 0, no SoC) keeps a full hybrid gated."""
    from mimirheim.core.post_process import apply_gain_threshold

    raw = _config_hybrid_soc().model_dump(exclude_none=True)
    raw["objectives"] = {**raw.get("objectives", {}), "min_dispatch_gain_eur": 1.0}
    config = MimirheimConfig.model_validate(raw)
    bundle = _bundle_with_hybrid_soc(10.0, horizon=2)
    planned = _result([_hybrid_step(t, -1.0, 10.0) for t in range(2)]).model_copy(
        update={"naive_cost_eur": 0.5, "optimised_cost_eur": 0.4}
    )
    suppressed = apply_gain_threshold(planned, bundle, config)
    assert suppressed.dispatch_suppressed
    assert all(s.devices["hi"].soc_kwh is None and s.devices["hi"].kw == 0.0 for s in suppressed.schedule)
    # Keep the steps at zero exchange (PV covering the load) so the
    # arbitration has to pick an enforcer on them.
    suppressed = suppressed.model_copy(
        update={
            "schedule": [
                s.model_copy(update={"grid_import_kw": 0.0, "grid_export_kw": 0.0})
                for s in suppressed.schedule
            ]
        }
    )
    out = assign_control_authority(suppressed, bundle, config)
    assert [_flags(s) for s in out.schedule] == [_PV_OVER_HI, _PV_OVER_HI]
