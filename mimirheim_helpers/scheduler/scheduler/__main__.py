"""Entry point for the mimirheim scheduler daemon.

This module is responsible for:

1. Parsing the ``--config`` command-line argument.
2. Loading and validating the YAML configuration file.
3. Constructing and connecting the paho MQTT client.
4. Starting the paho network loop in a background thread.
5. Registering SIGTERM and SIGINT handlers that set the stop event.
6. Running the schedule loop on the main thread until the stop event is set.
7. Disconnecting cleanly on exit.

What this module does not do:
- It does not implement scheduling logic — that is loop.py's responsibility.
- It does not parse configuration — that is config.py's responsibility.

The scheduler also opts into the Config Service protocol via
``helper_common.config_owner.ConfigOwnerSupport``, so its configuration is
discoverable, renderable, and editable in the same running Config Editor as
mimirheim core's (config-editor-v3 ticket 06). Unlike every other helper,
the scheduler has no ``MqttDaemon``/``HelperDaemon`` base class to wire
this into: it drives its own paho client directly, so the four
``ConfigOwnerSupport`` calls are made from this module's own
``_on_connect``/``_on_message`` callbacks and from ``main()`` around
``client.connect``/``client.disconnect``.
"""

import argparse
import logging
import signal
import ssl
import threading
from pathlib import Path

import paho.mqtt.client as paho

from helper_common.config import load_helper_config
from helper_common.config_owner import ConfigOwnerSupport

from scheduler.config import SchedulerConfig
from scheduler.formspec import SCHEDULER_CONFIG_FORM_SPEC
from scheduler.loop import run

logger = logging.getLogger("scheduler")

# Stable regardless of user configuration; the Config Editor needs a fixed
# identity for this tool across restarts and reconfiguration.
CONFIG_OWNER_ID = "scheduler"
CONFIG_OWNER_DISPLAY_NAME = "Scheduler"

# How often the restart-request watcher thread checks the Config Owner's
# restart_requested event (ADR-0012). Mirrors helper_common.daemon's own
# poll interval; the scheduler has no MqttDaemon base class to inherit that
# polling from, so it is replicated here.
_RESTART_POLL_INTERVAL_S = 0.5


def _make_paho_client(config: SchedulerConfig, config_owner: ConfigOwnerSupport) -> paho.Client:
    """Construct and connect a paho MQTT client for the scheduler.

    The scheduler's own job publishes and never subscribes to any topic, but
    the client now also carries the Config Owner's ``validate_and_write``
    subscription (see ``config_owner``). Reconnection is handled
    automatically by paho's internal loop.

    Args:
        config: Validated scheduler configuration, used for broker address
            and credentials.
        config_owner: This tool's Config Service wiring. Its last-will is
            registered here, before ``connect()``; its ``on_connect`` and
            ``handle_message`` are wired into this client's own callbacks.

    Returns:
        A connected paho ``Client`` instance with ``loop_start()`` not yet
        called. The caller must call ``loop_start()`` before publishing.
    """
    client = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=config.mqtt.client_id,
    )

    if config.mqtt.tls:
        cert_reqs = ssl.CERT_NONE if config.mqtt.tls_allow_insecure else ssl.CERT_REQUIRED
        client.tls_set(cert_reqs=cert_reqs)
        if config.mqtt.tls_allow_insecure:
            client.tls_insecure_set(True)

    if config.mqtt.username is not None:
        client.username_pw_set(config.mqtt.username, config.mqtt.password)

    config_owner.register_last_will(client)

    def _on_connect(
        cl: paho.Client,
        userdata: object,
        flags: object,
        reason_code: object,
        properties: object,
    ) -> None:
        if reason_code.is_failure:
            logger.warning("MQTT connection failed: reason_code=%s", reason_code)
            return
        logger.info(
            "Connected to MQTT broker %s:%d.", config.mqtt.host, config.mqtt.port
        )
        config_owner.on_connect(cl)

    def _on_disconnect(
        cl: paho.Client,
        userdata: object,
        disconnect_flags: object,
        reason_code: object,
        properties: object,
    ) -> None:
        if reason_code != 0:
            logger.warning(
                "MQTT disconnected unexpectedly (reason_code=%s); paho will reconnect.",
                reason_code,
            )

    def _on_message(
        cl: paho.Client,
        userdata: object,
        message: paho.MQTTMessage,
    ) -> None:
        config_owner.handle_message(cl, message)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message = _on_message

    client.connect(config.mqtt.host, config.mqtt.port, keepalive=60)
    return client


