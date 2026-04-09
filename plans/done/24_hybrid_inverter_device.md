# Step 24 — DC-coupled Hybrid Inverter device

## References

- IMPLEMENTATION_DETAILS §7, subsection "Device Protocol"
- IMPLEMENTATION_DETAILS §6, subsection "Module boundaries"
- IMPLEMENTATION_DETAILS §8, subsections "Power balance", "Piecewise efficiency"
- `mimirheim/devices/battery.py` — existing AC-coupled battery for structural reference
- `mimirheim/devices/pv.py` — existing PV device for structural reference
- `mimirheim/config/schema.py` — `BatteryConfig`, `PvConfig`

---

## Files to create

- `mimirheim/devices/hybrid_inverter.py`
- `mimirheim/config/schema.py` (new models: `HybridInverterConfig`, `HybridInverterInputs`)
- `tests/unit/test_hybrid_inverter_constraints.py`

## Files to modify

- `mimirheim/core/model_builder.py` — wire the new device into the solve loop
- `mimirheim/config/schema.py` — add `hybrid_inverters` map to `MimirheimConfig`
- `mimirheim/core/bundle.py` — add `HybridInverterInputs` and map in `SolveBundle`
- `mimirheim/io/input_parser.py` — parse MQTT inputs for the new device
- `mimirheim/io/mqtt_publisher.py` — publish setpoints for the new device

---

## Background

The existing `Battery` device models an **AC-coupled** storage system: the battery
inverter is a standalone unit on the AC bus. PV also connects to the AC bus via a
separate inverter. The solver sees only AC-side power flows and does not need to know
about the internal DC wiring.

A **DC-coupled hybrid inverter** is architecturally different. The PV array (MPPT
input) and battery (DC bus) are both connected to the same DC bus inside the inverter.
The inverter converts between the shared DC bus and the AC bus. This means:

- PV charges the battery directly on the DC bus without going through AC conversion.
- The DC bus power balance must be modelled explicitly:

```
pv_dc[t] + battery_dc_from_ac[t] = battery_dc_to_ac[t] + battery_dc_charge[t]
```

where:
- `pv_dc[t]` — PV power at the MPPT input (DC), in kW
- `battery_dc_from_ac[t]` — power flowing from AC bus through inverter to DC bus
  (charging from grid/load); AC to DC conversion at inverter efficiency
- `battery_dc_to_ac[t]` — power flowing from DC bus through inverter to AC bus
  (discharging to grid/load); DC to AC conversion at inverter efficiency
- `battery_dc_charge[t]` — net DC power delivered to the battery cells (after BMS
  and cell efficiency); subject to the battery's own charge/discharge efficiency

The AC-side variables exposed to `build_and_solve`'s power balance are:
- `net_power_ac[t] = battery_dc_to_ac[t] × η_inverter − battery_dc_from_ac[t] / η_inverter + pv_dc[t] × η_inverter`
  (simplified; see implementation for the exact linearisation)

**Why a separate device class?** The hybrid inverter's power balance is fundamentally
different from an AC-coupled battery plus an AC-coupled PV. A hybrid inverter cannot
be configured as a `Battery` plus a `PvDevice` because:

1. The DC bus constraint couples the PV and battery variables — they share a solver
   constraint that does not exist in AC-coupled systems.
2. An AC-coupled battery must not simultaneously charge from AC and discharge to AC;
   a hybrid inverter must additionally not have PV flow and DC charging conflict.
3. The setpoint published after the solve is a single combined command to the same
   physical unit, not separate commands to two devices.

**When does this matter?** Use `HybridInverterDevice` when:
- The battery and PV share a DC bus (typical of Huawei SUN2000, Sungrow SH, Fronius
  Gen24, GoodWe ET/BT series hybrid inverters).
- The battery SOC feedback comes from the inverter, not a separate BMS.

Use separate `Battery` + `PvDevice` when:
- The battery has its own standalone inverter on the AC bus.
- PV connects to the AC bus via a separate string inverter.

---

## Tests first

Create `tests/unit/test_hybrid_inverter_constraints.py`. Use a real solver with
`T=4`, `dt=0.25`.

- `test_dc_bus_balance_pv_charges_battery_directly` — configure a hybrid inverter
  with 5 kW PV and a battery at 50% SOC. Give zero base load and a flat price.
  Assert that the battery charges from PV via the DC bus (no AC import), that
  `pv_dc[t]` equals `battery_dc_charge[t]`, and AC import is zero.
- `test_dc_bus_balance_battery_discharges_to_ac` — configure a hybrid inverter with
  no PV (forecast=0). Force a fixed AC load. Assert that `battery_dc_to_ac[t]` covers
  the load and no import occurs.
- `test_dc_bus_pv_surplus_exported_to_ac` — PV exceeds load and battery is full.
  Assert that surplus flows from `pv_dc[t]` through the inverter to AC (`net_power_ac`
  is positive) and battery charge is zero.
- `test_no_simultaneous_ac_import_and_export_from_hybrid` — provide conditions where
  an unconstrained LP might import and export simultaneously. Assert that the
  hybrid inverter constraints prevent this without needing the separate grid binary.
- `test_hybrid_inverter_soc_tracks_charging` — same physical scenario as
  `test_battery_soc_tracks_charging` from plan 07. Assert SOC tracking is identical
  for a DC-coupled system delivering the same net AC power.
- `test_hybrid_inverter_wear_cost_discourages_cycling` — same scenario as
  `test_battery_wear_cost_suppresses_cycling`. Wear cost applies to DC throughput.
