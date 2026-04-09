"""Entry point for the homeassistant_db baseload forecast daemon.

This module implements ``HaDbBaseloadDaemon``, a subclass of ``HelperDaemon``
that fetches HA recorder statistics and publishes a baseload forecast on each
trigger message.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, 5-second debounce, and signal
handling.

It does not implement any forecasting logic — it delegates to fetcher, forecast,
and publisher.

The database is accessed via SQLAlchemy, so this entry point works with any
HA-supported recorder backend (SQLite, PostgreSQL, MariaDB) without any code
changes — only the ``db_url`` in config.yaml needs to be updated.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

from helper_common.config import apply_mqtt_env_overrides
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon

from baseload_ha_db.config import BaseloadConfig
from baseload_ha_db.fetcher import FetchError, fetch_statistics
from baseload_ha_db.forecast import build_forecast
from baseload_ha_db.publisher import publish_forecast

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


class HaDbBaseloadDaemon(HelperDaemon):
    """Daemon that fetches HA recorder statistics and publishes a baseload forecast on demand.

    Subscribes to the configured trigger topic. On each trigger, queries the
    HA recorder database, builds a weighted forecast, and publishes it retained
    to the configured output topic.
    """

    TOOL_NAME = "baseload_homeassistant_db"

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Fetch HA recorder statistics, build the forecast, and publish it.

        If the database fetch fails, the error is logged and the existing
        retained payload on the output topic is left unchanged.

        Args:
            client: Connected paho MQTT client.
        """
        config = self._config
        ha = config.homeassistant
        sum_entity_ids = [e.entity_id for e in ha.sum_entities]
        subtract_entity_ids = [e.entity_id for e in ha.subtract_entities]
        all_entity_ids = sum_entity_ids + subtract_entity_ids

        # Build per-entity unit overrides from the config (None means auto-detect).
        # fetch_statistics will look up any missing units from statistics_meta itself.
        unit_overrides = {
            e.entity_id: e.unit
            for e in ha.sum_entities + ha.subtract_entities
            if e.unit is not None
        }
        # Build per-entity outlier factor overrides. Only entities whose factor
        # differs from the default need to be listed; fetch_statistics falls back
        # to outlier_factor=10.0 for entities not present in the dict.
        outlier_factors = {
            e.entity_id: e.outlier_factor
            for e in ha.sum_entities + ha.subtract_entities
        }

        try:
            all_readings = fetch_statistics(
                db_url=ha.db_url,
                entity_ids=all_entity_ids,
                lookback_days=ha.lookback_days,
                unit_overrides=unit_overrides or None,
                outlier_factors=outlier_factors,
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

    HaDbBaseloadDaemon(_load_config(args.config)).run()


if __name__ == "__main__":
    main()
