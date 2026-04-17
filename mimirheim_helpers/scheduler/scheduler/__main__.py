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
"""

import argparse
import logging
import signal
import ssl
import threading

import paho.mqtt.client as paho

from scheduler.config import SchedulerConfig, load_config
from scheduler.loop import run

logger = logging.getLogger("scheduler")


def _make_paho_client(config: SchedulerConfig) -> paho.Client:
    """Construct and connect a paho MQTT client for the scheduler.

    The scheduler only publishes — it never subscribes to any topic.
    Reconnection is handled automatically by paho's internal loop.

    Args:
        config: Validated scheduler configuration, used for broker address
            and credentials.

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

    def _on_connect(
        cl: paho.Client,
        userdata: object,
        flags: object,
        reason_code: object,
        properties: object,
    ) -> None:
        if reason_code == 0:
            logger.info(
                "Connected to MQTT broker %s:%d.", config.mqtt.host, config.mqtt.port
            )
        else:
            logger.warning("MQTT connection failed: reason_code=%s", reason_code)

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

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect

    client.connect(config.mqtt.host, config.mqtt.port, keepalive=60)
    return client


def main() -> None:
    """Run the scheduler daemon until SIGTERM or SIGINT.

    Parses ``--config``, loads and validates the configuration, connects to
    the MQTT broker, then enters the schedule loop. The loop publishes an
    empty trigger message to each configured topic when its cron expression
    fires. On SIGTERM or SIGINT, the stop event is set and the loop exits
    after the current sleep completes.
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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    schedules = config.parsed_schedules()

    stop_event = threading.Event()

    def _request_shutdown(signum: int, frame: object) -> None:
        logger.info("Received signal %d; shutting down.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    client = _make_paho_client(config)
    client.loop_start()

    logger.info(
        "Scheduler started. Connecting to %s:%d with %d schedule entries.",
        config.mqtt.host,
        config.mqtt.port,
        len(schedules),
    )

    try:
        run(client, schedules, stop_event)
    finally:
        client.loop_stop()
        client.disconnect()
        logger.info("Scheduler shut down cleanly.")


if __name__ == "__main__":
    main()
