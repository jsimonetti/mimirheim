# Plan 49 — Auto-derive global system topics from `mqtt.topic_prefix`

## Motivation

Every mimirheim deployment currently requires eight explicit topic strings in the
`outputs:` section (schedule, current, last_solve, availability), plus the
`inputs.prices` topic and `reporting.notify_topic`. All of these are simple
functions of `mqtt.topic_prefix`. A deployment with the default prefix `"mimirheim"`
ends up writing:

```yaml
outputs:
  schedule:     "mimir/strategy/schedule"
  current:      "mimir/strategy/current"
  last_solve:   "mimir/status/last_solve"
  availability: "mimir/status/availability"
```

This is configuration that adds no information: the operator has merely
transcribed the naming convention by hand. If they change `topic_prefix` they
must also update four output topics and `reporting.notify_topic` (which
currently hardcodes `"mimirheim"` in its default string, silently wrong when the
prefix is anything else).

This plan makes those topics optional. When not set, they are derived
automatically from `mqtt.topic_prefix` using the convention documented in the
new IMPLEMENTATION_DETAILS §12 (introduced in this plan). When set explicitly,
the provided value is used unchanged as the escape hatch. The derivation is
applied by a `model_validator` on `MimirheimConfig`, so every downstream consumer
of the config always sees fully resolved strings — no `or f"{prefix}/..."` is
needed in the IO layer.

This plan also moves the `inputs.prices` derivation out of the IO layer and
into the schema validator, making the pattern consistent: the IO layer always
reads a fully-resolved topic, never a `None`.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §1 Configuration parsing and validation
- §3 Config schema design
- §9 Concurrency model (topic subscriptions)

---

## New convention: IMPLEMENTATION_DETAILS §12

A new section §12 "MQTT topic naming convention and auto-derivation" must be
added to `IMPLEMENTATION_DETAILS.md`. Its content is specified in the
Implementation section below. Plan 50 will extend §12 with device-level topics.

---

## Critical design decisions

### Derivation in the schema, not the IO layer

The current approach for `inputs.prices` defers derivation to `mqtt_client.py`:

```python
prices_topic = config.inputs.prices or f"{prefix}/input/prices"
```

This works but means that every future optional topic needs the same `or`
pattern added at every site that reads the topic. This is fragile: a developer
who adds a new topic read site can silently read `None` if they forget the `or`.

After this plan, derivation happens once — in `MimirheimConfig._derive_global_topics`
— under `model_validator(mode="after")`. All downstream code reads
`config.inputs.prices` and always gets a resolved string. The `or` in
`mqtt_client.py` is removed.

### `model_validator(mode="after")` can mutate Pydantic v2 models

Pydantic v2 models are mutable by default (they are only immutable if
`frozen=True` is explicitly set in `model_config`, which mimirheim does not do).
Inside a `mode="after"` validator, direct attribute assignment is safe:

```python
@model_validator(mode="after")
def _derive_global_topics(self) -> "MimirheimConfig":
    p = self.mqtt.topic_prefix
    if self.outputs.schedule is None:
        self.outputs.schedule = f"{p}/strategy/schedule"
    ...
    return self
```

No `model_copy` is necessary here because the models are not frozen.

### `OutputsConfig` fields change from required `str` to `str | None`

The fields `schedule`, `current`, `last_solve`, and `availability` currently
have no default (they are required). Changing them to `str | None = None`
makes them optional in the YAML; omitting the entire `outputs:` section is
now valid. When a user has an existing config with explicit topics, those values
are preserved (the validator only fills in `None` values).

This is a backwards-compatible change: existing configs with explicit topics
continue to work.

### `OutputsConfig` validator remains in `MimirheimConfig`, not inside `OutputsConfig`

`OutputsConfig` does not have access to `mqtt.topic_prefix`. Only `MimirheimConfig`
has both pieces of information. The derivation validator must live on
`MimirheimConfig`.

### `reporting.notify_topic` default is fixed

`ReportingConfig.notify_topic` currently defaults to `"mimir/status/dump_available"`.
This hardcodes `"mimirheim"` as the prefix, so renaming the prefix silently breaks the
reporting notification. The fix: change the field to `str | None = None` and
derive it in `_derive_global_topics` as `f"{p}/status/dump_available"`. The
existing validator that rejects `enabled=True, dump_dir=None` is unaffected.

