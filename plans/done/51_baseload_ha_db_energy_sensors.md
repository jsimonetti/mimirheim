# Plan 51 — Energy sensor support and outlier detection for baseload_ha_db

## Motivation

The `baseload_ha_db` tool currently only reads the `mean` column from the HA
recorder `statistics` table. This works correctly for power sensors that update
frequently (e.g. a P1 meter at 10 s intervals), but has two weaknesses:

1. **Energy sensors are excluded.** HA's `total_increasing` kWh counters are the
   most reliable per-hour energy source because the HA recorder integrates the
   counter over any reporting gaps. There is no way to configure them today.

2. **Corrupt `mean` values are not detected.** The Marstek inverter integration
   wrote two garbage values (250,255 W and 118,845 W) into the recorder for a
   device rated at 2,500 W maximum. Because those appeared at the same local hour
   (18:00), they dominated the hour-of-day average, producing a spurious 7 kW spike
   in the published forecast. The tool had no way to detect or reject them.

---

## Relevant source locations

```
mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/fetcher.py
mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/forecast.py
mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/config.py
mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/__main__.py
mimirheim_helpers/baseload/homeassistant_db/tests/unit/test_fetcher.py
mimirheim_helpers/baseload/homeassistant_db/tests/unit/test_forecast.py
mimirheim_helpers/baseload/homeassistant_db/tests/unit/test_config.py
mimirheim_helpers/baseload/homeassistant_db/README.md
mimirheim_helpers/examples/baseload-ha-db.yaml
dev/baseload-ha-db.yaml
```

---

## Design decisions

### Sensor type detection — automatic from the database

`statistics_meta.unit_of_measurement` determines the sensor type. No config field
is required from the user.

| Unit | Sensor type | Column used | Conversion to kWh/h |
|---|---|---|---|
| `mW`, `W`, `kW`, `MW`, `GW` | power | `mean` | multiply by power unit multiplier |
| `Wh`, `kWh`, `MWh` | energy | `sum` delta | multiply by energy unit multiplier |

An entity whose unit is in neither table raises `FetchError` with a descriptive
message that names the unsupported unit and the entity ID.

The user-configurable `unit` override on `EntityConfig` (introduced in Plan 51)
continues to work. When a unit override is present it takes precedence over the
database value for type detection.

### Energy sensor delta computation

For an energy sensor, `fetch_statistics` queries the `sum` column. To compute the
energy consumed in each hourly bucket, consecutive differences are taken:

    delta[t] = sum[t] - sum[t-1]

To ensure the first delta in the lookback window can be computed, one extra row
**before** the window start is fetched. Without this, the first bucket of the
window would always be missing.

HA guarantees that `sum` is monotonically non-decreasing at the recorder level
(it normalises resets from `total_increasing` sensors internally). If a delta is
negative despite this guarantee -- indicating a recorder bug or data corruption --
the delta is treated as an outlier and handled by the detection step below.

### Outlier detection -- P95 relative threshold

Outlier detection is applied per entity, after raw value extraction, before
any hour-of-day averaging. It applies to both power sensors (on the absolute
`mean` value) and energy sensors (on the computed delta). The algorithm:

1. Collect all extracted values for the entity (absolute `mean` for power sensors,
   computed deltas for energy sensors) across the full lookback window.
2. If fewer than **24** valid samples exist: skip detection entirely, log a WARNING,
   and pass all values through unchanged. With fewer than one full day of data
   the statistical threshold would be unreliable.
3. Sort the values and compute the **95th percentile** (P95). P95 is chosen rather
   than the maximum because it is robust against the very outliers being detected:
   with 1360 readings, two corrupt values represent 0.15% of the data and do not
   influence P95 at all.
4. **Zero-inflation guard:** if P95 == 0.0 (common for devices that are idle most
   of the time, e.g. a battery at rest), use the highest non-zero value as the
   effective P95. This prevents a threshold of 0 from incorrectly flagging all
   non-zero readings.
5. `threshold = P95_effective * outlier_factor`
   The default `outlier_factor` is **10.0**. At this factor:
   - Marstek at P95_effective ~ 2 kW gives threshold = 20 kW. Both 250 kW and
     119 kW corrupt readings are caught with an order-of-magnitude margin.
   - A dishwasher at P95 ~ 2 kW also gives threshold = 20 kW. No legitimate
     appliance reading in a residential context would be flagged.
   - The factor can be tightened per entity via `outlier_factor: 5.0` when the
     operator knows the device's physical maximum.
6. Any reading whose absolute value exceeds the threshold is **dropped** (not
   clamped). Clamping would silently corrupt the average; dropping makes the
   decision explicit and logged.
7. Every dropped reading is logged at WARNING level with: entity ID, timestamp,
   actual value, and threshold.

**Important:** P95 is used only to set the detection threshold. The baseload
forecast is always the weighted same-hour-of-day average of the surviving clean
values. P95 itself is never used as a forecast value.

### Uniform output format

After outlier filtering, both sensor types produce a uniform list of
`{"start": ISO str, "mean": kWh_value}` dicts. The `kWh_value` is already
converted from the entity's native unit. `HourlyProfile.from_readings` receives
these dicts and performs the hour-of-day averaging -- it no longer needs to know
the original unit or sensor type.

