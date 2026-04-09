# Step 03 — Core runtime models (SolveBundle and SolveResult)

## References

- IMPLEMENTATION_DETAILS §7 (SolveBundle, per-device input models, SolveResult)

---

## Files to create

- `mimirheim/core/bundle.py`
- `tests/unit/test_bundle.py`

Note: `tests/unit/test_bundle.py` is not listed in the canonical test structure in AGENTS.md. Create it as a new file — it covers a distinct concern (runtime model validation) that does not belong in `test_config_schema.py`.

---

## Note on SolveBundle fields

The golden file example in IMPLEMENTATION_DETAILS §4 shows a `solve_time_utc` field and structured price objects (`{"t": 0, "import_price_eur_kwh": ..., "confidence": ...}`). The `SolveBundle` model described in §7 uses flat lists (`horizon_prices: list[float]`). The flat-list format from §7 is authoritative for the implementation. `solve_time_utc` must be added to `SolveBundle` — it is required to translate EV window datetimes (step 09) and deferrable load windows (step 10) to time step indices. Add it as `solve_time_utc: datetime`.

---

## Tests first

Create `tests/unit/test_bundle.py`. All tests must fail before any implementation exists.

### Happy path

- `test_battery_inputs_valid` — fresh timestamp (now), valid `soc_kwh=5.0`
- `test_ev_inputs_valid` — `available=True`, `soc_kwh=20.0`, valid `window_earliest` and `window_latest`
- `test_ev_inputs_not_plugged` — `available=False`, no window required
- `test_deferrable_window_valid` — `earliest` before `latest`
- `test_solve_bundle_strategy_defaults_to_minimize_cost` — construct `SolveBundle` without providing `strategy`; assert `bundle.strategy == "minimize_cost"`
- `test_solve_bundle_valid` — 96-step price/confidence/pv lists, no device inputs
- `test_solve_bundle_with_devices` — includes `battery_inputs` and `ev_inputs`
- `test_solve_result_valid` — constructs `SolveResult` with one `ScheduleStep`

### Sad path

- `test_battery_inputs_stale_rejected` — `timestamp` 10 minutes in the past must raise `ValidationError`
- `test_battery_inputs_negative_soc_rejected` — `soc_kwh=-1.0` must raise
- `test_solve_bundle_prices_too_short_rejected` — fewer than 96 price steps must raise
- `test_solve_bundle_extra_field_rejected` — unknown field on `SolveBundle` must raise
- `test_solve_result_extra_field_rejected` — unknown field on `SolveResult` must raise
- `test_device_setpoint_extra_field_rejected`

Run `uv run pytest tests/unit/test_bundle.py` — all tests must fail before proceeding.

---

## Implementation

Implement `mimirheim/core/bundle.py` containing all of the following. Every model must have `model_config = ConfigDict(extra="forbid")`.

**`BatteryInputs`**
- `soc_kwh: float` — Field(ge=0)
- `timestamp: datetime`
- `model_validator(mode="after")`: raise `ValueError` if `datetime.now(UTC) - self.timestamp > timedelta(minutes=5)`

**`EvInputs`**
- `soc_kwh: float` — Field(ge=0)
- `available: bool`
- `window_earliest: datetime | None` — default None
- `window_latest: datetime | None` — default None
- `timestamp: datetime`

**`DeferrableWindow`**
- `earliest: datetime`
- `latest: datetime`

**`SolveBundle`**
- `strategy: str` — default `"minimize_cost"`; the active optimisation strategy, populated from `mimir/input/strategy` MQTT topic by the IO layer before assembling the bundle
- `solve_time_utc: datetime` — the UTC timestamp at which this solve cycle begins; used to convert window datetimes to step indices
- `horizon_prices: list[float]` — Field(min_length=96); import price in EUR/kWh per step
- `horizon_export_prices: list[float]` — Field(min_length=96); export price in EUR/kWh per step
- `horizon_confidence: list[float]` — Field(min_length=96); per-step forecast confidence in [0, 1]
- `pv_forecast: list[float]` — Field(min_length=96); PV power forecast in kW per step
- `base_load_forecast: list[float]` — Field(min_length=96); static load forecast in kW per step
- `battery_inputs: dict[str, BatteryInputs]` — default empty
- `ev_inputs: dict[str, EvInputs]` — default empty
- `deferrable_windows: dict[str, DeferrableWindow]` — default empty

**`DeviceSetpoint`**
- `kw: float` — net power setpoint; positive = producing, negative = consuming
- `type: str` — device type derived from config section (e.g. "battery", "ev_charger")

**`ScheduleStep`**
- `t: int` — time step index
- `grid_import_kw: float`
- `grid_export_kw: float`
- `devices: dict[str, DeviceSetpoint]` — keyed by device name

**`SolveResult`**
- `strategy: str`
- `objective_value: float`
- `solve_status: str` — "optimal" | "feasible" | "infeasible"
- `schedule: list[ScheduleStep]`

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_bundle.py
```

All tests green.

---

## Done

```bash
mv plans/03_core_models.md plans/done/
```
