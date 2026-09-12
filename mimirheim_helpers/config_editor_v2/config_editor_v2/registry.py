"""Registry of configuration files config-editor-v2 can edit.

This module is part of config-editor-v2's rendering-library-agnostic core.
It defines `RegistryEntry`, `resolve_model` (which imports the Pydantic
model class a registry entry names), and `REGISTRY`, the list of entries
this editor's own Python environment can actually produce, built once at
import time by `_build_registry`.

`REGISTRY`'s contents are hand-maintained in the sense that adding support
for a new configuration file means adding one entry to `_build_registry`;
it is not a plugin-discovery mechanism. Each entry is still gated on a
successful import of its model's package, so an environment missing an
optional helper dependency gets a shorter `REGISTRY`, not a crash. See
mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md, section
"Registry", for the full design.

This module does not validate configuration data, generate JSON Schema, or
read or write YAML files. It only describes what can be edited and how to
locate the Pydantic model responsible for it.
"""

from __future__ import annotations

import importlib

from pydantic import BaseModel, ConfigDict


class RegistryEntry(BaseModel):
    """Describes one top-level configuration file this editor can produce.

    Attributes:
        name: Human-readable name shown in the editor's navigation.
        filename: The YAML filename this entry writes to, relative to the
            configured config directory.
        model_path: Fully qualified dotted import path to the Pydantic model
            class that defines and validates this file's contents, e.g.
            "mimirheim.config.schema.MimirheimConfig".
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    filename: str
    model_path: str


def resolve_model(entry: RegistryEntry) -> type[BaseModel]:
    """Imports and returns the Pydantic model class named by an entry.

    Args:
        entry: The registry entry naming the model to resolve.

    Returns:
        The imported Pydantic model class.

    Raises:
        ImportError: If the module in `entry.model_path` cannot be imported.
        AttributeError: If the module does not define the named class.
        TypeError: If the resolved attribute is not a BaseModel subclass.
    """
    module_name, _, class_name = entry.model_path.rpartition(".")
    module = importlib.import_module(module_name)
    resolved = getattr(module, class_name)
    if not (isinstance(resolved, type) and issubclass(resolved, BaseModel)):
        raise TypeError(f"{entry.model_path} does not name a Pydantic BaseModel subclass")
    return resolved


def _build_registry() -> list[RegistryEntry]:
    """Builds the list of real, importable configuration sources.

    `MimirheimConfig` is always registered; it is a core dependency of this
    project and always importable. Every helper entry is wrapped in its own
    `try`/`except ImportError`, mirroring config-editor (v1)'s
    `_load_helper_models()` pattern (see
    mimirheim_helpers/config_editor/config_editor/server.py). Some helper
    packages pull in optional third-party dependencies (for example
    `pv_ml_learner` needs `xgboost` and `sqlalchemy`) that may not be
    installed in every environment this editor runs in. A helper whose
    package is not installed is silently omitted from the registry rather
    than crashing this editor's own startup.

    Not registered here, per IMPLEMENTATION_DETAILS.md's "Non-goals" section
    and this editor's own scope:

    - `helper_common`'s `MqttConfig` and `HomeAssistantConfig`: these are
      shared sub-models embedded within other configs, not top-level
      configuration files of their own.
    - `config_editor.config.ConfigEditorConfig` (v1's own bootstrap config)
      and this editor's own `ConfigEditorV2Config`: neither editor is
      designed to edit its own bootstrap configuration through itself.

    Returns:
        The registry entries for every configuration source importable in
        the current Python environment.
    """
    entries = [
        RegistryEntry(
            name="Mimirheim",
            filename="mimirheim.yaml",
            model_path="mimirheim.config.schema.MimirheimConfig",
        ),
    ]

    try:
        import nordpool.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="Nordpool",
                filename="nordpool.yaml",
                model_path="nordpool.config.NordpoolConfig",
            )
        )
    except ImportError:
        pass

    try:
        import zonneplan_prices.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="Zonneplan",
                filename="zonneplan.yaml",
                model_path="zonneplan_prices.config.ZonneplanPricesConfig",
            )
        )
    except ImportError:
        pass

    try:
        import epexpredictor_prices.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="EPEX Predictor",
                filename="epexpredictor.yaml",
                model_path="epexpredictor_prices.config.EpexPredictorPricesConfig",
            )
        )
    except ImportError:
        pass

    try:
        import pv_fetcher.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="PV Forecast (forecast.solar)",
                filename="pv-fetcher.yaml",
                model_path="pv_fetcher.config.PvFetcherConfig",
            )
        )
    except ImportError:
        pass

    try:
        import pv_openmeteo.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="PV Forecast (Open-Meteo)",
                filename="pv-openmeteo.yaml",
                model_path="pv_openmeteo.config.PvOpenMeteoConfig",
            )
        )
    except ImportError:
        pass

    try:
        import pv_ml_learner.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="PV Forecast (ML learner)",
                filename="pv-ml-learner.yaml",
                model_path="pv_ml_learner.config.PvLearnerConfig",
            )
        )
    except ImportError:
        pass

    try:
        import baseload_static.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="Baseload (static)",
                filename="baseload-static.yaml",
                model_path="baseload_static.config.BaseloadConfig",
            )
        )
    except ImportError:
        pass

    try:
        import baseload_ha.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="Baseload (Home Assistant)",
                filename="baseload-ha.yaml",
                model_path="baseload_ha.config.BaseloadConfig",
            )
        )
    except ImportError:
        pass

    try:
        import baseload_ha_db.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="Baseload (Home Assistant + database)",
                filename="baseload-ha-db.yaml",
                model_path="baseload_ha_db.config.BaseloadConfig",
            )
        )
    except ImportError:
        pass

    try:
        import reporter.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="Reporter",
                filename="reporter.yaml",
                model_path="reporter.config.ReporterConfig",
            )
        )
    except ImportError:
        pass

    try:
        import scheduler.config  # noqa: F401

        entries.append(
            RegistryEntry(
                name="Scheduler",
                filename="scheduler.yaml",
                model_path="scheduler.config.SchedulerConfig",
            )
        )
    except ImportError:
        pass

    return entries


# Built once at import time from whichever helper packages are actually
# installed in this environment. See `_build_registry` for what is
# registered, what is silently omitted, and what is deliberately excluded.
REGISTRY: list[RegistryEntry] = _build_registry()