---

## Naming convention (global topics)

The following table defines the full global topic convention introduced in this
plan. It is documented in IMPLEMENTATION_DETAILS §12 (partial — device topics
are added in Plan 50).

| Config field | Default topic (prefix = `mimirheim`) |
|---|---|
| `outputs.schedule` | `mimir/strategy/schedule` |
| `outputs.current` | `mimir/strategy/current` |
| `outputs.last_solve` | `mimir/status/last_solve` |
| `outputs.availability` | `mimir/status/availability` |
| `inputs.prices` | `mimir/input/prices` |
| `reporting.notify_topic` | `mimir/status/dump_available` |

The strategy and trigger topics are not configurable and are always built
directly from prefix in the IO layer:

| Topic | Always derived as |
|---|---|
| strategy input | `{prefix}/input/strategy` |
| solve trigger | `{prefix}/input/trigger` |

---

## Relevant source locations

```
mimirheim/config/schema.py            — change OutputsConfig, InputsConfig,
                                   ReportingConfig; add MimirheimConfig validator
tests/unit/test_config_schema.py — add derivation and override tests
mimirheim/io/mqtt_client.py           — remove prices "or" pattern
mimirheim/config/example.yaml         — make outputs section optional with comments
README.md                        — update topic table to show derived defaults
IMPLEMENTATION_DETAILS.md        — add §12: topic naming convention
```

---

## Tests first

Add the following tests to `tests/unit/test_config_schema.py`. All must fail
against the current implementation before any implementation work begins.

```python
# --- OutputsConfig derivation ---

def test_outputs_fields_are_optional() -> None:
    """OutputsConfig can be constructed with all fields set to None."""

def test_outputs_all_derived_from_prefix() -> None:
    """When outputs section is omitted, all four output topics are derived
    from mqtt.topic_prefix.
    config = MimirheimConfig.model_validate(minimal_yaml_without_outputs)
    assert config.outputs.schedule == "mimir/strategy/schedule"
    assert config.outputs.current == "mimir/strategy/current"
    assert config.outputs.last_solve == "mimir/status/last_solve"
    assert config.outputs.availability == "mimir/status/availability"
    """

def test_outputs_explicit_topic_overrides_derived() -> None:
    """An explicit outputs.schedule value is used instead of the derived one.
    config.outputs.schedule == "custom/my_schedule"
    config.outputs.current == f"{prefix}/strategy/current"  # others derived
    """

def test_outputs_custom_prefix_reflected_in_derived_topics() -> None:
    """When mqtt.topic_prefix is 'myhome', derived topics use that prefix.
    config.outputs.schedule == "myhome/strategy/schedule"
    """

# --- InputsConfig derivation ---

def test_inputs_prices_derived_from_prefix() -> None:
    """When inputs section is omitted, inputs.prices is derived from prefix.
    config.inputs.prices == "mimir/input/prices"
    """

def test_inputs_prices_explicit_override_preserved() -> None:
    """An explicit inputs.prices value is kept."""

# --- ReportingConfig derivation ---

def test_reporting_notify_topic_derived_from_prefix() -> None:
    """When reporting.notify_topic is not set, it is derived from prefix.
    config.reporting.notify_topic == "mimir/status/dump_available"
    """

def test_reporting_notify_topic_explicit_override_preserved() -> None:
    """An explicit reporting.notify_topic value is kept."""

def test_reporting_notify_topic_uses_custom_prefix() -> None:
    """When mqtt.topic_prefix is 'home/v2', the notify topic uses that prefix."""
```

Run `uv run pytest tests/unit/test_config_schema.py` — all new tests must fail
before implementation begins.

---

## Implementation

### `mimirheim/config/schema.py`

#### `OutputsConfig`

Change all four fields from required `str` to `str | None = None`:

```python
class OutputsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule: str | None = Field(
        default=None,
        description=(
            "Topic for the full horizon schedule. "
            "Defaults to '{mqtt.topic_prefix}/strategy/schedule'."
        ),
    )
    current: str | None = Field(
        default=None,
        description=(
            "Topic for the current-step strategy summary. "
            "Defaults to '{mqtt.topic_prefix}/strategy/current'."
        ),
    )
    last_solve: str | None = Field(
        default=None,
        description=(
            "Topic for the retained solve-status message. "
            "Defaults to '{mqtt.topic_prefix}/status/last_solve'."
        ),
    )
    availability: str | None = Field(
        default=None,
        description=(
            "Topic for birth ('online') and last-will ('offline') messages. "
            "Defaults to '{mqtt.topic_prefix}/status/availability'."
        ),
    )
```

`MimirheimConfig` now requires `outputs: OutputsConfig = Field(default_factory=OutputsConfig)`.
Change the existing `outputs: OutputsConfig` (currently required, no default) to
use `default_factory=OutputsConfig`.

#### `ReportingConfig`

Change `notify_topic` from `str = Field(default="mimir/status/dump_available")` to
`str | None = Field(default=None, ...)`. Update the docstring to note the derived
default.

#### `MimirheimConfig` — new validator

Add a `model_validator(mode="after")` named `_derive_global_topics` that runs after
all field validators. It fills in any `None` global topic fields using `mqtt.topic_prefix`.
This validator must run after all existing validators (Pydantic executes `mode="after"`
validators in definition order on the same model, so define it last in the model body,
after `device_names_unique`):

```python
@model_validator(mode="after")
def _derive_global_topics(self) -> "MimirheimConfig":
    """Fill in global topic fields that were not explicitly set.

    Any global topic field left as None in the YAML is derived from
    mqtt.topic_prefix using the standard naming convention. Explicit values
    are preserved unchanged. After this validator, no global topic field is
    None; downstream code can read these fields without a fallback.

    See IMPLEMENTATION_DETAILS §12 for the full naming convention.
    """
    p = self.mqtt.topic_prefix

    # Output topics
    if self.outputs.schedule is None:
        self.outputs.schedule = f"{p}/strategy/schedule"
    if self.outputs.current is None:
        self.outputs.current = f"{p}/strategy/current"
    if self.outputs.last_solve is None:
        self.outputs.last_solve = f"{p}/status/last_solve"
    if self.outputs.availability is None:
        self.outputs.availability = f"{p}/status/availability"

    # Global input topics
    if self.inputs.prices is None:
        self.inputs.prices = f"{p}/input/prices"

    # Reporting notification topic
    if self.reporting.notify_topic is None:
        self.reporting.notify_topic = f"{p}/status/dump_available"

    return self
```

**Important**: place `_derive_global_topics` **after** `device_names_unique` in the
class body. Both are `mode="after"` validators and Pydantic v2 runs them in class
definition order. Derivation must happen after structural validation is complete.

#### `MimirheimConfig.__doc__` and field docstrings

Update the `MimirheimConfig` docstring to note that `outputs` now has a `default_factory`
and that all global topics are auto-derived when not set.

### `mimirheim/io/mqtt_client.py`

Remove the `or f"{prefix}/input/prices"` fallback:

```python
# Before:
prices_topic = config.inputs.prices or f"{prefix}/input/prices"

# After:
prices_topic = config.inputs.prices
```

`config.inputs.prices` is guaranteed to be a non-None string after the schema
validator runs.

### `mimirheim/config/example.yaml`

Remove the explicit `outputs:` block. Replace with a comment block that explains
the defaults and shows how to override individual topics if needed:

```yaml
# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
# MQTT topics mimirheim publishes to. All are retained.
#
# These topics are derived automatically from mqtt.topic_prefix when not set.
# With the default prefix "mimir" the topics are:
#
#   {prefix}/strategy/schedule   (full horizon schedule JSON)
#   {prefix}/strategy/current    (current-step strategy summary)
#   {prefix}/status/last_solve   (retained solve-status message)
#   {prefix}/status/availability (birth "online" / last-will "offline")
#
# Override individual topics when you need non-standard paths, for example
# when sharing a broker across multiple mimirheim instances or bridging to an
# external integration:
#
# outputs:
#   schedule:    "mimir/strategy/schedule"   # explicit override
#   current:     "mimir/strategy/current"
#   last_solve:  "mimir/status/last_solve"
#   availability: "mimir/status/availability"
```

Update the `inputs:` comment block similarly: show that `prices` is optional and
derived from prefix by default. Update the `reporting:` block to note that
`notify_topic` is derived.

