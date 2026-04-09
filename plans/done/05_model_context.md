# Step 05 — ModelContext

## References

- IMPLEMENTATION_DETAILS §8, subsection "ModelContext"

---

## Files to create

- `mimirheim/core/context.py`
- `tests/unit/test_model_context.py`

Note: `tests/unit/test_model_context.py` is not in the canonical test structure. Create it.

---

## Tests first

Create `tests/unit/test_model_context.py`. Tests must fail before any implementation exists.

- `test_model_context_T_is_range` — construct with `horizon=4`, `dt=0.25`; assert `ctx.T == range(4)`
- `test_model_context_dt_stored` — assert `ctx.dt == 0.25`
- `test_model_context_solver_stored` — assert `ctx.solver` is the object passed in
- `test_model_context_no_bundle_attribute` — assert `ModelContext` has no `bundle` or `config` attribute (use `hasattr`)

Run `uv run pytest tests/unit/test_model_context.py` — all tests must fail before proceeding.

---

## Implementation

```python
# mimirheim/core/context.py
from mimirheim.core.solver_backend import SolverBackend

class ModelContext:
    def __init__(self, solver: SolverBackend, horizon: int, dt: float) -> None:
        self.solver = solver   # the live solver instance
        self.T = range(horizon)
        self.dt = dt
```

`ModelContext` stores only these three attributes. It does not carry `SolveBundle`, `MimirheimConfig`, device lists, or any mutable state beyond the solver handle.

`dt` is derived at solve time from the horizon length (see step 13). A 96-step horizon is quarter-hourly (`dt=0.25`). A 24-step horizon is hourly (`dt=1.0`).

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_model_context.py
```

All tests green.

---

## Done

```bash
mv plans/05_model_context.md plans/done/
```
