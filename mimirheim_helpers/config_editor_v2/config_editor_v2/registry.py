"""Static registry of configuration files config-editor-v2 can edit.

This module is part of config-editor-v2's rendering-library-agnostic core.
It defines `RegistryEntry`, the hand-maintained `REGISTRY` list, and
`resolve_model`, which imports the Pydantic model class a registry entry
names.

This module does not validate configuration data, generate JSON Schema, or
read or write YAML files. It only describes what can be edited and how to
locate the Pydantic model responsible for it. See
mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md, section
"Registry", for the full design.
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


# Hand-maintained list of configuration sources this editor can produce.
# Adding support for a new configuration file means adding one entry here,
# per IMPLEMENTATION_DETAILS.md's "Registry" section. Wiring in real
# mimirheim and helper configuration models is deferred to step 71_5, once
# the full save path exists; this step ships the list empty.
REGISTRY: list[RegistryEntry] = []
