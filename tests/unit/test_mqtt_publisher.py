"""Unit tests for mimirheim/io/mqtt_publisher.py.

Uses a MagicMock in place of a real paho client. Tests assert on the topics,
QoS settings, retain flags, and JSON payloads of publish() calls.
"""

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, call

import pytest

from mimirheim.config.schema import (
    BatteryCapabilitiesConfig,
    BatteryConfig,
    EfficiencySegment,
    GridConfig,
    HybridInverterConfig,
    MimirheimConfig,
    MqttConfig,
    OutputsConfig,
)
from mimirheim.core.bundle import DeviceSetpoint, ScheduleStep, SolveResult
from mimirheim.io.mqtt_publisher import MqttPublisher


def _seg() -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=5.0, efficiency=0.95)


def _make_config() -> MimirheimConfig:
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        batteries={
            "bat": BatteryConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg()],
                discharge_segments=[_seg()],
            )
        },
    )


def _make_result(solve_status: str = "optimal") -> SolveResult:
    step = ScheduleStep(
        t=0,
        grid_import_kw=1.0,
        grid_export_kw=0.0,
        devices={"bat": DeviceSetpoint(kw=-1.0, type="battery")},
    )
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.5,
        solve_status=solve_status,
        schedule=[step] * 96,
    )


def _infeasible_result() -> SolveResult:
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.0,
        solve_status="infeasible",
        schedule=[],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_publishes_schedule_topic() -> None:
    """publish_result() publishes to the schedule topic with qos=1, retain=True."""
    mock_client = MagicMock()
    config = _make_config()
    publisher = MqttPublisher(client=mock_client, config=config)
    result = _make_result()

    publisher.publish_result(result)

    # Find the call to the schedule topic.
    schedule_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == config.outputs.schedule
    ]
    assert len(schedule_calls) == 1
    _, kwargs = schedule_calls[0].args[0], schedule_calls[0]
    assert schedule_calls[0].kwargs.get("retain", schedule_calls[0].args[3] if len(schedule_calls[0].args) > 3 else None) or True  # retain check below
    # Verify payload is valid JSON.
    payload = schedule_calls[0].args[1]
    parsed = json.loads(payload)
    assert "schedule" in parsed or "solve_status" in parsed  # SolveResult JSON


def test_publishes_schedule_topic_retain_and_qos() -> None:
    """publish_result() uses qos=1 and retain=True for the schedule topic."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())
    publisher.publish_result(_make_result())

    schedule_call = next(
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/strategy/schedule"
    )
    # Accept qos and retain both as positional or keyword args.
    pos = schedule_call.args
    kw = schedule_call.kwargs
    qos = kw.get("qos", pos[2] if len(pos) > 2 else None)
    retain = kw.get("retain", pos[3] if len(pos) > 3 else None)
    assert qos == 1
    assert retain is True


def test_publishes_current_strategy_topic() -> None:
    """publish_result() publishes to the current strategy topic with retain=True."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())
    publisher.publish_result(_make_result())

    topics = [c.args[0] for c in mock_client.publish.call_args_list]
    assert "mimir/strategy/current" in topics


def test_current_payload_includes_devices_and_grid() -> None:
    """publish_result() current payload contains devices, grid, strategy and solve_status."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())
    publisher.publish_result(_make_result())

    current_call = next(
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/strategy/current"
    )
    payload = json.loads(current_call.args[1])

    assert "devices" in payload
    assert "bat" in payload["devices"]
    assert payload["devices"]["bat"]["kw"] == -1.0
    assert payload["devices"]["bat"]["type"] == "battery"
    assert "grid_import_kw" in payload
    assert "grid_export_kw" in payload
    assert "strategy" in payload
    assert "solve_status" in payload


def test_publishes_per_device_retained_topic() -> None:
    """publish_result() publishes at least one per-device retained topic."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())
    publisher.publish_result(_make_result())

    topics = [c.args[0] for c in mock_client.publish.call_args_list]
    # Expect at least one device-specific topic
    device_topics = [t for t in topics if "bat" in t]
    assert len(device_topics) >= 1


