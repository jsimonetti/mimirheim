"""Entry point for the epexpredictor_prices daemon.

This module implements ``EpexPredictorPricesDaemon``, a subclass of
``HelperDaemon`` that fetches EPEX day-ahead spot price predictions on each
trigger message and publishes them to the configured output topic.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, 5-second debounce, and signal
handling.

It does not perform any price calculation itself; it delegates to fetcher and
series.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from helper_common.config import load_helper_config
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon
from helper_common.discovery import PRICE_FORECAST_ATTRIBUTES_TEMPLATE

from epexpredictor_prices.config import EpexPredictorPricesConfig
from epexpredictor_prices.fetcher import FetchError, fetch_raw_prices
from epexpredictor_prices.publisher import publish_prices
from epexpredictor_prices.series import ConfidenceDecay, build_price_steps

# Named explicitly, not derived from __name__: this module runs as
# `python -m epexpredictor_prices`, where __name__ is "__main__" and the
# records would not join the ones MqttDaemon emits under the package name.
logger = logging.getLogger("epexpredictor_prices")


class EpexPredictorPricesDaemon(HelperDaemon):
    """Daemon that fetches EpexPredictor price predictions on demand.

    Subscribes to the configured trigger topic. On each trigger, fetches the
    current EPEX day-ahead prediction from the EpexPredictor API and
    publishes the result retained to the configured output topic.
    """

    TOOL_NAME = "epexpredictor_prices"
    FORECAST_VALUE_TEMPLATE = "{{ value_json[0].import_eur_per_kwh | default(0) | round(4) }}"
    FORECAST_UNIT = "EUR/kWh"
    FORECAST_DEVICE_CLASS = None
    FORECAST_ATTRIBUTES_TEMPLATE = PRICE_FORECAST_ATTRIBUTES_TEMPLATE

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Fetch the current EpexPredictor prediction and publish it.

        If the EpexPredictor API call fails, the error is logged and the
        existing retained payload on the output topic is left unchanged.

        Args:
            client: Connected paho MQTT client.
        """
        config = self._config
        fetch_time = datetime.now(tz=timezone.utc)
        try:
            result = fetch_raw_prices(
                area=config.epexpredictor.area,
                horizon_hours=config.epexpredictor.horizon_hours,
                price_interval=config.epexpredictor.price_interval,
                base_url=config.epexpredictor.base_url,
            )
        except FetchError:
            logger.exception(
                "EpexPredictor fetch failed — retaining existing payload on %s",
                config.output_topic,
            )
            return
        decay = ConfidenceDecay(
            hours_0_to_6=config.confidence_decay.hours_0_to_6,
            hours_6_to_24=config.confidence_decay.hours_6_to_24,
            hours_24_to_48=config.confidence_decay.hours_24_to_48,
            hours_48_plus=config.confidence_decay.hours_48_plus,
        )
        steps = build_price_steps(
            result.steps,
            fetch_time=fetch_time,
            decay=decay,
            import_formula=config.epexpredictor.import_formula,
            export_formula=config.epexpredictor.export_formula,
            known_until=result.known_until,
            known_until_confidence=config.confidence_decay.known_until_confidence,
        )
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
        step_hours = 0.25 if config.epexpredictor.price_interval == "quarter_hourly" else 1.0
        return CycleResult(horizon_hours=len(steps) * step_hours)


def main() -> None:
    """Parse arguments, load config, and start the epexpredictor_prices daemon."""
    parser = argparse.ArgumentParser(description="epexpredictor_prices fetcher for mimirheim")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    EpexPredictorPricesDaemon(
        load_helper_config(args.config, EpexPredictorPricesConfig, logger)
    ).run()


if __name__ == "__main__":
    main()
