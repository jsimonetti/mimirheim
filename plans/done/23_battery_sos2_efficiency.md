# Step 23 — Battery SOS2 piecewise-linear efficiency interpolation

## References

- IMPLEMENTATION_DETAILS §8, subsection "Piecewise efficiency (battery and EV)"
- IMPLEMENTATION_DETAILS §4, subsection "SolverBackend Protocol"
- `mimirheim/core/solver_backend.py` — `SolverBackend`, `HiGHSSolverBackend`
- `mimirheim/devices/battery.py` — `Battery`
- `mimirheim/config/schema.py` — `BatteryConfig`, `EfficiencySegment`

---

## Files to modify

- `mimirheim/config/schema.py`
- `mimirheim/core/solver_backend.py`
- `mimirheim/devices/battery.py`
- `tests/unit/test_battery_constraints.py`
- `tests/unit/test_config_schema.py`
- `tests/unit/test_solver_backend.py`

---

## Background

The current battery model uses **stacked capacity segments**: each segment is an
independent LP variable, and the solver can place power in any segment in any order.
This models a step-function efficiency curve — each segment has a flat efficiency.

A more accurate model is **piecewise-linear interpolation** via SOS2 (Special Ordered
Sets of type 2). A SOS2 constraint over a set of weight variables `{w_0, …, w_N}`
specifies that at most two adjacent weights may be nonzero simultaneously. This
enforces that the operating point lies on exactly one linear segment of the efficiency
curve, not a blend of non-adjacent segments.

**When to use SOS2:**
Use the SOS2 model when you have manufacturer-measured efficiency at several power
levels and want the solver to interpolate correctly between them. Use the stacked
segment model when you have a simple low/high-power efficiency approximation and do not
need continuous interpolation.

**Config:** The two models use different config models. When `charge_efficiency_curve`
is provided in `BatteryConfig`, the SOS2 model is used; otherwise the existing
stacked-segment model is used unchanged. Both modes can coexist in the same
installation — the same `BatteryConfig` always uses one model or the other.

**SOS2 model details:**

For each direction (charge/discharge) and each time step `t`, introduce `N + 1` weight
variables `w[t, 0], …, w[t, N]` where `N` is the number of breakpoints minus one:

```
Σ_s w[t, s] = 1            (convex-combination constraint: weights sum to 1)
w[t, s] >= 0               (weight bounds)
SOS2({w[t, 0], …, w[t, N]}, {P_0, …, P_N})
                            (at most two adjacent weights are nonzero)
```

The power and efficiency at time step `t` are the convex combinations:

```
charge_ac_kw[t]   = Σ_s (w_c[t, s] × P_c[s])
charge_dc_kw[t]   = Σ_s (w_c[t, s] × P_c[s] × η_c[s])   (power stored in battery)
discharge_ac_kw[t] = Σ_s (w_d[t, s] × P_d[s])
discharge_dc_kw[t] = Σ_s (w_d[t, s] × P_d[s] / η_d[s])  (power drawn from battery)
```

The SOC update becomes:

```
soc[t] = soc[t-1] + dt × charge_dc_kw[t] − dt × discharge_dc_kw[t]
```

The first breakpoint must be at `P_0 = 0` with efficiency at zero power (typically
the highest efficiency). Setting `w[t, 0] = 1` models zero power, allowing the solver
to idle the battery.

The Big-M charge/discharge guard (`mode[t]` binary) is still required to prevent
the solver from simultaneously using nonzero `charge_ac_kw[t]` and
`discharge_ac_kw[t]`.

---

## New config model

A new `EfficiencyBreakpoint` model describes one point on a continuous efficiency
curve, as distinct from `EfficiencySegment` which describes a flat-efficiency bucket.

---

## Tests first

### Config tests (`test_config_schema.py`)

- `test_efficiency_breakpoint_validates_power_nonnegative` — `power_kw < 0` raises.
- `test_efficiency_breakpoint_validates_efficiency_range` — `efficiency <= 0` or
  `efficiency > 1.0` raises.
- `test_battery_sos2_curve_requires_minimum_two_breakpoints` — a
  `charge_efficiency_curve` with fewer than two entries raises a `ValidationError`.
- `test_battery_sos2_curve_first_breakpoint_must_be_zero_power` — first breakpoint
  `power_kw != 0` raises a `ValidationError`.
- `test_battery_sos2_curve_powers_must_be_strictly_increasing` — a curve with two
  adjacent breakpoints where `power_kw` is not strictly increasing raises.
- `test_battery_sos2_requires_segments_or_curve_not_both` — providing both
  `charge_segments` and `charge_efficiency_curve` raises a `ValidationError`.

### Solver backend tests (`test_solver_backend.py`)

- `test_solver_backend_add_sos2_accepted` — call `backend.add_sos2(variables,
  breakpoint_weights)` on a freshly built model and assert no exception is raised.
- `test_solver_backend_sos2_enforces_at_most_two_adjacent_nonzero` — build a model
  with three weight variables [w0, w1, w2] and a SOS2 constraint. Add objective that
  would prefer w0=1 and w2=1 (non-adjacent). Assert that the solve result has one of
  the adjacent pairs nonzero, not the non-adjacent combination.

### Battery solver tests (`test_battery_constraints.py`)

- `test_sos2_soc_tracks_charging_single_segment` — a two-breakpoint curve with a
  single linear segment (P_0=0, P_1=5 kW, both at η=0.95). Force 5 kW input for
  T=4 steps. Assert SOC increases by `5 × 0.95 × 0.25 = 1.1875 kWh` per step.