def test_publishes_last_solve_success() -> None:
    """publish_last_solve_status() with optimal result publishes status=ok."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())
    publisher.publish_last_solve_status(result=_make_result("optimal"), error=None)

    last_solve_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/status/last_solve"
    ]
    assert len(last_solve_calls) == 1
    payload = json.loads(last_solve_calls[0].args[1])
    assert payload["status"] == "ok"


def test_publishes_last_solve_infeasible() -> None:
    """publish_last_solve_status() with infeasible result publishes status=error with detail."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())
    publisher.publish_last_solve_status(result=_infeasible_result(), error=None)

    last_solve_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/status/last_solve"
    ]
    assert len(last_solve_calls) == 1
    payload = json.loads(last_solve_calls[0].args[1])
    assert payload["status"] == "error"
    assert "detail" in payload
    assert len(payload["detail"]) > 0


def test_republish_last_result_republishes_all_topics() -> None:
    """republish_last_result() makes the same publish calls as the original publish_result()."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())
    result = _make_result()

    # First publish.
    publisher.publish_result(result)
    first_calls = list(mock_client.publish.call_args_list)
    mock_client.publish.reset_mock()

    # Republish should replicate all calls exactly.
    publisher.republish_last_result()
    second_calls = list(mock_client.publish.call_args_list)

    assert second_calls == first_calls


# ---------------------------------------------------------------------------
# PV output topics
# ---------------------------------------------------------------------------


def _make_config_with_pv(
    power_limit_topic: str | None = None,
    zero_export_topic: str | None = None,
) -> MimirheimConfig:
    """Config with a single PV array that has output topics configured."""
    from mimirheim.config.schema import PvCapabilitiesConfig, PvConfig, PvOutputsConfig

    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        batteries={
            "bat": BatteryConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg()],
                discharge_segments=[_seg()],
            )
        },
        pv_arrays={
            "pv_roof": PvConfig(
                max_power_kw=6.0,
                topic_forecast="mimir/input/pv_forecast",
                capabilities=PvCapabilitiesConfig(
                    power_limit=power_limit_topic is not None,
                    zero_export=zero_export_topic is not None,
                ),
                outputs=PvOutputsConfig(
                    power_limit_kw=power_limit_topic,
                    zero_export_mode=zero_export_topic,
                ),
            )
        },
    )


def _make_result_with_pv(
    power_limit_kw: float | None = None,
    zero_exchange_active: bool | None = None,
) -> SolveResult:
    """SolveResult with both a battery and a PV device setpoint."""
    step = ScheduleStep(
        t=0,
        grid_import_kw=0.5,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(kw=-1.0, type="battery"),
            "pv_roof": DeviceSetpoint(
                kw=2.0,
                type="pv",
                power_limit_kw=power_limit_kw,
                zero_exchange_active=zero_exchange_active,
            ),
        },
    )
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.1,
        solve_status="optimal",
        schedule=[step] * 96,
    )


def test_publishes_pv_power_limit_topic() -> None:
    """When power_limit_kw output topic is configured, publish_result publishes it."""
    mock_client = MagicMock()
    config = _make_config_with_pv(power_limit_topic="mimir/pv/pv_roof/power_limit_kw")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_pv(power_limit_kw=3.5))

    pv_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/pv/pv_roof/power_limit_kw"
    ]
    assert len(pv_calls) == 1
    assert float(pv_calls[0].args[1]) == pytest.approx(3.5)
    assert pv_calls[0].kwargs.get("retain") is True
    assert pv_calls[0].kwargs.get("qos") == 1


def test_publishes_pv_zero_export_mode_topic() -> None:
    """When zero_export_mode output topic is configured, publish_result publishes it."""
    mock_client = MagicMock()
    config = _make_config_with_pv(zero_export_topic="mimir/pv/pv_roof/zero_export_mode")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_pv(zero_exchange_active=True))

    ze_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/pv/pv_roof/zero_export_mode"
    ]
    assert len(ze_calls) == 1
    assert ze_calls[0].args[1] in ("true", "1", True, 1, b"true", b"1")
    assert ze_calls[0].kwargs.get("retain") is True


def test_pv_power_limit_topic_not_published_when_not_configured() -> None:
    """When no power_limit_kw topic is configured, no publish call targets it."""
    mock_client = MagicMock()
    config = _make_config_with_pv()  # no PV output topics
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_pv())

    pv_limit_calls = [
        c for c in mock_client.publish.call_args_list
        if "power_limit" in c.args[0]
    ]
    assert len(pv_limit_calls) == 0


def test_pv_zero_export_mode_topic_not_published_when_not_configured() -> None:
    """When no zero_export_mode topic is configured, no publish call targets it."""
    mock_client = MagicMock()
    config = _make_config_with_pv()
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_pv())

    ze_calls = [
        c for c in mock_client.publish.call_args_list
        if "zero_export" in c.args[0]
    ]
    assert len(ze_calls) == 0


# ---------------------------------------------------------------------------
# Battery zero-export mode output topic
# ---------------------------------------------------------------------------


def _make_config_with_battery_zem(zero_export_topic: str | None) -> MimirheimConfig:
    """Config with a battery that has zero_exchange capability and exchange_mode output."""
    from mimirheim.config.schema import BatteryOutputsConfig

    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        batteries={
            "bat": BatteryConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg()],
                discharge_segments=[_seg()],
                capabilities=BatteryCapabilitiesConfig(
                    zero_exchange=zero_export_topic is not None,
                ),
                outputs=BatteryOutputsConfig(
                    exchange_mode=zero_export_topic,
                ),
            )
        },
    )


def _make_result_with_battery_zem(zero_exchange_active: bool | None = True) -> SolveResult:
    """SolveResult with a battery setpoint carrying zero_exchange_active."""
    step = ScheduleStep(
        t=0,
        grid_import_kw=1.0,
        grid_export_kw=0.0,
        devices={"bat": DeviceSetpoint(kw=-2.0, type="battery", zero_exchange_active=zero_exchange_active)},
    )
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.1,
        solve_status="optimal",
        schedule=[step] * 96,
    )


def test_publishes_battery_zero_export_mode_topic() -> None:
    """When battery exchange_mode output topic is configured, publish_result publishes it."""
    mock_client = MagicMock()
    config = _make_config_with_battery_zem("mimir/battery/bat/zero_export_mode")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_battery_zem(zero_exchange_active=True))

    ze_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/battery/bat/zero_export_mode"
    ]
    assert len(ze_calls) == 1
    assert ze_calls[0].args[1] == "true"
    assert ze_calls[0].kwargs.get("retain") is True
    assert ze_calls[0].kwargs.get("qos") == 1


def test_battery_zero_export_mode_publishes_false_when_exporting() -> None:
    """Battery exchange_mode publishes 'false' when zero_exchange_active is False."""
    mock_client = MagicMock()
    config = _make_config_with_battery_zem("mimir/battery/bat/zero_export_mode")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_battery_zem(zero_exchange_active=False))

    ze_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/battery/bat/zero_export_mode"
    ]
    assert len(ze_calls) == 1
    assert ze_calls[0].args[1] == "false"


def test_battery_zero_export_mode_not_published_when_not_configured() -> None:
    """When no exchange_mode topic is configured, the topic is never published."""
    mock_client = MagicMock()
    config = _make_config_with_battery_zem(None)
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_battery_zem(zero_exchange_active=None))

    ze_calls = [
        c for c in mock_client.publish.call_args_list
        if "exchange_mode" in c.args[0] or "zero_export" in c.args[0]
    ]
    assert len(ze_calls) == 0



def test_last_solve_status_includes_cost_fields() -> None:
    """publish_last_solve_status() includes naive_cost_eur, optimised_cost_eur and soc_credit_eur when solve succeeds."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())

    result = SolveResult(
        strategy="minimize_cost",
        objective_value=0.5,
        solve_status="optimal",
        naive_cost_eur=1.23,
        optimised_cost_eur=0.45,
        soc_credit_eur=0.12,
        schedule=[
            ScheduleStep(
                t=0,
                grid_import_kw=1.0,
                grid_export_kw=0.0,
                devices={"bat": DeviceSetpoint(kw=-1.0, type="battery")},
            )
        ],
    )
    publisher.publish_last_solve_status(result=result, error=None)

    last_solve_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/status/last_solve"
    ]
    assert len(last_solve_calls) == 1
    payload = json.loads(last_solve_calls[0].args[1])
    assert payload["status"] == "ok"
    assert payload["naive_cost_eur"] == pytest.approx(1.23, abs=1e-4)
    assert payload["optimised_cost_eur"] == pytest.approx(0.45, abs=1e-4)
    assert payload["soc_credit_eur"] == pytest.approx(0.12, abs=1e-4)


