# Step 19 — Grid simultaneous import/export binary guard

## References

- IMPLEMENTATION_DETAILS §8, subsection "Grid device"
- `mimirheim/devices/grid.py` — `Grid`
- `mimirheim/config/schema.py` — `GridConfig`

---

## Files to modify

- `mimirheim/config/schema.py`
- `mimirheim/devices/grid.py`
- `tests/unit/test_grid_constraints.py`

---

## Background

The current `Grid` device has two continuous variables per step: `import_[t]` and
`export_[t]`, each bounded below by 0 and above by the connection limit. There is no
explicit constraint preventing both from being nonzero at the same step.

For a pure LP (no batteries, no binaries), simultaneous import and export cannot occur
at optimality — any simultaneous flow would be eliminated by the solver because it
represents avoidable energy cost. In a MILP with battery mode binaries, the LP
relaxation can temporarily assign simultaneous flows, and in rare cases the
branch-and-bound optimal solution can also exhibit them (because the battery mode
binaries constrain what combinations are feasible, creating pressure that the LP
resolves with a small simultaneous flow).

Adding a binary guard hardens this prohibition:

```
import_active[t] ∈ {0, 1}
export_active[t] ∈ {0, 1}
import_[t] <= import_limit_kw × import_active[t]
export_[t] <= export_limit_kw × export_active[t]
import_active[t] + export_active[t] <= 1
```

At most one of `import_active[t]` and `export_active[t]` can be 1, so at most one
direction can carry a nonzero flow at each step.

The guard adds `2 × T` binary variables and `3 × T` constraints. This increases solver
time for a 96-step horizon. Enable it only when simultaneous flows would have a material
effect on billing (e.g. when the retailer charges a standby fee per direction, or when
the meter cannot net simultaneous flows correctly).

The guard is opt-in via a new `GridConfig` field with a default of `False`.

---

## Tests first

Add to `tests/unit/test_grid_constraints.py`. Use a real solver with `T=4`, `dt=0.25`.

- `test_grid_no_simultaneous_binaries_by_default` — with default `GridConfig`
  (prevent_simultaneous_import_export not set), the Grid device must not create any
  binary variables. Count variables before and after `add_variables` and assert the
  delta contains no `var_type="B"` entries.
- `test_grid_prevent_simultaneous_blocks_concurrent_flows` — with
  `prevent_simultaneous_import_export: True`, construct a scenario where an unconstrained
  solver would set both import and export to positive values at step 0 (e.g. import
  price = export price = 0, negative total that "wants" to recirculate energy). Assert
  that after solving at each step `min(import_[t], export_[t]) < 1e-6`.
- `test_grid_prevent_simultaneous_allows_import_only` — with the flag True, a scenario
  with only a static load (no PV, no battery) must still solve with nonzero import and
  zero export at all steps.
- `test_grid_prevent_simultaneous_allows_export_only` — with the flag True, a scenario
  with only PV surplus (no load, no battery) must solve with zero import and nonzero
  export at all steps.
- `test_grid_prevent_simultaneous_respects_connection_limits` — with the flag True,
  assert `import_[t] <= import_limit_kw` and `export_[t] <= export_limit_kw` still
  hold at all steps (the binaries do not widen the limits beyond what was already
  enforced by variable bounds).

Run `uv run pytest tests/unit/test_grid_constraints.py -k "simultaneous"` — all five
tests must fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py` — `GridConfig`

Add one field:

```python
prevent_simultaneous_import_export: bool = Field(
    default=False,
    description=(
        "When True, the solver adds binary variables that enforce at most one of "
        "import or export to be nonzero at each time step. This eliminates "
        "simultaneous bidirectional grid flows in MILP solutions. Adds 2×T binary "
        "variables and increases solve time. Leave False (default) unless billing "
        "or metering requires strict exclusion."
    ),
)
```

Update the `GridConfig` docstring to document the new field.

### `mimirheim/devices/grid.py` — `Grid`

**`add_variables(ctx: ModelContext, config: GridConfig) -> None`**

After the existing `import_[t]` and `export_[t]` variable creation, add:

```python
if config.prevent_simultaneous_import_export:
    for t in ctx.T:
        # import_active[t]: binary, 1 when grid is importing at step t.
        # export_active[t]: binary, 1 when grid is exporting at step t.
        # These are sentinel variables — the solver uses them to enforce
        # that both directions cannot be simultaneously active (constraint
        # added in add_constraints).
        self._import_active[t] = ctx.solver.add_var(
            var_type="B", name=f"import_active_{t}"
        )
        self._export_active[t] = ctx.solver.add_var(
            var_type="B", name=f"export_active_{t}"
        )
```

Store `self._prevent_simultaneous = config.prevent_simultaneous_import_export`.

**`add_constraints(ctx: ModelContext, config: GridConfig) -> None`**

After the existing limit constraints, add:

```python
if self._prevent_simultaneous:
    for t in ctx.T:
        # Tie the continuous import/export variables to their binary sentinels.
        # When import_active[t]=0, this forces import_[t] = 0.
        # When import_active[t]=1, import can be up to import_limit_kw (the
        # variable bound already enforces this, so Big-M = import_limit_kw).
        ctx.solver.add_constraint(
            self.import_[t] <= config.import_limit_kw * self._import_active[t]
        )
        ctx.solver.add_constraint(
            self.export_[t] <= config.export_limit_kw * self._export_active[t]
        )
        # Mutual exclusion: at most one direction is active per step.
        ctx.solver.add_constraint(
            self._import_active[t] + self._export_active[t] <= 1
        )
```

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_grid_constraints.py
```

All tests green, including all pre-existing tests.

```bash
uv run pytest tests/scenarios/
```

No golden file changes expected — existing scenarios do not have the new flag set.

---

## Done

```bash
mv plans/19_grid_import_export_binary.md plans/done/
```
