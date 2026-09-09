"""Entry point for the mimirheim Open-Meteo PV forecast fetcher daemon.

This module implements ``PvOpenMeteoDaemon``, a subclass of ``HelperDaemon``
that fetches a solar power forecast for every configured array on each trigger
message and publishes it to that array's output topic.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, five-second debounce, rate-limit
suppression, and signal handling.

What this module does not do:
- It does not implement fetch logic. That is fetcher.py's responsibility.
- It does not format payloads. That is publisher.py's responsibility.
- It does not compute confidence. That is series.py's responsibility.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import traceback
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from helper_common.config import load_helper_config
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon
from helper_common.discovery import publish_trigger_discovery

from pv_openmeteo.config import PvOpenMeteoConfig
from pv_openmeteo.fetcher import FetchError, RatelimitError, fetch_array
from pv_openmeteo.publisher import publish_forecast, publish_trigger
from pv_openmeteo.series import ConfidenceDecay, apply_confidence, trim_history

logger = logging.getLogger("pv_openmeteo")


class PvOpenMeteoDaemon(HelperDaemon):
    """Daemon that fetches PV forecasts from Open-Meteo on demand.

    Subscribes to the configured trigger topic. On each trigger it fetches a
    forecast for every configured array and publishes it. One array is one
    library call, however many planes it contains.

    When Open-Meteo returns a rate-limit response, ``_run_cycle`` returns a
    reset time and the base class suppresses all further triggers until it has
    passed.
    """

    TOOL_NAME = "pv_open_meteo"

    def _publish_discovery(self) -> None:
        """Publish HA discovery for the trigger button and one sensor per array.

        The base implementation reads a single top-level ``output_topic`` to
        build the forecast sensor. This helper has no such field: every array
        publishes to its own topic, so a single sensor cannot represent them.
        The button and the stats sensors come from the base class; this method
        adds one forecast sensor per array afterwards, grouped under the same
        HA device, the way the pv_ml_learner helper does it.

        Publishing is idempotent, so the base class may call this on every
        reconnect and on every HA birth message.
        """
        super()._publish_discovery()
        ha = self._ha_config()
        if ha is None or not ha.forecast_sensor:
            return
        for name, array in self._config.arrays.items():
            publish_trigger_discovery(
                self._client,
                tool_name=f"{self.TOOL_NAME}_{name}",
                tool_label=name.replace("_", " ").title(),
                # No button: the array shares the device with the trigger
                # button the base class already published.
                trigger_topic=None,
                forecast_sensor=True,
                output_topic=array.output_topic,
                forecast_value_template=self.FORECAST_VALUE_TEMPLATE,
                forecast_unit=self.FORECAST_UNIT,
                forecast_device_class=self.FORECAST_DEVICE_CLASS,
                forecast_attributes_template=self.FORECAST_ATTRIBUTES_TEMPLATE,
                device_id=self.TOOL_NAME,
                device_label=self._tool_label(),
                discovery_prefix=ha.discovery_prefix,
            )

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Fetch every configured array and publish its forecast.

        Arrays are fetched in order. A failure on one array is logged and the
        rest still run, so a transient error on one roof does not leave the
        solver without a forecast for the other. A rate-limit response aborts
        the whole cycle: the limit is per client, so the remaining arrays would
        only collect more of the same.

        Args:
            client: The connected paho MQTT client.

        Returns:
            ``None`` when nothing was published. A ``CycleResult`` carrying the
            published horizon on success, or ``suppress_until`` when the cycle
            was aborted by a rate-limit response.
        """
        config: PvOpenMeteoConfig = self._config
        fetch_time = datetime.now(tz=timezone.utc)
        decay = ConfidenceDecay(
            hours_0_to_6=config.confidence_decay.hours_0_to_6,
            hours_6_to_24=config.confidence_decay.hours_6_to_24,
            hours_24_to_48=config.confidence_decay.hours_24_to_48,
            hours_48_plus=config.confidence_decay.hours_48_plus,
        )

        published = 0
        horizon_hours: list[float] = []
        for name, array in config.arrays.items():
            try:
                series = asyncio.run(fetch_array(array=array, api=config.open_meteo))
            except RatelimitError as exc:
                logger.warning(
                    "Open-Meteo rate limit exceeded; aborting fetch cycle. "
                    "Requests resume at %s UTC.",
                    exc.reset_at.strftime("%H:%M:%S"),
                )
                return CycleResult(suppress_until=exc.reset_at)
            except FetchError:
                logger.error(
                    "Failed to fetch forecast for array %r:\n%s",
                    name,
                    traceback.format_exc(),
                )
                continue

            series = trim_history(
                series, now=fetch_time, past_hours=config.open_meteo.past_hours
            )
            steps = apply_confidence(series, fetch_time, decay)
            if not steps:
                # Everything the API returned lies before the retention window.
                # Publishing an empty list would write "[]" to the retained
                # topic, which mimirheim rejects, leaving the array with no
                # usable forecast at all. Leave the previous payload in place.
                logger.warning(
                    "Skipping publish for array %r: no forecast steps within the horizon.",
                    name,
                )
                continue

            # Report how far the forecast reaches beyond now, not how many
            # steps were published: the payload also carries up to past_hours
            # of already-elapsed forecast, which is of no use to the solver.
            ahead_hours = max(
                0.0,
                (datetime.fromisoformat(steps[-1]["ts"]) - fetch_time).total_seconds() / 3600,
            )
            peak = max(steps, key=lambda step: step["kw"])
            logger.info(
                "Array %r: publishing %d steps, %.1f h ahead. Peak %.3f kW at %s.",
                name,
                len(steps),
                ahead_hours,
                peak["kw"],
                peak["ts"],
            )
            publish_forecast(client, array.output_topic, steps)
            published += 1
            horizon_hours.append(ahead_hours)

        if published == 0:
            return None

        if config.signal_mimir:
            publish_trigger(client, config.mimir_trigger_topic)
            logger.info("Published mimirheim trigger to %s", config.mimir_trigger_topic)

        # The solver can only plan as far as its shortest input reaches, so the
        # weakest array is the honest figure to report. Taking the maximum
        # would claim coverage the solve does not have whenever one array
        # returns a shorter series than the others.
        return CycleResult(horizon_hours=min(horizon_hours))


def main() -> None:
    """Parse arguments, load config, and start the Open-Meteo PV fetcher daemon."""
    parser = argparse.ArgumentParser(
        description=(
            "mimirheim Open-Meteo PV forecast fetcher - fetch solar forecasts "
            "and publish them to MQTT."
        ),
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Path to the YAML configuration file.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    PvOpenMeteoDaemon(load_helper_config(args.config, PvOpenMeteoConfig, logger)).run()


if __name__ == "__main__":
    main()
