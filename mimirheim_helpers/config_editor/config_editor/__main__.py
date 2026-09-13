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

from config_editor.config import load_config
from config_editor.mqtt_client import ConfigEditorMqttClient
from config_editor.registry import ConfigOwnerRegistry
from config_editor.server import ConfigEditorServer

# Named explicitly, not derived from __name__: this module runs as
# `python -m config_editor`, where __name__ is "__main__".
logger = logging.getLogger("config_editor")


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


def main() -> None:
    """Load configuration, connect to MQTT, and run the Config Editor HTTP server."""
    args = _parse_args()
    cfg = load_config(args.config, logger)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    registry = ConfigOwnerRegistry()
    mqtt_client = ConfigEditorMqttClient(cfg, registry)
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

    stop_event.wait()
    server.shutdown()
    mqtt_client.stop()
    sys.exit(0)


if __name__ == "__main__":
    main()
