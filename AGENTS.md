# Mimirheim — Agent Instructions

This file governs how an AI coding agent should behave when working on the mimirheim codebase. Read it in full before making any changes.

---

## Source of truth

**README.md** is the authoritative specification for external behaviour: MQTT topics, configuration schema, output format, and strategy semantics. **IMPLEMENTATION_DETAILS.md** is the authoritative specification for internal architecture: class design, data flow, concurrency model, testing approach, and all implementation decisions.

Before writing or modifying any code, read the relevant sections of both documents. If a user request conflicts with or deviates from the documented design, flag the conflict explicitly and ask the user to confirm the direction before proceeding. Do not silently implement something that contradicts a documented decision.

The `wiki/` directory contains user-facing and developer documentation derived from the above sources. These pages are supplementary — the primary sources above remain authoritative.

| Wiki page | Purpose |
|---|---|
| [wiki/Quick-Start.md](wiki/Quick-Start.md) | New user setup guide (PV + battery scenario) |
| [wiki/Configuration.md](wiki/Configuration.md) | Narrative guide for `config.yaml` |
| [wiki/Developer/Helper-API.md](wiki/Developer/Helper-API.md) | MQTT contract for writing custom helpers |
| [wiki/Developer/Architecture.md](wiki/Developer/Architecture.md) | Internal architecture summary |
| [wiki/Reference/MQTT-Topics.md](wiki/Reference/MQTT-Topics.md) | Complete MQTT topic reference |
| `wiki/Reference/Config-*.md` | Auto-generated field reference (run `python3 scripts/extract_config_docs.py` to regenerate) |
| `wiki/Helpers/*.md` | Per-helper setup and configuration guides |

---

## Implementation plan

The `plans/` directory contains numbered step files that define the implementation sequence. Each file is self-contained: it lists the relevant IMPLEMENTATION_DETAILS sections, the tests to write first, the files to create or edit, and explicit acceptance criteria.

Before starting any implementation work:

1. Run `ls plans/` to find the lowest-numbered step file.
2. Read that file in full before writing any code.
3. Follow the TDD workflow it prescribes: write the tests first, confirm they fail, then implement.
4. When all acceptance criteria pass, move the file:

```bash
mv plans/NN_step_name.md plans/done/
```

5. Then read the next step file.

Do not read ahead into future step files during implementation. Each step is designed to be approached without assumptions about how later steps will be structured. Do not begin a new step until the current step's tests are green and its file has been moved to `plans/done/`.

---

## Behaviour rules

### Be critical

Do not accept instructions uncritically. If a request is vague, contradictory, likely to introduce a regression, or inconsistent with the documented architecture, say so clearly and explain why before asking how to proceed.

If a user asks for something that could be done in multiple ways with meaningfully different trade-offs, present the options and their consequences rather than picking one silently.

### Ask before assuming

If any aspect of a request is unclear — scope, device type, configuration field name, expected behaviour, test coverage expectations — ask a focused question before writing code. A short clarifying exchange is cheaper than a large wrong implementation.

Do not ask multiple questions at once. Identify the single most important ambiguity and ask about that first.

### No surprises in scope

Do not add features, refactors, or "improvements" that were not requested. If you notice something worth improving while working on a task, mention it as a separate suggestion after completing the requested work. Do not bundle unsolicited changes into a pull request.

---

## Code standards

### Type annotations

All public functions and methods must have complete type annotations on parameters and return values. This is non-negotiable: the codebase uses Pydantic and a typed Protocol (`Device`), and unannotated code breaks the contract.

### Pydantic models

Every Pydantic model must include:

```python
model_config = ConfigDict(extra="forbid")
```

This is a hard project rule documented in IMPLEMENTATION_DETAILS §1. An unguarded model silently discards unknown fields, which defeats the purpose of schema validation at system boundaries.

### Exception handling

Never use a bare `except:` or `except Exception:` without immediately re-raising or logging with full traceback. Catch the most specific exception type possible. The fault resilience design (IMPLEMENTATION_DETAILS §10) depends on exceptions surfacing cleanly to the solve loop, not being swallowed inside helper functions.

### No emoticons

Do not use emoticons, emoji, or decorative symbols anywhere in code, comments, docstrings, commit messages, or documentation.

---

## Comments and documentation

### Assume limited MIP knowledge

The intended reader of code comments is a competent Python developer who has not worked with mixed-integer linear programming before. Do not assume familiarity with terms like "decision variable", "constraint matrix", "Big-M", "feasible region", or "incumbent". When these concepts appear, explain them in plain language alongside the formal notation.

### Energy and power jargon is appropriate

Conversely, assume the reader understands basic energy concepts: kW vs kWh, state of charge, grid import and export, efficiency losses, the difference between power and energy. Use these terms without explanation.