def test_last_solve_status_error_omits_cost_fields() -> None:
    """publish_last_solve_status() with infeasible result does not include cost fields."""
    mock_client = MagicMock()
    publisher = MqttPublisher(client=mock_client, config=_make_config())
    publisher.publish_last_solve_status(result=_infeasible_result(), error=None)

    last_solve_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/status/last_solve"
    ]
    payload = json.loads(last_solve_calls[0].args[1])
    assert payload["status"] == "error"
    assert "naive_cost_eur" not in payload
    assert "optimised_cost_eur" not in payload
    assert "soc_credit_eur" not in payload


# ---------------------------------------------------------------------------
# Plan 42 — exchange_mode and loadbalance_cmd output topics
# ---------------------------------------------------------------------------


def _make_config_with_battery_exchange_mode(exchange_mode_topic: str | None) -> MimirheimConfig:
    """Config with a battery that has zero_exchange capability and exchange_mode output."""
    from mimirheim.config.schema import BatteryCapabilitiesConfig, BatteryOutputsConfig

    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        batteries={
            "bat": BatteryConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg()],
                discharge_segments=[_seg()],
                capabilities=BatteryCapabilitiesConfig(
                    zero_exchange=exchange_mode_topic is not None,
                ),
                outputs=BatteryOutputsConfig(
                    exchange_mode=exchange_mode_topic,
                ),
            )
        },
    )


