"""MQTT client wrapper — wires paho to the input parser and readiness state.

This module is deliberately thin. Its only responsibilities are:

1. Wrapping the paho ``Client`` and starting the network loop.
2. Routing ``on_message`` events to ``input_parser`` functions.
3. Passing the parsed values to ``ReadinessState.update()``.
4. Calling ``publisher.republish_last_result()`` from ``on_connect``.
5. Queuing a SolveBundle on ``solve_queue`` when a trigger message arrives.

Solves are only triggered by messages on ``{prefix}/input/trigger``.
Regular data topic messages (prices, PV, battery SOC, etc.) only update
``ReadinessState``; they do not directly queue a solve. This separates data
ingestion from solve scheduling, allowing the scheduler (or an operator) to
control how often the solver runs.

All business logic lives elsewhere:
- Parsing: ``input_parser.py``
- Freshness tracking: ``readiness.py``
- Solving: ``model_builder.py``
- Publishing: ``mqtt_publisher.py``

This module imports from ``mimirheim.io`` (parser, publisher) and ``mimirheim.core``
(readiness, bundle types) but does not import from ``mimirheim.devices`` or call
``build_and_solve`` directly.
"""

import logging
import queue
import random
import threading
import time
from datetime import UTC, datetime
from typing import Any

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import DeferrableWindow
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.input_parser import (
    parse_battery_inputs,
    parse_combi_hp_sh_demand,
    parse_combi_hp_temp,
    parse_current_indoor_temp,
    parse_datetime,
    parse_ev_inputs,
    parse_hybrid_inverter_soc,
    parse_outdoor_temp_forecast,
    parse_power_forecast,
    parse_price_steps,
    parse_space_heating_demand,
    parse_strategy,
    parse_thermal_boiler_temp,
)
from mimirheim.io.mqtt_publisher import MqttPublisher
from mimirheim.io.ha_discovery import publish_discovery

logger = logging.getLogger("mimirheim.mqtt")

# Home Assistant publishes "online" retained to this topic when it starts up
# or reloads the MQTT integration. All mimirheim entities become unavailable in HA
# at that point. Subscribing here allows mimirheim to re-publish discovery payloads
# so HA can restore those entities without waiting for the next restart.
# Reference: https://www.home-assistant.io/integrations/mqtt/#birth-and-last-will-messages
_HA_STATUS_TOPIC = "homeassistant/status"

# Minimum interval between accepted trigger messages. Rapid trigger bursts
# (e.g. when multiple input topics publish simultaneously) would queue
# redundant solves. Only the first trigger in each burst is acted on.
_DEBOUNCE_SECONDS: float = 5.0


