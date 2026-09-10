# 1. Configuration parsing & validation

**Decision: Pydantic v2**

mimirheim uses [Pydantic v2](https://docs.pydantic.dev/latest/) for YAML config loading, validation, and schema generation.

## Rationale

- **Single source of truth.** Field types, constraints (`ge=0`, `le=1`), defaults, and documentation (`title`, `description`) are declared once on the model. No separate JSON Schema file to keep in sync.
- **JSON Schema generation.** `model.model_json_schema()` produces a JSON Schema that can be consumed directly by UI form libraries (jsonforms, react-jsonschema-form). This is the intended path to an auto-generated configuration UI.
- **Rich field annotations.** `Field(title=..., description=..., examples=[...])` carry UI hints at the model level. Annotate all fields from the start so the generated schema is useful.
- **v2 performance.** The Rust core (pydantic-core) makes re-validating config on every solve negligible.

## Known limitations

- **JSON Schema ≠ UI schema.** Pydantic generates the data schema. Most form libraries also require a separate *UI schema* for field ordering, grouping, and conditional visibility (e.g. `strategy_weights` only shown when `strategy: balanced`). That layer must be written once per target UI library.
- **`additionalProperties` maps.** Sections like `batteries: dict[str, BatteryConfig]` generate `additionalProperties` schemas. Most form generators do not render "add a named entry to a map" well out of the box; a custom widget will be needed for device lists.

## Usage pattern

```python
# config/schema.py
import yaml
from pydantic import BaseModel, Field, model_validator

class BatteryConfig(BaseModel):
    capacity_kwh: float = Field(gt=0, title="Capacity", description="Usable battery capacity in kWh")
    ...

class MimirheimConfig(BaseModel):
    batteries: dict[str, BatteryConfig] = Field(default_factory=dict)
    ...

    @model_validator(mode="after")
    def device_names_unique(self) -> "MimirheimConfig":
        # names must be unique across all device sections (they become output keys)
        ...

def load_config(path: str) -> MimirheimConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return MimirheimConfig.model_validate(raw)
```

## Unknown fields are forbidden

All Pydantic models in Mimirheim — config models, `SolveBundle`, per-device input models — must set:

```python
model_config = ConfigDict(extra="forbid")
```

This means a config file or MQTT bundle containing an unrecognised field raises a hard validation error immediately, rather than silently succeeding. The benefit: schema changes are always explicit. A renamed field produces a clear error at load time rather than a silent wrong value or stale field lingering undetected.

Versioning (a top-level `version:` field in `config.yaml`, `schema_version` in golden files) is deferred until the first schema stabilises. `extra="forbid"` is the minimal safeguard that makes the absence of versioning safe during early development.
