"""Generate benchmark scenario data files.

Run this script once to produce input.json and config.yaml for each benchmark
scenario.  It is safe to re-run; existing files will be overwritten.

Usage:
    uv run python tests/benchmarks/_generate_data.py

The output directories are committed alongside the script:
    tests/benchmarks/minimal_home_24h/
    tests/benchmarks/prosumer_ev_48h/
    tests/benchmarks/worst_case_7d/
"""

import json
import math
import pathlib
import random

DT = 0.25  # 15-minute step size in hours
BASE = pathlib.Path(__file__).parent
random.seed(42)


# ---------------------------------------------------------------------------
# Data generators
# ---------------------------------------------------------------------------

def solar_curve(steps: int, peak_kw: float, peak_hour: float, half_width_h: float = 5.0) -> list[float]:
    """Return a per-step PV generation curve in kW.

    Uses a Gaussian bell peaking at peak_hour each day, representing the
    production of a single PV array on a clear spring day in NL/BE (day-of-
    year ~90, latitude ~51°N).

    Args:
        steps: Total number of 15-minute time steps.
        peak_kw: Peak generation power in kW (at solar noon for south array).
        peak_hour: Hour-of-day at which generation peaks (24h clock).
        half_width_h: Approximate half-width of the generation window in hours.
            Wider for south arrays (5-6h), narrower for east/west (4-5h).

    Returns:
        List of non-negative floats, length == steps.
    """
    sigma = half_width_h / 2.5
    out: list[float] = []
    for i in range(steps):
        h = (i * DT) % 24.0
        dist = abs(h - peak_hour)
        if dist > 12.0:
            dist = 24.0 - dist
        v = peak_kw * math.exp(-0.5 * (dist / sigma) ** 2)
        out.append(round(max(0.0, v), 3))
    return out


def base_load_curve(steps: int) -> list[float]:
    """Return a per-step household base load in kW.

    Follows a typical residential Belgian load profile: low overnight, morning
    and evening peaks, moderate midday consumption.

    Args:
        steps: Total number of 15-minute time steps.

    Returns:
        List of positive floats, length == steps.
    """
    out: list[float] = []
    for i in range(steps):
        h = (i * DT) % 24.0
        if h < 6.0:
            v = 0.22
        elif h < 7.0:
            v = 0.30 + 0.50 * (h - 6.0)
        elif h < 9.0:
            v = 0.85 + 0.25 * math.sin(math.pi * (h - 7.0) / 2.0)
        elif h < 12.0:
            v = 0.55
        elif h < 13.0:
            v = 0.70
        elif h < 17.0:
            v = 0.50
        elif h < 18.0:
            v = 0.60
        elif h < 22.0:
            v = 1.10 + 0.25 * math.sin(math.pi * (h - 18.0) / 4.0)
        else:
            v = 0.45
        v += random.gauss(0, 0.02)
        out.append(round(max(0.10, v), 3))
    return out


def import_prices(steps: int) -> list[float]:
    """Return per-step day-ahead import prices in EUR/kWh.

    Based on the NL/BE EPEX spot pattern for spring 2025: cheap overnight
    (0.06-0.10), medium midday with solar merit-order dip (0.10-0.18), peak
    evening (0.25-0.35).  Day-to-day scaling applies light variation (+/- 20%)
    to simulate realistic week-long spread.

    Args:
        steps: Total number of 15-minute time steps.

    Returns:
        List of positive floats, length == steps.
    """
    # Representative hourly template, indexed by hour-of-day
    hourly_template = [
        0.085, 0.080, 0.075, 0.072, 0.074, 0.088,   # 0-5  overnight valley
        0.105, 0.140, 0.178, 0.185, 0.168, 0.152,   # 6-11 morning ramp + peak
        0.143, 0.140, 0.138, 0.145, 0.162, 0.198,   # 12-17 solar dip + ramp up
        0.268, 0.312, 0.295, 0.248, 0.190, 0.118,   # 18-23 evening peak + fall
    ]
    # Daily scaling factors: 7 days, mild week-to-week variation
    day_factors = [1.00, 0.85, 1.18, 0.92, 1.22, 0.96, 1.06]
    out: list[float] = []
    for i in range(steps):
        h_idx = int(i * DT) % 24
        day = i // 96
        factor = day_factors[day % 7]
        v = hourly_template[h_idx] * factor + random.gauss(0, 0.004)
        out.append(round(max(0.01, v), 4))
    return out