- `test_sos2_efficiency_interpolated_between_breakpoints` — a three-breakpoint curve
  at P=0 (η=0.98), P=3 kW (η=0.95), P=6 kW (η=0.88). Force a charge at 4.5 kW
  (midpoint of the second segment). Assert stored DC power equals the linearly
  interpolated DC value at 4.5 kW: `4.5 × ((0.95 + 0.88) / 2) = 4.5 × 0.915`.
- `test_sos2_no_simultaneous_charge_discharge` — same conditions that trigger the
  Big-M guard test for the stacked-segment model (plan 07 test). The SOS2 model must
  exhibit the same mutual-exclusion behaviour.
- `test_sos2_model_falls_back_to_stacked_when_no_curve` — a `BatteryConfig` with
  `charge_segments` and no `charge_efficiency_curve` uses the stacked model (no SOS2
  variables). Confirm by asserting that `soc_tracks_charging` with `efficiency=1.0`
  gives the same result as the plan 07 baseline.

Run `uv run pytest tests/unit/test_battery_constraints.py tests/unit/test_config_schema.py tests/unit/test_solver_backend.py -k "sos2"` — all tests must fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py` — new model and `BatteryConfig` changes

Add the new model before `BatteryConfig`:

```python
class EfficiencyBreakpoint(BaseModel):
    """A single point on a piecewise-linear battery efficiency curve.

    Used with the SOS2 efficiency model (see BatteryConfig.charge_efficiency_curve).
    The efficiency at the operating power is linearly interpolated between adjacent
    breakpoints by the solver's SOS2 constraint.

    Attributes:
        power_kw: AC power at this breakpoint, in kW. The first breakpoint must
            be at 0.0 kW. Subsequent breakpoints must be strictly increasing.
        efficiency: Round-trip efficiency fraction at this power level. At P=0,
            efficiency is typically at its maximum; it decreases as power increases
            and conduction losses grow.
    """

    model_config = ConfigDict(extra="forbid")

    power_kw: float = Field(ge=0.0)
    efficiency: float = Field(gt=0.0, le=1.0)
```

Modify `BatteryConfig` to make `charge_segments` and `discharge_segments` optional,
and add the new curve fields. A model validator must enforce "segments or curve,
not both, and at least one is required":

```python
charge_segments: list[EfficiencySegment] | None = Field(
    default=None,
    min_length=1,
    description="Stacked-segment efficiency model. Use when exact breakpoints are unknown.",
)
discharge_segments: list[EfficiencySegment] | None = Field(
    default=None,
    min_length=1,
)
charge_efficiency_curve: list[EfficiencyBreakpoint] | None = Field(
    default=None,
    min_length=2,
    description=(
        "SOS2 piecewise-linear efficiency curve for charging. When provided, "
        "charge_segments must be None. First breakpoint must be at power_kw=0.0."
    ),
)
discharge_efficiency_curve: list[EfficiencyBreakpoint] | None = Field(
    default=None,
    min_length=2,
)
```

### `mimirheim/core/solver_backend.py` — `SolverBackend` Protocol

Add to the Protocol:

```python
def add_sos2(self, variables: list[Any], weights: list[float]) -> None:
    """Add a SOS type-2 constraint over the given variables.

    A SOS2 constraint specifies that at most two adjacent variables (in the
    order defined by their weights) may be nonzero simultaneously. This is
    used to enforce piecewise-linear interpolation: the weights are the
    breakpoint power values, and the variables are the SOS2 weight variables.

    Args:
        variables: Solver variable objects to include in the constraint.
        weights: Numeric ordering weights for the SOS constraint, one per
            variable. In highspy, these determine adjacency. They should be
            the power breakpoint values (strictly increasing).
    """
    ...
```

Add the implementation to `HiGHSSolverBackend`. Consult the `highspy` API for the
correct method call. As of `highspy` 1.7, SOS constraints are added via
`self._model.addSOS(type=2, inds=var_indices, weights=weights)` or equivalent.
Determine exact API from the installed `highspy` version at implementation time.

### `mimirheim/devices/battery.py` — `Battery`

The device must detect at variable-creation time whether the SOS2 or stacked model
is active:

```python
self._use_sos2 = config.charge_efficiency_curve is not None
```

**When `self._use_sos2` is True:**

In `add_variables`, for each time step `t` and for charge and discharge separately:

1. Build the breakpoint list from `charge_efficiency_curve`.
2. Create `N + 1` weight variables `w_charge[t, s]` in `[0, 1]`.
3. Call `ctx.solver.add_sos2(w_charge_vars, breakpoint_powers)`.
4. Store the weight variables and pre-compute the linear expressions:
   - `charge_ac_kw_expr[t]` = solver expression `Σ_s (w[s] × P[s])`
   - `charge_dc_kw_expr[t]` = solver expression `Σ_s (w[s] × P[s] × η[s])`

In `add_constraints`, for each `t`:
5. Add: `Σ_s w_charge[t, s] == 1` (convex-combination constraint).
6. Add the Big-M guard using `charge_ac_kw_expr[t]` instead of `charge_total`.
7. Add the SOC update using `charge_dc_kw_expr[t]`.

**When `self._use_sos2` is False:** existing stacked-segment code runs unchanged.

`net_power(t)` returns `discharge_ac_kw_expr[t] - charge_ac_kw_expr[t]` in SOS2
mode, or the existing expression in stacked mode.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_battery_constraints.py tests/unit/test_config_schema.py tests/unit/test_solver_backend.py
```

All tests green.

```bash
uv run pytest tests/scenarios/
```

No golden file changes expected — existing scenarios use `charge_segments` (stacked
model). Confirm by running without `--update-golden`.

---

## Done

```bash
mv plans/23_battery_sos2_efficiency.md plans/done/
```