def _configure_logging() -> None:
    """Set up log formatting and levels for the daemon.

    APScheduler logs every job it runs and every job that finishes, at INFO,
    each line carrying the full trigger repr. Those two lines restate what
    ``loop.py`` already reports once per fire in a readable form, so with the
    recommended schedule they account for a few hundred redundant lines a day.
    Quietening its loggers to WARNING removes the duplication and keeps the
    misfire warning, which is the only place a skipped trigger is reported.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _watch_for_restart_request(
    config_owner: ConfigOwnerSupport, stop_event: threading.Event
) -> None:
    """Set ``stop_event`` once a Restart Request is acknowledged.

    ``loop.run()`` only wakes on ``stop_event``, and a Restart Request is
    delivered over the MQTT connection rather than as an OS signal, so
    nothing else notices it. Run this in its own thread, alongside
    ``loop.run()`` on the main thread; it returns once it has set
    ``stop_event``, either because it observed the Restart Request or
    because something else (a SIGTERM/SIGINT handler) set ``stop_event``
    first.

    Args:
        config_owner: This tool's Config Service wiring; its
            ``restart_requested`` event (ADR-0012) is polled here.
        stop_event: Set when a Restart Request is observed. Also checked so
            this thread exits promptly if shutdown was triggered some other
            way.
    """
    while not stop_event.is_set():
        if config_owner.restart_requested.wait(timeout=_RESTART_POLL_INTERVAL_S):
            logger.info("Restart requested; shutting down.")
            stop_event.set()
            return


def main() -> None:
    """Run the scheduler daemon until SIGTERM or SIGINT.

    Parses ``--config``, loads and validates the configuration, connects to
    the MQTT broker, then enters the schedule loop. The loop publishes an
    empty trigger message to each configured topic when its cron expression
    fires. On SIGTERM or SIGINT, the stop event is set and the loop returns.
    """
    parser = argparse.ArgumentParser(
        description="mimirheim scheduler — publish MQTT trigger messages on a cron schedule.",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to the YAML configuration file.",
    )
    args = parser.parse_args()

    _configure_logging()

    config = load_helper_config(
        args.config,
        SchedulerConfig,
        logger,
        owner_id=CONFIG_OWNER_ID,
        display_name=CONFIG_OWNER_DISPLAY_NAME,
        form_spec=SCHEDULER_CONFIG_FORM_SPEC,
    )
    schedules = config.parsed_schedules()

    config_owner = ConfigOwnerSupport(
        owner_id=CONFIG_OWNER_ID,
        display_name=CONFIG_OWNER_DISPLAY_NAME,
        model=SchedulerConfig,
        form_spec=SCHEDULER_CONFIG_FORM_SPEC,
        config_path=Path(args.config),
    )

    stop_event = threading.Event()

    def _request_shutdown(signum: int, frame: object) -> None:
        logger.info("Received signal %d; shutting down.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    client = _make_paho_client(config, config_owner)
    client.loop_start()

    logger.info(
        "Scheduler started. Connecting to %s:%d with %d schedule entries.",
        config.mqtt.host,
        config.mqtt.port,
        len(schedules),
    )

    restart_watcher = threading.Thread(
        target=_watch_for_restart_request, args=(config_owner, stop_event), daemon=True
    )
    restart_watcher.start()

    try:
        run(client, schedules, stop_event)
    finally:
        config_owner.clear_descriptor(client)
        client.loop_stop()
        client.disconnect()
        logger.info("Scheduler shut down cleanly.")


if __name__ == "__main__":
    main()
