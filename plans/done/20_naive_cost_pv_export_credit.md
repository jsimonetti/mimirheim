# Step 20 — Fix PV export credit in naive cost baseline

## References

- IMPLEMENTATION_DETAILS §8, subsection "Naive baseline cost"
- `mimirheim/core/model_builder.py` — `_compute_naive_cost`

---

## Files to modify

- `mimirheim/core/model_builder.py`
- `tests/unit/` — new file `tests/unit/test_model_builder.py`
- `tests/scenarios/*/golden.json` — update via `pytest --update-golden`

---

## Background

`_compute_naive_cost` computes the energy cost a household would incur without any
storage dispatch: the grid supplies whatever PV cannot cover. Currently:

```python
sum(
    max(0.0, bundle.base_load_forecast[t] - bundle.pv_forecast[t])
    * bundle.horizon_prices[t]
    * dt
    for t in range(horizon)
)
```

The `max(0.0, ...)` clips the net to zero when PV exceeds base load. This means PV
surplus is modelled as "free" — the formula ignores that a real grid-connected
household *exports* the surplus and receives export revenue (or incurs a cost when
export prices are negative).

The correct naive baseline is:

```
for each step t:
    net_kw = base_load[t] - pv[t]
    if net_kw >= 0:
        # Load exceeds PV: household imports net_kw from the grid.
        cost += net_kw × import_price[t] × dt
    else:
        # PV exceeds load: household exports -net_kw to the grid.
        # net_kw is negative, so this subtracts from total cost (adds revenue).
        # If export_price[t] is also negative, this adds to total cost.
        cost += net_kw × export_price[t] × dt
```

This change may reduce `naive_cost_eur` in scenarios with PV surplus (more revenue
credited), or increase it if the export price is negative. It makes `naive_cost_eur`
an accurate baseline for computing the gain from storage dispatch.

---

## Tests first

Create `tests/unit/test_model_builder.py` with the following tests. Tests must be
pure unit tests that call `_compute_naive_cost` and `_compute_optimised_cost` directly
without a solver.

- `test_naive_cost_no_pv` — base_load = 4.0 kW for 4 steps, no PV, import_price =
  0.25 EUR/kWh, dt = 0.25. Expected naive cost = 4 × 0.25 × 0.25 = 0.25 EUR. Assert
  `abs(result - 0.25) < 1e-9`.
- `test_naive_cost_pv_exactly_covers_load` — base_load = pv = 3.0 kW at all steps.
  Expected naive cost = 0.0. (No import or export.) Assert cost is 0.0.
- `test_naive_cost_pv_surplus_credits_export_revenue` — step 0: base_load = 1.0 kW,
  pv = 3.0 kW, import_price = 0.25, export_price = 0.08. Net = −2.0 kW (exporting).
  `dt = 0.25`. Expected contribution from step 0 = −2.0 × 0.08 × 0.25 = −0.04 EUR.
  Total naive cost for a 1-step horizon = −0.04 EUR. Assert result < 0.
- `test_naive_cost_negative_export_price_adds_to_cost` — step 0: base_load = 0.0,
  pv = 4.0 kW, export_price = −0.02 EUR/kWh. Exporting 4 kW at a negative price
  incurs a cost. Expected contribution = −4.0 × (−0.02) × 0.25 = +0.02 EUR. Assert
  result > 0.
- `test_naive_cost_mixed_steps` — two steps: step 0 has surplus (pv > load), step 1
  has deficit (load > pv). Assert the total equals the sum of the individual step
  contributions computed with the new formula.
- `test_naive_cost_does_not_use_old_max_zero_clip` — in the `pv_surplus` scenario,
  assert that the result differs from what the old formula (which clips to zero) would
  produce. This documents that the regression guard compares against the corrected
  baseline.

Run `uv run pytest tests/unit/test_model_builder.py` — all six tests must fail before
writing any implementation code.

---

## Implementation

### `mimirheim/core/model_builder.py` — `_compute_naive_cost`

Replace the function body with:

```python
total = 0.0
for t in range(horizon):
    net_kw = bundle.base_load_forecast[t] - bundle.pv_forecast[t]
    if net_kw >= 0.0:
        # Load exceeds PV: import the shortfall from the grid.
        total += net_kw * bundle.horizon_prices[t] * dt
    else:
        # PV exceeds load: export the surplus to the grid.
        # net_kw is negative; multiplying by export_price and dt gives a
        # negative cost (revenue) when export_price > 0, or a positive cost
        # (penalty) when export_price < 0.
        total += net_kw * bundle.horizon_export_prices[t] * dt
return total
```

Update the function docstring to reflect the corrected formula, including the
explicit treatment of PV surplus as export revenue.

### Golden file updates

After the implementation passes its unit tests, run:

```bash
uv run pytest tests/scenarios/ --update-golden
```

Review each diff. For scenarios with daytime PV surplus, `naive_cost_eur` will
decrease (export revenue credited). For scenarios with near-zero export prices,
the change will be negligible. For scenarios with negative export prices, it will
increase. Confirm each change is directionally correct before committing.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_model_builder.py
```

All six tests green.

```bash
uv run pytest tests/scenarios/
```

Scenarios green (golden files updated in the step above).

---

## Done

```bash
mv plans/20_naive_cost_pv_export_credit.md plans/done/
```