This means the `unit` parameter is **removed** from `HourlyProfile.from_readings`.
All unit conversion happens in `fetch_statistics`, not in the forecast layer.
`build_forecast` also drops `sum_units` and `subtract_units` parameters.

### Config changes -- minimal

`EntityConfig` gains one new optional field:

```python
outlier_factor: float = Field(default=10.0, gt=0.0)
```

The `unit` Literal is widened to include energy units:
`"mW" | "W" | "kW" | "MW" | "GW" | "Wh" | "kWh" | "MWh"`.

No `sensor_type` field is added. Type is always derived from the resolved unit.

---

## Tests to write first (all must fail before implementation begins)

Run: `uv run pytest mimirheim_helpers/baseload/homeassistant_db/tests/ --tb=short -q`

### `tests/unit/test_fetcher.py` additions

```python
def test_power_entity_uses_mean_column() -> None:
    """An entity with unit W returns mean-based kWh/h values."""

def test_energy_entity_uses_sum_deltas() -> None:
    """An entity with unit kWh returns sum-delta kWh values."""

def test_energy_entity_fetches_extra_pre_window_row() -> None:
    """The first delta in the window is computed from the row immediately
    before the window start rather than being absent."""

def test_negative_energy_delta_is_discarded() -> None:
    """A negative sum delta is dropped and does not appear in the returned
    readings."""

def test_unknown_unit_raises_fetch_error() -> None:
    """An entity whose statistics_meta unit is not in the power or energy
    tables raises FetchError naming the unsupported unit and entity ID."""

def test_outlier_above_p95_threshold_is_dropped() -> None:
    """A power reading > P95 * outlier_factor is not in the returned
    readings."""

def test_reading_just_below_threshold_is_kept() -> None:
    """A reading at P95 * outlier_factor - epsilon survives."""

def test_zero_inflated_distribution_uses_max_nonzero_as_p95() -> None:
    """When P95 == 0, the threshold is based on the max non-zero value."""

def test_fewer_than_24_samples_skips_detection() -> None:
    """With < 24 samples no values are dropped regardless of magnitude."""

def test_energy_unit_wh_correctly_converted_to_kwh() -> None:
    """An entity with unit Wh has deltas divided by 1000 before returning."""

def test_energy_unit_mwh_correctly_converted_to_kwh() -> None:
    """An entity with unit MWh has deltas multiplied by 1000."""
```

### `tests/unit/test_forecast.py` additions

```python
def test_from_readings_does_not_accept_unit_parameter() -> None:
    """Passing unit= to HourlyProfile.from_readings raises TypeError."""

def test_kwh_values_pass_through_unchanged() -> None:
    """A reading with mean=3.5 produces kw_for_hour == 3.5."""
```

### `tests/unit/test_config.py` additions

```python
def test_entity_config_outlier_factor_defaults_to_ten() -> None:
def test_entity_config_outlier_factor_above_zero_accepted() -> None:
def test_entity_config_outlier_factor_zero_rejected() -> None:
def test_entity_config_outlier_factor_negative_rejected() -> None:
def test_entity_config_energy_unit_kwh_accepted() -> None:
def test_entity_config_energy_unit_wh_accepted() -> None:
def test_entity_config_energy_unit_mwh_accepted() -> None:
```

---

## Implementation sequence

1. Write all tests above -- confirm red.
2. `config.py`: add `outlier_factor`; widen `unit` Literal.
3. `fetcher.py`:
   - Add `_ENERGY_UNIT_TO_KWH` table.
   - Add `_sensor_type(unit)` -> `"power" | "energy"` or raise `FetchError`.
   - Add `_compute_deltas(rows)` -> list of `(start_ts, delta)` with
     negative-delta discard.
   - Add `_detect_outliers(values, outlier_factor)` -> filtered list + warning log.
   - Rewrite `fetch_statistics` to branch on type, apply deltas + detection,
     return uniform kWh readings.
4. `forecast.py`:
   - Remove `unit` param from `HourlyProfile.from_readings`.
   - Remove `_to_kw_multiplier` call.
   - Remove `sum_units` / `subtract_units` from `build_forecast`.
5. `__main__.py`: remove unit dicts; pass `outlier_factor` per entity.
6. `README.md`: add section on power vs. energy detection and full outlier
   detection documentation.
7. Update `dev/baseload-ha-db.yaml` and `mimirheim_helpers/examples/baseload-ha-db.yaml`.
8. `uv run pytest` -- all green.

---

## Acceptance criteria

- [ ] Power entity (W) returns mean-based kWh/h values
- [ ] Energy entity (kWh) returns sum-delta kWh values
- [ ] Extra pre-window row fetched; first delta not missing
- [ ] Corrupt delta (250 kWh in one hour) dropped with WARNING log
- [ ] Reading just below threshold kept
- [ ] Zero-inflated P95 falls back to max non-zero
- [ ] Fewer than 24 samples skips detection
- [ ] Unknown unit raises FetchError with entity ID and unit in message
- [ ] `HourlyProfile.from_readings` has no `unit` parameter
- [ ] `EntityConfig.outlier_factor` defaults to 10.0; rejects <= 0
- [ ] `EntityConfig.unit` accepts Wh, kWh, MWh
- [ ] README documents outlier detection algorithm
- [ ] Dev config updated
- [ ] Full test suite green
