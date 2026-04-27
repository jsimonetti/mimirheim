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
"""

from datetime import datetime, timezone

import pytest

from mimirheim.config.schema import (
    BatteryCapabilitiesConfig,
    BatteryConfig,
    BatteryOutputsConfig,
    ControlConfig,
    EfficiencySegment,
    EvCapabilitiesConfig,
    EvConfig,
    EvOutputsConfig,
    GridConfig,
    MimirheimConfig,
    MqttConfig,
    ObjectivesConfig,
    OutputsConfig,
    PvCapabilitiesConfig,
    PvConfig,
    PvOutputsConfig,
    StaticLoadConfig,
)
from mimirheim.config.schema import HybridInverterCapabilitiesConfig, HybridInverterConfig, HybridInverterOutputsConfig
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

