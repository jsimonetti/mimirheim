# 3. Config schema design

**Decision: typed sections instead of a discriminated device map**

The configuration uses typed top-level sections (`batteries:`, `pv_arrays:`, `ev_chargers:`, etc.) rather than a single `devices:` map with a `type:` discriminator field.

## Rationale

A `dict[str, Battery | PV | EV | ...]` discriminated union produces `oneOf` / `anyOf` blocks in JSON Schema. While Pydantic handles these correctly, many UI form generators (json-editor, AutoForm, simpler jsonforms configurations) either fail to render them or require significant custom configuration to do so correctly.

Typed sections produce `additionalProperties: { $ref: "#/$defs/BatteryConfig" }` — a pattern every form library understands. Each section is a homogeneous map with a single, unambiguous schema.

## Consequence

Device names must be unique across all sections since they become keys in the MQTT output payload. This is enforced by a `model_validator` on `MimirheimConfig` at load time. The `type` field present in output payloads is derived at solve time from which section a device belongs to — it is not stored in the config.
