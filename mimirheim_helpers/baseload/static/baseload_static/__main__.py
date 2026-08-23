"""Entry point for the static baseload forecast daemon.

This module implements ``StaticBaseloadDaemon``, a subclass of
``HelperDaemon`` that builds and publishes a static baseload forecast on each
trigger message.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, 5-second debounce, and signal
handling.

It does not fetch any external data — the profile is fully defined in config.yaml.
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from helper_common.config import load_helper_config
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon
from helper_common.discovery import POWER_NO_CONFIDENCE_FORECAST_ATTRIBUTES_TEMPLATE

from baseload_static.config import BaseloadConfig
from baseload_static.forecast import build_forecast
from baseload_static.publisher import publish_forecast

# Named explicitly, not derived from __name__: this module runs as
# `python -m baseload_static`, where __name__ is "__main__" and the records would
# not join the ones MqttDaemon emits under the package name.
logger = logging.getLogger("baseload_static")


class StaticBaseloadDaemon(HelperDaemon):
    """Daemon that publishes a static baseload forecast on demand.

    Subscribes to the configured trigger topic. On each trigger, builds a
    forecast from the statically configured profile and publishes it retained
    to the configured output topic. No external I/O is performed.
    """

    TOOL_NAME = "baseload_static"
    FORECAST_ATTRIBUTES_TEMPLATE = POWER_NO_CONFIDENCE_FORECAST_ATTRIBUTES_TEMPLATE

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Build the static forecast and publish it.

        Constructs a horizon of steps from the configured profile and publishes
        the result retained to the output topic.

        Args:
            client: Connected paho MQTT client.
        """
        config = self._config
        steps = build_forecast(
            profile_kw=config.baseload.profile_kw,
            horizon_hours=config.baseload.horizon_hours,
            now=datetime.now(tz=timezone.utc),
            weekly_profiles_kw=config.baseload.weekly_profiles_kw,
        )
        publish_forecast(
            client,
            config.output_topic,
            steps,
            signal_mimir=config.signal_mimir,
            mimir_trigger_topic=config.mimir_trigger_topic,
        )
        return CycleResult(horizon_hours=config.baseload.horizon_hours)


def main() -> None:
    """Parse arguments, load config, and start the static baseload daemon."""
    parser = argparse.ArgumentParser(
        description="Static baseload forecast tool for mimirheim"
    )
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    StaticBaseloadDaemon(load_helper_config(args.config, BaseloadConfig, logger)).run()


if __name__ == "__main__":
    main()
