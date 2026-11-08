# Boost heat valued at the running minimum import price

Status: accepted

Boost heat displaces normal-program electricity immediately and at the drift rate, but forcing that timing in the model needs a binary per step. Without it the plan is free to choose when boost heat is consumed and picks the evening price peak, which cannot happen physically and makes the plan heat from the grid at a loss. We value boost heat consumed at step t at `p_ref[t] / normal_cop` with `p_ref[t] = max(0, min over s <= t of confidence[s] * import_price[s])`, and boost heat left at the horizon end at `p_ref` of the last step. The confidence weighting matches the grid cost terms, which are scaled by the same per-step forecast confidence; without it a grid boost in a low-confidence step would look cheaper than it is. Taking the minimum over the weighted price, instead of weighting the minimum afterwards, keeps the curve non-increasing even when confidence rises later in the horizon. `p_ref` is precomputed from the inputs, so it adds no variables or constraints. The running minimum is the largest non-increasing curve that never exceeds the effective (confidence-weighted) price, so consuming early is never worth less than consuming late, and the free choice of the plan coincides with the physical behaviour. The clamp keeps heat from ever being worth less than free.

## Considered Options

- Real price with free timing: exploitable, rejected for the reason above.
- Real price with forced consumption via binaries: exact, but breaks the requirement that the device solves as cheaply as a battery.
- Running minimum from the heating step to the use step: less undervaluation, but needs a variable per heating-step and use-step pair (about T²/2), again too expensive.
- Mean import price as terminal value (as for batteries): re-creates the peak exploit at the horizon edge.
- Running minimum of the raw price, then multiplied by `confidence[t]`: matches the grid cost scaling, but the result can increase over time when confidence rises, which re-opens the timing exploit.

## Consequences

Boost heat is undervalued when prices rise after it is stored. The rolling horizon softens this, because every solve restarts the running minimum from the current step.
