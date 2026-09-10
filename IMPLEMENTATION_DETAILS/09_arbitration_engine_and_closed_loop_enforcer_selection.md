# 9. Arbitration engine and closed-loop enforcer selection

**Module: `mimirheim/core/control_arbitration.py`**

After `build_and_solve()` and `apply_gain_threshold()`, the solve loop calls:

```python
result = assign_control_authority(result, bundle, config)
```

This pure function sets `DeviceSetpoint.zero_exchange_active` (and `loadbalance_active` for EVs) on every step in the schedule. It is the sole code path that sets these fields.

## Why the solver does not zero out closed-loop device variables

When a battery or EV will operate in closed-loop zero-exchange mode for a step, the solver variable for that device is **not** suppressed or fixed to zero. This is intentional.

The device will still be physically charging or discharging — it is just doing so autonomously under firmware control rather than following a numeric setpoint from mimirheim. If the solver does not model that behavior, the SOC trajectory across the horizon becomes incorrect: the solver assumes the battery is idle when it is actually absorbing or supplying several kilowatts. Incorrect SOC estimates cause wrong decisions on the steps immediately before and after the closed-loop step.

The correct model is: let the solver plan a numeric setpoint for the battery on closed-loop steps, because that plan represents the best prediction of what the hardware will actually do (absorbing surplus to achieve near-zero exchange). The post-process layer then overrides the published command with the closed-loop enable flag. The solver plan is advisory; the hardware firmware performs the real-time enforcement.

SOC state is continuous across all steps, including closed-loop steps. If the hardware does not track the solved setpoint exactly (expected — firmware PID loops are not perfect), the next solve cycle corrects the SOC trajectory using the fresh reading from MQTT.

## Enforcer selection

A step is **near-zero-exchange** when:

```
grid_import_kw <= exchange_epsilon_kw  AND  grid_export_kw <= exchange_epsilon_kw
```

Only near-zero-exchange steps trigger enforcer selection. All other steps clear all closed-loop flags.

A device is an **eligible candidate** for a near-zero-exchange step when:

1. Its capability flag is set (`zero_exchange` for batteries/EVs, `zero_export` for PV).
2. For EVs: the vehicle is plugged in (`bundle.ev_inputs[name].available` is True).
3. Its absorption headroom is >= `config.control.headroom_margin_kw`.

**Absorption headroom** is the additional power the device can absorb at its current operating point: for batteries and EVs, `max_charge_kw - actual_charge_kw + actual_discharge_kw`; for PV, the current production kW.

Candidates are scored by a four-level cascade (descending; later levels break ties):

1. Efficiency at the expected compensation power (PV always scores 0.0 — last resort).
2. Headroom margin (headroom minus expected compensation — more slack is better).
3. Wear proxy: lower `wear_cost_eur_per_kwh` wins.
4. Type priority (battery=3, EV=2, PV=1), then device name (lexicographic).

**Hysteresis:** a challenger must exceed the current enforcer's efficiency score by `config.control.switch_delta` to trigger a switch.

**Minimum dwell:** once selected, a device holds the enforcer role for at least `config.control.min_enforcer_dwell_steps` consecutive steps, unless it becomes ineligible.

## Loadbalance suppression

When a battery is the `zero_exchange_active` enforcer for a step, any EV with `capabilities.loadbalance=True` receives `loadbalance_active=False` for that step. The battery's closed-loop controller and an EVSE loadbalance controller both target the same grid current transformer; only one may be authoritative per step.

An EV that is itself the `zero_exchange_active` enforcer receives `zero_exchange_active=True` and `loadbalance_active=False`. `loadbalance_active=True` is only set on steps where the EV is not the `zero_exchange` enforcer.
