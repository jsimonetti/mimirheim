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
from datetime import UTC, datetime, timedelta
from typing import Any

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import DeviceSetpoint, ScheduleStep, SolveResult

logger = logging.getLogger("mimirheim.publisher")

_STEP_HOURS: float = 15 / 60.0


def _schedule_summary(schedule: list[ScheduleStep]) -> dict[str, float]:
    """Compute grid and self-sufficiency metrics from a solved schedule.

    Args:
        schedule: Ordered list of ScheduleStep objects from a SolveResult.

    Returns:
        Dict with grid_import_kwh, grid_export_kwh, and self_sufficiency_pct.
    """
    grid_import_kwh = sum(step.grid_import_kw * _STEP_HOURS for step in schedule)
    grid_export_kwh = sum(step.grid_export_kw * _STEP_HOURS for step in schedule)

    load_total_kwh = 0.0
    for step in schedule:
        for setpoint in step.devices.values():
            if setpoint.type in ("static_load", "deferrable_load"):
                load_total_kwh += max(0.0, -setpoint.kw) * _STEP_HOURS

    load_served_local = max(0.0, load_total_kwh - grid_import_kwh)
    self_sufficiency_pct = (
        round(load_served_local / load_total_kwh * 100.0, 1)
        if load_total_kwh > 0.0
        else 0.0
    )

    return {
        "grid_import_kwh": round(grid_import_kwh, 4),
        "grid_export_kwh": round(grid_export_kwh, 4),
        "self_sufficiency_pct": self_sufficiency_pct,
    }


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

    @staticmethod
    def _step_origin(result: SolveResult) -> datetime:
        """Return the wall-clock time that schedule step 0 refers to.

        ``build_and_solve`` copies the bundle's ``solve_time_utc`` onto every
        result, so in the running daemon this is always populated and the
        result carries its own time axis.

        The fallback covers results constructed directly, which happens in
        tests and in golden files written before the field existed. It floors
        the current time to the enclosing 15-minute slot, reproducing the
        previous behaviour for those callers only.

        Args:
            result: The result about to be published.

        Returns:
            A timezone-aware UTC datetime aligned to a 15-minute boundary.
        """
        if result.solve_time_utc is not None:
            return result.solve_time_utc
        now = datetime.now(UTC)
        return now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)

    def publish_result(self, result: SolveResult) -> None:
        """Publish a ``SolveResult`` to all output topics.

        Publishes, in order:

        1. The full schedule as JSON to ``config.outputs.schedule``.
        2. The current-step summary to ``config.outputs.current``.
        3. One retained setpoint topic per device in the current step.
        4. PV control topics for each PV array that declares them: production
           limit, zero-export mode, on/off mode, and the mode-agnostic
           curtailment flag.
        5. EV closed-loop topics: exchange mode and load-balance command.
        6. Battery exchange-mode topics.
        7. Hybrid inverter exchange-mode topics.
        8. Recommended start times for deferrable loads the solver scheduled.

        Items 4 to 8 are published only for devices whose configuration
        declares the matching capability and output topic, so a minimal
        installation sees only the first three.

        Everything is retained, so the broker holds the latest value for
        subscribers that connect later. When the schedule is empty only item 1
        is published; the solve loop does not call this method for an
        infeasible result in any case.

        Stores ``result`` for later re-publication via ``republish_last_result()``.

        Args:
            result: The output from the most recent ``build_and_solve`` call.
        """
        self._last_result = result

        # The origin of the step time axis. It comes from the result, not from
        # the clock, because publishing is not simultaneous with solving: a
        # solve can take up to the solver time limit, and
        # republish_last_result() re-runs this method whenever the broker
        # connection is restored, potentially hours later. Reading the clock
        # here would relabel an old schedule as starting now, and every
        # consumer of the schedule and current-step topics would act on it.
        step_start = self._step_origin(result)

        # 1. Full schedule blob with per-step ISO timestamps.
        # result.model_dump() carries integer step indices in each step's 't'
        # field. We inject a 'ts' key (ISO UTC string) on each step so that
        # downstream consumers (e.g. HA json_attributes_template for apexcharts)
        # have a time axis without needing to compute offsets themselves.
        schedule_dict = result.model_dump(mode="json")
        for step in schedule_dict["schedule"]:
            step["ts"] = (
                step_start + timedelta(minutes=15 * step["t"])
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._client.publish(
            self._config.outputs.schedule,
            json.dumps(schedule_dict),
            qos=1,
            retain=True,
        )

        # 2. Current-step summary.
        if result.schedule:
            current = result.schedule[0]
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

            # 3-7. Per-device topics.
            #
            # One pass over the devices, dispatching on type. The setpoint
            # topic is published for every device; the control topics that
            # follow it depend on which capabilities and output topics the
            # device declares. Publishing per device rather than per topic
            # family keeps the "which topics does this device get" question
            # answerable in one place.
            prefix = self._config.mqtt.topic_prefix
            for device_name, setpoint in current.devices.items():
                self._client.publish(
                    f"{prefix}/device/{device_name}/setpoint",
                    json.dumps({"kw": setpoint.kw, "type": setpoint.type}),
                    qos=1,
                    retain=True,
                )
                handler = self._CONTROL_PUBLISHERS.get(setpoint.type)
                if handler is not None:
                    handler(self, device_name, setpoint)

            # 8. Deferrable load recommended-start output topics.
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

    def _publish_bool(self, topic: str | None, value: bool | None) -> None:
        """Publish a boolean control flag to ``topic`` as "true" or "false".

        Does nothing when either argument is None. Both are optional for the
        same reason: a device only receives a control topic when its config
        declares one, and only carries a flag when the matching capability is
        enabled. Publishing on a half-configured device would either target no
        topic or carry no decision.

        Args:
            topic: The configured output topic, or None if not configured.
            value: The flag from the device setpoint, or None if the device has
                no such capability.
        """
        if topic is None or value is None:
            return
        self._client.publish(topic, "true" if value else "false", qos=1, retain=True)

    def _publish_pv_controls(self, name: str, sp: DeviceSetpoint) -> None:
        """Publish the control topics for one PV array.

        These are separate retained topics rather than fields inside the
        generic setpoint payload so that an inverter automation can subscribe
        to a single, purpose-specific topic without parsing JSON.

        Args:
            name: Device name, used to look the array up in config.
            sp: The array's setpoint for the current step.
        """
        cfg = self._config.pv_arrays.get(name)
        if cfg is None:
            return

        if cfg.has_power_limit_output and sp.power_limit_kw is not None:
            self._client.publish(
                cfg.outputs.power_limit_kw, str(sp.power_limit_kw), qos=1, retain=True
            )
        if cfg.has_zero_export_output:
            self._publish_bool(cfg.outputs.zero_export_mode, sp.zero_exchange_active)
        if cfg.has_on_off_output:
            # Payload semantics: "true" = inverter is ON (producing), "false" =
            # OFF. The internal solver variable is pv_curtailed with the
            # opposite polarity; on_off_active has already inverted it.
            self._publish_bool(cfg.outputs.on_off_mode, sp.on_off_active)
        # Mode-agnostic curtailment signal: true means mimirheim is holding PV
        # output below the available forecast. Published for staged,
        # power_limit and on_off arrays; a fixed-mode array is not controllable
        # so has_is_curtailed_output is False. Using the same property that
        # ha_discovery uses to advertise the entity keeps the two in step.
        if cfg.has_is_curtailed_output:
            self._publish_bool(cfg.outputs.is_curtailed, sp.pv_is_curtailed)

    def _publish_ev_controls(self, name: str, sp: DeviceSetpoint) -> None:
        """Publish the closed-loop control topics for one EV charger.

        Args:
            name: Device name, used to look the charger up in config.
            sp: The charger's setpoint for the current step.
        """
        cfg = self._config.ev_chargers.get(name)
        if cfg is None:
            return
        if cfg.has_exchange_mode_output:
            self._publish_bool(cfg.outputs.exchange_mode, sp.zero_exchange_active)
        if cfg.has_loadbalance_output:
            self._publish_bool(cfg.outputs.loadbalance_cmd, sp.loadbalance_active)

    def _publish_battery_controls(self, name: str, sp: DeviceSetpoint) -> None:
        """Publish the exchange-mode topic for one battery.

        Args:
            name: Device name, used to look the battery up in config.
            sp: The battery's setpoint for the current step.
        """
        cfg = self._config.batteries.get(name)
        if cfg is None or not cfg.has_exchange_mode_output:
            return
        self._publish_bool(cfg.outputs.exchange_mode, sp.zero_exchange_active)

    def _publish_hybrid_inverter_controls(self, name: str, sp: DeviceSetpoint) -> None:
        """Publish the exchange-mode topic for one hybrid inverter.

        Args:
            name: Device name, used to look the inverter up in config.
            sp: The inverter's setpoint for the current step.
        """
        cfg = self._config.hybrid_inverters.get(name)
        if cfg is None or not cfg.has_exchange_mode_output:
            return
        self._publish_bool(cfg.outputs.exchange_mode, sp.zero_exchange_active)

    # Device type to control-topic publisher. Types absent from this table
    # (static loads, deferrable loads, the three heat pump types) have no
    # control topics beyond the generic setpoint.
    _CONTROL_PUBLISHERS = {
        "pv": _publish_pv_controls,
        "ev_charger": _publish_ev_controls,
        "battery": _publish_battery_controls,
        "hybrid_inverter": _publish_hybrid_inverter_controls,
    }

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
            summary = _schedule_summary(result.schedule)
            payload = json.dumps({
                "status": "ok",
                "solve_status": result.solve_status,
                "dispatch_suppressed": result.dispatch_suppressed,
                "naive_cost_eur": round(result.naive_cost_eur, 4),
                "optimised_cost_eur": round(result.optimised_cost_eur, 4),
                "soc_credit_eur": round(result.soc_credit_eur, 4),
                "grid_import_kwh": summary["grid_import_kwh"],
                "grid_export_kwh": summary["grid_export_kwh"],
                "self_sufficiency_pct": summary["self_sufficiency_pct"],
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
        reconnects. Re-publishing restores the retained topics after a broker
        restart has dropped its retained state.

        The payloads are byte-identical to the original publication. In
        particular the step time axis still refers to the solve that produced
        the result, so a schedule re-published an hour later is not presented
        as though it started at the moment of reconnection. Consumers can
        compare ``solve_time_utc`` against their own clock to judge how stale
        the plan is.

        If no result has been stored yet (process just started, no solve has
        completed), this method is a no-op.
        """
        if self._last_result is None:
            logger.debug("republish_last_result: no previous result; skipping.")
            return
        self.publish_result(self._last_result)
