---
status: accepted
---
# Heat-store temperature inputs map to an energy core

The user-facing model describes a store by heat capacity and temperatures, but the device model tracks energy above the normal setpoint. A pure translation layer converts the measured temperature and configuration into energy bounds; it separates useful maintenance loss from wasteful extra loss. Overshoot at a stage cap is handled by partial draw allowed only in steps where the store ends at the active stage's cap: the command stays discrete (the boost stage) and only the planned power is continuous. Each stage cap is the temperature at which the device actually stops heating in that stage.

## Considered Options

- Model the device's full temperature response and normal heating: rejected because it depends on uncertain draw and control inputs and adds solver complexity.
- Keep only an energy-based configuration: deferred because most target devices report temperature, not stored energy.
- Always plan full stage power and absorb overshoot with paid-for spill: rejected because at negative import or export prices the solver plans full-power draw while the store is full, consumption the real device never makes.
- Force stage 0 when the measured temperature is at a cap: rejected because it only covers the first step, the solver already chooses stage 0 when holding does not pay, and holding at the cap is correct at negative prices.

## Consequences

- A per-kelvin heat capacity does not represent latent-heat stores; those require a later energy-based configuration mode.
- Each store needs one additional binary per step for the at-cap state.
- Planned power can be below a stage's power in steps at the cap; discrete integrations switch on the boost stage, not on planned power.
- Stored heat use is bounded by the maintenance loss and fixed to it only where heat already in the store provably covers it. An exact per-step "empty" binary was rejected: its weak LP relaxation let the solver return useless flat-price boosts within the MIP gap. Heat from a planned boost can therefore be held back through zero-credit steps; this is a documented approximation.
