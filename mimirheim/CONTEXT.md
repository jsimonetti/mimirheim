# Solver Domain

The vocabulary of the devices mimirheim plans and the economics it optimises. Partially modeled: terms are added as design sessions resolve them.

## Language

### Heat stores

**Heat store**:
A thermal store (DHW tank, pool, hot tub) whose normal program runs independently of mimirheim and that mimirheim can only boost.
_Avoid_: thermal buffer, surplus heat store, PV heat store

**Boost**:
Heating that mimirheim requests on top of what the normal program does, through an element or a heat pump mode, possibly up to well above the normal setpoint.
_Avoid_: element (names hardware, not the request), PV mode, diversion

**Boost heat**:
Heat in a heat store that spares the normal program from heating: everything above the normal setpoint, and any boost that lifts the store toward it. It displaces normal-program electricity as it is consumed.
_Avoid_: surplus heat, excess heat, stored heat

**Normal program**:
The heating control a thermal store runs on its own, independent of mimirheim, that keeps the store at its normal setpoint.
_Avoid_: default mode, built-in schedule

**Normal setpoint**:
The temperature the normal program maintains. Heat above it is extra heat that only mimirheim adds.
_Avoid_: target temperature, setpoint (unqualified)