def export_prices(prices: list[float]) -> list[float]:
    """Return export prices as 80% of import, floored at 0.02 EUR/kWh."""
    return [round(max(0.02, p * 0.80), 4) for p in prices]


def make_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Scenario 1: minimal_home_24h
# ---------------------------------------------------------------------------
# Minimal residential setup:
#   - 1 standalone battery (10 kWh / 3.6 kW)
#   - 1 AC-coupled south PV (5 kWp)
#   - 1 static load (household)
# Horizon: 96 steps (24 hours)
# Purpose: fast baseline — small variable count, validates core battery+PV
# arbitrage.
# ---------------------------------------------------------------------------

def generate_scenario_1() -> None:
    s = BASE / "minimal_home_24h"
    make_dir(s)
    n = 96
    p = import_prices(n)
    bundle = {
        "strategy": "minimize_cost",
        "solve_time_utc": "2026-04-01T00:00:00Z",
        "horizon_prices": p,
        "horizon_export_prices": export_prices(p),
        "horizon_confidence": [1.0] * n,
        "pv_forecast": solar_curve(n, peak_kw=4.5, peak_hour=13.0),
        "base_load_forecast": base_load_curve(n),
        "battery_inputs": {"home_bat": {"soc_kwh": 4.0}},
        "ev_inputs": {},
        "hybrid_inverter_inputs": {},
        "thermal_boiler_inputs": {},
        "space_heating_inputs": {},
        "combi_hp_inputs": {},
        "deferrable_windows": {},
        "deferrable_start_times": {},
    }
    (s / "input.json").write_text(json.dumps(bundle, indent=2))

    config = """\
mqtt:
  host: localhost
  client_id: mimir-bench-1
  topic_prefix: mimir

outputs:
  schedule: mimir/schedule
  current: mimir/current
  last_solve: mimir/status/last_solve
  availability: mimir/status/availability

grid:
  import_limit_kw: 10.0
  export_limit_kw: 6.0

batteries:
  home_bat:
    capacity_kwh: 10.0
    charge_segments:
      - {power_max_kw: 3.6, efficiency: 0.95}
    discharge_segments:
      - {power_max_kw: 3.6, efficiency: 0.95}
    wear_cost_eur_per_kwh: 0.02
    inputs:
      soc:
        topic: mimir/input/home_bat/soc
        unit: kwh

pv_arrays:
  roof_south:
    max_power_kw: 5.0
    topic_forecast: mimir/input/pv/roof_south

static_loads:
  household:
    topic_forecast: mimir/input/baseload
"""
    (s / "config.yaml").write_text(config)
    print(f"  Scenario 1 written: {n} steps, {len(bundle['horizon_prices'])} price points")


# ---------------------------------------------------------------------------
# Scenario 2: prosumer_ev_48h
# ---------------------------------------------------------------------------
# Mid-size prosumer with an EV that needs charging and a boiler to heat.
# The solver has a clear incentive to pre-charge the EV during cheap midday
# PV generation and to delay boiler activation to off-peak hours.
#   - 1 standalone battery (10 kWh / 3.6 kW)
#   - 1 AC-coupled south PV (7 kWp)
#   - 1 EV (75 kWh, 11 kW, 20 kWh SOC, target 50 kWh)
#   - 1 thermal boiler (200 L DHW, 2 kW)
#   - 1 deferrable load (washing machine, 8-step profile)
#   - 1 static load (household)
# Horizon: 192 steps (48 hours)
# ---------------------------------------------------------------------------

