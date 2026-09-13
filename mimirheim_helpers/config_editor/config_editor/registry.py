"""In-memory registry of Config Owners discovered via their retained Descriptors.

Populated by `mqtt_client.py` as Descriptors are published or cleared over
MQTT (see `mimirheim_shared/docs/adr/0001`); read by `server.py` to render the
Config Editor's discovery page and each Config Owner's form. Holds no MQTT or
HTTP concerns of its own.
"""

from __future__ import annotations

import threading

from mimirheim_shared.config_service import Descriptor


class ConfigOwnerRegistry:
    """Thread-safe in-memory map of a Config Owner's ID to its Descriptor.

    Descriptors are written from the MQTT network thread (`mqtt_client.py`'s
    `on_message` callback) and read from the HTTP server's request-handling
    threads; a single lock guards every access so a render never observes the
    registry mid-update.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._descriptors: dict[str, Descriptor] = {}

    def update(self, descriptor: Descriptor) -> None:
        """Registers a Config Owner, replacing any previous Descriptor for it.

        Args:
            descriptor: The newly received Descriptor.
        """
        with self._lock:
            self._descriptors[descriptor.owner_id] = descriptor

    def remove(self, owner_id: str) -> None:
        """Removes a Config Owner, e.g. when its retained Descriptor is cleared.

        A no-op if `owner_id` is not currently registered.

        Args:
            owner_id: The Config Owner's stable identifier.
        """
        with self._lock:
            self._descriptors.pop(owner_id, None)

    def get(self, owner_id: str) -> Descriptor | None:
        """Returns the named Config Owner's Descriptor, or None if unknown.

        Args:
            owner_id: The Config Owner's stable identifier.
        """
        with self._lock:
            return self._descriptors.get(owner_id)

    def all(self) -> list[Descriptor]:
        """Returns every currently registered Descriptor, sorted by display name."""
        with self._lock:
            return sorted(self._descriptors.values(), key=lambda d: d.display_name)
