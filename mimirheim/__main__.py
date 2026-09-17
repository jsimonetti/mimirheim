"""Entry point for Mimirheim — Home Integrated Energy Optimiser.

This module is the application's main entry point. It is responsible for:

1. Parsing the ``--config`` command-line argument.
2. Validating Broker Settings (the ``mqtt:`` section) on their own, first;
   only a Broker Settings failure is fatal (ADR-0009).
3. Attempting full validation of the rest of the configuration file. On
   failure (or a missing file), running ``_run_awaiting_configuration``
   instead of exiting: connected to MQTT, serving only the Config Service
   protocol, running none of mimirheim's own function (ADR-0009, ADR-0011).
4. On full success, constructing all application components (ReadinessState,
   MqttClient, MqttPublisher) and wiring them together.
5. Starting the MQTT network loop.
6. Running the solve loop on the main thread until SIGTERM, SIGINT, or a
   Restart Request (ADR-0012).
7. Exiting cleanly.

What this module does not do:
- Solving: delegated to ``model_builder.build_and_solve``.
- Parsing MQTT payloads: delegated to ``io.input_parser``.
- Publishing results: delegated to ``io.mqtt_publisher``.
- Tracking readiness: delegated to ``core.readiness``.
- Building the Config Service Descriptor: delegated to ``io.config_service``,
  published/cleared by ``MqttClient`` (Operational) or
  ``io.awaiting_configuration.AwaitingConfigurationClient`` (Awaiting
  Configuration) on their one connection (see
  ``mimirheim_shared/docs/adr/0005``).
"""

import argparse
import json
import logging
import queue
import signal
import sys
import threading
from pathlib import Path

import paho.mqtt.client as paho
import yaml
from pydantic import ValidationError

from mimirheim.config.schema import MimirheimConfig, MqttConfig
from mimirheim.core.bundle import SolveBundle, SolveResult
from mimirheim.core.model_builder import debug_dump, build_and_solve
from mimirheim.core.post_process import apply_gain_threshold
from mimirheim.core.control_arbitration import assign_control_authority
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.awaiting_configuration import AwaitingConfigurationClient
from mimirheim.io.config_service import apply_mqtt_env_overrides
from mimirheim.io.mqtt_client import MqttClient
from mimirheim.io.mqtt_publisher import MqttPublisher

logger = logging.getLogger("mimirheim")


def _clip_bundle(bundle: SolveBundle, max_steps: int) -> SolveBundle:
    """Trim all per-step arrays in bundle to at most max_steps entries.

    If the bundle's horizon is already within the limit, the original object
    is returned unchanged. Otherwise every list field that tracks the solve
    horizon is sliced to max_steps and a new validated SolveBundle is returned.

    Clipping is applied in the solve loop when
    ``config.solver.max_horizon_steps`` is set. It keeps model construction
    time predictable regardless of how many hours of forecast data happen to
    be available at solve time.

    Args:
        bundle: The input bundle assembled from live MQTT data.
        max_steps: Maximum number of 15-minute steps to pass to the solver.

    Returns:
        The original bundle if ``len(bundle.horizon_prices) <= max_steps``,
        otherwise a new SolveBundle with all per-step arrays truncated.
    """
    if len(bundle.horizon_prices) <= max_steps:
        return bundle

    d = bundle.model_dump()
    for key in ("horizon_prices", "horizon_export_prices", "horizon_confidence",
                "pv_forecast", "base_load_forecast"):
        d[key] = d[key][:max_steps]

    for name in d["pv_forecasts"]:
        d["pv_forecasts"][name] = d["pv_forecasts"][name][:max_steps]

    for inv in d["hybrid_inverter_inputs"].values():
        inv["pv_forecast_kw"] = inv["pv_forecast_kw"][:max_steps]

    for sh in d["space_heating_inputs"].values():
        if sh.get("outdoor_temp_forecast_c") is not None:
            sh["outdoor_temp_forecast_c"] = sh["outdoor_temp_forecast_c"][:max_steps]

    for chp in d["combi_hp_inputs"].values():
        if chp.get("outdoor_temp_forecast_c") is not None:
            chp["outdoor_temp_forecast_c"] = chp["outdoor_temp_forecast_c"][:max_steps]

    return SolveBundle.model_validate(d)


def _format_solve_error(exc: BaseException) -> str:
    """Summarise an exception for the retained last-solve status topic.

    ``outputs.last_solve`` is published with ``retain=True``, so whatever goes
    into it stays on the broker until the next solve overwrites it and is
    delivered to every subscriber that connects in the meantime. A formatted
    traceback would expose filesystem paths and internal call structure there,
    which is why ``MqttPublisher.publish_last_solve_status`` documents its
    ``error`` argument as excluding them. The full traceback is written to the
    process log instead, where operators can reach it.

    Newlines are collapsed because the summary is embedded in a JSON payload
    that operators read in an MQTT client. Pydantic validation errors in
    particular span many lines.

    Args:
        exc: The exception raised by the solve cycle.

    Returns:
        A single-line ``"ExceptionType: message"`` string, or just the type
        name when the exception carries no message.
    """
    message = " ".join(str(exc).split())
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"


