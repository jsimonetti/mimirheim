"""Entry point for the config-editor-v2 service.

Loads config-editor-v2.yaml, starts the HTTP server on the configured port,
and blocks until SIGTERM or SIGINT.

What this module does not do:
- Configuration validation: delegated to config_editor_v2.config.
- Request handling: delegated to config_editor_v2.server.ConfigEditorV2Server.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from config_editor_v2.config import load_config
from config_editor_v2.server import ConfigEditorV2Server

# Named explicitly, not derived from __name__: this module runs as
# `python -m config_editor_v2`, where __name__ is "__main__".
logger = logging.getLogger("config_editor_v2")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="config-editor-v2: in-container web UI for editing mimirheim "
        "and helper configuration files."
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="CONFIG_PATH",
        help="Path to the YAML configuration file.",
    )
    return parser.parse_args()


def main() -> None:
    """Loads configuration and runs the config-editor-v2 HTTP server."""
    args = _parse_args()
    cfg = load_config(args.config)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = ConfigEditorV2Server(config_dir=cfg.config_dir, port=cfg.port)

    # The stop event is set by the signal handler and waited on by the main
    # thread. The server runs in a daemon thread so that setting the event
    # unblocks the main thread, which then calls server.shutdown() cleanly.
    #
    # Why not call server.shutdown() directly from the signal handler?
    # serve_forever() runs in a daemon thread. shutdown() blocks until
    # serve_forever() notices the shutdown flag and exits. Calling shutdown()
    # from the signal handler while serve_forever() runs in the same thread
    # would deadlock: shutdown() waits for serve_forever() to set the
    # "is_shut_down" event, but serve_forever() cannot run while the signal
    # handler is executing.
    stop_event = threading.Event()

    def _handle_stop(signum: int, frame: object) -> None:
        logger.info("Received signal %d, shutting down.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    stop_event.wait()
    server.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    main()
