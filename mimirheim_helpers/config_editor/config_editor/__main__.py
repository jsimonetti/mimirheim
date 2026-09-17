"""Entry point for the mimirheim Config Editor service.

Loads config-editor.yaml, connects to MQTT to discover Config Owners,
starts the HTTP server on the configured port, and blocks until SIGTERM or
SIGINT.

What this module does not do:
- Configuration validation: delegated to `config_editor.config`.
- Descriptor discovery: delegated to `config_editor.mqtt_client`.
- Request handling: delegated to `config_editor.server.ConfigEditorServer`.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from config_editor.config import load_config
from config_editor.mqtt_client import ConfigEditorMqttClient
from config_editor.registry import ConfigOwnerRegistry
from config_editor.server import ConfigEditorServer

# Named explicitly, not derived from __name__: this module runs as
# `python -m config_editor`, where __name__ is "__main__".
logger = logging.getLogger("config_editor")

# How often the run loop checks for an acknowledged Restart Request while
# waiting for a stop signal. Mirrors helper_common.daemon.MqttDaemon.run's
# own poll interval (ADR-0012): a Restart Request arrives over MQTT, not as
# an OS signal, so there is nothing to interrupt a plain wait() with.
_RESTART_POLL_INTERVAL_S = 0.5


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="mimirheim Config Editor: renders every discovered Config Owner's configuration."
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="CONFIG_PATH",
        help="Path to the YAML configuration file.",
    )
    return parser.parse_args()


def _watch_for_restart_request(
    mqtt_client: ConfigEditorMqttClient, stop_event: threading.Event
) -> None:
    """Set `stop_event` once a Restart Request for config-editor's own owner_id arrives.

    Runs in its own background thread (see `main`): a Restart Request is
    delivered over MQTT, on the paho network thread, not as an OS signal, so
    there is nothing for the main thread's `stop_event.wait()` to be
    interrupted by other than this poll.

    Args:
        mqtt_client: The running ConfigEditorMqttClient, whose own Config
            Owner records an acknowledged Restart Request.
        stop_event: Set once a Restart Request arrives, so `main`'s
            `stop_event.wait()` unblocks the same way it does for SIGTERM.
    """
    while not stop_event.is_set():
        if mqtt_client.restart_requested.wait(timeout=_RESTART_POLL_INTERVAL_S):
            logger.info("Restart requested; shutting down.")
            stop_event.set()
            return


def main() -> None:
    """Load configuration, connect to MQTT, and run the Config Editor HTTP server."""
    args = _parse_args()
    cfg = load_config(args.config, logger)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    registry = ConfigOwnerRegistry()
    mqtt_client = ConfigEditorMqttClient(cfg, registry, Path(args.config))
    server = ConfigEditorServer(registry, mqtt_client, port=cfg.port, allowed_ip=cfg.allowed_ip)

    # The stop event is set by the signal handler and waited on by the main
    # thread. The server runs in a daemon thread so that setting the event
    # unblocks the main thread, which then calls server.shutdown() cleanly.
    # A signal handler must return quickly and must not itself call blocking
    # cleanup code; deferring the actual shutdown to the main thread keeps the
    # handler itself trivial.
    stop_event = threading.Event()

    def _handle_stop(signum: int, frame: object) -> None:
        logger.info("Received signal %d, shutting down.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    mqtt_client.start()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    restart_watcher = threading.Thread(
        target=_watch_for_restart_request, args=(mqtt_client, stop_event), daemon=True
    )
    restart_watcher.start()

    stop_event.wait()
    server.shutdown()
    mqtt_client.stop()
    sys.exit(0)


if __name__ == "__main__":
    main()
