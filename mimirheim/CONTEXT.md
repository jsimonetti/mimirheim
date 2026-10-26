# Solver and Devices

This context describes how mimirheim plans energy use across devices. It distinguishes the schedule mimirheim commands from the autonomous controls and physical behavior of each device.

## Language

### Planning and optimisation

**Solve cycle**:
A single pass of the optimiser over one input snapshot: inputs gathered, model built and solved, schedule published, repeated every cycle.
_Avoid_: Solve bundle (names the input-snapshot data structure, not the cycle), tick

**Horizon**:
The forward-looking planning window the optimiser covers, discretised into 15-minute steps and re-derived each cycle from how far the input forecasts reach.
_Avoid_: Window, lookahead

**Time step**:
One 15-minute slice of the horizon; the universal time granularity for every forecast, decision variable, and schedule entry.
_Avoid_: Interval, period, slot

**Rolling horizon**:
The strategy of re-solving the whole horizon every cycle with fresh data and committing only the first step, so later steps are advisory and continually revised.
_Avoid_: Receding horizon

**Schedule**:
The optimiser's output: a per-step plan of device setpoints and grid exchange across the horizon.
_Avoid_: Plan, dispatch, SolveResult (the data carrier, not the concept)

**Power balance**:
The constraint that total production equals total consumption at every step, with the grid absorbing any difference.
_Avoid_: Energy balance, equilibrium

**Confidence**:
A per-step forecast-quality weight between 0.0 and 1.0 supplied with each input series; the optimiser trusts low-confidence steps less. Supplied externally, never computed or decayed by mimirheim.
_Avoid_: Certainty, forecast weight

**Readiness**:
The condition of having received every MQTT input a solve requires; until it holds, mimirheim does not run a solve cycle.
_Avoid_: Warm-up, initialised

### Strategy

**Strategy**:
The user-selected optimisation objective, read live from MQTT: `minimize_cost`, `minimize_consumption`, or `balanced`.
_Avoid_: Mode, policy

**Strategy degraded**:
A flag signalling that the requested strategy could not be fully delivered and a fallback result was published instead.
_Avoid_: Fallback, downgraded

**Exchange shaping**:
A secondary objective term that, among solutions of equal net cost, prefers the one with the least total grid import plus export.
_Avoid_: Exchange minimisation, smoothing

### Grid and loads

**Grid import / grid export**:
Power drawn from the grid (import) or fed back to it (export); the primary quantities the objective prices.
_Avoid_: Buy/sell, draw/feed-in

**Deferrable load**:
A load with fixed power and fixed duration that must finish inside a time window; the optimiser chooses only when it starts.
_Avoid_: Shiftable load, flexible load

**Static load**:
Household consumption mimirheim cannot control, entered into the power balance as a forecast with no decision variable.
_Avoid_: Base load (names the forecast quantity, not the device), fixed load

### Storage and conversion

**State of charge**:
The energy currently stored in a battery or EV, in kWh, tracked as a decision variable across the horizon.
_Avoid_: Charge level, fill level

**Hybrid inverter**:
A device combining battery storage with a directly coupled PV input behind one grid connection, letting PV charge the battery without a round-trip through the grid.
_Avoid_: All-in-one inverter, PV battery

**V2H (vehicle-to-home)**:
Discharging an EV battery back to the home or grid; available only when discharge segments are configured.
_Avoid_: V2G (names the grid-only direction), bidirectional charging

**Wear cost**:
A per-kWh penalty on battery or EV throughput that discourages cycling the device harder than the saving justifies.
_Avoid_: Cycle cost, degradation cost

**Piecewise efficiency**:
Representing a device's non-constant round-trip efficiency as a series of linear segments so the model stays linear.
_Avoid_: Efficiency map

**Derating**:
The reduction of a device's usable charge or discharge power as it approaches full or empty.
_Avoid_: Power taper, throttling

### Building thermal

**Building thermal model (BTM)**:
An optional model that tracks indoor temperature as a decision variable using first-order heat dynamics, replacing the degree-days demand with physical temperature behaviour.
_Avoid_: Thermal dynamics model, house model

**Degree-days model**:
The simpler heat-demand model used when no BTM is configured: deliver at least a forecast quantity of heat over the horizon.
_Avoid_: Heat demand model

**Comfort band**:
The indoor-temperature range the heat-pump schedule must keep within; violating it makes the solve infeasible.
_Avoid_: Comfort range, setpoint band

**Pre-heating**:
Running the heat pump while electricity is cheap to store heat in the building fabric for later, enabled by the BTM.
_Avoid_: Thermal pre-loading

### Real-time control

**Closed-loop enforcer**:
A device that, for a given step, holds grid exchange near zero on its own under firmware control while the optimiser still plans its setpoint for SOC accuracy.
_Avoid_: Regulator, controller

**Zero-exchange mode**:
A device capability in which firmware autonomously absorbs or supplies power to keep net grid flow near zero in real time.
_Avoid_: Self-consumption mode, zero-feed-in

**Arbitration**:
The post-solve step that picks at most one eligible device per near-zero-exchange step to act as the closed-loop enforcer.
_Avoid_: Control assignment, mediation

**Loadbalance**:
An EV-charger autonomy mode that clamps charging to available PV surplus by measuring grid current; suppressed on steps where a battery is the enforcer.
_Avoid_: Solar matching, PV following

### Thermal storage

**Heat store**:
A device that stores thermal energy and can accept an optional boost above the temperature maintained by its own normal program.
_Avoid_: Thermal battery

**Normal program**:
The device's own thermostat, timer, or control logic that maintains its usual setpoint independently of mimirheim's boost schedule.
_Avoid_: Baseline boost

**Boost stage**:
A discrete boost mode commanded by mimirheim, defined by its electrical power, efficiency, and maximum store temperature.
_Avoid_: Heating stage, normal stage

**Boost run state**:
The persisted record of the boost stage commanded by mimirheim and when the current continuous boost run began.
_Avoid_: Measured run state

**Stored heat**:
Thermal energy held above the normal setpoint and available to replace heat that the normal program would otherwise supply.
_Avoid_: State of charge

**Maintenance loss**:
Heat loss at the normal setpoint that the normal program would replace, and that stored heat can temporarily cover.
_Avoid_: Standby drain

**Extra loss**:
The additional heat loss caused by holding the store above its normal setpoint; it reduces stored heat without displacing normal heating.
_Avoid_: Maintenance loss
