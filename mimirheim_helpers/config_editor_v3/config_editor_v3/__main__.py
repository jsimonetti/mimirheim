"""Entry point for the mimirheim Config Editor (v3) service.

Loads config-editor-v3.yaml, connects to MQTT to discover Config Owners,
starts the HTTP server on the configured port, and blocks until SIGTERM or
SIGINT.

What this module does not do:
- Configuration validation: delegated to `config_editor_v3.config`.
- Descriptor discovery: delegated to `config_editor_v3.mqtt_client`.
- Request handling: delegated to `config_editor_v3.server.ConfigEditorServer`.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from helper_common.config import load_helper_config

from config_editor_v3.config import ConfigEditorV3Config
from config_editor_v3.mqtt_client import ConfigEditorMqttClient
from config_editor_v3.registry import ConfigOwnerRegistry
from config_editor_v3.server import ConfigEditorServer

# Named explicitly, not derived from __name__: this module runs as
# `python -m config_editor_v3`, where __name__ is "__main__".
logger = logging.getLogger("config_editor_v3")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="mimirheim Config Editor (v3): renders every discovered Config Owner's configuration."
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
    cfg = load_helper_config(args.config, ConfigEditorV3Config, logger)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    registry = ConfigOwnerRegistry()
    mqtt_client = ConfigEditorMqttClient(cfg, registry)
    server = ConfigEditorServer(registry, port=cfg.port)

    # The stop event is set by the signal handler and waited on by the main
    # thread. The server runs in a daemon thread so that setting the event
    # unblocks the main thread, which then calls server.shutdown() cleanly.
    # See config_editor's own __main__.py for why this cannot instead call
    # shutdown() directly from the signal handler.
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
