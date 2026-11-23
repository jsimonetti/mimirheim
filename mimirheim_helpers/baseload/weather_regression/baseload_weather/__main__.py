"""Entry point for the weather-dependent base load forecast daemon.

``WeatherBaseloadDaemon`` is a ``HelperDaemon`` subclass. On each trigger it
reads hourly energy and outdoor temperature history from the Home Assistant
recorder, fetches an Open-Meteo temperature forecast, fits the per-hour
heating-degree model and publishes the forecast retained for a mimirheim
static load.

The base class handles the MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, debounce, run statistics and
signal handling. The daemon also opts into the Config Service protocol via
``helper_common.config_owner.ConfigOwnerSupport``.

It contains no data access or modelling logic of its own; it delegates to
``recorder``, ``openmeteo``, ``model`` and ``publisher``. A failure before the
forecast is published publishes nothing, so mimirheim keeps the last retained
forecast; the optional model statistics published afterwards are best effort.
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from helper_common.config import load_helper_config
from helper_common.config_owner import ConfigOwnerSupport
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon
from helper_common.discovery import POWER_NO_CONFIDENCE_FORECAST_ATTRIBUTES_TEMPLATE
from helper_common.publish import PublishError

from baseload_weather.config import WeatherBaseloadConfig
from baseload_weather.formspec import WEATHER_BASELOAD_CONFIG_FORM_SPEC
from baseload_weather.model import ModelError, build_forecast, history_window
from baseload_weather.openmeteo import RateLimitError, WeatherForecastError, fetch_hourly_temperature
from baseload_weather.publisher import publish_forecast, publish_model_stats
from baseload_weather.recorder import RecorderError, fetch_history

# Named explicitly, not derived from __name__: this module runs as
# `python -m baseload_weather`, where __name__ is "__main__" and the records
# would not join the ones MqttDaemon emits under the package name.
logger = logging.getLogger("baseload_weather")

# Stable regardless of user configuration; the Config Editor needs a fixed
# identity for this tool across restarts and reconfiguration.
CONFIG_OWNER_ID = "baseload_weather"
CONFIG_OWNER_DISPLAY_NAME = "Baseload (weather-dependent)"


class WeatherBaseloadDaemon(HelperDaemon):
    """Daemon that publishes a weather-dependent base load forecast on demand."""

    TOOL_NAME = "baseload_weather"
    FORECAST_ATTRIBUTES_TEMPLATE = POWER_NO_CONFIDENCE_FORECAST_ATTRIBUTES_TEMPLATE

    def __init__(self, config: WeatherBaseloadConfig, config_path: Path) -> None:
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
            model=WeatherBaseloadConfig,
            form_spec=WEATHER_BASELOAD_CONFIG_FORM_SPEC,
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
        """Read history, fetch the forecast, fit the model and publish.

        A failure before publishing (recorder, forecast or model) is logged and
        nothing is published, so the retained forecast on the output topic
        stays as it was. An Open-Meteo rate limit additionally suppresses
        triggers until its reset time. Once the forecast is published, the
        optional model statistics are best effort: separate MQTT publishes
        cannot be rolled back, so a statistics failure is logged and the
        cycle still counts as successful.

        Args:
            client: Connected paho MQTT client.

        Returns:
            The cycle outcome for the daemon's run statistics.
        """
        config = self._config
        now = datetime.now(UTC)
        start = now.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=config.horizon_hours)
        grid_start, _ = history_window(start, config.model)

        try:
            history = fetch_history(
                config.homeassistant, start_ts=grid_start, end_ts=int(start.timestamp())
            )
        except RecorderError:
            logger.exception("Recorder read failed; retaining existing payload on %s", config.output_topic)
            return CycleResult(success=False)

        try:
            forecast_temps = fetch_hourly_temperature(config.weather_forecast, start=start, end=end)
        except RateLimitError as exc:
            logger.warning("%s", exc)
            return CycleResult(success=False, suppress_until=exc.reset_at)
        except WeatherForecastError:
            logger.exception(
                "Temperature forecast failed; retaining existing payload on %s", config.output_topic
            )
            return CycleResult(success=False)

        try:
            result = build_forecast(
                energy=history.energy,
                temperature_recorder=history.temperature_c,
                temperature_forecast=forecast_temps,
                sum_ids=[e.entity_id for e in config.homeassistant.sum_entities],
                subtract_ids=[e.entity_id for e in config.homeassistant.subtract_entities],
                config=config.model,
                start=start,
                horizon_hours=config.horizon_hours,
            )
        except ModelError:
            logger.exception("Model fit failed; retaining existing payload on %s", config.output_topic)
            return CycleResult(success=False)

        publish_forecast(
            client,
            config.output_topic,
            result.points,
            signal_mimir=config.signal_mimir,
            mimir_trigger_topic=config.mimir_trigger_topic,
        )
        if config.model_stats_topic:
            stats = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), **result.stats}
            # Best effort: the forecast (and any solve trigger) is already
            # out and MQTT publishes cannot be rolled back, so a failure here
            # must not turn the cycle into a reported failure.
            try:
                publish_model_stats(client, config.model_stats_topic, stats)
            except PublishError:
                logger.exception("Model statistics publish failed on %s", config.model_stats_topic)
        logger.info(
            "Published %d forecast points (T_base=%s, levels=%s).",
            len(result.points),
            result.stats["t_base_c"],
            result.stats["levels"],
        )
        return CycleResult(horizon_hours=config.horizon_hours)


def main() -> None:
    """Parse arguments, load the configuration and run the daemon."""
    parser = argparse.ArgumentParser(
        description="Weather-dependent base load forecast tool for mimirheim"
    )
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO; keep it quiet like the other helpers.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    WeatherBaseloadDaemon(
        load_helper_config(
            args.config,
            WeatherBaseloadConfig,
            logger,
            owner_id=CONFIG_OWNER_ID,
            display_name=CONFIG_OWNER_DISPLAY_NAME,
            form_spec=WEATHER_BASELOAD_CONFIG_FORM_SPEC,
        ),
        Path(args.config),
    ).run()


if __name__ == "__main__":
    main()
