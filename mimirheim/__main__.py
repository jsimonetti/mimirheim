"""Entry point for Mimirheim — Home Integrated Energy Optimiser.

This module is the application's main entry point. It is responsible for:

1. Parsing the ``--config`` command-line argument.
2. Loading and validating the YAML configuration file.
3. Constructing all application components (ReadinessState, MqttClient,
   MqttPublisher) and wiring them together.
4. Starting the MQTT network loop.
5. Running the solve loop on the main thread until SIGTERM or SIGINT.
6. Exiting cleanly.

What this module does not do:
- Solving: delegated to ``model_builder.build_and_solve``.
- Parsing MQTT payloads: delegated to ``io.input_parser``.
- Publishing results: delegated to ``io.mqtt_publisher``.
- Tracking readiness: delegated to ``core.readiness``.
"""

import argparse
import json
import logging
import os
import queue
import signal
import sys
import traceback
from pathlib import Path

import paho.mqtt.client as paho
import yaml
from pydantic import ValidationError

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle, SolveResult
from mimirheim.core.model_builder import debug_dump, build_and_solve
from mimirheim.core.post_process import apply_gain_threshold
from mimirheim.core.control_arbitration import assign_control_authority
from mimirheim.core.readiness import ReadinessState
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

    for inv in d["hybrid_inverter_inputs"].values():
        inv["pv_forecast_kw"] = inv["pv_forecast_kw"][:max_steps]

    for sh in d["space_heating_inputs"].values():
        if sh.get("outdoor_temp_forecast_c") is not None:
            sh["outdoor_temp_forecast_c"] = sh["outdoor_temp_forecast_c"][:max_steps]

    for chp in d["combi_hp_inputs"].values():
        if chp.get("outdoor_temp_forecast_c") is not None:
            chp["outdoor_temp_forecast_c"] = chp["outdoor_temp_forecast_c"][:max_steps]

    return SolveBundle.model_validate(d)


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


def _apply_mqtt_env_overrides(raw: dict) -> None:
    """Override the mqtt: section of the raw config dict from environment variables.

    When running as a HA add-on, the Supervisor injects MQTT broker credentials
    as environment variables (written by container/etc/cont-init.d/01-mqtt-env.sh
    before any s6 service starts). These take precedence over whatever appears in
    the YAML config file so users do not need to copy broker credentials into
    mimirheim.yaml.

    When the environment variables are absent (plain Docker, no Supervisor) this
    function is a no-op and the YAML values are used as-is.

    Args:
        raw: The raw dict parsed from the YAML config file. Modified in-place.
    """
    overrides: dict = {}
    if host := os.environ.get("MQTT_HOST"):
        overrides["host"] = host
    if port_str := os.environ.get("MQTT_PORT"):
        overrides["port"] = int(port_str)
    if username := os.environ.get("MQTT_USERNAME"):
        overrides["username"] = username
    if password := os.environ.get("MQTT_PASSWORD"):
        overrides["password"] = password
    if ssl_str := os.environ.get("MQTT_SSL"):
        overrides["tls"] = ssl_str.lower() == "true"
    if overrides:
        raw.setdefault("mqtt", {})
        raw["mqtt"].update(overrides)


def _load_config(path: str) -> MimirheimConfig:
    """Load and validate the YAML configuration file.

    Reads the YAML file at ``path``, parses it, and validates it against
    ``MimirheimConfig``. On failure, prints a human-readable error message and
    raises ``SystemExit(1)``.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        The validated ``MimirheimConfig`` instance.

    Raises:
        SystemExit: With exit code 1 if the file cannot be read or the
            configuration fails Pydantic validation.
    """
    try:
        with Path(path).open() as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        print(f"ERROR: Cannot read config file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    # When running as a HA add-on, the Supervisor writes MQTT broker
    # credentials to the s6 container environment via cont-init.d/01-mqtt-env.sh.
    # These override any mqtt: values in the YAML file so users do not need to
    # copy broker credentials into mimirheim.yaml.
    _apply_mqtt_env_overrides(raw)

    try:
        return MimirheimConfig.model_validate(raw)
    except ValidationError as exc:
        print(f"ERROR: Invalid configuration in {path!r}:\n{exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Run the mimirheim optimiser until SIGTERM or SIGINT.

    Parses the ``--config`` argument, loads the configuration, constructs all
    application components, starts the MQTT network loop, and then enters the
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

    config = _load_config(args.config)

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
    paho_client = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=config.mqtt.client_id,
    )
    if config.mqtt.tls:
        import ssl
        cert_reqs = ssl.CERT_NONE if config.mqtt.tls_allow_insecure else ssl.CERT_REQUIRED
        paho_client.tls_set(cert_reqs=cert_reqs)
        if config.mqtt.tls_allow_insecure:
            paho_client.tls_set(cert_reqs=cert_reqs)
        if config.mqtt.tls_allow_insecure:
            paho_client.tls_insecure_set(True)
    if config.mqtt.username is not None:
        paho_client.username_pw_set(config.mqtt.username, config.mqtt.password)
    publisher = MqttPublisher(client=paho_client, config=config)
    mqtt_client = MqttClient(
        config=config,
        readiness=readiness,
        publisher=publisher,
        paho_client=paho_client,
        solve_queue=solve_queue,
    )

    # Register SIGTERM and SIGINT handlers. Both set `running` to False, which
    # causes the solve loop to exit cleanly after the current solve completes.
    running = True

    def _request_shutdown(signum: int, frame: object) -> None:
        nonlocal running
        logger.info("Received signal %d; shutting down.", signum)
        running = False

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

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
            if result.solve_status != "infeasible":
                publisher.publish_result(result)
        except Exception:
            error_msg = traceback.format_exc()
            logger.error("Solve failed:\n%s", error_msg)

        publisher.publish_last_solve_status(result, error_msg)

        if result is not None and config.debug.enabled:
            debug_dump(bundle, result, config, config.debug.dump_dir, config.debug.max_dumps)

        if result is not None and config.reporting.enabled:
            _publish_reporting_notification(bundle, result, config, paho_client)

    mqtt_client.stop()
    logger.info("mimirheim stopped.")


if __name__ == "__main__":
    main()
