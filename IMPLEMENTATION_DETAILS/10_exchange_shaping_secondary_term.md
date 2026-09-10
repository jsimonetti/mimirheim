# 10. Exchange-shaping secondary term

Under a net-of-meter (NoM) tariff, import and export prices are symmetric. The `minimize_cost` objective naturally produces near-zero exchange as a consequence — there is no economic benefit to importing energy you could supply from storage. However, when prices are flat or very close to symmetric, the solver is indifferent among solutions with the same net cost but different gross exchange magnitudes. Floating-point degeneracy can cause it to choose a solution with unnecessary cycling.

The `objectives.exchange_shaping_weight` field adds an optional secondary term:

```
lambda * sum_t(import_t + export_t)
```

to the objective. The weight `lambda` must be orders of magnitude smaller than typical energy prices so it cannot reverse a dispatch decision that is economically justified. A value of `1e-4` EUR/kWh is appropriate for European retail tariff levels (0.20–0.35 EUR/kWh): the maximum influence on a 24-hour horizon with 10 kW continuous exchange is `1e-4 * 10 * 96 = 0.096 EUR`, which is well below typical dispatch profitability thresholds.

The term is applied in all three strategies (`minimize_cost`, `minimize_consumption`, `balanced`). In `minimize_consumption`, it is added only in phase 2 so it does not distort the phase-1 import minimisation.

The term is implemented in `mimirheim/core/objective.py` in the `_exchange_shaping_terms` helper method.
