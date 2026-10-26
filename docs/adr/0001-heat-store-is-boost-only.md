---
status: accepted
---
# Heat stores are boost-only devices

The heat-store feature commands optional, discrete boosts above a device's normal setpoint; the device's own thermostat and program remain responsible for ordinary heating. We reject full thermostat emulation because it would require poorly observed draw and device-control behavior while adding substantial solver complexity, and the planned integration remains independent of vendor-specific controls.

## Consequences

- Normal heating remains in the static load forecast and is not scheduled by the heat-store model.
- Integrations map boost stages to their hardware controls and must stop boosting when mimirheim is unavailable.
- Blocking normal heating and rebound modeling are out of scope for this design.