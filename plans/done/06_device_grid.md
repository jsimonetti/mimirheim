# Step 06 — Grid device

## References

- IMPLEMENTATION_DETAILS §8, subsections "Grid device" and "Device method contract"

---

## Files to create

- `mimirheim/devices/grid.py`
- `tests/unit/test_grid_constraints.py`

Note: `tests/unit/test_grid_constraints.py` is not in the canonical test structure. Create it.

---

## Tests first

Create `tests/unit/test_grid_constraints.py`. Use a real `HiGHSSolverBackend` and `ModelContext` with `T=2`, `dt=0.25`. Tests must fail before any implementation exists.

- `test_grid_import_bounded_by_config` — `import_limit_kw=5.0`; add objective minimise `-import_[0]` (incentive to import as much as possible); solve; assert `var_value(import_[0]) <= 5.0 + 1e-6`
- `test_grid_export_bounded_by_config` — `export_limit_kw=3.0`; minimise `-export_[0]`; assert `var_value(export_[0]) <= 3.0 + 1e-6`
- `test_grid_net_power_is_import_minus_export` — verify by constructing a constraint `net_power(0) == 2.0` and checking that `import_[0] - export_[0]` equals 2.0 at the solution
- `test_grid_objective_terms_returns_zero` — `objective_terms(0)` does not affect the objective (add it and verify objective value is unchanged)
- `test_grid_add_constraints_accepts_none_inputs` — `add_constraints(ctx, inputs=None)` must not raise

Run `uv run pytest tests/unit/test_grid_constraints.py` — all tests must fail before proceeding.

---

## Implementation

`mimirheim/devices/grid.py` — the Grid device. There is exactly one Grid per solve; it is not in a named map.

```python
class Grid:
    name: str = "grid"

    def __init__(self, config: GridConfig) -> None: ...
    def add_variables(self, ctx: ModelContext) -> None: ...
    def add_constraints(self, ctx: ModelContext, inputs: None) -> None: ...
    def net_power(self, t: int) -> Any: ...       # LinExpr: import_[t] - export_[t]
    def objective_terms(self, t: int) -> int: ... # always 0; economics are in ObjectiveBuilder
```

`add_variables` declares two non-negative variables per step:
- `import_[t]` — power imported from the grid in kW; upper bound is `config.import_limit_kw`
- `export_[t]` — power exported to the grid in kW; upper bound is `config.export_limit_kw`

`add_constraints` has no work to do — the bounds on import and export are set on the variables themselves. The method exists to satisfy the Device Protocol and accepts `inputs=None`.

`net_power(t)` returns the expression `import_[t] - export_[t]`. Positive means net import; the sign convention matches the power balance constraint in `build_and_solve()`.

`ObjectiveBuilder` holds a direct reference to the Grid instance to access `import_[t]` and `export_[t]` when building economic terms. No other component accesses Grid variables directly.

Comment every variable declaration to the depth required by AGENTS.md (physical quantity, units, why the bound exists, what would go wrong without it).

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_grid_constraints.py
```

All tests green.

---

## Done

```bash
mv plans/06_device_grid.md plans/done/
```
