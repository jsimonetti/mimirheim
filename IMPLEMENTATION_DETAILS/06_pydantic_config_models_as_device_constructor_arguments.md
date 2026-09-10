# 6. Pydantic config models as device constructor arguments

**Decision: device solver classes accept their Pydantic `*Config` model directly**

Each device solver class takes its validated Pydantic config model as a constructor argument rather than unpacking individual fields:

```python
# mimirheim/devices/battery.py
from mimirheim.config.schema import BatteryConfig

class Battery:
    def __init__(self, name: str, config: BatteryConfig) -> None:
        self.name = name
        self.config = config
        # access as self.config.capacity_kwh, self.config.charge_segments, etc.
```

Devices are instantiated in the build pipeline by iterating over the typed config sections:

```python
# mimirheim/core/model_builder.py
batteries = [
    Battery(name=name, config=cfg)
    for name, cfg in hioo_config.batteries.items()
]
```

## Rationale

- **No parameter duplication.** Adding a field to `BatteryConfig` (e.g. `soc_init_kwh`) automatically makes it available inside `Battery` without updating a constructor signature. With unpacked arguments, every new field requires two changes.
- **Validation already done.** By the time a device class is constructed, the config has passed Pydantic validation. No defensive checks are needed inside device classes for missing or out-of-range values.
- **`model_dump()` is the serialisation path.** When assembling `SolveBundle.device_states` or writing debug dumps, `config.model_dump()` produces the correct JSON with no manual field listing.
- **Refactoring is safe.** Renaming a field in `BatteryConfig` produces a Pydantic validation error immediately at load time, not a silent wrong value deep in the solver.

## Boundary rule

Device classes (`mimirheim/devices/`) may import from `mimirheim/config/schema.py` as a typed argument, but must not import from `mimirheim/io/` or any MQTT/YAML machinery. Config flows downward from IO → config → devices. Devices never call back into IO.

## Confidence is external, not internal

A key divergence from common reference implementations: mimirheim does **not** compute confidence internally using decay parameters (`alpha_price`, `alpha_pv`). Confidence is a per-step float in `SolveBundle`, supplied by the publisher.

Internally computing confidence would:
- Hardcode a specific decay model (exponential) that may not suit all publishers
- Require solver config changes to tune forecast quality
- Prevent publishers from using richer models (ML uncertainty bands, ensemble spread)

The publisher computes confidence from whatever model it uses and injects it into the input schema. `mimirheim/core/confidence.py` contains only *helpers that consume* per-step confidence values — it does not produce them. This is the correct separation.