### Comment every non-trivial constraint and variable

For every MIP variable declared and every constraint added, include a comment that explains:

1. What physical quantity the variable represents and its units
2. Why the bound or constraint exists (what physical or operational rule it encodes)
3. What would go wrong if the constraint were removed

Example of the required comment depth:

```python
# charge_seg[t, i] represents the power delivered to the battery in kilowatts
# during time step t via efficiency segment i.
#
# Decision variable: the solver chooses how much power flows through each segment
# to maximise the objective. A "segment" is a power range with a fixed efficiency;
# using multiple segments approximates the real curve where efficiency varies with
# power level.
#
# Upper bound: segment i can deliver at most segment.power_max_kw kilowatts.
# The total across all segments is the maximum charge power for this time step.
# There is no separate max_charge_kw field; the segment bounds define it implicitly.
charge_seg[t, i] = ctx.solver.add_var(lb=0.0, ub=segment.power_max_kw)
```

### Docstring format

Use Google-style docstrings on all public classes and functions. This format is supported by most Python documentation generators (pdoc, mkdocs-material with mkdocstrings, Sphinx with napoleon).

```python
def build_and_solve(bundle: SolveBundle, config: MimirheimConfig) -> SolveResult:
    """Build and solve the MILP optimisation model for the current time horizon.

    This is the central function of mimirheim. It takes a snapshot of all current
    inputs (prices, forecasts, device states) and the static system configuration,
    constructs a linear programme, solves it, and returns the optimal schedule.

    The function is deliberately a pure function with no I/O side effects. It
    does not read from MQTT, write files, or log. Callers are responsible for
    obtaining the inputs and acting on the result.

    Args:
        bundle: Validated snapshot of all runtime inputs for this solve cycle.
            Assembled from the latest retained MQTT values by the IO layer.
        config: Validated static system configuration loaded at startup.

    Returns:
        SolveResult containing the full schedule and solve metadata. If the
        solver finds no feasible solution, solve_status is "infeasible" and
        the schedule list is empty.

    Raises:
        ValueError: If bundle and config are internally inconsistent (e.g. a
            device named in bundle.battery_inputs has no matching entry in
            config.batteries).
    """
```

### Module-level docstrings

Every module must have a module-level docstring that explains its purpose, its place in the module hierarchy, and what it does not do (to prevent scope creep).

---

## Development environment

All commands must be run inside the managed virtual environment. The project uses `uv` for dependency management. If the `.venv` directory does not exist, run `uv sync` first to create it and install all dependencies from the committed lockfile.

The project targets **Python 3.12**. If `uv sync` picks a different interpreter, pin it explicitly:

```bash
uv sync --python 3.12 --all-extras   # first-time setup: installs core + all helper dependencies
uv sync --all-extras                  # subsequent syncs reuse the pinned interpreter
uv run pytest                         # run tests
uv run python -m mimirheim --config config.yaml   # run the application
```

Never invoke `python`, `pytest`, or `pip` directly — always prefix with `uv run` to ensure the correct interpreter and installed packages are used. Do not modify `uv.lock` by hand; it is updated automatically by `uv add` and `uv sync`.

To install only the core solver without any helper dependencies:

```bash
pip install mimirheim
```

To install the core solver with specific helpers:

```bash
pip install "mimirheim[nordpool,pv-fetcher,reporter]"
pip install "mimirheim[helpers]"   # all helpers at once
```

---

## Testing discipline

### Preferred methodology: test-driven development

Write the test before writing the implementation. For unit-testable components — Pydantic models, device constraint logic, MQTT publisher behaviour, readiness state — this is strictly enforced: the test must exist and fail before any implementation code is written. A failing test is an unambiguous specification; the agent's work is complete when the test passes and nothing more.

`uv run pytest` produces a binary signal. Use it after every implementation step, not just at the end.

**Golden file scenarios are an exception.** The solver output cannot be known before the solver exists. For these, use a fixture-first variant of TDD:

1. Write `input.json` and `config.yaml` for the scenario, defining the inputs and their structure.
2. Write the test that loads them and asserts against `golden.json` — this will fail because `golden.json` does not yet exist.
3. Once the solver implementation passes all unit tests, run `pytest --update-golden` to generate `golden.json` from the first passing solve.
4. Review the generated values for correctness, then commit. From that point forward the golden file is locked.

**Integration tests are written last.** They require enough of the stack to be in place to be meaningful. Write integration tests after the unit-tested components are complete.

### Run tests before writing substantial code

Before starting any substantial implementation or modification, run the full test suite and confirm the baseline. "Substantial" means any change that affects solver constraints, objective terms, MQTT topic handling, Pydantic model fields, or the solve loop.

```bash
uv run pytest
```

