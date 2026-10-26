# 10. Exchange-shaping secondary term

Under a net-of-meter (NoM) tariff, import and export prices are symmetric. The `minimize_cost` objective naturally produces near-zero exchange as a consequence — there is no economic benefit to importing energy you could supply from storage. However, when prices are flat or very close to symmetric, the solver is indifferent among solutions with the same net cost but different gross exchange magnitudes. Floating-point degeneracy can cause it to choose a solution with unnecessary cycling.

The `objectives.exchange_shaping_weight` field adds an optional secondary term:

```
lambda * sum_t(import_t + export_t)
```

to the objective. The weight `lambda` must be orders of magnitude smaller than typical energy prices so it cannot reverse a dispatch decision that is economically justified. A value of `1e-4` EUR/kWh is appropriate for European retail tariff levels (0.20–0.35 EUR/kWh): the maximum influence on a 24-hour horizon with 10 kW continuous exchange is `1e-4 * 10 * 96 = 0.096 EUR`, which is well below typical dispatch profitability thresholds.

The term is applied in all three strategies (`minimize_cost`, `minimize_consumption`, `balanced`). In `minimize_consumption`, it is added only in phase 2 so it does not distort the phase-1 import minimisation.

The term is implemented in `mimirheim/core/objective.py` in the `_exchange_shaping_terms` helper method.

## Device economic terms

A device can contribute to the economic objective through the optional `economic_terms(t)` hook (§8, "Device method contract"). Its terms are added to grid cost before confidence weighting:

```
economic[t] = confidence[t] * (import_price[t] * import[t] - export_price[t] * export[t]
                               + sum_d device.economic_terms(t))
```

The hook exists for terms that are valued at forecast prices, such as a credit for the grid electricity a device's stored energy displaces later. Such a term has the same forecast uncertainty as the grid cost it competes against, so it is weighted the same way. Under `balanced` it is part of the cost component and is scaled by the cost weight `cw`. Adding it at full weight, like a wear term, would let it outweigh the `cw`-scaled grid cost that pays for the energy and schedule dispatch that does not pay. Under `minimize_consumption` it is part of phase 2 only; phase 1 minimises import volume and contains no prices.

Devices that value displaced consumption need to know how much of it would have been imported. `_baseline_import_kwh` in `model_builder.py` computes the pre-solve import per step:

```
baseline_import[t] = max(0, base_load_forecast[t] - clipped_pv_kw[t]) * dt   # kWh
```

`clipped_pv_kw` is the series `build_and_solve()` accumulates for the naive-cost baseline: every array's forecast clipped to its deliverable ceiling, plus each hybrid inverter's panels clipped to `max_pv_kw` and taken through the inverter. `build_and_solve()` calls it once per solve, after the hybrid inverter loop and only when a heat store is configured, and passes the result to each heat store's `add_constraints` explicitly (§8, "Heat store model").

## Phase-1 import credit

A device can reduce the import volume that `minimize_consumption` minimises through the optional `phase1_import_credit(t)` hook, in kW. Phase 1 minimises

```
sum_t (import[t] - sum_d device.phase1_import_credit(t))
```

and the phase-1 lock constrains the same expression, so phase 2 cannot trade the credited volume away. The `balanced` self-sufficiency term uses the same per-step quantity: `sw * (import[t] - sum_d credit[t])`.

The credit is not confidence-weighted, matching the plain import sum it is subtracted from. It lets a device count grid import it displaces later, so that, for example, storing surplus PV as heat counts towards self-sufficiency. A device must only credit import it actually displaces; a credit larger than the import that produced it would make grid-powered dispatch win in phase 1.
