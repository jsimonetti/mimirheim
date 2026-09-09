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
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.battery_care import care_plan, is_newer, is_older
from mimirheim.core.bundle import (
    BatteryCareStatus,
    DeviceSetpoint,
    ScheduleStep,
    SolveResult,
)

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
        # Freshest observed full-charge timestamps, set by the solve loop just
        # before publishing. See set_battery_care_overrides.
        # The solve loop and the MQTT network thread both set and publish care
        # state, so the whole read-and-publish is serialised. The lock alone is
        # not enough: two threads can still publish in the wrong order if one
        # is descheduled, so _care_published_full and _care_published_since
        # record what is already on the topic and refuse to regress it. That
        # topic is the only durable store of both timestamps, and a regression
        # there survives a restart.
        self._care_lock = threading.Lock()
        self._care_published_full: dict[str, datetime] = {}
        self._care_published_since: dict[str, datetime] = {}
        self._care_overrides: dict[str, datetime] = {}
        self._care_baselines: dict[str, datetime] = {}
        self._care_history: dict[str, datetime] = {}

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
        if not schedule_dict.get("battery_care"):
            # An installation with no full-charge policy configured must see
            # the payload it saw before the policy existed. An always-present
            # empty map is a schema change for every consumer of the schedule
            # topic, bought for nothing.
            schedule_dict.pop("battery_care", None)
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
            step_dict["strategy_degraded"] = result.strategy_degraded
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

        # 9. Full-charge policy status, outside the schedule guard: the floor
        # and the last-full timestamp are true whether or not this solve
        # produced a schedule, and mimirheim reads this topic back on startup.
        self.publish_battery_care(result)

    def set_battery_care_overrides(
        self,
        observations: dict[str, datetime],
        baselines: dict[str, datetime] | None = None,
        history: dict[str, datetime] | None = None,
    ) -> None:
        """Supply state that changed after the solve's snapshot was taken.

        The solve loop reads both from ``ReadinessState`` immediately before
        publishing, so anything that arrived on the MQTT thread while the
        solver was running still reaches the retained topic. Without it a slow
        solve can move the broker's record backwards.

        The three are handled differently on purpose. An observation is an
        event: the battery finished a balance charge, so the derived fields are
        reset with it. A baseline only ever corrects the start of the interval
        backwards. History moves the last-full timestamp forward without
        claiming anything about the present, which is what a retained value
        restored from the broker is.

        Args:
            observations: Full charges seen live since startup, per battery.
            baselines: Earliest known policy baseline per battery.
            history: Best known last-full timestamp per battery, including
                values restored from the broker.
        """
        with self._care_lock:
            self._care_overrides = dict(observations)
            self._care_baselines = dict(baselines or {})
            self._care_history = dict(history or {})

    def _care_status_snapshot(self) -> dict[str, BatteryCareStatus]:
        """Rebuild policy state from observations when no solve result exists.

        Used on the paths that have no solve result to describe: a solve that
        raised before producing one, and a trigger rejected for readiness.
        Every field here is derivable
        without a model: the floor is a pure function of the reference
        timestamp, the interval and the step, which is the property that lets
        the policy survive a restart in the first place. A horizon of zero
        keeps ``care_plan`` from proposing a deadline, since there is no
        horizon this cycle to place one in.

        Returns:
            One entry per battery with the policy enabled and a status topic
            configured, keyed by battery name.
        """
        now = datetime.now(UTC)
        out: dict[str, BatteryCareStatus] = {}
        for name, cfg in self._config.batteries.items():
            if not cfg.soc_ratchet.enabled or cfg.outputs.soc_ratchet is None:
                continue
            # The freshest of the two, not the first that happens to be set.
            # A live observation and a retained correction can both be present
            # and either can be newer, and publish_battery_care applies the
            # same max further down. Taking the override unconditionally here
            # would compute the floor and the age from the older timestamp and
            # then publish the newer one beside them.
            observed = self._care_overrides.get(name)
            restored = self._care_history.get(name)
            last_full = observed if is_newer(observed, restored) else restored
            care_since = self._care_baselines.get(name)
            if last_full is None and care_since is None:
                continue
            plan = care_plan(
                config=cfg.soc_ratchet,
                capacity_kwh=cfg.capacity_kwh,
                last_full_utc=last_full,
                care_since_utc=care_since,
                solve_time_utc=now,
                horizon=0,
                dt=0.25,
            )
            out[name] = BatteryCareStatus(
                last_full_utc=last_full,
                care_since_utc=care_since,
                floor_kwh=plan.floor_kwh,
                hours_since_full=plan.hours_since_full,
            )
        return out

    def publish_battery_care(self, result: SolveResult | None) -> None:
        """Publish each battery's full-charge policy status, retained.

        The payload serves two purposes at once. It is the observability the
        policy needs — a minimum-SOC floor that moves without saying so is
        exactly the failure mode being replaced — and it is the only store of
        the "last measured full charge" timestamp, which mimirheim reads back
        on startup. Retained at QoS 1 for that reason: a restart with no
        retained value would reset the policy and the cells would never
        balance.

        Batteries without the policy enabled contribute no entry to
        ``result.battery_care`` and are silently skipped.

        Public because it is called directly on the paths that never reach
        ``publish_result``: an infeasible result, a solve that raised, and a
        trigger rejected because readiness was not met. The policy state is true whether or not a schedule came out,
        and losing it for a cycle would mean losing a full charge observation
        that has nowhere else to live — the broker would keep an older
        timestamp and a restart would re-arm a policy the battery has already
        satisfied.

        Args:
            result: The output from the most recent ``build_and_solve`` call,
                or None when the solve raised before producing one. In that
                case the payload is rebuilt from the observations alone: the
                floor is a pure function of the timestamps, and the
                forward-looking fields are left empty because no horizon was
                ever built to place them in.
        """
        # Held across the whole read-and-publish, because the solve loop and
        # the MQTT network thread both land here. Without it one thread can
        # install its maps between the other's read and its publish, and the
        # message that lands last wins regardless of which is newer.
        #
        # Holding a non-reentrant lock across client.publish() is safe here,
        # and worth stating because it does not look it. paho's publish()
        # enqueues and returns; nothing in mimirheim calls wait_for_publish or
        # bounds the queue with max_queued_messages_set, so it never waits on
        # the network thread. That matters because the not-ready trigger path
        # calls this *from* that thread, and a publish that blocked on it would
        # deadlock against itself. Nothing re-enters either: the only other
        # acquisition is set_battery_care_overrides, and publish_result calls
        # publish_battery_care without holding the lock.
        with self._care_lock:
            statuses = (
                result.battery_care
                if result is not None
                else self._care_status_snapshot()
            )
            self._publish_care_statuses(statuses)

    def _publish_care_statuses(
        self, statuses: dict[str, BatteryCareStatus]
    ) -> None:
        """Apply the override precedence and publish. Caller holds ``_care_lock``."""
        for name, status in statuses.items():
            cfg = self._config.batteries.get(name)
            if cfg is None or cfg.outputs.soc_ratchet is None:
                continue
            fresher = self._care_overrides.get(name)
            if is_newer(fresher, status.last_full_utc):
                # A full charge observed while the solver was running is not in
                # the snapshot this result was built from. Publishing the stale
                # value would put an older timestamp on the retained topic, and
                # a restart before the next successful solve would read it back
                # and re-arm a policy that had already been satisfied.
                #
                # The forward-looking fields go with it. They describe a
                # policy that was overdue at snapshot time and has since been
                # satisfied; publishing a fresh reset timestamp beside a
                # standing floor and a pending deadline would show consumers a
                # state that never existed.
                #
                # enforced_target_kwh and enforced_step deliberately stay. They
                # are not a pending demand but a record of what the solve was
                # actually held to, which does not stop being true because a
                # reading arrived afterwards. Clearing them would also put this
                # payload at odds with the same fields in the schedule topic,
                # which carries the solve unedited.
                status = status.model_copy(
                    update={
                        "last_full_utc": fresher,
                        "floor_kwh": 0.0,
                        "hours_since_full": 0.0,
                        "full_target_kwh": None,
                        "deadline_step": None,
                    }
                )
            known = self._care_history.get(name)
            if is_newer(known, status.last_full_utc):
                # A retained correction that arrived after the snapshot. It is
                # history, not an event, so only the timestamp moves: the floor
                # and the deadline still describe the battery as the solve
                # found it.
                status = status.model_copy(update={"last_full_utc": known})

            earliest = self._care_baselines.get(name)
            if is_older(earliest, status.care_since_utc):
                # A baseline correction only moves the start of the interval
                # backwards. It says nothing about the battery's current state,
                # so nothing derived is touched.
                status = status.model_copy(update={"care_since_utc": earliest})

            # Never let either timestamp regress. Two threads publish here, and
            # the lock only makes one transaction atomic — a caller that read
            # the maps first can still publish second, putting older state on a
            # retained topic that is the policy's only durable store.
            #
            # The two move in opposite directions, so each needs its own guard.
            # last_full_utc only ever advances; letting it fall back re-arms a
            # policy the battery has already satisfied. care_since_utc only
            # ever retreats, because it marks the start of the interval the
            # battery has been waiting through; letting it advance discards
            # elapsed waiting, and for a battery never yet seen full that is
            # the only clock it has — pushing it forward on every stale publish
            # postpones its first balance charge indefinitely.
            # If either timestamp would regress, this whole payload is a view
            # of the policy older than the one already retained, so it is
            # dropped rather than corrected. Patching just the timestamps and
            # publishing the rest was the first attempt, and it produced a
            # worse artefact than the regression it prevented: a fresh
            # last_full_utc paired with the floor, age, target and deadline
            # from the stale snapshot, which is a state that never existed.
            # Skipping loses nothing, because the broker keeps the better
            # message and the next solve republishes.
            if is_newer(self._care_published_full.get(name), status.last_full_utc):
                continue
            if is_older(self._care_published_since.get(name), status.care_since_utc):
                continue
            if status.last_full_utc is not None:
                self._care_published_full[name] = status.last_full_utc
            if status.care_since_utc is not None:
                self._care_published_since[name] = status.care_since_utc

            self._client.publish(
                cfg.outputs.soc_ratchet,
                json.dumps(status.model_dump(mode="json")),
                qos=1,
                retain=True,
            )

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
        # An error means the cycle failed, whatever else came back. A result
        # can be present and still be wrong to report as "ok": the exception
        # may have struck in post-processing or partway through publishing, so
        # the schedule on the broker is not the one this result describes.
        if error is not None:
            result = None
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
                # Without this, a cost-optimal schedule produced by the
                # minimize_consumption fallback is indistinguishable here from
                # a genuine volume-minimising one: the strategy name is the
                # same on both.
                "strategy_degraded": result.strategy_degraded,
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
