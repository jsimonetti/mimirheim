"""Base classes for mimirheim helper daemons.

Two classes are provided:

``MqttDaemon``
    Pure MQTT lifecycle base: paho client construction, connection, signal
    handling, and clean shutdown.  Subclasses override ``_on_connect``,
    ``_on_disconnect``, and ``_on_message`` to add behaviour.  Also provides
    ``_publish_stats`` for publishing per-cycle JSON statistics to a configured
    MQTT topic.  Use this when the daemon is event-driven rather than
    trigger-driven (e.g. the reporter, or ``PvLearnerDaemon`` which has two
    independent trigger topics).

``HelperDaemon(MqttDaemon)``
    Extends ``MqttDaemon`` with the trigger-based cycle pattern used by all
    data-input helpers (nordpool, baseload-*, pv-fetcher, scheduler):

    - Subscription to the tool's trigger topic and ``homeassistant/status``.
    - Retain guard (broker-replayed messages are ignored).
    - 5-second trigger debounce.
    - Optional rate-limit suppression: ``_run_cycle`` may return a ``datetime``
      to suppress all further triggers until that UTC time has passed.
    - HA MQTT discovery publication on connect and on HA birth message.

    Subclasses implement a single method: ``_run_cycle(client)``.
"""
from __future__ import annotations

import abc
import json
import logging
import random
import signal
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from helper_common.config import HomeAssistantConfig, MqttConfig
from helper_common.cycle import CycleResult
from helper_common.discovery import publish_trigger_discovery

logger = logging.getLogger(__name__)

# Home Assistant publishes "online" retained to this topic on startup and on
# MQTT integration reload. Subscribing allows the daemon to re-publish its
# discovery payload so HA restores the trigger button without a full restart.
_HA_STATUS_TOPIC = "homeassistant/status"