def _publish_reporting_notification(
    bundle: SolveBundle,
    result: SolveResult,
    config: MimirheimConfig,
    paho_client: paho.Client,
) -> None:
    """Write a reporting dump and publish a dump-available notification.

    Called after a successful solve when ``config.reporting.enabled`` is
    True. Writes dump files via ``debug_dump`` and then publishes a small
    JSON pointer to ``config.reporting.notify_topic``.

    The notification payload is at most ~200 bytes and is published with
    QoS 0 and ``retain=False`` so that the mimirheim-reporter subscriber does
    not re-process the last dump on reconnect. The reporter handles missed
    notifications via a filesystem catch-up scan on startup.

    If ``debug_dump`` returns ``None`` (because ``dump_dir`` is unset), the
    function returns without publishing. This is a defensive check; in
    practice ``reporting.dump_dir`` is required when ``reporting.enabled``
    is True and is enforced by ``ReportingConfig``'s validator.

    Args:
        bundle: The solve inputs passed to this solve cycle.
        result: The solve result produced by this solve cycle.
        config: The validated static configuration.
        paho_client: The paho MQTT client used to publish the notification.
    """
    if not config.reporting.enabled:
        return

    paths = debug_dump(
        bundle, result, config, config.reporting.dump_dir, config.reporting.max_dumps
    )
    if paths is None:
        return

    input_path, output_path = paths
    # Convert the filename-safe timestamp (hyphens in time part) back to
    # ISO 8601 format (colons in time part only; date separators are hyphens).
    # e.g. "2026-04-03T16-00-00Z" -> "2026-04-03T16:00:00Z"
    ts_file = input_path.name.replace("_input.json", "")
    if "T" in ts_file:
        date_part, time_part = ts_file.split("T", 1)
        ts_iso = date_part + "T" + time_part.replace("-", ":", 2)
    else:
        ts_iso = ts_file

    payload = json.dumps(
        {
            "ts": ts_iso,
            "input_path": str(input_path),
            "output_path": str(output_path),
        }
    )
    paho_client.publish(
        config.reporting.notify_topic,
        payload,
        qos=0,
        retain=False,
    )


def _read_raw_config(path: str) -> tuple[dict, str | None]:
    """Read and parse the YAML configuration file.

    A missing file, an unreadable one, or one that is not valid YAML no
    longer exits the process by itself (ADR-0009, ADR-0011): Broker Settings
    may still come entirely from the environment, in which case mimirheim
    core connects and enters Awaiting Configuration instead of exiting. This
    function reports what went wrong rather than deciding what to do about
    it; ``_load_broker_settings`` (fatal) and ``_try_load_full_config``
    (not fatal) do that.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        A tuple of the parsed dict (``{}`` if the file could not be read or
        parsed) and either ``None`` (read and parsed successfully) or a
        human-readable description of what went wrong.
    """
    try:
        text = Path(path).read_text()
    except OSError as exc:
        return {}, f"Cannot read config file {path!r}: {exc}"
    try:
        return yaml.safe_load(text) or {}, None
    except yaml.YAMLError as exc:
        return {}, f"Cannot parse config file {path!r}: {exc}"


def _load_broker_settings(raw: dict, path: str) -> MqttConfig:
    """Validate Broker Settings (the mqtt: section) on its own, before anything else.

    Broker Settings failure is always fatal (ADR-0009): without a broker
    connection mimirheim core cannot become reachable over the Config
    Service protocol, so there is no recovery path to wait in place for.

    Args:
        raw: The raw dict parsed from the YAML config file, with
            ``apply_mqtt_env_overrides`` already applied.
        path: Path to the YAML configuration file, for the error message.

    Returns:
        The validated ``MqttConfig`` instance.

    Raises:
        SystemExit: With exit code 1 if the ``mqtt:`` section fails
            validation.
    """
    try:
        return MqttConfig.model_validate(raw.get("mqtt") or {})
    except ValidationError as exc:
        print(
            f"ERROR: Invalid Broker Settings (mqtt: section) in {path!r}:\n{exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def _try_load_full_config(
    raw: dict, load_error: str | None
) -> tuple[MimirheimConfig | None, str | None]:
    """Attempt full validation of the configuration file.

    Unlike Broker Settings, a failure here is never fatal (ADR-0009,
    ADR-0011): the caller uses the returned detail to enter Awaiting
    Configuration instead of exiting.

    Args:
        raw: The raw dict parsed from the YAML config file, with
            ``apply_mqtt_env_overrides`` already applied.
        load_error: The error from ``_read_raw_config``, if reading or
            parsing the file itself failed; validation is not attempted
            when this is set.

    Returns:
        ``(config, None)`` if the file validates in full. ``(None, detail)``
        otherwise, where ``detail`` is ``load_error`` when set, or a
        human-readable summary of the Pydantic ``ValidationError``.
    """
    if load_error is not None:
        return None, load_error
    try:
        return MimirheimConfig.model_validate(raw), None
    except ValidationError as exc:
        return None, str(exc)


