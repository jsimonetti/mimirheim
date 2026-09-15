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

``HaDbBaseloadDaemon`` also opts into the Config Service protocol via
``helper_common.config_owner.ConfigOwnerSupport``, so its configuration is
discoverable, renderable, and editable in the same running Config Editor as
mimirheim core's (config-editor-v3 ticket 06). This is additive: it changes
nothing about the fetch-and-publish behavior above.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from helper_common.config import load_helper_config
from helper_common.config_owner import ConfigOwnerSupport
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon
from helper_common.discovery import POWER_NO_CONFIDENCE_FORECAST_ATTRIBUTES_TEMPLATE

from baseload_ha_db.config import BaseloadConfig
from baseload_ha_db.fetcher import FetchError, fetch_statistics
from baseload_ha_db.forecast import build_forecast
from baseload_ha_db.formspec import BASELOAD_CONFIG_FORM_SPEC
from baseload_ha_db.publisher import publish_forecast

# Named explicitly, not derived from __name__: this module runs as
# `python -m baseload_ha_db`, where __name__ is "__main__" and the records would
# not join the ones MqttDaemon emits under the package name.
logger = logging.getLogger("baseload_ha_db")

# Stable regardless of user configuration; the Config Editor needs a fixed
# identity for this tool across restarts and reconfiguration.
CONFIG_OWNER_ID = "baseload_ha_db"
CONFIG_OWNER_DISPLAY_NAME = "Baseload (Home Assistant DB)"


class HaDbBaseloadDaemon(HelperDaemon):
    """Daemon that fetches HA recorder statistics and publishes a baseload forecast on demand.

    Subscribes to the configured trigger topic. On each trigger, queries the
    HA recorder database, builds a weighted forecast, and publishes it retained
    to the configured output topic.
    """

    TOOL_NAME = "baseload_homeassistant_db"
    FORECAST_ATTRIBUTES_TEMPLATE = POWER_NO_CONFIDENCE_FORECAST_ATTRIBUTES_TEMPLATE

    def __init__(self, config: BaseloadConfig, config_path: Path) -> None:
        """Construct the daemon and register it as a Config Service owner.

        Args:
            config: Validated tool configuration.
            config_path: Path to the YAML file this daemon was started with;
                the target of a successful validate_and_write request.
        """
        super().__init__(config)
        self._config_owner = ConfigOwnerSupport(
            owner_id=CONFIG_OWNER_ID,
            display_name=CONFIG_OWNER_DISPLAY_NAME,
            model=BaseloadConfig,
            form_spec=BASELOAD_CONFIG_FORM_SPEC,
            config_path=config_path,
        )
        # Must be registered before self._client.connect() (called by run()).
        self._config_owner.register_last_will(self._client)

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        super()._on_connect(client, userdata, flags, reason_code, properties)
        if reason_code.is_failure:
            return
        self._config_owner.on_connect(client)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        if self._config_owner.handle_message(client, message):
            return
        super()._on_message(client, userdata, message)

    def _on_shutdown(self) -> None:
        self._config_owner.clear_descriptor(self._client)

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

    HaDbBaseloadDaemon(
        load_helper_config(
            args.config,
            BaseloadConfig,
            logger,
            owner_id=CONFIG_OWNER_ID,
            display_name=CONFIG_OWNER_DISPLAY_NAME,
            form_spec=BASELOAD_CONFIG_FORM_SPEC,
        ),
        Path(args.config),
    ).run()


if __name__ == "__main__":
    main()
