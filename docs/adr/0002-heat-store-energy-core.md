---
status: proposed
---
# Heat-store temperature inputs map to an energy core

The user-facing model describes a store by heat capacity and temperatures, but the device model tracks energy above the normal setpoint. A pure translation layer converts the measured temperature and configuration into energy bounds; it separates useful maintenance loss from wasteful extra loss and represents stage-cap overshoot as paid-for spill, avoiding partial-power commands and extra stage binaries.

## Considered Options

- Model the device's full temperature response and normal heating: rejected because it depends on uncertain draw and control inputs and adds solver complexity.
- Keep only an energy-based configuration: deferred because most target devices report temperature, not stored energy.

## Consequences

- A per-kelvin heat capacity does not represent latent-heat stores; those require a later energy-based configuration mode.
- The final step that reaches a stage cap may conservatively overestimate electrical consumption because the model assumes full-step power.