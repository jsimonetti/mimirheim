# Mimirheim — Implementation Details

This document is the index into mimirheim's internal implementation decisions: library choices, architectural rationale, and constraints that are not visible in the public API or configuration schema. It is intended for contributors and maintainers, not end users.

Each numbered section below is a one-line summary; the full prose lives in the linked file under `IMPLEMENTATION_DETAILS/`. References elsewhere in the repo (code comments, CLAUDE.md, `plans/`) use `IMPLEMENTATION_DETAILS §N` or `IMPLEMENTATION_DETAILS §N.M` — resolve them by finding `## N.` below and following its link.

---

## Contents

## 1. Configuration parsing & validation

Pydantic v2 for YAML config loading, validation, and schema generation; `extra="forbid"` everywhere. See [`IMPLEMENTATION_DETAILS/01_configuration_parsing_and_validation.md`](IMPLEMENTATION_DETAILS/01_configuration_parsing_and_validation.md).

## 2. Solver backend

CBC via `python-mip`, behind a `SolverBackend` Protocol; the benchmark data behind that choice, the time-limit budget, and the SOS2 emulation. See [`IMPLEMENTATION_DETAILS/02_solver_backend.md`](IMPLEMENTATION_DETAILS/02_solver_backend.md).

## 3. Config schema design

Typed top-level sections instead of a discriminated device map, and why. See [`IMPLEMENTATION_DETAILS/03_config_schema_design.md`](IMPLEMENTATION_DETAILS/03_config_schema_design.md).

## 4. Testing architecture

`build_and_solve()` as a pure function, the three test layers (unit, golden-file scenario, MQTT integration), golden file format, directory layout, and CI matrix. See [`IMPLEMENTATION_DETAILS/04_testing_architecture.md`](IMPLEMENTATION_DETAILS/04_testing_architecture.md).

## 5. Debug solve dump

Structured JSON input/output dump written when `config.debug.enabled` is true. See [`IMPLEMENTATION_DETAILS/05_debug_solve_dump.md`](IMPLEMENTATION_DETAILS/05_debug_solve_dump.md).

## 6. Pydantic config models as device constructor arguments

Device classes take their validated `*Config` model directly; module boundary rules; confidence is supplied externally. See [`IMPLEMENTATION_DETAILS/06_pydantic_config_models_as_device_constructor_arguments.md`](IMPLEMENTATION_DETAILS/06_pydantic_config_models_as_device_constructor_arguments.md).

## 7. SolveBundle and per-device input models

All runtime MQTT inputs as Pydantic models collected into `SolveBundle`; forecast resampling; input/output model definitions. See [`IMPLEMENTATION_DETAILS/07_solvebundle_and_per_device_input_models.md`](IMPLEMENTATION_DETAILS/07_solvebundle_and_per_device_input_models.md).

## 8. MIP model design: device contract, split variables, piecewise efficiency, and objective builder

`ModelContext`, the device method contract, the `Grid` device, split charge/discharge variables, piecewise efficiency, wear cost, vendor capability flags, `ObjectiveBuilder`, the power balance constraint, and the thermal device models (boiler, space heating, combi heat pump, building thermal model). See [`IMPLEMENTATION_DETAILS/08_mip_model_design.md`](IMPLEMENTATION_DETAILS/08_mip_model_design.md).

## 9. Arbitration engine and closed-loop enforcer selection

Why the solver does not zero out closed-loop device variables; enforcer eligibility, scoring, hysteresis, and dwell; loadbalance suppression. See [`IMPLEMENTATION_DETAILS/09_arbitration_engine_and_closed_loop_enforcer_selection.md`](IMPLEMENTATION_DETAILS/09_arbitration_engine_and_closed_loop_enforcer_selection.md).

## 10. Exchange-shaping secondary term

The small `exchange_shaping_weight` objective term that breaks degeneracy under symmetric net-of-meter pricing. See [`IMPLEMENTATION_DETAILS/10_exchange_shaping_secondary_term.md`](IMPLEMENTATION_DETAILS/10_exchange_shaping_secondary_term.md).

## 11. Concurrency model

paho-mqtt background thread, main-thread solve loop, `ReadinessState` locking, single-item queue. See [`IMPLEMENTATION_DETAILS/11_concurrency_model.md`](IMPLEMENTATION_DETAILS/11_concurrency_model.md).

## 12. Fault resilience

Behaviour on infeasible solves, solver exceptions, stale inputs, MQTT disconnection, config load failure, and the `last_solve` status topic. See [`IMPLEMENTATION_DETAILS/12_fault_resilience.md`](IMPLEMENTATION_DETAILS/12_fault_resilience.md).

## 13. Development environment and dependency management

`uv`, `pyproject.toml` structure, dependency groups, `.venv` handling. See [`IMPLEMENTATION_DETAILS/13_development_environment_and_dependency_management.md`](IMPLEMENTATION_DETAILS/13_development_environment_and_dependency_management.md).

## 14. MQTT topic naming convention and auto-derivation

How topics are derived from `mqtt.topic_prefix`, the global and per-device derivation tables, and how to override a derived topic. See [`IMPLEMENTATION_DETAILS/14_mqtt_topic_naming_convention_and_auto_derivation.md`](IMPLEMENTATION_DETAILS/14_mqtt_topic_naming_convention_and_auto_derivation.md).