class MqttDaemon(abc.ABC):
    """MQTT lifecycle base class for mimirheim helper daemons.

    Provides paho client construction, connection management, signal handling,
    and clean shutdown.  Subclasses override ``_on_connect``, ``_on_disconnect``,
    and ``_on_message`` to add their own MQTT behaviour.

    The config object must have a ``.mqtt`` attribute of type ``MqttConfig``.
    """

    def __init__(self, config: Any) -> None:
        """Initialise with a validated config object.

        Args:
            config: Validated tool configuration.  Must have a ``.mqtt``
                attribute of type ``MqttConfig``.
        """
        self._config = config
        # Derive the logger name from the package that defines the subclass.
        # When run via `python -m baseload_static`, __module__ is "__main__".
        # sys.modules["__main__"].__spec__.name is "baseload_static.__main__";
        # we take only the package part (before the first dot) so the logger
        # is named "baseload_static" rather than "baseload_static.__main__".
        # Fall back to the class name for direct-script invocations.
        _module = self.__class__.__module__
        if _module == "__main__":
            _spec = getattr(sys.modules.get("__main__"), "__spec__", None)
            if _spec is not None:
                _module = _spec.name.split(".")[0]
            else:
                _module = type(self).__name__
        self._logger = logging.getLogger(_module)
        self._client = self._build_client()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Connect to the broker and run until SIGTERM or SIGINT.

        Connects the paho client, starts the network loop in a background
        thread, then blocks on a stop event. On signal, stops the loop and
        disconnects cleanly.
        """
        cfg: MqttConfig = self._config.mqtt
        stop_event = threading.Event()

        def _shutdown(signum: int, frame: object) -> None:
            self._logger.info("Signal %d received; shutting down.", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        self._client.connect(cfg.host, cfg.port)
        self._client.loop_start()

        self._logger.info(
            "%s started. Connecting to %s:%d.",
            self.__class__.__name__,
            cfg.host,
            cfg.port,
        )

        stop_event.wait()

        self._client.loop_stop()
        self._client.disconnect()
        self._logger.info("%s shut down cleanly.", self.__class__.__name__)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_client(self) -> mqtt.Client:
        """Construct and configure the paho client."""
        cfg: MqttConfig = self._config.mqtt
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.client_id,
        )
        if cfg.tls_allow_insecure:
            client.tls_set(cert_reqs=ssl.CERT_NONE)
            client.tls_insecure_set(True)
        if cfg.username is not None:
            client.username_pw_set(cfg.username, cfg.password)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if reason_code != 0:
            self._logger.error("MQTT connect failed: %s", reason_code)
            return
        cfg: MqttConfig = self._config.mqtt
        self._logger.info("Connected to %s:%d.", cfg.host, cfg.port)

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        if reason_code != 0:
            self._logger.warning(
                "MQTT disconnected unexpectedly (reason_code=%s); reconnecting.",
                reason_code,
            )

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: Any,
    ) -> None:
        pass  # base no-op; subclasses override

    def _publish_stats(
        self,
        start_ts: datetime,
        duration_s: float,
        result: CycleResult,
    ) -> None:
        """Publish a per-cycle statistics payload to ``stats_topic``.

        Called after every completed cycle (success or exception). The payload
        is a retained QoS-1 JSON message suitable for consumption by HA
        MQTT sensors published via ``publish_trigger_discovery``.

        Does nothing if ``config.stats_topic`` is absent or ``None``.

        Args:
            start_ts: UTC datetime at which the cycle started. Used as the
                ``ts`` field in the payload.
            duration_s: Wall-clock seconds elapsed during the cycle.
            result: The ``CycleResult`` returned (or constructed on exception).
                Provides ``success``, ``horizon_hours``, ``exit_code``, and
                ``exit_message`` fields.
        """
        stats_topic: str | None = getattr(self._config, "stats_topic", None)
        if stats_topic is None:
            return
        payload = {
            "ts": start_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "success": result.success,
            "duration_s": duration_s,
            "horizon_hours": result.horizon_hours,
            "exit_code": result.exit_code,
            "exit_message": result.exit_message,
        }
        self._client.publish(stats_topic, json.dumps(payload), qos=1, retain=True)
        self._logger.debug("Published stats to %s.", stats_topic)


class HelperDaemon(MqttDaemon, abc.ABC):
    """Abstract base class for trigger-based mimirheim helper daemons.

    Extends ``MqttDaemon`` with the trigger-based cycle pattern used by all
    data-input helpers (nordpool, baseload-*, pv-fetcher, scheduler):

    - Subscription to the tool's trigger topic and ``homeassistant/status``.
    - Retain guard (broker-replayed messages are ignored).
    - 5-second trigger debounce.
    - Optional rate-limit suppression: ``_run_cycle`` may return a
      ``CycleResult`` with ``suppress_until`` set to a future UTC datetime,
      which suppresses all further triggers until that time has passed.
    - Per-cycle stats publication to ``stats_topic`` when configured.
    - HA MQTT discovery publication on connect and on HA birth message.

    Subclasses must define a class-level ``TOOL_NAME`` string.  This is used
    as the HA entity ``unique_id`` and ``object_id``, and should be stable
    across releases (changing it creates a duplicate entity in HA).

    Example::

        class NordpoolDaemon(HelperDaemon):
            TOOL_NAME = "nordpool_prices"

            def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
                steps = asyncio.run(fetch_prices(...))
                publish_prices(client, ...)
                return None

    A subclass that needs to suppress further triggers after a rate-limit
    response (e.g. pv_fetcher hitting forecast.solar's 429 limit) returns a
    ``CycleResult`` with ``suppress_until`` set::

        def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
            try:
                result = fetch(...)
            except RatelimitError as exc:
                return CycleResult(suppress_until=exc.reset_at)
            publish(client, result)
            return None

    Attributes:
        TOOL_NAME: Stable snake_case identifier for this tool.
    """

    TOOL_NAME: str  # must be overridden in each subclass

    def __init__(self, config: Any) -> None:
        self._last_trigger_at: float | None = None
        self._ratelimit_until: datetime | None = None
        super().__init__(config)

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Execute one fetch-and-publish cycle.

        Called each time a non-retained, non-debounced, non-rate-limited
        message arrives on the trigger topic.

        Unhandled exceptions are caught by the base class, which constructs a
        ``CycleResult(success=False)`` on behalf of the subclass. Subclasses
        should therefore not catch broad exceptions; let specific ones propagate
        so the base class records the failure correctly.

        Args:
            client: The connected paho MQTT client. Use it to publish results.

        Returns:
            ``None`` on success with no rate-limit suppression required.
            A ``CycleResult`` to report horizon length, or to set
            ``suppress_until`` when an external API returns a rate-limit
            response. The base class stores ``suppress_until`` and checks it
            automatically on the next trigger.
        """

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ha_config(self) -> HomeAssistantConfig | None:
        """Return the HA discovery config if present and enabled."""
        ha = getattr(self._config, "ha_discovery", None)
        if ha is not None and ha.enabled:
            return ha
        return None

    def _tool_label(self) -> str:
        """Return the display label for this tool."""
        ha = getattr(self._config, "ha_discovery", None)
        if ha is not None and ha.device_name:
            return ha.device_name
        return self.TOOL_NAME.replace("_", " ").title()

    def _publish_discovery(self) -> None:
        """Publish the HA discovery payload for this tool if discovery is enabled.

        Publishes the button entity and, when ``stats_topic`` is configured,
        the four stats sensor entities. Any previously published discovery
        topics that are no longer active (e.g. sensors from a config where
        ``stats_topic`` has since been removed) are deleted unconditionally.
        """
        ha = self._ha_config()
        if ha is None:
            return
        publish_trigger_discovery(
            self._client,
            tool_name=self.TOOL_NAME,
            tool_label=self._tool_label(),
            trigger_topic=self._config.trigger_topic,
            stats_topic=getattr(self._config, "stats_topic", None),
            discovery_prefix=ha.discovery_prefix,
        )

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        super()._on_connect(client, userdata, flags, reason_code, properties)
        if reason_code != 0:
            return
        client.subscribe(self._config.trigger_topic, qos=1)
        # Subscribe to HA birth message so discovery can be re-published when
        # HA restarts or reloads its MQTT integration.
        client.subscribe(_HA_STATUS_TOPIC, qos=1)
        self._publish_discovery()

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: Any,
    ) -> None:
        topic = message.topic

        # HA birth message: HA publishes "online" retained here on startup or
        # MQTT reload. This check must come BEFORE the retain guard because
        # the birth message itself is retained.
        if topic == _HA_STATUS_TOPIC:
            if message.payload == b"online":
                delay = random.uniform(1.0, 5.0)
                self._logger.info(
                    "Received HA birth message; will re-publish discovery in %.1f s.",
                    delay,
                )
                threading.Timer(delay, self._publish_discovery).start()
            return

        # Retained messages are broker-replayed on every subscribe. A retained
        # trigger represents a past request, not a new one.
        if message.retain:
            self._logger.debug("Ignoring retained message on %r.", topic)
            return

        # Rate-limit suppression: when _run_cycle returns a datetime, all
        # triggers are dropped until that UTC time has passed. This is used by
        # tools that call external APIs with request quotas (e.g. forecast.solar).
        now = datetime.now(tz=timezone.utc)
        if self._ratelimit_until is not None and now < self._ratelimit_until:
            self._logger.debug(
                "Rate limit active until %s UTC; ignoring trigger.",
                self._ratelimit_until.strftime("%H:%M:%S"),
            )
            return

        now_mono = time.monotonic()
        if (
            self._last_trigger_at is not None
            and now_mono - self._last_trigger_at < 5.0
        ):
            self._logger.debug(
                "Trigger debounced (%.1f s since last).",
                now_mono - self._last_trigger_at,
            )
            return

        self._last_trigger_at = now_mono
        self._logger.info("Trigger received on %r.", topic)
        stats_topic: str | None = getattr(self._config, "stats_topic", None)
        start_ts = datetime.now(tz=timezone.utc)
        start_mono = now_mono
        try:
            result: CycleResult | None = self._run_cycle(client)
        except Exception:
            # An unhandled exception in _run_cycle would propagate into the
            # paho network thread and kill it, taking down the whole daemon.
            # Log the full traceback and continue running so the next trigger
            # can still be processed.
            self._logger.exception(
                "Unhandled exception in _run_cycle — daemon continues."
            )
            result = CycleResult(success=False)
        if stats_topic is not None:
            duration_s = time.monotonic() - start_mono
            self._publish_stats(start_ts, duration_s, result or CycleResult())
        if result is not None:
            self._ratelimit_until = result.suppress_until
        else:
            self._ratelimit_until = None



