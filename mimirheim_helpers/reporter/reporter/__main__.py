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
from pathlib import Path

from helper_common.config import load_helper_config

from reporter.config import ReporterConfig
from reporter.daemon import (
    CONFIG_OWNER_DISPLAY_NAME,
    CONFIG_OWNER_ID,
    REPORTER_CONFIG_FORM_SPEC,
    ReporterDaemon,
    logger,
)


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
    config = load_helper_config(
        args.config,
        ReporterConfig,
        logger,
        owner_id=CONFIG_OWNER_ID,
        display_name=CONFIG_OWNER_DISPLAY_NAME,
        form_spec=REPORTER_CONFIG_FORM_SPEC,
    )
    daemon = ReporterDaemon(config, Path(args.config))
    daemon.run()


if __name__ == "__main__":
    main()