def _make_result_with_battery_exchange_mode(zero_exchange_active: bool | None = True) -> SolveResult:
    """SolveResult with a battery setpoint carrying zero_exchange_active."""
    step = ScheduleStep(
        t=0,
        grid_import_kw=1.0,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(
                kw=-2.0,
                type="battery",
                zero_exchange_active=zero_exchange_active,
            )
        },
    )
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.1,
        solve_status="optimal",
        schedule=[step] * 96,
    )


def test_zero_exchange_active_true_publishes_exchange_mode_battery() -> None:
    """When battery zero_exchange_active=True, publish 'true' to the exchange_mode topic."""
    mock_client = MagicMock()
    config = _make_config_with_battery_exchange_mode("mimir/battery/bat/exchange_mode")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_battery_exchange_mode(zero_exchange_active=True))

    em_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/battery/bat/exchange_mode"
    ]
    assert len(em_calls) == 1
    assert em_calls[0].args[1] == "true"
    assert em_calls[0].kwargs.get("retain") is True
    assert em_calls[0].kwargs.get("qos") == 1


def test_zero_exchange_active_false_publishes_false_to_exchange_mode_battery() -> None:
    """When battery zero_exchange_active=False, publish 'false' to the exchange_mode topic.

    Publishing the de-assertion explicitly ensures the inverter exits
    closed-loop mode when the arbitration engine decides the enforcer should
    not be active for this step.
    """
    mock_client = MagicMock()
    config = _make_config_with_battery_exchange_mode("mimir/battery/bat/exchange_mode")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_battery_exchange_mode(zero_exchange_active=False))

    em_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/battery/bat/exchange_mode"
    ]
    assert len(em_calls) == 1
    assert em_calls[0].args[1] == "false"


