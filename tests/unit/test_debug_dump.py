"""Unit tests for the debug dump writer in mimirheim/core/model_builder.py.

``debug_dump`` serialises a ``SolveResult`` to the JSON files the reporter
reads. It builds each device entry by hand rather than from ``model_dump``, so
a field added to ``DeviceSetpoint`` does not reach the dump until someone also
edits the writer. These tests pin the two properties that keeps breaking: every
field is written, and its value survives the round trip.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from mimirheim.config.schema import (
    BatteryConfig,
    DebugConfig,
    EfficiencySegment,
    GridConfig,
    MimirheimConfig,
    MqttConfig,
    OutputsConfig,
    PvCapabilitiesConfig,
    PvConfig,
)
from mimirheim.core.bundle import (
    DeviceSetpoint,
    ScheduleStep,
    SolveBundle,
    SolveResult,
)
from mimirheim.core.model_builder import debug_dump


def _seg() -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=5.0, efficiency=0.95)


def _config(dump_dir: Path) -> MimirheimConfig:
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        outputs=OutputsConfig(
            schedule="mimir/schedule",
            current="mimir/current",
            last_solve="mimir/status",
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
            "pv": PvConfig(
                topic_forecast="mimir/input/pv/forecast",
                max_power_kw=5.0,
                capabilities=PvCapabilitiesConfig(power_limit=True),
            )
        },
        debug=DebugConfig(enabled=True, dump_dir=str(dump_dir)),
    )


def _bundle() -> SolveBundle:
    return SolveBundle(
        solve_time_utc=datetime(2026, 8, 23, 12, 30, tzinfo=UTC),
        horizon_prices=[0.20, 0.25],
        horizon_export_prices=[0.05, 0.06],
        horizon_confidence=[1.0, 1.0],
        pv_forecast=[1.0, 2.0],
        base_load_forecast=[0.5, 0.5],
    )


def _result() -> SolveResult:
    """A result whose setpoints populate every optional DeviceSetpoint field."""
    return SolveResult(
        strategy="minimize_cost",
        objective_value=1.0,
        solve_status="optimal",
        schedule=[
            ScheduleStep(
                t=t,
                grid_import_kw=1.0,
                grid_export_kw=0.0,
                devices={
                    "bat": DeviceSetpoint(
                        kw=-2.0,
                        type="battery",
                        zero_exchange_active=True,
                        loadbalance_active=False,
                        soc_kwh=4.25 + t,
                    ),
                    "pv": DeviceSetpoint(
                        kw=1.5,
                        type="pv",
                        power_limit_kw=1.5,
                        on_off_active=True,
                        pv_is_curtailed=True,
                    ),
                },
            )
            for t in range(2)
        ],
    )


def _dump(tmp_path: Path) -> dict:
    config = _config(tmp_path)
    paths = debug_dump(_bundle(), _result(), config, tmp_path, max_dumps=5)
    assert paths is not None
    _, output_path = paths
    return json.loads(Path(output_path).read_text())


def test_dump_writes_every_device_setpoint_field(tmp_path: Path) -> None:
    """No DeviceSetpoint field may be silently dropped by the writer.

    soc_kwh was added to the model and consumed by the reporter, but the
    writer was never updated, so the reporter read a missing key, fell back to
    0.0, and drew a flat SOC line for months. This test fails on the next
    field that is added to the model and forgotten here.
    """
    out = _dump(tmp_path)

    written: set[str] = set()
    for step in out["schedule"]:
        for entry in step["devices"].values():
            written |= set(entry)

    expected = set(DeviceSetpoint.model_fields)
    assert expected - written == set(), (
        f"debug_dump drops DeviceSetpoint field(s): {sorted(expected - written)}"
    )


def test_dump_round_trips_soc_kwh(tmp_path: Path) -> None:
    """The battery SOC series must reach the dump with its values intact.

    This is what the reporter charts. A present-but-wrong value would be as
    misleading as a missing one.
    """
    out = _dump(tmp_path)

    soc = [step["devices"]["bat"]["soc_kwh"] for step in out["schedule"]]
    assert soc == [4.25, 5.25]


def test_dump_round_trips_pv_is_curtailed(tmp_path: Path) -> None:
    """The curtailment flag must reach the dump; the reporter reads it."""
    out = _dump(tmp_path)

    assert [step["devices"]["pv"]["pv_is_curtailed"] for step in out["schedule"]] == [
        True,
        True,
    ]


def test_dump_omits_fields_that_do_not_apply(tmp_path: Path) -> None:
    """A device must not gain keys for capabilities it does not have.

    The writer omits None rather than emitting nulls, which keeps the dump
    readable and lets the reporter distinguish "no such capability" from a
    real value. Adding the missing fields must not change that.
    """
    out = _dump(tmp_path)

    bat = out["schedule"][0]["devices"]["bat"]
    pv = out["schedule"][0]["devices"]["pv"]

    # The battery has no PV-only fields.
    assert "power_limit_kw" not in bat
    assert "pv_is_curtailed" not in bat
    assert "on_off_active" not in bat
    # The PV array is not a storage device and carries no SOC.
    assert "soc_kwh" not in pv