If tests are already failing before your change, stop and report this to the user before proceeding.

### Fix regressions before continuing

If your change causes a previously passing test to fail, fix the regression before writing more code. Do not defer regression fixes.

### New tests for every substantial change

Every substantial change must be accompanied by new unit tests that cover the changed behaviour. New tests belong in the appropriate layer:

- New device constraint logic → `tests/unit/test_<device>_constraints.py`
- New config field → `tests/unit/test_config_schema.py` (happy path + validation rejection)
- New MQTT topic → `tests/unit/test_mqtt_publisher.py`
- New solve scenario → `tests/scenarios/<scenario_name>/` golden file set

### Golden file discipline

Golden files (`tests/scenarios/*/golden.json`) are committed to the repository and represent the expected solver output for a specific input. They must only be updated deliberately:

```bash
pytest --update-golden
```

Never run `--update-golden` to fix a failing test without first confirming that the output change is correct and intentional. A golden file diff in a pull request is a material change to solver behaviour and must be reviewed as such. Always include a comment in the pull request explaining what changed in the output and why it is correct.

### Test the sad paths

For every Pydantic input model, write at least one test that confirms `model_validate` raises on invalid input. For every fault resilience path (infeasible solve, stale input, MQTT disconnect), write a test that confirms the correct behaviour: retained schedule unchanged, status topic updated, process continues.

---

## Project structure

The following structure is canonical. Do not create modules outside this layout without a documented reason.

```
mimirheim/
  config/
    schema.py           # MimirheimConfig and all *Config Pydantic models
    example.yaml        # Annotated example configuration
  core/
    bundle.py           # SolveBundle, SolveResult, per-device input models
    context.py          # ModelContext
    solver_backend.py   # SolverBackend Protocol and HiGHS implementation
    objective.py        # ObjectiveBuilder
    model_builder.py    # build_and_solve() and power balance assembly
    confidence.py       # Helpers that consume per-step confidence values
    readiness.py        # ReadinessState and per-topic staleness tracking
  devices/
    battery.py
    pv.py
    ev.py
    grid.py
    deferrable_load.py
    static_load.py
  io/
    mqtt_client.py      # paho wrapper, loop_start, on_message dispatch
    mqtt_publisher.py   # Publishes SolveResult to all output topics
    input_parser.py     # Parses raw MQTT payloads into validated input models
  __main__.py           # Entry point: config load, solve loop, signal handling
tests/
  unit/
    test_battery_constraints.py
    test_pv_constraints.py
    test_ev_constraints.py
    test_deferrable_load_constraints.py
    test_objective_builder.py
    test_horizon.py
    test_config_schema.py
    test_input_parser.py
    test_mqtt_publisher.py
    test_readiness.py
  scenarios/
    high_price_spread/
    flat_price/
    negative_export_price/
    ev_not_plugged/
    low_confidence_horizon/
    zero_export_constrained/
  integration/
    test_mqtt_roundtrip.py
    test_readiness_mqtt.py
  conftest.py
pyproject.toml
README.md
IMPLEMENTATION_DETAILS.md
AGENTS.md
plans/
  01_project_scaffold.md
  02_config_schema.md
  ...etc
  done/
```

Device modules must not import from `mimirheim/io/`. Config models must not import from `mimirheim/core/` or `mimirheim/io/`. See IMPLEMENTATION_DETAILS §6 and §7 for the full boundary rules.

---

## Boundary and architecture rules

These rules are documented in IMPLEMENTATION_DETAILS and must not be violated without an explicit design change:

- `build_and_solve()` is a pure function. It has no side effects and no I/O. Do not add logging, file writes, or MQTT calls inside it. `debug_dump()` is called by the solve loop after `build_and_solve()` returns, not inside it.
- Device classes never import from `mimirheim/io/`. Runtime inputs are passed in as arguments; devices never fetch their own data.
- All Pydantic models use `extra="forbid"`.
- Confidence values are supplied externally in `SolveBundle`. mimirheim does not compute or decay confidence internally.
- `ModelContext` does not carry `SolveBundle` or `MimirheimConfig`. These are passed explicitly at each call site.
- There is exactly one `Grid` device instance per solve. It is not in a named map in config.
- The power balance constraint is assembled in `build_and_solve()`, not inside any device.

---

## Writing style

Use a professional, precise writing style in all documentation, comments, commit messages, and docstrings. Write in complete sentences. Avoid colloquialisms, filler phrases ("simply", "just", "obviously"), and rhetorical questions.

Prefer active voice. Prefer short paragraphs. If a concept requires more than three sentences to introduce, consider whether it belongs in IMPLEMENTATION_DETAILS rather than an inline comment.

Do not add hedging language ("might", "could possibly", "in some cases") unless there is genuine uncertainty that the reader needs to know about.