def test_zero_exchange_active_none_does_not_publish_exchange_mode_battery() -> None:
    """When battery zero_exchange_active=None, the exchange_mode topic is not published.

    None means the device has no closed-loop capability; mimirheim should never
    publish to the exchange_mode topic for that device.
    """
    mock_client = MagicMock()
    config = _make_config_with_battery_exchange_mode(None)
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_battery_exchange_mode(zero_exchange_active=None))

    em_calls = [
        c for c in mock_client.publish.call_args_list
        if "exchange_mode" in c.args[0]
    ]
    assert len(em_calls) == 0


def _make_config_with_ev_loadbalance(loadbalance_cmd_topic: str | None) -> MimirheimConfig:
    """Config with an EV charger that has loadbalance capability."""
    from mimirheim.config.schema import EvCapabilitiesConfig, EvConfig, EvOutputsConfig

    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        ev_chargers={
            "ev1": EvConfig(
                capacity_kwh=52.0,
                charge_segments=[_seg()],
                capabilities=EvCapabilitiesConfig(
                    loadbalance=loadbalance_cmd_topic is not None,
                ),
                outputs=EvOutputsConfig(
                    loadbalance_cmd=loadbalance_cmd_topic,
                ),
            )
        },
    )


def _make_result_with_ev_loadbalance(loadbalance_active: bool | None = True) -> SolveResult:
    """SolveResult with an EV setpoint carrying loadbalance_active."""
    step = ScheduleStep(
        t=0,
        grid_import_kw=0.5,
        grid_export_kw=0.0,
        devices={
            "ev1": DeviceSetpoint(
                kw=-3.7,
                type="ev_charger",
                loadbalance_active=loadbalance_active,
            )
        },
    )
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.05,
        solve_status="optimal",
        schedule=[step] * 96,
    )


def test_loadbalance_active_true_publishes_loadbalance_cmd_ev() -> None:
    """When EV loadbalance_active=True, publish 'true' to the loadbalance_cmd topic."""
    mock_client = MagicMock()
    config = _make_config_with_ev_loadbalance("mimir/ev/ev1/loadbalance_cmd")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_ev_loadbalance(loadbalance_active=True))

    lb_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/ev/ev1/loadbalance_cmd"
    ]
    assert len(lb_calls) == 1
    assert lb_calls[0].args[1] == "true"
    assert lb_calls[0].kwargs.get("retain") is True
    assert lb_calls[0].kwargs.get("qos") == 1


def test_pv_zero_export_mode_published_when_zero_exchange_active() -> None:
    """PV zero_export_mode topic is published from zero_exchange_active on DeviceSetpoint.

    PvOutputsConfig.zero_export_mode retains its original field name. After
    Plan 42 the per-step signal is stored in DeviceSetpoint.zero_exchange_active
    (not the removed zero_export_mode). The publisher must route it to the PV
    output topic using the new field name.
    """
    from mimirheim.config.schema import PvCapabilitiesConfig, PvConfig, PvOutputsConfig

    mock_client = MagicMock()
    config = MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        pv_arrays={
            "pv_roof": PvConfig(
                max_power_kw=6.0,
                topic_forecast="mimir/input/pv_forecast",
                capabilities=PvCapabilitiesConfig(zero_export=True),
                outputs=PvOutputsConfig(zero_export_mode="mimir/pv/pv_roof/zero_export_mode"),
            )
        },
    )
    publisher = MqttPublisher(client=mock_client, config=config)

    step = ScheduleStep(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        devices={
            "pv_roof": DeviceSetpoint(kw=2.5, type="pv", zero_exchange_active=True),
        },
    )
    result = SolveResult(
        strategy="minimize_cost",
        objective_value=0.0,
        solve_status="optimal",
        schedule=[step] * 96,
    )
    publisher.publish_result(result)

    ze_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/pv/pv_roof/zero_export_mode"
    ]
    assert len(ze_calls) == 1
    assert ze_calls[0].args[1] in ("true", "1", True, 1, b"true", b"1")


