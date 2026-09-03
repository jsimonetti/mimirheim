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

import paho.mqtt.client as mqtt

from helper_common.config import load_helper_config
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon
from helper_common.discovery import PRICE_FORECAST_ATTRIBUTES_TEMPLATE

from nordpool.config import NordpoolConfig
from nordpool.fetcher import FetchError, fetch_prices
from nordpool.publisher import publish_prices

# Named explicitly, not derived from __name__: this module runs as
# `python -m nordpool`, where __name__ is "__main__" and the records would
# not join the ones MqttDaemon emits under the package name.
logger = logging.getLogger("nordpool")


class NordpoolDaemon(HelperDaemon):
    """Daemon that fetches Nordpool prices on demand.

    Subscribes to the configured trigger topic. On each trigger, fetches
    today's and tomorrow's prices from the Nordpool API and publishes the
    result retained to the configured output topic.
    """

    TOOL_NAME = "nordpool_prices"
    FORECAST_VALUE_TEMPLATE = "{{ value_json[0].import_eur_per_kwh | default(0) | round(4) }}"
    FORECAST_UNIT = "EUR/kWh"
    FORECAST_DEVICE_CLASS = None
    FORECAST_ATTRIBUTES_TEMPLATE = PRICE_FORECAST_ATTRIBUTES_TEMPLATE

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
                    price_interval=config.nordpool.price_interval,
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
        # Each step covers one hour in "hourly" mode or 15 minutes in
        # "quarter_hourly" mode. len(steps) alone is a step count, not hours —
        # it must be scaled by the step duration to report true horizon_hours.
        step_hours = 0.25 if config.nordpool.price_interval == "quarter_hourly" else 1.0
        return CycleResult(horizon_hours=len(steps) * step_hours)


def main() -> None:
    """Parse arguments, load config, and start the nordpool daemon."""
    parser = argparse.ArgumentParser(description="nordpool price fetcher for mimirheim")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    NordpoolDaemon(load_helper_config(args.config, NordpoolConfig, logger)).run()


if __name__ == "__main__":
    main()