- `test_hybrid_inverter_net_power_ac_sign_convention` — charging reduces net AC power
  (pulling from AC bus): `net_power_ac < 0`. Discharging increases it: `net_power_ac > 0`.

Run `uv run pytest tests/unit/test_hybrid_inverter_constraints.py` — all tests must
fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py`

Add `HybridInverterConfig`:

```python
class HybridInverterConfig(BaseModel):
    """Configuration for a DC-coupled hybrid inverter.

    A hybrid inverter integrates PV MPPT input, a battery DC bus, and an AC
    grid connection into a single unit. Use this device class when PV and
    battery share a DC bus inside the inverter. Use separate PvDevice and
    Battery for AC-coupled systems.

    Attributes:
        capacity_kwh: Usable battery capacity in kWh.
        min_soc_kwh: Minimum battery SOC in kWh.
        max_charge_kw: Maximum DC charge power to the battery cells in kW.
        max_discharge_kw: Maximum DC discharge power from the battery cells in kW.
        battery_charge_efficiency: Efficiency of the battery charge process (DC in
            to cell energy stored). Accounts for BMS losses.
        battery_discharge_efficiency: Efficiency of battery discharge (cell energy
            to DC out).
        inverter_efficiency: AC-to-DC (and DC-to-AC) conversion efficiency of the
            hybrid inverter. Applied symmetrically to both directions.
        max_pv_kw: Peak PV power at the MPPT input in kW. Used to clip forecasts.
        wear_cost_eur_per_kwh: Battery degradation cost per kWh of DC throughput.
        topic_pv_forecast: MQTT topic for per-step PV DC power forecast in kW.
        inputs: MQTT topics for live battery state readings.
    """

    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float = Field(gt=0)
    min_soc_kwh: float = Field(ge=0, default=0.0)
    max_charge_kw: float = Field(gt=0)
    max_discharge_kw: float = Field(gt=0)
    battery_charge_efficiency: float = Field(gt=0, le=1.0, default=0.95)
    battery_discharge_efficiency: float = Field(gt=0, le=1.0, default=0.95)
    inverter_efficiency: float = Field(gt=0, le=1.0, default=0.97)
    max_pv_kw: float = Field(gt=0)
    wear_cost_eur_per_kwh: float = Field(ge=0, default=0.0)
    topic_pv_forecast: str
    inputs: BatteryInputsConfig | None = None
```

Add `hybrid_inverters: dict[str, HybridInverterConfig] = Field(default_factory=dict)`
to `MimirheimConfig`.

### `mimirheim/devices/hybrid_inverter.py`

`HybridInverterDevice` implements the Device Protocol.

**Variables per time step `t`:**

- `pv_dc[t]` — PV DC power at MPPT, bounds `[0, pv_forecast[t]]`; continuous
- `bat_charge_dc[t]` — DC power flowing into battery cells, bounds `[0, max_charge_kw]`
- `bat_discharge_dc[t]` — DC power flowing out of battery cells, bounds `[0, max_discharge_kw]`
- `ac_to_dc[t]` — power drawn from AC bus by inverter (grid/load → battery charging or house loads), bounds `[0, max_charge_kw / inverter_efficiency]`
- `dc_to_ac[t]` — power delivered to AC bus by inverter (discharge → grid, or PV surplus), bounds `[0, (max_discharge_kw + max_pv_kw) × inverter_efficiency]`
- `soc[t]` — battery SOC in kWh, bounds `[min_soc_kwh, capacity_kwh]`
- `mode[t]` — binary; 1 = net DC charging (battery receives power), 0 = net DC discharging

**Constraints:**

*DC bus balance (the core hybrid inverter constraint):*
```
pv_dc[t] + (ac_to_dc[t] × η_inv) = (dc_to_ac[t] / η_inv) + bat_charge_dc[t]
```
Equivalently:
```
pv_dc[t] + ac_to_dc[t] × η_inv − dc_to_ac[t] / η_inv − bat_charge_dc[t] = 0
```
(Note: use `η_inv` for both directions; alternatively model asymmetric conversion
if the inverter specifies separate AC→DC and DC→AC efficiencies.)

*Battery SOC dynamics:*
```
soc[t] = soc[t-1] + (bat_charge_dc[t] × η_bat_charge − bat_discharge_dc[t] / η_bat_discharge) × dt
```

*Big-M guard (simultaneous charge/discharge prevention):*
```
bat_charge_dc[t]    ≤ max_charge_kw    × mode[t]
bat_discharge_dc[t] ≤ max_discharge_kw × (1 − mode[t])
```

**`net_power_ac(t)`:**
```
net_power_ac[t] = dc_to_ac[t] − ac_to_dc[t]
```
Positive = net injection to AC bus (discharging / PV export). Negative = net
consumption from AC bus (charging from grid).

**`objective_terms(t)`:**
```
wear_cost_eur_per_kwh × (bat_charge_dc[t] + bat_discharge_dc[t]) × dt
```

### `mimirheim/core/model_builder.py`

Instantiate `HybridInverterDevice` instances for each entry in
`config.hybrid_inverters` and include them in the power balance, alongside batteries
and PV devices. The power balance term is `device.net_power_ac(t)` (consistent with
the Device Protocol's `net_power(t)` method name — rename in the device class if
needed to maintain Protocol compatibility).

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_hybrid_inverter_constraints.py
```

All tests green.

```bash
uv run pytest tests/scenarios/ tests/unit/
```

All pre-existing tests remain green. No golden file changes.

---

## Done

```bash
mv plans/24_hybrid_inverter_device.md plans/done/
```
