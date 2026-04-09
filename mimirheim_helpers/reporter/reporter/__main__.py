"""Entry point for the mimirheim-reporter daemon.

Loads the YAML configuration, constructs a ``ReporterDaemon``, and runs it
until SIGTERM or SIGINT.

What this module does not do:
- Rendering: delegated to ``reporter.daemon.ReporterDaemon``.
- Configuration validation: delegated to ``reporter.config``.
- MQTT connection management: delegated to ``helper_common.daemon.HelperDaemon``.
"""
from __future__ import annotations

import argparse
import logging
import sys

from reporter.config import load_config
from reporter.daemon import ReporterDaemon


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="mimirheim-reporter: event-driven HTML report generator for mimirheim."
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="CONFIG_PATH",
        help="Path to the YAML configuration file.",
    )
    return parser.parse_args()


def main() -> None:
    """Load config and run the reporter daemon."""
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    daemon = ReporterDaemon(config)
    daemon.run()


if __name__ == "__main__":
    main()