def generate_scenario_2() -> None:
    s = BASE / "prosumer_ev_48h"
    make_dir(s)
    n = 192
    p = import_prices(n)
    bundle = {
        "strategy": "minimize_cost",
        "solve_time_utc": "2026-04-01T06:00:00Z",
        "horizon_prices": p,
        "horizon_export_prices": export_prices(p),
        "horizon_confidence": [1.0] * n,
        "pv_forecast": solar_curve(n, peak_kw=6.5, peak_hour=13.0),
        "base_load_forecast": base_load_curve(n),
        "battery_inputs": {"home_bat": {"soc_kwh": 3.0}},
        "ev_inputs": {
            "car": {
                "soc_kwh": 20.0,
                "available": True,
                "target_soc_kwh": 50.0,
                "window_earliest": "2026-04-01T06:00:00Z",
                "window_latest": "2026-04-02T08:00:00Z",
            }
        },
        "hybrid_inverter_inputs": {},
        "thermal_boiler_inputs": {
            "dhw": {"current_temp_c": 43.0}
        },
        "space_heating_inputs": {},
        "combi_hp_inputs": {},
        "deferrable_windows": {
            "washing_machine": {
                "earliest": "2026-04-01T08:00:00Z",
                "latest": "2026-04-01T22:00:00Z",
            }
        },
        "deferrable_start_times": {},
    }
    (s / "input.json").write_text(json.dumps(bundle, indent=2))

    config = """\
mqtt:
  host: localhost
  client_id: mimir-bench-2
  topic_prefix: mimir

outputs:
  schedule: mimir/schedule
  current: mimir/current
  last_solve: mimir/status/last_solve
  availability: mimir/status/availability

grid:
  import_limit_kw: 11.0
  export_limit_kw: 6.0

batteries:
  home_bat:
    capacity_kwh: 10.0
    charge_segments:
      - {power_max_kw: 3.6, efficiency: 0.95}
    discharge_segments:
      - {power_max_kw: 3.6, efficiency: 0.95}
    wear_cost_eur_per_kwh: 0.02
    inputs:
      soc:
        topic: mimir/input/home_bat/soc
        unit: kwh

pv_arrays:
  roof_south:
    max_power_kw: 7.0
    topic_forecast: mimir/input/pv/roof_south

ev_chargers:
  car:
    capacity_kwh: 75.0
    min_soc_kwh: 15.0
    charge_segments:
      - {power_max_kw: 11.0, efficiency: 0.93}
    discharge_segments: []
    wear_cost_eur_per_kwh: 0.03
    inputs:
      soc:
        topic: mimir/input/car/soc
        unit: kwh
      plugged_in_topic: mimir/input/car/available

thermal_boilers:
  dhw:
    volume_liters: 200.0
    elec_power_kw: 2.0
    cop: 1.0
    setpoint_c: 60.0
    min_temp_c: 43.0
    cooling_rate_k_per_hour: 1.5
    inputs:
      topic_current_temp: mimir/input/dhw/temp

deferrable_loads:
  washing_machine:
    power_profile: [2.0, 2.0, 0.5, 0.5, 0.5, 0.5, 2.2, 2.2]
    topic_window_earliest: mimir/input/washing_machine/earliest
    topic_window_latest: mimir/input/washing_machine/latest

static_loads:
  household:
    topic_forecast: mimir/input/baseload
"""
    (s / "config.yaml").write_text(config)
    print(f"  Scenario 2 written: {n} steps")


# ---------------------------------------------------------------------------
# Scenario 3: worst_case_7d
# ---------------------------------------------------------------------------
# Full residential installation at maximum realistic complexity:
#   - Grid: 11 kW import / 11 kW export (3-phase, 16A/phase)
#   - Batteries:
#     * standalone_bat: 14 kWh, 2-segment charge/discharge (7.2 kW peak)
#     * hybrid_south: 10 kWh hybrid inverter with 8 kWp south PV
#     * hybrid_se: 8 kWh hybrid inverter with 6 kWp south-east PV
#   - PV:
#     * hybrid_south PV (8 kWp south, DC, via hybrid_south inverter)
#     * hybrid_se PV (6 kWp south-east, DC, via hybrid_se inverter)
#     * roof_east (4 kWp east, AC-coupled)
#     * roof_west (4 kWp west, AC-coupled)
#     * bundle.pv_forecast = east + west (summed, AC side)
#   - EVs:
#     * ev_commuter: 65 kWh, plugged in overnight, needs 55 kWh by 07:30
#     * ev_secondary: 40 kWh, plugged in all week, gentle target
#   - Boiler: 200 L DHW, 3 kW element, needs daily reheating
#   - Space heating HP: 8 kW, COP 3.5, on/off, min-run 4 steps
#   - Deferrable loads: washing machine, tumble dryer, dishwasher
#   - Static load: household base load
# Horizon: 672 steps (7 days × 96 steps/day)
# ---------------------------------------------------------------------------