### `IMPLEMENTATION_DETAILS.md` — new §12

Add a new top-level section at the end of the document:

```markdown
## 12. MQTT topic naming convention and auto-derivation

### Motivation

All MQTT topics used by mimirheim follow a predictable naming convention derived
from `mqtt.topic_prefix`. Requiring operators to spell out every topic
explicitly produces configuration that adds no information — it simply
transcribes the convention by hand, with the risk of introducing silent
inconsistencies when the prefix is changed.

Topics that follow the convention are optional in the YAML. Any topic field
left unset is automatically filled in by `MimirheimConfig._derive_global_topics` (for
global topics, Plan 49) and `MimirheimConfig._derive_device_topics` (for per-device
topics, Plan 50) using a `model_validator(mode="after")`. After schema loading,
no topic field is ever `None`; downstream code reads a fully resolved string at
every site.

An operator who needs a non-standard topic for a specific field can override it
by supplying an explicit value in the YAML. Only that field uses the supplied
value; all other fields retain their derived defaults. This escape hatch is
sufficient for scenarios where a broker is shared across multiple mimirheim instances
with conflicting namespaces, or where a helper publishes to a legacy topic path.

### How derivation works

`MimirheimConfig._derive_global_topics` is a `model_validator(mode="after")`. It runs
after all field and sub-model validators have passed. At that point `mqtt.topic_prefix`
is validated and available. The validator iterates over every optional topic field,
checks whether it is `None`, and if so sets it to the derived string. Explicit values
are untouched.

The validator must run after `device_names_unique` (the structural device-name check)
because derivation for device topics (Plan 50) requires iterating the device maps.

### Global topic naming convention

| Config field             | Derived topic                            |
|--------------------------|------------------------------------------|
| `outputs.schedule`       | `{prefix}/strategy/schedule`             |
| `outputs.current`        | `{prefix}/strategy/current`              |
| `outputs.last_solve`     | `{prefix}/status/last_solve`             |
| `outputs.availability`   | `{prefix}/status/availability`           |
| `inputs.prices`          | `{prefix}/input/prices`                  |
| `reporting.notify_topic` | `{prefix}/status/dump_available`         |

Two topics are not configurable and are always derived directly in the IO layer
(they have no config field because they are structural, not user-facing):

| Topic              | Always derived as               |
|--------------------|---------------------------------|
| Strategy input     | `{prefix}/input/strategy`       |
| Solve trigger      | `{prefix}/input/trigger`        |

### Device-level topic naming convention

Per-device topics are documented in Plan 50, which extends this section.
The full device-level table will appear here after Plan 50 is complete.

### Security note

Topic strings are validated at startup using Pydantic. An operator who controls
the config file could potentially craft topic strings that conflict with system
topics or foreign namespaces on a shared broker. This is not a remote-code-
execution risk; it is an operational configuration concern. The validation ensures
topics are non-empty strings but does not restrict the character set beyond what
MQTT requires. Restrict access to the config file to the mimirheim process user.
```

### `README.md`

Locate the section that documents MQTT output topics. Update it to:

1. Note that `outputs:` is optional and topics are derived from `mqtt.topic_prefix`.
2. Show the default topic paths in a table.
3. Add a brief "Override example" showing how a user would override one topic.

---

## Acceptance criteria

- `OutputsConfig` fields `schedule`, `current`, `last_solve`, `availability` are
  all `str | None = None`.
- `inputs.prices` is `str | None = None`.
- `reporting.notify_topic` is `str | None = None`.
- `MimirheimConfig` has a `_derive_global_topics` `mode="after"` validator that fills
  in all six fields when they are None.
- A config with no `outputs:`, no `inputs:`, and no `reporting.notify_topic` field
  produces the same resolved topics as one that spells them out explicitly.
- A config that overrides one output topic keeps the override for that topic and
  derives the rest.
- `mqtt_client.py` reads `config.inputs.prices` directly without a fallback.
- All new unit tests pass. All existing tests remain green (`uv run pytest`).
- `example.yaml` no longer requires the operator to spell out the `outputs:` block.
- `IMPLEMENTATION_DETAILS.md` §12 documents the naming convention (global topics)
  and cross-references Plan 50 for device topics.
- `README.md` topic table updated to show derived defaults.
