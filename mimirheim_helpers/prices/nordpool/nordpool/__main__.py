"""Entry point for the nordpool price fetcher daemon.

This module implements ``NordpoolDaemon``, a subclass of ``HelperDaemon``
that fetches today's and tomorrow's Nordpool prices on each trigger message
and publishes them to the configured output topic.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, 5-second debounce, and signal
handling.

It does not perform any price calculation itself; it delegates to fetcher and
publisher.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

from helper_common.config import apply_mqtt_env_overrides
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon

from nordpool.config import NordpoolConfig
from nordpool.fetcher import FetchError, fetch_prices
from nordpool.publisher import publish_prices

logger = logging.getLogger(__name__)


def _load_config(path: str) -> NordpoolConfig:
    """Load and validate the YAML configuration file.

    Args:
        path: Filesystem path to the config.yaml file.

    Returns:
        Validated NordpoolConfig instance.

    Raises:
        SystemExit: If the file cannot be read or fails validation.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text())
        apply_mqtt_env_overrides(raw)
        return NordpoolConfig.model_validate(raw)
    except Exception:
        logger.exception("Failed to load configuration from %s", path)
        sys.exit(1)


class NordpoolDaemon(HelperDaemon):
    """Daemon that fetches Nordpool prices on demand.

    Subscribes to the configured trigger topic. On each trigger, fetches
    today's and tomorrow's prices from the Nordpool API and publishes the
    result retained to the configured output topic.
    """

    TOOL_NAME = "nordpool_prices"

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Fetch current Nordpool prices and publish them.

        If the Nordpool API call fails, the error is logged and the existing
        retained payload on the output topic is left unchanged.

        Args:
            client: Connected paho MQTT client.
        """
        config = self._config
        try:
            steps = asyncio.run(
                fetch_prices(
                    area=config.nordpool.area,
                    import_formula=config.nordpool.import_formula,
                    export_formula=config.nordpool.export_formula,
                )
            )
        except FetchError:
            logger.exception(
                "Nordpool fetch failed — retaining existing payload on %s",
                config.output_topic,
            )
            return
        publish_prices(
            client,
            config.output_topic,
            steps,
            signal_mimir=config.signal_mimir,
            mimir_trigger_topic=config.mimir_trigger_topic,
        )
        return CycleResult(horizon_hours=len(steps) * 0.25)


def main() -> None:
    """Parse arguments, load config, and start the nordpool daemon."""
    parser = argparse.ArgumentParser(description="nordpool price fetcher for mimirheim")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    NordpoolDaemon(_load_config(args.config)).run()


if __name__ == "__main__":
    main()
