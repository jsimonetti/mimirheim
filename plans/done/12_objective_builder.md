# Step 12 — ObjectiveBuilder and confidence helpers

## References

- IMPLEMENTATION_DETAILS §8, subsection "ObjectiveBuilder"
- README.md (strategy modes: `minimize_cost`, `minimize_consumption`, `balanced`)

---

## Files to create

- `mimirheim/core/objective.py`
- `mimirheim/core/confidence.py`
- `tests/unit/test_objective_builder.py`

---

## Tests first

Create `tests/unit/test_objective_builder.py`. Use a real solver with `T=4`, `dt=0.25`, one Grid device, and one Battery device. Tests must fail before any implementation exists.

- `test_minimize_cost_imports_at_cheapest_step` — prices `[0.30, 0.10, 0.30, 0.30]`; fixed load needing 1 kWh total; assert grid import is concentrated at step 1 (the cheap step)
- `test_minimize_cost_exports_at_most_expensive_step` — export prices `[0.05, 0.20, 0.05, 0.05]`; battery with SOC to discharge; assert export at step 1
- `test_minimize_consumption_minimises_total_import` — equal prices; PV available at some steps; assert total import is minimised (prefer self-consumption over import)
- `test_balanced_lies_between_extremes` — solve with `minimize_cost`, then `minimize_consumption`, then `balanced` (equal weights); assert balanced total import is between the other two
- `test_confidence_zero_makes_step_economically_neutral` — confidence `[0.0, 1.0, 1.0, 1.0]`; assert objective does not penalise import at step 0 vs step 1 for equal prices
- `test_wear_cost_added_from_devices` — battery with `wear_cost_eur_per_kwh=1.0` and small price spread; assert battery does not cycle (wear cost outweighs marginal gain)
- `test_import_limit_constraint_enforced` — `constraints.max_import_kw=2.0`; assert grid import never exceeds 2.0 kW at any step

Run `uv run pytest tests/unit/test_objective_builder.py` — all tests must fail before proceeding.

---

## Implementation

### mimirheim/core/confidence.py

Helper functions that weight solver expressions by per-step confidence values. This module does not produce or decay confidence values — it only multiplies existing expressions by a supplied confidence scalar.

```python
def weight_by_confidence(expr: Any, confidence: float) -> Any:
    """Return expr scaled by confidence. If confidence is 0, return 0."""
```

### mimirheim/core/objective.py

```python
class ObjectiveBuilder:
    def build(
        self,
        ctx: ModelContext,
        devices: list[Device],
        grid: Grid,
        bundle: SolveBundle,
        config: MimirheimConfig,
    ) -> None:
        """Set the objective on ctx.solver according to bundle.strategy."""
```

`build` calls `ctx.solver.set_objective_minimize(...)` (or `set_objective_maximize` — never use this; always minimise with negated terms).

#### minimize_cost

For each step t, add to the objective:

```
confidence[t] × (import_price[t] × import_[t] − export_price[t] × export_[t])
```

Plus device wear terms:

```
Σ_d device.objective_terms(t)
```

#### minimize_consumption

Two-solve lexicographic approach (the only case where two solver calls happen inside one `build_and_solve` invocation):

1. First solve: minimise `Σ_t import_[t]`.
2. Record the optimal total import `I*`.
3. Add constraint `Σ_t import_[t] <= I* + epsilon` (epsilon = 1e-4 to avoid numeric infeasibility).
4. Second solve: maximise `Σ_t (export_price[t] × export_[t])` subject to the import bound.
5. Return the result of the second solve.

The two solves are hidden behind the same `build_and_solve()` interface — callers see one `SolveResult`.

#### balanced

Weighted sum using `config.objectives.balanced_weights`:

```
cost_weight × cost_objective + self_sufficiency_weight × (−Σ_t export_[t])
```

Normalise weights so they sum to 1 before combining. If `balanced_weights` is None, default both weights to 1.0.

#### Hard cap constraints (all strategies)

If `config.constraints.max_import_kw` is set, add a constraint at each step:
```
import_[t] <= config.constraints.max_import_kw
```

Similarly for `max_export_kw`. These are constraints, not objective terms.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_objective_builder.py
```

All tests green.

---

## Done

```bash
mv plans/12_objective_builder.md plans/done/
```