def _build_paho_client(mqtt: MqttConfig) -> paho.Client:
    """Construct a paho client from Broker Settings, applying TLS and credentials.

    Shared by the Operational and Awaiting Configuration startup paths: both
    connect using the same validated Broker Settings (ADR-0009), before
    either knows whether the rest of the configuration validates.

    Args:
        mqtt: The validated Broker Settings to connect with.

    Returns:
        A paho ``Client``, not yet connected.
    """
    client = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=mqtt.client_id or "mimir",
    )
    if mqtt.tls:
        import ssl
        cert_reqs = ssl.CERT_NONE if mqtt.tls_allow_insecure else ssl.CERT_REQUIRED
        client.tls_set(cert_reqs=cert_reqs)
        if mqtt.tls_allow_insecure:
            client.tls_insecure_set(True)
    if mqtt.username is not None:
        client.username_pw_set(mqtt.username, mqtt.password)
    return client


def _run_awaiting_configuration(mqtt: MqttConfig, config_path: Path, detail: str) -> None:
    """Serve the Config Service protocol without running mimirheim's own function.

    See ``mimirheim_shared/CONTEXT.md``'s Awaiting Configuration entry and
    ADR-0009/ADR-0011: no solve loop, no data topics, no schedule — only
    Descriptor discovery, Operational State, ``get_current_values``,
    ``validate_and_write``, and ``restart_request`` over a connection built
    from Broker Settings alone, until a Restart Request or a termination
    signal exits the process.

    Args:
        mqtt: The validated Broker Settings to connect with.
        config_path: Path to mimirheim core's own YAML configuration file,
            the target of a successful validate_and_write request.
        detail: A human-readable summary of why the configuration does not
            currently validate, published on the Operational State topic.
    """
    paho_client = _build_paho_client(mqtt)

    # A single Event, set from either the signal handler or a Restart
    # Request acknowledged on the paho network thread, replaces the solve
    # loop's polling `running` flag: there is no queue to poll here, so a
    # blocking wait is simpler and avoids a busy loop.
    shutdown_event = threading.Event()

    def _request_shutdown(signum: int, frame: object) -> None:
        logger.info("Received signal %d; shutting down.", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    client = AwaitingConfigurationClient(
        paho_client=paho_client,
        mqtt=mqtt,
        config_path=config_path,
        detail=detail,
        on_restart_requested=shutdown_event.set,
    )
    client.start()
    logger.warning("mimirheim is awaiting configuration: %s", detail)

    shutdown_event.wait()
    client.stop()
    logger.info("mimirheim stopped (awaiting configuration).")


def main() -> None:
    """Run the mimirheim optimiser until SIGTERM, SIGINT, or a Restart Request.

    Parses the ``--config`` argument, validates Broker Settings first
    (always fatal on failure, ADR-0009), then attempts full validation of
    the rest of the configuration file. If that fails or the file is
    missing, hands off to ``_run_awaiting_configuration`` and returns
    instead of exiting (ADR-0009, ADR-0011). Otherwise constructs all
    application components, starts the MQTT network loop, and enters the
    solve loop. The loop blocks on a queue that is populated by the MQTT
    ``on_message`` callback whenever all required inputs are present and fresh.

    Each iteration:
    1. Waits for a ``SolveBundle`` on the queue (1 s timeout, then retry).
    2. Calls ``build_and_solve`` to produce a ``SolveResult``.
    3. If the result is feasible, publishes the full schedule.
    4. Publishes the last-solve status (success or error) unconditionally.
    5. If debug dump is enabled and the logger is at DEBUG, writes dump files.
    """
    parser = argparse.ArgumentParser(
        description="Home Integrated Energy Optimiser — solve and publish energy schedules.",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to the YAML configuration file.",
    )
    args = parser.parse_args()

    raw, load_error = _read_raw_config(args.config)

    # When running as a HA add-on, the Supervisor writes MQTT broker
    # credentials to the s6 container environment via cont-init.d/01-mqtt-env.sh.
    # These override any mqtt: values in the YAML file so users do not need to
    # copy broker credentials into mimirheim.yaml.
    apply_mqtt_env_overrides(raw)

    broker_settings = _load_broker_settings(raw, args.config)
    config, config_error = _try_load_full_config(raw, load_error)

    if config is None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        _run_awaiting_configuration(broker_settings, Path(args.config), config_error)
        return

    logging.basicConfig(
        level=logging.DEBUG if config.debug.enabled else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # solve_queue carries SolveBundle objects from the MQTT on_message callback
    # to the solve loop below. maxsize=1 means that if a solve is still in
    # progress when a new bundle arrives, the new bundle is discarded. This
    # prevents bundles from queuing up during a slow solve: by the time the
    # solver is free, a freshly-triggered bundle is more useful than a stale one
    # from several minutes ago.
    solve_queue: queue.Queue = queue.Queue(maxsize=1)

    readiness = ReadinessState(config)
    paho_client = _build_paho_client(config.mqtt)
    publisher = MqttPublisher(client=paho_client, config=config)

    # Register SIGTERM and SIGINT handlers, and the Restart Request callback
    # given to MqttClient below. All three set `running` to False, which
    # causes the solve loop to exit cleanly after the current solve completes
    # (a Restart Request never reconstructs the loop in place, ADR-0012).
    running = True

    def _request_shutdown(signum: int, frame: object) -> None:
        nonlocal running
        logger.info("Received signal %d; shutting down.", signum)
        running = False

    def _request_restart() -> None:
        nonlocal running
        logger.info("Restart requested; shutting down for supervisor restart.")
        running = False

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    mqtt_client = MqttClient(
        config=config,
        readiness=readiness,
        publisher=publisher,
        paho_client=paho_client,
        config_path=Path(args.config),
        solve_queue=solve_queue,
        on_restart_requested=_request_restart,
    )

    mqtt_client.start()
    logger.info(
        "mimirheim started. Connecting to broker %s:%d.",
        config.mqtt.host,
        config.mqtt.port,
    )

    while running:
        try:
            bundle = solve_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        result = None
        error_msg: str | None = None

        try:
            bundle = _clip_bundle(bundle, config.solver.max_horizon_steps)
            result = build_and_solve(bundle, config)
            # Both functions use model_copy() internally and carry all
            # SolveResult fields through. Do not replace them with functions
            # that construct SolveResult(...) explicitly — doing so silently
            # drops any field not listed in the constructor call.
            result = apply_gain_threshold(result, bundle, config)
            result = assign_control_authority(result, bundle, config)
            # Hand over any full charge observed since the snapshot was taken,
            # so a slow solve cannot publish a stale timestamp over a fresher
            # one on the retained policy topic.
            publisher.set_battery_care_overrides(
                readiness.battery_care_observations(),
                readiness.battery_care_baselines(),
                readiness.battery_care_history(),
            )
            if result.solve_status != "infeasible":
                publisher.publish_result(result)
            else:
                # No schedule to publish, but the policy state still has to
                # reach the broker: it is the only store of the last measured
                # full charge, and an infeasible cycle must not lose it.
                publisher.publish_battery_care(result)
        except Exception as exc:
            # The traceback goes to the log, where it is useful and private.
            # Only a one-line summary reaches the retained status topic; see
            # _format_solve_error.
            logger.exception("Solve failed.")
            error_msg = _format_solve_error(exc)
            try:
                # A full charge observed since the last cycle lives only in
                # memory until it is published, and the retained topic is the
                # only store of it. Losing it here would leave the broker
                # holding an older timestamp, so a restart would re-arm a
                # policy the battery has already satisfied — and keep doing so
                # for as long as the solve keeps failing.
                publisher.set_battery_care_overrides(
                    readiness.battery_care_observations(),
                    readiness.battery_care_baselines(),
                    readiness.battery_care_history(),
                )
                # If build_and_solve got as far as a result, publish that
                # rather than a no-horizon snapshot: the exception may have
                # come from post-processing or from partway through
                # publish_result, and replacing the real policy state with a
                # bare one would drop the target and the deadline the solve
                # actually computed.
                publisher.publish_battery_care(result)
            except Exception:
                # Never let the policy topic turn a failed solve into a
                # crashed daemon; the status topic below still reports the
                # original error.
                logger.exception("Could not publish battery care state.")

        publisher.publish_last_solve_status(result, error_msg)

        if result is not None and config.debug.enabled:
            debug_dump(bundle, result, config, config.debug.dump_dir, config.debug.max_dumps)

        if result is not None and config.reporting.enabled:
            _publish_reporting_notification(bundle, result, config, paho_client)

    mqtt_client.stop()
    logger.info("mimirheim stopped.")


if __name__ == "__main__":
    main()