class MqttClient:
    """Paho wrapper that routes incoming MQTT messages to the mimirheim pipeline.

    Constructs and manages a paho ``Client`` instance. Subscribes to all
    topics listed in the device configuration, the ``{prefix}/input/*``
    data topics, and the trigger topic ``{prefix}/input/trigger``.

    Data topic messages are routed to the appropriate parser and stored in
    ``ReadinessState``. A solve is only queued when a message arrives on the
    trigger topic AND ``ReadinessState.is_ready()`` is True at that moment.

    Attributes:
        _client: The underlying paho ``Client``.
        _config: Static system configuration.
        _readiness: The shared readiness state updated by each incoming message.
        _publisher: The MQTT publisher; called from ``on_connect`` to republish.
        _solve_queue: Optional queue that receives a SolveBundle on trigger.
        _trigger_topic: The MQTT topic that requests a new solve cycle.
        _topic_handlers: Mapping from data topic string to a handler function.
    """

    def __init__(
        self,
        config: MimirheimConfig,
        readiness: ReadinessState,
        publisher: MqttPublisher,
        paho_client: Any,
        solve_queue: queue.Queue | None = None,
    ) -> None:
        """Construct the client and register callbacks.

        Args:
            config: Static system configuration.
            readiness: The shared readiness state.
            publisher: The MQTT publisher (for republish on connect).
            paho_client: An already-constructed paho ``Client`` instance.
            solve_queue: Optional queue that receives a ``SolveBundle`` each
                time a trigger message arrives and ``is_ready()`` is True.
                Uses ``put_nowait``; if the queue is full the bundle is
                discarded. Pass ``None`` to disable queuing.
        """
        self._client = paho_client
        self._config = config
        self._readiness = readiness
        self._publisher = publisher
        self._solve_queue = solve_queue
        # Monotonic timestamp of the last accepted trigger, used to enforce a
        # 5-second debounce window. Two triggers arriving closer together than
        # _DEBOUNCE_SECONDS are deduplicated: only the first is acted on.
        self._last_trigger_at: float | None = None

        prefix = config.mqtt.topic_prefix
        self._trigger_topic = f"{prefix}/input/trigger"

        # Register the last-will BEFORE connecting. If the TCP connection is
        # lost without a clean DISCONNECT (e.g. power cut, kernel kill), the
        # broker will publish this payload automatically so downstream
        # subscribers see the device go offline.
        self._client.will_set(
            config.outputs.availability,
            payload="offline",
            qos=1,
            retain=True,
        )

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        self._topic_handlers = self._build_topic_handlers()

    def start(self) -> None:
        """Connect to the broker and start the network loop in a background thread.

        Blocks until the initial connection is established (paho's
        ``connect()`` initiates TCP), then calls ``loop_start()`` which spawns
        a daemon thread for all subsequent network I/O.
        """
        self._client.connect(self._config.mqtt.host, self._config.mqtt.port)
        self._client.loop_start()

    def stop(self) -> None:
        """Publish offline, stop the network loop, and disconnect cleanly.

        Publishing ``"offline"`` before disconnecting ensures the broker retains
        the correct availability state even when the shutdown is clean (the
        last-will is only triggered on unclean disconnects).
        """
        self._client.publish(
            self._config.outputs.availability,
            payload="offline",
            qos=1,
            retain=True,
        )
        self._client.disconnect()
        self._client.loop_stop()

    # ------------------------------------------------------------------
    # Private: paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client: Any, userdata: Any, _connect_flags: Any, reason_code: Any, properties: Any) -> None:
        """Called by paho when the broker connection is established or restored.

        Subscribes to all expected input topics and asks the publisher to
        re-publish the last known result so that retained topics on the broker
        are current after a restart.

        Args:
            client: The paho client instance.
            userdata: Unused.
            _connect_flags: Connection flags from the broker (unused; prefixed with
                underscore to signal that this argument is part of the paho callback
                signature but not used by this implementation).
            reason_code: A ``ReasonCode`` object; ``is_failure`` is True when
                the connection was refused.
            properties: MQTT v5 properties (unused in v3.1.1 connections).
        """
        if reason_code.is_failure:
            logger.error("MQTT connect failed: %s", reason_code)
            return

        logger.info("MQTT connected. Subscribing to input topics.")
        for topic in self._topic_handlers:
            client.subscribe(topic, qos=1)

        # Subscribe to the trigger topic. A message here requests a solve if
        # readiness is currently met.
        client.subscribe(self._trigger_topic, qos=1)

        # Subscribe to the HA birth message topic so mimirheim can re-publish
        # discovery payloads after HA restarts or reloads its MQTT integration.
        client.subscribe(_HA_STATUS_TOPIC, qos=1)

        # Publish the birth message retained so any subscriber that connects
        # later immediately sees the current online state without waiting for
        # the next message on this topic.
        client.publish(
            self._config.outputs.availability,
            payload="online",
            qos=1,
            retain=True,
        )

        publish_discovery(client, self._config)
        self._publisher.republish_last_result()

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        """Called by paho when an MQTT message arrives.

        The trigger topic and data topics are handled differently:

        - **Trigger topic** (``{prefix}/input/trigger``): attempt to queue a
          new solve if ``ReadinessState.is_ready()`` is True. The payload is
          ignored. If not ready, log the reason and take no action.
        - **Data topics**: route to the appropriate parser, call
          ``ReadinessState.update()``. Never queue a solve. Parse errors are
          logged and swallowed — the readiness state is simply not updated,
          leaving the affected topic stale.

        Args:
            client: The paho client instance.
            userdata: Unused.
            message: A paho ``MQTTMessage`` with ``.topic`` and ``.payload``.
        """
        topic = message.topic

        # HA birth message: HA publishes "online" (retained) to
        # homeassistant/status when it starts or reloads MQTT. All mimirheim
        # entities are unavailable in HA at that point. Re-publish discovery
        # after a random 1-5 s delay to avoid broker thundering-herd if many
        # devices respond simultaneously. This check is intentionally placed
        # before the retain guard because the birth message itself is retained.
        if topic == _HA_STATUS_TOPIC:
            if message.payload == b"online":
                logger.info(
                    "Received HA birth message; will re-publish discovery "
                    "payloads in %.1f s.",
                    delay := random.uniform(1.0, 5.0),
                )
                threading.Timer(
                    delay, publish_discovery, args=(client, self._config)
                ).start()
            return

        # --- Trigger topic: attempt to queue a solve ---
        if topic == self._trigger_topic:
            # Retained trigger messages are dropped. Trigger topics must use
            # retain=False (as the scheduler and input tools do), but a
            # misconfigured publisher could leave a retained message on the
            # broker. If that were replayed on every reconnect it would cause
            # a spurious solve before sensor data has been received. Data topics
            # are intentionally not guarded here: sensor values (battery SOC,
            # EV SOC, etc.) are published with retain=True so that mimirheim picks
            # them up immediately on (re)connect without waiting for a fresh
            # measurement.
            if message.retain:
                logger.debug("Ignoring retained message on trigger topic %r.", topic)
                return

            # Debounce: discard triggers that arrive within 5 seconds of the
            # previous one. Rapid bursts of triggers (e.g. when multiple input
            # tools publish simultaneously) would otherwise queue redundant
            # solves that consume CPU without producing meaningfully different
            # results. Only the first trigger in each burst is acted on.
            now_mono = time.monotonic()
            if (
                self._last_trigger_at is not None
                and now_mono - self._last_trigger_at < _DEBOUNCE_SECONDS
            ):
                logger.debug(
                    "Trigger on %r debounced (%.1f s since last trigger).",
                    topic,
                    now_mono - self._last_trigger_at,
                )
                return
            self._last_trigger_at = now_mono
            if self._solve_queue is not None:
                if self._readiness.is_ready():
                    try:
                        bundle = self._readiness.snapshot()
                        self._solve_queue.put_nowait(bundle)
                    except queue.Full:
                        logger.debug("Solve queue full; trigger on %r discarded.", topic)
                    except Exception as exc:  # noqa: BLE001 — snapshot errors must not crash
                        logger.error("Failed to assemble SolveBundle on trigger: %s", exc)
                else:
                    reason = self._readiness.not_ready_reason()
                    logger.warning(
                        "Trigger received but readiness not met; solve skipped. %s",
                        reason,
                    )
                    self._publisher.publish_last_solve_status(
                        None,
                        f"Trigger received but readiness not met. {reason}",
                    )
            return

        # --- Data topics: update readiness only, never queue a solve ---
        payload = message.payload
        handler = self._topic_handlers.get(topic)
        if handler is None:
            logger.debug("Received message on unrecognised topic %r; ignoring.", topic)
            return

        try:
            validated_input = handler(payload)
            self._readiness.update(topic, validated_input)
        except Exception as exc:  # noqa: BLE001 — log all parse errors, re-raise nothing
            logger.warning("Failed to parse message on topic %r: %s", topic, exc)


    # ------------------------------------------------------------------
    # Private: topic handler registry
    # ------------------------------------------------------------------

    def _build_topic_handlers(self) -> dict[str, Any]:
        """Build a mapping from topic → parser function.

        Returns:
            Dict mapping each expected topic string to a callable that takes
            a raw bytes payload and returns a validated input object.
        """
        handlers: dict[str, Any] = {}
        config = self._config
        prefix = config.mqtt.topic_prefix

        # Prices topic: parse into a PricesPayload (import, export, confidence).
        # For now, parse each prices-related flat list separately and combine.
        prices_topic = config.inputs.prices
        handlers[prices_topic] = parse_price_steps

        # Strategy topic: parse the strategy string.
        strategy_topic = f"{prefix}/input/strategy"
        handlers[strategy_topic] = parse_strategy

        # Battery SOC topics: parse to float (kWh).
        for bat_cfg in config.batteries.values():
            if bat_cfg.inputs is not None:
                topic = bat_cfg.inputs.soc.topic

                def _make_bat_parser(capacity_kwh: float, unit: str) -> Any:
                    def _parse_bat(raw: bytes) -> float:
                        soc = parse_battery_inputs(raw)
                        if unit == "percent":
                            return soc * capacity_kwh / 100.0
                        return soc

                    return _parse_bat

                handlers[topic] = _make_bat_parser(
                    bat_cfg.capacity_kwh, bat_cfg.inputs.soc.unit
                )

        # EV SOC topics + plug state.
        for ev_cfg in config.ev_chargers.values():
            if ev_cfg.inputs is not None:
                def _make_ev_soc_parser(capacity_kwh: float, unit: str) -> Any:
                    def _parse_ev_soc(raw: bytes) -> float:
                        soc = parse_ev_inputs(raw)
                        if unit == "percent":
                            return soc * capacity_kwh / 100.0
                        return soc

                    return _parse_ev_soc

                handlers[ev_cfg.inputs.soc.topic] = _make_ev_soc_parser(
                    ev_cfg.capacity_kwh, ev_cfg.inputs.soc.unit
                )
                # Plug state: parse to bool.
                plug_topic = ev_cfg.inputs.plugged_in_topic

                def _parse_plug(raw: bytes) -> bool:
                    text = raw.decode("utf-8").strip().lower()
                    if text in ("true", "on", "1", '"true"', "yes"):
                        return True
                    if text in ("false", "off", "0", '"false"', "no"):
                        return False
                    raise ValueError(f"Unrecognised plug state payload: {text!r}")

                handlers[plug_topic] = _parse_plug

        # PV forecast topics: parse to list[PowerForecastStep].
        for pv_cfg in config.pv_arrays.values():
            handlers[pv_cfg.topic_forecast] = parse_power_forecast

        # Static load forecast topics: parse to list[PowerForecastStep].
        for sl_cfg in config.static_loads.values():
            handlers[sl_cfg.topic_forecast] = parse_power_forecast

        # Deferrable window topics and optional start_time topics: parse to datetime.
        for dl_cfg in config.deferrable_loads.values():
            handlers[dl_cfg.topic_window_earliest] = parse_datetime
            handlers[dl_cfg.topic_window_latest] = parse_datetime
            if dl_cfg.topic_committed_start_time is not None:
                handlers[dl_cfg.topic_committed_start_time] = parse_datetime

        # Hybrid inverter SOC and PV forecast topics (plan 24).
        for hi_cfg in config.hybrid_inverters.values():
            if hi_cfg.inputs is not None:
                soc_topic = hi_cfg.inputs.soc.topic

                def _make_hi_soc_parser(capacity_kwh: float, unit: str) -> Any:
                    def _parse_hi_soc(raw: bytes) -> float:
                        soc = parse_hybrid_inverter_soc(raw)
                        if unit == "percent":
                            return soc * capacity_kwh / 100.0
                        return soc

                    return _parse_hi_soc

                handlers[soc_topic] = _make_hi_soc_parser(
                    hi_cfg.capacity_kwh, hi_cfg.inputs.soc.unit
                )
            handlers[hi_cfg.topic_pv_forecast] = parse_power_forecast

        # Thermal boiler temperature topics (plan 25).
        for tb_cfg in config.thermal_boilers.values():
            if tb_cfg.inputs is not None:
                handlers[tb_cfg.inputs.topic_current_temp] = parse_thermal_boiler_temp

        # Space heating heat pump demand topics (plan 26).
        for sh_cfg in config.space_heating_hps.values():
            if sh_cfg.inputs is not None:
                handlers[sh_cfg.inputs.topic_heat_needed_kwh] = parse_space_heating_demand
            # BTM indoor temp and outdoor forecast topics (plan 28).
            if sh_cfg.building_thermal is not None and sh_cfg.building_thermal.inputs is not None:
                btm_inputs = sh_cfg.building_thermal.inputs
                handlers[btm_inputs.topic_current_indoor_temp_c] = parse_current_indoor_temp
                handlers[btm_inputs.topic_outdoor_temp_forecast_c] = parse_outdoor_temp_forecast

        # Combi heat pump DHW temp + SH demand topics (plan 27).
        for chp_cfg in config.combi_heat_pumps.values():
            if chp_cfg.inputs is not None:
                handlers[chp_cfg.inputs.topic_current_temp] = parse_combi_hp_temp
                handlers[chp_cfg.inputs.topic_heat_needed_kwh] = parse_combi_hp_sh_demand
            # BTM indoor temp and outdoor forecast topics (plan 28).
            if chp_cfg.building_thermal is not None and chp_cfg.building_thermal.inputs is not None:
                btm_inputs = chp_cfg.building_thermal.inputs
                handlers[btm_inputs.topic_current_indoor_temp_c] = parse_current_indoor_temp
                handlers[btm_inputs.topic_outdoor_temp_forecast_c] = parse_outdoor_temp_forecast

        return handlers

