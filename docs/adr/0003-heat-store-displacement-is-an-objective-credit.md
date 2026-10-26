---
status: accepted
---
# Value displaced heating with an objective credit

Keep normal heating in the static load forecast and value heat-store discharge with an objective credit for the normal-program electricity it displaces, split between import and export value using the pre-solve load and PV baseline. This avoids making the heat-store model responsible for rewriting another subsystem's load forecast, while recognizing that displaced heating may have used surplus PV rather than imported electricity.

## Considered Options

- Subtract displaced heating from the static load forecast: rejected because the normal heating schedule is not modeled and the forecast is owned outside the heat-store device.
- Value all displaced heating at import price: rejected because it overvalues discharge when PV would have covered the normal load.

## Consequences

- The credit is an approximation: it ignores battery dispatch and assumes the device's normal program would have supplied the displaced heat.
- Under `minimize_consumption`, a separate phase-one import-credit hook is needed to let surplus-PV boosting count without making grid-powered boosting beneficial.
- The credit is part of the confidence-weighted economic objective, not a device penalty term: under `balanced` it is scaled by the cost weight like grid cost, which preserves the guarantee that flat prices never trigger a boost.
- Credit prices are clamped at zero so that stored heat is always used at its physical rate, even when displacing it has negative value.
- The phase-one import credit is expressed in kW per step and is not confidence-weighted, matching the import sum it is subtracted from.
