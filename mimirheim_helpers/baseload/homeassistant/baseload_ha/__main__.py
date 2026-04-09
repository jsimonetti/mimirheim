"""Entry point for the homeassistant baseload forecast daemon.

This module implements ``HaBaseloadDaemon``, a subclass of ``HelperDaemon``
that fetches HA statistics and publishes a baseload forecast on each trigger
message.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, 5-second debounce, and signal
handling.

It does not implement any forecasting logic — it delegates to fetcher, forecast,
and publisher.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

from helper_common.config import apply_mqtt_env_overrides
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon

from baseload_ha.config import BaseloadConfig
from baseload_ha.fetcher import FetchError, fetch_statistics
from baseload_ha.forecast import build_forecast
from baseload_ha.publisher import publish_forecast

logger = logging.getLogger(__name__)


def _load_config(path: str) -> BaseloadConfig:
    """Load and validate the YAML configuration file.

    Args:
        path: Filesystem path to the config.yaml file.

    Returns:
        Validated BaseloadConfig instance.

    Raises:
        SystemExit: If the file cannot be read or fails validation.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text())
        apply_mqtt_env_overrides(raw)
        return BaseloadConfig.model_validate(raw)
    except Exception:
        logger.exception("Failed to load configuration from %s", path)
        sys.exit(1)


class HaBaseloadDaemon(HelperDaemon):
    """Daemon that fetches HA statistics and publishes a baseload forecast on demand.

    Subscribes to the configured trigger topic. On each trigger, fetches
    historical statistics from Home Assistant, builds a weighted forecast, and
    publishes it retained to the configured output topic.
    """

    TOOL_NAME = "baseload_homeassistant"

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Fetch HA statistics, build the forecast, and publish it.

        If the Home Assistant fetch fails, the error is logged and the existing
        retained payload on the output topic is left unchanged.

        Args:
            client: Connected paho MQTT client.
        """
        config = self._config
        ha = config.homeassistant
        sum_entity_ids = [e.entity_id for e in ha.sum_entities]
        subtract_entity_ids = [e.entity_id for e in ha.subtract_entities]
        sum_units = {e.entity_id: e.unit for e in ha.sum_entities}
        subtract_units = {e.entity_id: e.unit for e in ha.subtract_entities}
        all_entity_ids = sum_entity_ids + subtract_entity_ids

        try:
            all_readings = asyncio.run(
                fetch_statistics(
                    url=ha.url,
                    token=ha.token,
                    entity_ids=all_entity_ids,
                    lookback_days=ha.lookback_days,
                )
            )
        except FetchError:
            logger.exception(
                "HA statistics fetch failed — retaining existing payload on %s",
                config.output_topic,
            )
            return

        sum_readings = {eid: all_readings.get(eid, []) for eid in sum_entity_ids}
        subtract_readings = {eid: all_readings.get(eid, []) for eid in subtract_entity_ids}

        steps = build_forecast(
            sum_readings=sum_readings,
            subtract_readings=subtract_readings,
            sum_units=sum_units,
            subtract_units=subtract_units,
            now=datetime.now(tz=timezone.utc),
            horizon_hours=ha.horizon_hours,
            lookback_days=ha.lookback_days,
            lookback_decay=ha.lookback_decay,
        )

        publish_forecast(
            client,
            config.output_topic,
            steps,
            signal_mimir=config.signal_mimir,
            mimir_trigger_topic=config.mimir_trigger_topic,
        )
        return CycleResult(horizon_hours=ha.horizon_hours)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Home Assistant baseload forecast tool for mimirheim"
    )
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    HaBaseloadDaemon(_load_config(args.config)).run()


if __name__ == "__main__":
    main()
