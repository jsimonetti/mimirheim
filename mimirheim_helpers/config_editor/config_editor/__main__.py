"""Entry point for the mimirheim config editor service.

Loads config-editor.yaml, starts the HTTP server on the configured port,
and blocks until SIGTERM or SIGINT.

What this module does not do:
- Configuration validation: delegated to config_editor.config.
- Request handling: delegated to config_editor.server.ConfigEditorServer.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from config_editor.config import load_config
from config_editor.server import ConfigEditorServer


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="mimirheim config editor: in-container web UI for editing mimirheim.yaml."
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="CONFIG_PATH",
        help="Path to the YAML configuration file.",
    )
    return parser.parse_args()


def main() -> None:
    """Load configuration and run the config editor HTTP server."""
    args = _parse_args()
    cfg = load_config(args.config)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = ConfigEditorServer(config_dir=cfg.config_dir, port=cfg.port, allowed_ip=cfg.allowed_ip)

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
        logging.getLogger(__name__).info("Received signal %d, shutting down.", signum)
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