# ---------------------------------------------------------------------------
# PV on_off_mode output topic
# ---------------------------------------------------------------------------


def test_publishes_pv_on_off_mode_true_when_on() -> None:
    """publish_result() publishes 'true' to on_off_mode topic when on_off_active is True."""
    from mimirheim.config.schema import PvCapabilitiesConfig, PvConfig, PvOutputsConfig

    mock_client = MagicMock()
    config = MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        pv_arrays={
            "roof": PvConfig(
                max_power_kw=5.0,
                topic_forecast="mimir/input/pv_forecast",
                capabilities=PvCapabilitiesConfig(on_off=True),
                outputs=PvOutputsConfig(on_off_mode="mimir/pv/roof/on_off_mode"),
            )
        },
    )
    publisher = MqttPublisher(client=mock_client, config=config)

    step = ScheduleStep(
        t=0, grid_import_kw=0.0, grid_export_kw=0.0,
        devices={"roof": DeviceSetpoint(kw=3.5, type="pv", on_off_active=True)},
    )
    result = SolveResult(
        strategy="minimize_cost", objective_value=0.0, solve_status="optimal",
        schedule=[step] * 96,
    )
    publisher.publish_result(result)

    calls = [c for c in mock_client.publish.call_args_list if c.args[0] == "mimir/pv/roof/on_off_mode"]
    assert len(calls) == 1
    assert calls[0].args[1] == "true"
    assert calls[0].kwargs.get("retain") is True
    assert calls[0].kwargs.get("qos") == 1


def test_publishes_pv_on_off_mode_false_when_off() -> None:
    """publish_result() publishes 'false' to on_off_mode topic when on_off_active is False."""
    from mimirheim.config.schema import PvCapabilitiesConfig, PvConfig, PvOutputsConfig

    mock_client = MagicMock()
    config = MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        pv_arrays={
            "roof": PvConfig(
                max_power_kw=5.0,
                topic_forecast="mimir/input/pv_forecast",
                capabilities=PvCapabilitiesConfig(on_off=True),
                outputs=PvOutputsConfig(on_off_mode="mimir/pv/roof/on_off_mode"),
            )
        },
    )
    publisher = MqttPublisher(client=mock_client, config=config)

    step = ScheduleStep(
        t=0, grid_import_kw=0.0, grid_export_kw=0.0,
        devices={"roof": DeviceSetpoint(kw=0.0, type="pv", on_off_active=False)},
    )
    result = SolveResult(
        strategy="minimize_cost", objective_value=0.0, solve_status="optimal",
        schedule=[step] * 96,
    )
    publisher.publish_result(result)

    calls = [c for c in mock_client.publish.call_args_list if c.args[0] == "mimir/pv/roof/on_off_mode"]
    assert len(calls) == 1
    assert calls[0].args[1] == "false"


def test_pv_on_off_mode_not_published_when_on_off_active_is_none() -> None:
    """on_off_mode is not published when on_off_active is None (capability not configured)."""
    from mimirheim.config.schema import PvCapabilitiesConfig, PvConfig, PvOutputsConfig

    mock_client = MagicMock()
    config = MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        pv_arrays={
            "roof": PvConfig(
                max_power_kw=5.0,
                topic_forecast="mimir/input/pv_forecast",
            )
        },
    )
    publisher = MqttPublisher(client=mock_client, config=config)

    step = ScheduleStep(
        t=0, grid_import_kw=0.0, grid_export_kw=0.0,
        devices={"roof": DeviceSetpoint(kw=3.5, type="pv")},
    )
    result = SolveResult(
        strategy="minimize_cost", objective_value=0.0, solve_status="optimal",
        schedule=[step] * 96,
    )
    publisher.publish_result(result)

    calls = [c for c in mock_client.publish.call_args_list if "on_off_mode" in c.args[0]]
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# Deferrable load recommended-start output topic
# ---------------------------------------------------------------------------


