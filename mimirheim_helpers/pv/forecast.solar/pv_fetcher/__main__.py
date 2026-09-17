"""Entry point for the mimirheim PV forecast.solar fetcher daemon.

This module implements ``PvFetcherDaemon``, a subclass of ``HelperDaemon``
that fetches solar power forecasts from forecast.solar on each trigger message
and publishes them to the configured per-array output topics.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, 5-second debounce, rate-limit
suppression, and signal handling.

What this module does not do:
- It does not implement fetch logic — that is fetcher.py's responsibility.
- It does not format payloads — that is publisher.py's responsibility.
- It does not compute confidence — that is confidence.py's responsibility.

``PvFetcherDaemon`` also opts into the Config Service protocol via
``helper_common.config_owner.ConfigOwnerSupport``, so its configuration is
discoverable, renderable, and editable in the same running Config Editor as
mimirheim core's (config-editor-v3 ticket 06). This is additive: it changes
nothing about the fetch-and-publish behavior above.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from helper_common.config import load_helper_config
from helper_common.config_owner import ConfigOwnerSupport
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon
from helper_common.publish import publish_checked

from pv_fetcher.config import PvFetcherConfig
from pv_fetcher.confidence import ConfidenceDecay, apply_confidence
from pv_fetcher.fetcher import FetchError, RatelimitError, fetch_array
from pv_fetcher.formspec import PV_FETCHER_CONFIG_FORM_SPEC
from pv_fetcher.publisher import publish_array

logger = logging.getLogger("pv_fetcher")

# Stable regardless of user configuration; the Config Editor needs a fixed
# identity for this tool across restarts and reconfiguration.
CONFIG_OWNER_ID = "pv_forecast_solar"
CONFIG_OWNER_DISPLAY_NAME = "PV forecast (forecast.solar)"


class PvFetcherDaemon(HelperDaemon):
    """Daemon that fetches PV forecasts from forecast.solar on demand.

    Subscribes to the configured trigger topic. On each trigger, fetches
    forecasts for every configured array and publishes them to their respective
    output topics.

    When forecast.solar returns a 429 rate-limit response, ``_run_cycle``
    returns the API reset time. The base class stores this and suppresses
    all further triggers until that UTC time has passed.
    """

    TOOL_NAME = "pv_forecast_solar"

    def __init__(self, config: PvFetcherConfig, config_path: Path) -> None:
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
            model=PvFetcherConfig,
            form_spec=PV_FETCHER_CONFIG_FORM_SPEC,
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
        """Fetch all configured PV arrays and publish their forecasts.

        Runs each array fetch sequentially. A fetch failure for one array is
        logged and skipped without aborting the remaining arrays. If the API
        returns a rate-limit response, the entire cycle is aborted immediately
        to avoid wasting the limited request budget.

        Args:
            client: The connected paho MQTT client.

        Returns:
            ``None`` on success. A ``CycleResult`` with ``suppress_until`` set
            to the UTC reset time when the cycle aborts due to a rate-limit
            response. The base class uses this value to suppress triggers until
            the limit resets.
        """
        config: PvFetcherConfig = self._config
        fetch_time = datetime.now(tz=timezone.utc)
        decay = ConfidenceDecay(
            hours_0_to_6=config.confidence_decay.hours_0_to_6,
            hours_6_to_24=config.confidence_decay.hours_6_to_24,
            hours_24_to_48=config.confidence_decay.hours_24_to_48,
            hours_48_plus=config.confidence_decay.hours_48_plus,
        )

        any_success = False
        max_steps = 0
        for name, array_cfg in config.arrays.items():
            try:
                watts = asyncio.run(
                    fetch_array(
                        api_key=config.forecast_solar.api_key,
                        latitude=array_cfg.latitude,
                        longitude=array_cfg.longitude,
                        declination=array_cfg.declination,
                        azimuth=array_cfg.azimuth,
                        kwp=array_cfg.peak_power_kwp,
                    )
                )
            except RatelimitError as exc:
                # The rate limit is shared across all arrays. Attempting further
                # arrays would produce more 429 responses. Abort and return the
                # reset time so the base class suppresses future triggers.
                logger.warning(
                    "forecast.solar rate limit exceeded; aborting fetch cycle. "
                    "Rate limit resets at %s UTC.",
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

            nonzero = {ts: w for ts, w in watts.items() if w > 0}
            sorted_keys = sorted(watts)
            if len(sorted_keys) >= 2:
                # strict=False is deliberate: this pairs the list with its
                # own tail, so the second argument is always one shorter.
                inferred_step = min(
                    b - a
                    for a, b in zip(sorted_keys, sorted_keys[1:], strict=False)
                )
            else:
                inferred_step = None
            logger.info(
                "Array %r: received %d timestamps from API, %d non-zero. "
                "Raw span: %s to %s (tzinfo=%s). Inferred fill step: %s. "
                "First non-zero: %s, last non-zero: %s.",
                name,
                len(watts),
                len(nonzero),
                sorted_keys[0].strftime("%Y-%m-%dT%H:%M:%SZ") if sorted_keys else "none",
                sorted_keys[-1].strftime("%Y-%m-%dT%H:%M:%SZ") if sorted_keys else "none",
                sorted_keys[0].tzinfo if sorted_keys else "n/a",
                inferred_step,
                min(nonzero).strftime("%Y-%m-%dT%H:%MZ") if nonzero else "none",
                max(nonzero).strftime("%Y-%m-%dT%H:%MZ") if nonzero else "none",
            )

            steps = apply_confidence(watts, fetch_time, decay)
            if not steps:
                # forecast.solar returned no data for this array — most likely
                # a nighttime fetch where no daylight hours remain in the API's
                # horizon. Publishing an empty list would write "[]" as the
                # retained MQTT value, which mimirheim's parser rejects, leaving the
                # PV topic invalid. Skip publishing and leave the previous
                # retained forecast in place until a non-empty result arrives.
                logger.info(
                    "Skipping publish for array %r: forecast.solar returned no steps "
                    "(likely a nighttime fetch with no remaining daylight).",
                    name,
                )
                continue
            nonzero_steps = [s for s in steps if s["kw"] > 0]
            if nonzero_steps:
                peak = max(nonzero_steps, key=lambda s: s["kw"])
                logger.info(
                    "Array %r: publishing %d steps (%d non-zero). "
                    "Peak %.3f kW at %s.",
                    name,
                    len(steps),
                    len(nonzero_steps),
                    peak["kw"],
                    peak["ts"],
                )
            else:
                # An all-zero curve is a forecast of no production, which is
                # information the solver wants. This branch used to log the
                # line below and then skip the publish, leaving the earlier
                # non-zero forecast retained on the topic -- so after the last
                # daylight fetch mimirheim kept seeing PV that was not coming.
                # Distinct from the empty-result case above, which is an
                # absence of data rather than a forecast of zero.
                logger.info(
                    "Array %r: publishing %d steps, all zero kW.",
                    name,
                    len(steps),
                )
            publish_array(client, array_cfg.output_topic, steps, signal_mimir=False)
            any_success = True
            if len(steps) > max_steps:
                max_steps = len(steps)

        if any_success and config.signal_mimir:
            publish_checked(
                client,
                config.mimir_trigger_topic,
                b"",
                qos=0,
                retain=False,
                description="mimirheim solve trigger",
            )
            logger.info("Published mimirheim trigger to %s", config.mimir_trigger_topic)
        if any_success and max_steps > 0:
            return CycleResult(horizon_hours=max_steps * 0.25)
        return None


def main() -> None:
    """Parse arguments, load config, and start the PV forecast fetcher daemon."""
    parser = argparse.ArgumentParser(
        description="mimirheim PV forecast fetcher — fetch forecast.solar data and publish to MQTT.",
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

    config = load_helper_config(
        args.config,
        PvFetcherConfig,
        logger,
        owner_id=CONFIG_OWNER_ID,
        display_name=CONFIG_OWNER_DISPLAY_NAME,
        form_spec=PV_FETCHER_CONFIG_FORM_SPEC,
    )
    PvFetcherDaemon(config, Path(args.config)).run()


if __name__ == "__main__":
    main()


