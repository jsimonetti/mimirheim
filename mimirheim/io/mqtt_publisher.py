"""MQTT publisher — publishes SolveResult to all output topics.

This module is responsible for translating a ``SolveResult`` into one or more
MQTT ``publish`` calls. All published topics use ``retain=True`` so that Home
Assistant and other subscribers receive the latest schedule immediately on
subscribe, without needing to wait for the next solve cycle.

The publisher never subscribes, connects, or starts network threads. Those
responsibilities belong to ``mqtt_client.py``. The publisher receives an
already-connected paho client at construction and calls ``publish()`` on it.

Per-device retained topics follow the pattern:
    ``{topic_prefix}/device/{device_name}/setpoint``

They carry the current-step setpoint for each device, retained so that
automations using the topic can read the latest value at any time.

This module imports from ``mimirheim.core.bundle`` and ``mimirheim.config.schema`` but
never from ``mimirheim.io.input_parser`` or ``mimirheim.core.readiness``.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveResult

logger = logging.getLogger("mimirheim.publisher")


class MqttPublisher:
    """Publishes a ``SolveResult`` to all configured MQTT output topics.

    Exactly one instance lives per mimirheim process. It is constructed with an
    already-connected (or connecting) paho client. All ``publish()`` calls use
    ``qos=1, retain=True`` to guarantee at-least-once delivery and broker-side
    persistence.

    The previous result is stored so that ``republish_last_result()`` can
    re-publish after a broker reconnect without needing the solve loop to
    re-run.

    Attributes:
        _client: The paho MQTT client used for all publish calls.
        _config: Static configuration providing output topic names and prefix.
        _last_result: The most recent ``SolveResult`` passed to
            ``publish_result()``. None until the first successful solve.
    """

    def __init__(self, client: Any, config: MimirheimConfig) -> None:
        """Construct the publisher.

        Args:
            client: A paho-mqtt ``Client`` instance (or any object implementing
                ``publish(topic, payload, qos, retain)``).
            config: Static system configuration.
        """
        self._client = client
        self._config = config
        self._last_result: SolveResult | None = None

    def publish_result(self, result: SolveResult) -> None:
        """Publish a ``SolveResult`` to all output topics.

        Publishes:
        1. The full schedule as JSON to ``config.outputs.schedule``.
        2. The current-step summary to ``config.outputs.current``.
        3. One retained setpoint topic per device in the current step.

        Stores ``result`` for later re-publication via ``republish_last_result()``.

        Args:
            result: The output from the most recent ``build_and_solve`` call.
        """
        self._last_result = result

        # 1. Full schedule blob.
        self._client.publish(
            self._config.outputs.schedule,
            result.model_dump_json(),
            qos=1,
            retain=True,
        )

        # 2. Current-step summary.
        if result.schedule:
            current = result.schedule[0]
            now = datetime.now(UTC)
            step_start = now.replace(
                minute=(now.minute // 15) * 15,
                second=0,
                microsecond=0,
            )
            # Build from model_dump so the devices dict is included automatically.
            # exclude_none=True drops optional fields (e.g. power_limit_kw) that
            # are not relevant for this device type, keeping the payload lean.
            step_dict = current.model_dump(exclude_none=True)
            # Override the integer step index with a human-readable UTC datetime.
            step_dict["t"] = step_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            step_dict["strategy"] = result.strategy
            step_dict["solve_status"] = result.solve_status
            # Inject the solver-recommended start time into the per-device entry
            # that already carries kw and type, so all device state is co-located.
            for name, dt in result.deferrable_recommended_starts.items():
                if name in step_dict.get("devices", {}):
                    step_dict["devices"][name]["recommended_start"] = dt.strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
            current_payload = json.dumps(step_dict)
            self._client.publish(
                self._config.outputs.current,
                current_payload,
                qos=1,
                retain=True,
            )

            # 3. Per-device retained setpoint topics.
            prefix = self._config.mqtt.topic_prefix
            for device_name, setpoint in current.devices.items():
                device_topic = f"{prefix}/device/{device_name}/setpoint"
                device_payload = json.dumps({
                    "kw": setpoint.kw,
                    "type": setpoint.type,
                })
                self._client.publish(
                    device_topic,
                    device_payload,
                    qos=1,
                    retain=True,
                )

            # 4. PV control output topics (production limit and zero-export mode).
            # These are separate retained topics rather than part of the generic
            # setpoint payload so that inverter automations can subscribe to a
            # single, purpose-specific topic without parsing the setpoint JSON.
            for device_name, setpoint in current.devices.items():
                if setpoint.type != "pv":
                    continue
                pv_cfg = self._config.pv_arrays.get(device_name)
                if pv_cfg is None:
                    continue

                if (
                    pv_cfg.capabilities.power_limit
                    and pv_cfg.outputs.power_limit_kw is not None
                    and setpoint.power_limit_kw is not None
                ):
                    self._client.publish(
                        pv_cfg.outputs.power_limit_kw,
                        str(setpoint.power_limit_kw),
                        qos=1,
                        retain=True,
                    )

                if (
                    pv_cfg.capabilities.zero_export
                    and pv_cfg.outputs.zero_export_mode is not None
                    and setpoint.zero_exchange_active is not None
                ):
                    self._client.publish(
                        pv_cfg.outputs.zero_export_mode,
                        "true" if setpoint.zero_exchange_active else "false",
                        qos=1,
                        retain=True,
                    )

                if (
                    pv_cfg.capabilities.on_off
                    and pv_cfg.outputs.on_off_mode is not None
                    and setpoint.on_off_active is not None
                ):
                    # Payload semantics: "true" = inverter is ON (producing);
                    # "false" = inverter is OFF (curtailed by mimirheim).
                    # Note: the internal solver variable is pv_curtailed (0=on,
                    # 1=off), the opposite polarity. on_off_active already
                    # inverts it: True means on, False means off.
                    self._client.publish(
                        pv_cfg.outputs.on_off_mode,
                        "true" if setpoint.on_off_active else "false",
                        qos=1,
                        retain=True,
                    )

            # 5. EV closed-loop output topics.
            # The exchange_mode topic carries the zero-exchange closed-loop
            # assertion. The loadbalance_cmd topic carries the load-balance mode
            # assertion. Both are published only when the matching capability
            # flag is True and the output topic is configured.
            for device_name, setpoint in current.devices.items():
                if setpoint.type != "ev_charger":
                    continue
                ev_cfg = self._config.ev_chargers.get(device_name)
                if ev_cfg is None:
                    continue
                if (
                    ev_cfg.capabilities.zero_exchange
                    and ev_cfg.outputs.exchange_mode is not None
                    and setpoint.zero_exchange_active is not None
                ):
                    self._client.publish(
                        ev_cfg.outputs.exchange_mode,
                        "true" if setpoint.zero_exchange_active else "false",
                        qos=1,
                        retain=True,
                    )
                if (
                    ev_cfg.capabilities.loadbalance
                    and ev_cfg.outputs.loadbalance_cmd is not None
                    and setpoint.loadbalance_active is not None
                ):
                    self._client.publish(
                        ev_cfg.outputs.loadbalance_cmd,
                        "true" if setpoint.loadbalance_active else "false",
                        qos=1,
                        retain=True,
                    )

            # 6. Battery exchange_mode output topic.
            # Published when capabilities.zero_exchange is True and
            # outputs.exchange_mode topic is configured.
            for device_name, setpoint in current.devices.items():
                if setpoint.type != "battery":
                    continue
                bat_cfg = self._config.batteries.get(device_name)
                if bat_cfg is None:
                    continue
                if (
                    bat_cfg.capabilities.zero_exchange
                    and bat_cfg.outputs.exchange_mode is not None
                    and setpoint.zero_exchange_active is not None
                ):
                    self._client.publish(
                        bat_cfg.outputs.exchange_mode,
                        "true" if setpoint.zero_exchange_active else "false",
                        qos=1,
                        retain=True,
                    )

            # 7. Deferrable load recommended-start output topics.
            self._publish_deferrable_recommended_starts(result)

    def _publish_deferrable_recommended_starts(self, result: SolveResult) -> None:
        """Publish solver-recommended start datetimes for deferrable loads.

        Only publishes when a deferrable load was in binary scheduling state
        (i.e. its name appears in ``result.deferrable_recommended_starts``) and
        its configuration includes a ``topic_recommended_start_time``.

        The payload is an ISO 8601 UTC datetime string with second precision,
        e.g. ``2025-06-01T06:30:00Z``. The message is published retained so
        that Home Assistant reads the most recent value on reconnect.

        Args:
            result: The completed ``SolveResult`` from the current solve cycle.
        """
        for device_name, rec_start in result.deferrable_recommended_starts.items():
            dl_cfg = self._config.deferrable_loads.get(device_name)
            if dl_cfg is None or dl_cfg.topic_recommended_start_time is None:
                continue
            self._client.publish(
                dl_cfg.topic_recommended_start_time,
                rec_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                qos=1,
                retain=True,
            )

    def publish_last_solve_status(
        self, result: SolveResult | None, error: str | None
    ) -> None:
        """Publish a retained status message to the last_solve topic.

        Published after every solve attempt — successful or not. Downstream
        monitoring systems use this topic to detect solve failures without
        reading the full schedule.

        Args:
            result: The most recent ``SolveResult``, or None if no solve was
                attempted (e.g. stale inputs, initialisation state).
            error: A human-readable error description. Used when the result is
                None or when the solve was infeasible. Must not contain raw
                exception tracebacks.
        """
        is_infeasible = result is not None and result.solve_status == "infeasible"

        if result is None or is_infeasible:
            detail = error if error else "Solve returned infeasible — check device configuration."
            payload = json.dumps({
                "status": "error",
                "detail": detail,
                "generated_at": datetime.now(UTC).isoformat(),
            })
        else:
            payload = json.dumps({
                "status": "ok",
                "solve_status": result.solve_status,
                "dispatch_suppressed": result.dispatch_suppressed,
                "naive_cost_eur": round(result.naive_cost_eur, 4),
                "optimised_cost_eur": round(result.optimised_cost_eur, 4),
                "soc_credit_eur": round(result.soc_credit_eur, 4),
                "generated_at": datetime.now(UTC).isoformat(),
            })

        self._client.publish(
            self._config.outputs.last_solve,
            payload,
            qos=1,
            retain=True,
        )

    def republish_last_result(self) -> None:
        """Re-publish the last stored result to all output topics.

        Called from ``mqtt_client``'s ``on_connect`` callback when the broker
        reconnects. Re-publishing ensures retained topics are current even if
        the broker restarted and lost its retained state.

        If no result has been stored yet (process just started, no solve has
        completed), this method is a no-op.
        """
        if self._last_result is None:
            logger.debug("republish_last_result: no previous result; skipping.")
            return
        self.publish_result(self._last_result)