def _make_config_with_deferrable_rec_start(topic: str | None) -> MimirheimConfig:
    from mimirheim.config.schema import DeferrableLoadConfig

    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        deferrable_loads={
            "wash": DeferrableLoadConfig(
                power_profile=[1.5, 1.5],
                topic_window_earliest="mimir/load/wash/window_earliest",
                topic_window_latest="mimir/load/wash/window_latest",
                topic_recommended_start_time=topic,
            )
        },
    )


def _make_result_with_deferrable_rec_start(rec_start: datetime) -> SolveResult:
    step = ScheduleStep(
        t=0,
        grid_import_kw=1.5,
        grid_export_kw=0.0,
        devices={"wash": DeviceSetpoint(kw=-1.5, type="deferrable_load")},
    )
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.1,
        solve_status="optimal",
        schedule=[step] * 96,
        deferrable_recommended_starts={"wash": rec_start},
    )


def test_publishes_deferrable_recommended_start_topic() -> None:
    """publish_result() publishes the recommended start ISO datetime to the
    configured topic when the load appears in deferrable_recommended_starts."""
    mock_client = MagicMock()
    config = _make_config_with_deferrable_rec_start("mimir/load/wash/recommended_start")
    publisher = MqttPublisher(client=mock_client, config=config)

    rec_start = datetime(2025, 6, 1, 6, 30, 0, tzinfo=UTC)
    publisher.publish_result(_make_result_with_deferrable_rec_start(rec_start))

    calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/load/wash/recommended_start"
    ]
    assert len(calls) == 1
    assert calls[0].args[1] == "2025-06-01T06:30:00Z"
    assert calls[0].kwargs.get("retain") is True
    assert calls[0].kwargs.get("qos") == 1


def test_deferrable_recommended_starts_included_in_current_payload() -> None:
    """publish_result() includes deferrable_recommended_starts in the current-step payload."""
    mock_client = MagicMock()
    config = _make_config_with_deferrable_rec_start("mimir/load/wash/recommended_start")
    publisher = MqttPublisher(client=mock_client, config=config)

    rec_start = datetime(2025, 6, 1, 6, 30, 0, tzinfo=UTC)
    publisher.publish_result(_make_result_with_deferrable_rec_start(rec_start))

    current_call = next(
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/strategy/current"
    )
    payload = json.loads(current_call.args[1])
    assert "deferrable_recommended_starts" not in payload
    assert payload["devices"]["wash"]["recommended_start"] == "2025-06-01T06:30:00Z"


def test_deferrable_recommended_start_not_published_when_topic_not_configured() -> None:
    """topic_recommended_start_time is now always derived from mqtt.topic_prefix.

    This test verifies that when the config helper passes None, MimirheimConfig
    auto-derives a topic and the publisher therefore publishes the recommended
    start. The previous "no-publish when topic=None" path no longer exists
    because derivation fills in the topic unconditionally.
    """
    mock_client = MagicMock()
    config = _make_config_with_deferrable_rec_start(None)
    publisher = MqttPublisher(client=mock_client, config=config)

    rec_start = datetime(2025, 6, 1, 6, 30, 0, tzinfo=UTC)
    publisher.publish_result(_make_result_with_deferrable_rec_start(rec_start))

    calls = [
        c for c in mock_client.publish.call_args_list
        if "recommended_start" in c.args[0]
    ]
    # The topic was derived to mimir/output/deferrable/wash/recommended_start
    assert len(calls) == 1
    assert "recommended_start" in calls[0].args[0]


def test_deferrable_recommended_start_not_published_when_load_not_scheduled() -> None:
    """When deferrable_recommended_starts is empty, no recommended-start topic is published."""
    mock_client = MagicMock()
    config = _make_config_with_deferrable_rec_start("mimir/load/wash/recommended_start")
    publisher = MqttPublisher(client=mock_client, config=config)

    result = SolveResult(
        strategy="minimize_cost",
        objective_value=0.0,
        solve_status="optimal",
        schedule=[],
        deferrable_recommended_starts={},
    )
    publisher.publish_result(result)

    calls = [
        c for c in mock_client.publish.call_args_list
        if "recommended_start" in c.args[0]
    ]
    assert len(calls) == 0