def generate_scenario_3() -> None:
    s = BASE / "worst_case_7d"
    make_dir(s)
    n = 672  # 7 days
    p = import_prices(n)

    # Hybrid inverter PV forecasts (per-inverter, DC side)
    pv_hybrid_south = solar_curve(n, peak_kw=8.0, peak_hour=13.0, half_width_h=5.5)
    pv_hybrid_se = solar_curve(n, peak_kw=6.0, peak_hour=12.2, half_width_h=5.0)

    # AC-coupled PV (east + west combined into pv_forecast)
    pv_east = solar_curve(n, peak_kw=4.0, peak_hour=10.5, half_width_h=4.5)
    pv_west = solar_curve(n, peak_kw=4.0, peak_hour=15.5, half_width_h=4.5)
    pv_ac = [round(e + w, 3) for e, w in zip(pv_east, pv_west)]

    # Space heating: 35 kWh total across 7 days, mild April
    sh_heat_total = 35.0

    bundle = {
        "strategy": "minimize_cost",
        "solve_time_utc": "2026-04-01T00:00:00Z",
        "horizon_prices": p,
        "horizon_export_prices": export_prices(p),
        "horizon_confidence": [1.0] * n,
        "pv_forecast": pv_ac,
        "base_load_forecast": base_load_curve(n),
        "battery_inputs": {
            "standalone_bat": {"soc_kwh": 5.0}
        },
        "ev_inputs": {
            "ev_commuter": {
                "soc_kwh": 12.0,
                "available": True,
                "target_soc_kwh": 55.0,
                "window_earliest": "2026-04-01T18:00:00Z",
                "window_latest": "2026-04-02T07:30:00Z",
            },
            "ev_secondary": {
                "soc_kwh": 22.0,
                "available": True,
                "target_soc_kwh": 35.0,
                "window_earliest": "2026-04-01T00:00:00Z",
                "window_latest": "2026-04-07T23:45:00Z",
            },
        },
        "hybrid_inverter_inputs": {
            "hybrid_south": {
                "soc_kwh": 4.0,
                "pv_forecast_kw": pv_hybrid_south,
            },
            "hybrid_se": {
                "soc_kwh": 3.0,
                "pv_forecast_kw": pv_hybrid_se,
            },
        },
        "thermal_boiler_inputs": {
            "dhw_boiler": {"current_temp_c": 48.0}
        },
        "space_heating_inputs": {
            "sh_hp": {
                "heat_needed_kwh": sh_heat_total,
                "current_indoor_temp_c": None,
                "outdoor_temp_forecast_c": None,
            }
        },
        "combi_hp_inputs": {},
        "deferrable_windows": {
            "washing_machine": {
                "earliest": "2026-04-01T08:00:00Z",
                "latest": "2026-04-01T20:00:00Z",
            },
            "tumble_dryer": {
                "earliest": "2026-04-01T10:00:00Z",
                "latest": "2026-04-01T22:00:00Z",
            },
            "dishwasher": {
                "earliest": "2026-04-02T07:00:00Z",
                "latest": "2026-04-02T21:00:00Z",
            },
        },
        "deferrable_start_times": {},
    }
    (s / "input.json").write_text(json.dumps(bundle, indent=2))

    config = """\
mqtt:
  host: localhost
  client_id: mimir-bench-3
  topic_prefix: mimir

outputs:
  schedule: mimir/schedule
  current: mimir/current
  last_solve: mimir/status/last_solve
  availability: mimir/status/availability

grid:
  import_limit_kw: 11.0
  export_limit_kw: 11.0

batteries:
  standalone_bat:
    capacity_kwh: 14.0
    charge_segments:
      - {power_max_kw: 3.6, efficiency: 0.95}
      - {power_max_kw: 3.6, efficiency: 0.92}
    discharge_segments:
      - {power_max_kw: 4.0, efficiency: 0.95}
      - {power_max_kw: 3.2, efficiency: 0.91}
    wear_cost_eur_per_kwh: 0.02
    inputs:
      soc:
        topic: mimir/input/standalone_bat/soc
        unit: kwh

hybrid_inverters:
  hybrid_south:
    capacity_kwh: 10.0
    min_soc_kwh: 0.5
    max_charge_kw: 5.0
    max_discharge_kw: 5.0
    battery_charge_efficiency: 0.95
    battery_discharge_efficiency: 0.95
    inverter_efficiency: 0.97
    max_pv_kw: 9.0
    topic_pv_forecast: mimir/input/hybrid_south/pv
    wear_cost_eur_per_kwh: 0.02
    inputs:
      soc:
        topic: mimir/input/hybrid_south/soc
        unit: kwh

  hybrid_se:
    capacity_kwh: 8.0
    min_soc_kwh: 0.5
    max_charge_kw: 4.0
    max_discharge_kw: 4.0
    battery_charge_efficiency: 0.95
    battery_discharge_efficiency: 0.95
    inverter_efficiency: 0.97
    max_pv_kw: 7.0
    topic_pv_forecast: mimir/input/hybrid_se/pv
    wear_cost_eur_per_kwh: 0.02
    inputs:
      soc:
        topic: mimir/input/hybrid_se/soc
        unit: kwh

pv_arrays:
  roof_east:
    max_power_kw: 4.5
    topic_forecast: mimir/input/pv/east

  roof_west:
    max_power_kw: 4.5
    topic_forecast: mimir/input/pv/west

ev_chargers:
  ev_commuter:
    capacity_kwh: 65.0
    min_soc_kwh: 10.0
    charge_segments:
      - {power_max_kw: 11.0, efficiency: 0.92}
    discharge_segments: []
    wear_cost_eur_per_kwh: 0.03
    inputs:
      soc:
        topic: mimir/input/ev_commuter/soc
        unit: kwh
      plugged_in_topic: mimir/input/ev_commuter/available

  ev_secondary:
    capacity_kwh: 40.0
    min_soc_kwh: 8.0
    charge_segments:
      - {power_max_kw: 7.4, efficiency: 0.93}
    discharge_segments: []
    wear_cost_eur_per_kwh: 0.03
    inputs:
      soc:
        topic: mimir/input/ev_secondary/soc
        unit: kwh
      plugged_in_topic: mimir/input/ev_secondary/available

thermal_boilers:
  dhw_boiler:
    volume_liters: 200.0
    elec_power_kw: 3.0
    cop: 1.0
    setpoint_c: 60.0
    min_temp_c: 45.0
    cooling_rate_k_per_hour: 1.5
    inputs:
      topic_current_temp: mimir/input/dhw_boiler/temp

space_heating_hps:
  sh_hp:
    elec_power_kw: 8.0
    cop: 3.5
    min_run_steps: 4
    wear_cost_eur_per_kwh: 0.01
    inputs:
      topic_heat_needed_kwh: mimir/input/sh_hp/heat_needed

deferrable_loads:
  washing_machine:
    power_profile: [2.0, 2.0, 0.5, 0.5, 0.5, 0.5, 2.2, 2.2]
    topic_window_earliest: mimir/input/washing_machine/earliest
    topic_window_latest: mimir/input/washing_machine/latest

  tumble_dryer:
    power_profile: [2.5, 2.5, 2.5, 2.5]
    topic_window_earliest: mimir/input/tumble_dryer/earliest
    topic_window_latest: mimir/input/tumble_dryer/latest

  dishwasher:
    power_profile: [1.8, 0.3, 0.3, 0.3, 1.5, 1.5]
    topic_window_earliest: mimir/input/dishwasher/earliest
    topic_window_latest: mimir/input/dishwasher/latest

static_loads:
  household:
    topic_forecast: mimir/input/baseload
"""
    (s / "config.yaml").write_text(config)
    print(f"  Scenario 3 written: {n} steps")


if __name__ == "__main__":
    print("Generating benchmark scenarios...")
    generate_scenario_1()
    generate_scenario_2()
    generate_scenario_3()
    print("Done.  Run: uv run pytest tests/benchmarks/ --benchmark-only")
