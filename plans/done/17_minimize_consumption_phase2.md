# Step 17 — `minimize_consumption` phase-2 cost optimisation

## References

- IMPLEMENTATION_DETAILS §8, subsection "Two-phase `minimize_consumption`"
- IMPLEMENTATION_DETAILS §8, subsection "Confidence weights"
- `mimirheim/core/objective.py` — `ObjectiveBuilder._minimize_consumption` and `_minimize_cost`

---

## Files to modify

- `mimirheim/core/objective.py`
- `tests/unit/test_objective_builder.py`

---

## Background

The `minimize_consumption` strategy uses a two-phase lexicographic solve:

1. **Phase 1:** Minimise total grid import volume. Call `solver.solve()` to find I\*.
2. **Phase 2:** Lock in `Σ import[t] ≤ I* + ε`, then optimise a secondary objective.

The current Phase 2 secondary objective is *maximise export revenue plus terminal SoC
value*. This means the solver does not care which time steps the locked-in import
volume falls on — it is indifferent between importing 2 kWh at 0.30 EUR/kWh and
importing 2 kWh at 0.05 EUR/kWh, as long as total volume is the same.

The correct secondary objective is to **minimise the full net cost** (import cost minus
export revenue), subject to the Phase 1 import-volume constraint. This has two effects:

- Among all schedules with the same minimum import volume, the solver picks the one
  that places imports at the cheapest time slots.
- Export is still maximised (higher export revenue reduces net cost), but timing matters:
  exporting at a high-price step is preferred over the same volume at a low-price step.

The updated Phase 2 objective matches `_minimize_cost` exactly (confidence-weighted
net cost + device wear terms + terminal SoC value), with the additional I\* constraint
already in place from Phase 1.

---

## Tests first

Add to `tests/unit/test_objective_builder.py`. Use a real solver with `T=2`, `dt=0.25`.

- `test_minimize_consumption_phase2_shifts_import_to_cheaper_step` — configure two steps
  with import prices `[1.0, 0.10]` EUR/kWh and a static load of 1 kW per step (no PV,
  no storage). Assert that after Phase 2, `import[1] > import[0]` — the cheap step
  carries more of the forced import. The total import must equal the Phase 1 optimum.
- `test_minimize_consumption_phase2_maximises_export_at_higher_price_step` — configure
  two steps with equal import prices but export prices `[0.05, 0.15]`. Give the battery
  enough charge to export 1 kWh total. Assert that export is concentrated at step 1
  (higher export price) after Phase 2.
- `test_minimize_consumption_phase2_respects_i_star_constraint` — run a two-step scenario
  where Phase 1 produces a known I\*. Assert that the total import in the Phase 2
  solution is ≤ I\* + 2 × 1e-4.
- `test_minimize_consumption_phase2_includes_device_wear_terms` — configure a battery
  with `wear_cost_eur_per_kwh=1.0` and prices that give a zero-gain arbitrage. Assert
  that after Phase 2 the battery does not cycle (total throughput is zero or
  near-zero), whereas with `wear_cost_eur_per_kwh=0.0` it would.

Run `uv run pytest tests/unit/test_objective_builder.py -k "phase2"` — all four tests
must fail before writing any implementation code.

---

## Implementation

### `mimirheim/core/objective.py` — `_minimize_consumption`

Replace the Phase 2 objective construction (everything after the `add_constraint` call
that locks in I\*) with the same logic used in `_minimize_cost`:

```python
# Phase 2: minimise confidence-weighted net cost subject to the locked-in import
# volume from Phase 1. This shifts imports to the cheapest time slots and
# simultaneously maximises export revenue. Device wear cost and terminal SoC
# value are included exactly as in _minimize_cost.
obj_terms: list[Any] = []
for t in ctx.T:
    economic = weight_by_confidence(
        bundle.horizon_prices[t] * grid.import_[t]
        - bundle.horizon_export_prices[t] * grid.export_[t],
        bundle.horizon_confidence[t],
    )
    # weight_by_confidence returns Python int 0 when confidence == 0; only
    # append solver expressions.
    if not isinstance(economic, (int, float)):
        obj_terms.append(economic)
    for d in devices:
        wear = d.objective_terms(t)
        if not isinstance(wear, (int, float)):
            obj_terms.append(wear)

for term in self._terminal_soc_terms(ctx, devices, bundle):
    obj_terms.append(term)

if obj_terms:
    obj: Any = obj_terms[0]
    for term in obj_terms[1:]:
        obj = obj + term
    ctx.solver.set_objective_minimize(obj)
else:
    ctx.solver.set_objective_minimize(0)
```

Update the docstring for `_minimize_consumption` to document the new Phase 2:
replace the paragraph that describes Phase 2 as "maximise export revenue" with the
correct description: "Minimise confidence-weighted net cost (import cost minus export
revenue) plus device wear cost plus terminal SoC value, subject to the Phase 1 import
constraint."

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_objective_builder.py
```

All tests green. No existing tests may be broken.

Check that the scenarios still solve correctly:

```bash
uv run pytest tests/scenarios/
```

Golden files may differ if the updated Phase 2 produces a different schedule (which
is expected for scenarios with variable prices). Run `pytest --update-golden` only if
the new schedule is **demonstrably more correct** (e.g. imports shifted to the cheaper
time slot). Review the diff before committing.

---

## Done

```bash
mv plans/17_minimize_consumption_phase2.md plans/done/
```
