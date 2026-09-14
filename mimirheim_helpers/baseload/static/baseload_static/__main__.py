"""Entry point for the static baseload forecast daemon.

This module implements ``StaticBaseloadDaemon``, a subclass of
``HelperDaemon`` that builds and publishes a static baseload forecast on each
trigger message.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, 5-second debounce, and signal
handling.

It does not fetch any external data — the profile is fully defined in config.yaml.

``StaticBaseloadDaemon`` also opts into the Config Service protocol via
``helper_common.config_owner.ConfigOwnerSupport``, so its configuration is
discoverable, renderable, and editable in the same running Config Editor as
mimirheim core's (config-editor-v3 ticket 06). This is additive: it changes
nothing about the publish behavior above.
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

from baseload_static.config import BaseloadConfig
from baseload_static.forecast import build_forecast
from baseload_static.formspec import BASELOAD_CONFIG_FORM_SPEC
from baseload_static.publisher import publish_forecast

# Named explicitly, not derived from __name__: this module runs as
# `python -m baseload_static`, where __name__ is "__main__" and the records would
# not join the ones MqttDaemon emits under the package name.
logger = logging.getLogger("baseload_static")

# Stable regardless of user configuration; the Config Editor needs a fixed
# identity for this tool across restarts and reconfiguration.
CONFIG_OWNER_ID = "baseload_static"
CONFIG_OWNER_DISPLAY_NAME = "Baseload (static profile)"


class StaticBaseloadDaemon(HelperDaemon):
    """Daemon that publishes a static baseload forecast on demand.

    Subscribes to the configured trigger topic. On each trigger, builds a
    forecast from the statically configured profile and publishes it retained
    to the configured output topic. No external I/O is performed.
    """

    TOOL_NAME = "baseload_static"
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

    StaticBaseloadDaemon(load_helper_config(args.config, BaseloadConfig, logger), Path(args.config)).run()


if __name__ == "__main__":
    main()