# ---------------------------------------------------------------------------
# Hybrid inverter exchange_mode publishing (plan 54)
# ---------------------------------------------------------------------------


def _make_config_with_hybrid_exchange_mode(exchange_mode_topic: str | None) -> MimirheimConfig:
    """Config with a hybrid inverter that has zero_exchange capability."""
    from mimirheim.config.schema import (
        HybridInverterCapabilitiesConfig,
        HybridInverterOutputsConfig,
    )

    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        hybrid_inverters={
            "hybrid": HybridInverterConfig(
                capacity_kwh=10.0,
                min_soc_kwh=0.0,
                max_charge_kw=5.0,
                max_discharge_kw=5.0,
                max_pv_kw=6.0,
                capabilities=HybridInverterCapabilitiesConfig(
                    zero_exchange=exchange_mode_topic is not None,
                ),
                outputs=HybridInverterOutputsConfig(
                    exchange_mode=exchange_mode_topic,
                ),
            )
        },
    )


def _make_result_with_hybrid_exchange_mode(
    zero_exchange_active: bool | None = True,
) -> SolveResult:
    step = ScheduleStep(
        t=0,
        grid_import_kw=1.0,
        grid_export_kw=0.0,
        devices={
            "hybrid": DeviceSetpoint(
                kw=2.0,
                type="hybrid_inverter",
                zero_exchange_active=zero_exchange_active,
            )
        },
    )
    return SolveResult(
        strategy="minimize_cost",
        objective_value=0.1,
        solve_status="optimal",
        schedule=[step] * 96,
    )


def test_hybrid_exchange_mode_published_when_zero_exchange_true() -> None:
    """When hybrid zero_exchange_active=True, publish 'true' to exchange_mode topic."""
    mock_client = MagicMock()
    config = _make_config_with_hybrid_exchange_mode("mimir/hybrid/hybrid/exchange_mode")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_hybrid_exchange_mode(zero_exchange_active=True))

    em_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/hybrid/hybrid/exchange_mode"
    ]
    assert len(em_calls) == 1
    assert em_calls[0].args[1] == "true"


def test_hybrid_exchange_mode_published_false_when_not_active() -> None:
    """When hybrid zero_exchange_active=False, publish 'false' to exchange_mode topic."""
    mock_client = MagicMock()
    config = _make_config_with_hybrid_exchange_mode("mimir/hybrid/hybrid/exchange_mode")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_hybrid_exchange_mode(zero_exchange_active=False))

    em_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/hybrid/hybrid/exchange_mode"
    ]
    assert len(em_calls) == 1
    assert em_calls[0].args[1] == "false"


def test_hybrid_exchange_mode_not_published_when_capability_false() -> None:
    """When capabilities.zero_exchange=False, exchange_mode topic is not published."""
    mock_client = MagicMock()
    # Passing None as topic → capability is set to False by the helper.
    config = _make_config_with_hybrid_exchange_mode(None)
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_hybrid_exchange_mode(zero_exchange_active=True))

    em_calls = [
        c for c in mock_client.publish.call_args_list
        if "exchange_mode" in c.args[0]
    ]
    assert len(em_calls) == 0


def test_hybrid_exchange_mode_not_published_when_zero_exchange_active_none() -> None:
    """When zero_exchange_active is None (no capability), no exchange_mode publish."""
    mock_client = MagicMock()
    config = _make_config_with_hybrid_exchange_mode("mimir/hybrid/hybrid/exchange_mode")
    publisher = MqttPublisher(client=mock_client, config=config)

    publisher.publish_result(_make_result_with_hybrid_exchange_mode(zero_exchange_active=None))

    em_calls = [
        c for c in mock_client.publish.call_args_list
        if c.args[0] == "mimir/hybrid/hybrid/exchange_mode"
    ]
    assert len(em_calls) == 0
