"""Entry point for the Zonneplan prices daemon.

This module implements ``ZonneplanPricesDaemon``, a subclass of ``HelperDaemon``
that fetches hourly electricity prices from the Zonneplan API on each trigger
message and publishes them retained to the configured output topic.

The base class handles all MQTT boilerplate: TLS, authentication, trigger
subscription, HA MQTT discovery, retain guard, 5-second debounce, and signal
handling. This subclass is responsible only for the Zonneplan-specific fetch,
auth, and publish logic.

Authentication is handled entirely within ``_run_cycle``:

1. Load the token from disk.
2. If the token has expired, attempt a refresh via the refresh token.
3. If refresh fails (or no token exists), run ``attempt_auth`` to trigger the
   email OTP flow. The user must click a link in their inbox; no CLI or exec is
   required.
4. On successful authentication, fetch prices and publish.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import paho.mqtt.client as mqtt

from helper_common.config import load_helper_config
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon
from helper_common.discovery import PRICE_FORECAST_ATTRIBUTES_TEMPLATE

from zonneplan_prices.api import AuthError, FetchError, ZonneplanClient
from zonneplan_prices.auth import attempt_auth
from zonneplan_prices.config import ZonneplanPricesConfig
from zonneplan_prices.fetcher import fetch_prices
from zonneplan_prices.publisher import publish_prices
from zonneplan_prices.token import is_token_valid, load_token, save_token

# Named explicitly, not derived from __name__: this module runs as
# `python -m zonneplan_prices`, where __name__ is "__main__" and the records would
# not join the ones MqttDaemon emits under the package name.
logger = logging.getLogger("zonneplan_prices")


class ZonneplanPricesDaemon(HelperDaemon):
    """Daemon that fetches Zonneplan electricity prices on demand.

    Subscribes to the configured trigger topic. On each trigger, ensures a
    valid OAuth token is available (refreshing or re-authenticating as needed),
    fetches the current hourly prices, and publishes them retained to the
    configured output topic.

    The auth flow is fully self-contained: when no token file exists the daemon
    sends a login email automatically and waits for the user to click the link.
    No operator intervention beyond clicking the link is required.
    """

    TOOL_NAME = "zonneplan_prices"
    FORECAST_VALUE_TEMPLATE = "{{ value_json[0].import_eur_per_kwh | default(0) | round(4) }}"
    FORECAST_UNIT = "EUR/kWh"
    FORECAST_DEVICE_CLASS = None
    FORECAST_ATTRIBUTES_TEMPLATE = PRICE_FORECAST_ATTRIBUTES_TEMPLATE

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Fetch current Zonneplan prices and publish them.

        On auth or fetch failure the error is logged and the existing retained
        payload on the output topic is left unchanged. The daemon never crashes;
        it returns None and waits for the next trigger.

        Args:
            client: Connected paho MQTT client.

        Returns:
            CycleResult with the fetched coverage (in hours) as horizon_hours,
            or None if the cycle did not complete successfully.
        """
        zp_config = self._config.zonneplan
        token_path = Path(zp_config.token_file)
        pending_path = token_path.with_stem(token_path.stem + "_pending")

        output_topic = self._config.output_topic or (
            f"{self._config.mimir_topic_prefix}/input/prices"
        )

        # --- Step 1: Load stored token. ---
        token = load_token(token_path)

        # --- Step 2: Refresh if stale. ---
        if token and not is_token_valid(token):
            logger.info("Zonneplan access token expired — attempting refresh.")
            api_client = ZonneplanClient(access_token=None)
            try:
                token = api_client.refresh_token(token["refresh_token"])
                save_token(token_path, token)
                logger.info("Token refreshed successfully.")
            except AuthError:
                logger.warning(
                    "Zonneplan token refresh failed — will re-authenticate."
                )
                token = None

        # --- Step 3: If still no valid token, run the OTP auth flow. ---
        if not token:
            if not zp_config.email:
                logger.error(
                    "No Zonneplan token found and no email address configured. "
                    "Set zonneplan.email in config.yaml to enable automatic "
                    "authentication."
                )
                return None
            api_client = ZonneplanClient(access_token=None)
            token = attempt_auth(
                client=api_client,
                email=zp_config.email,
                token_path=token_path,
                pending_path=pending_path,
            )
            if not token:
                # Still waiting for the user to click the activation link.
                return None

        # --- Step 4: Fetch prices. ---
        api_client = ZonneplanClient(access_token=token["access_token"])
        try:
            steps = fetch_prices(
                client=api_client,
                price_interval=zp_config.price_interval,
                import_formula=zp_config.import_formula,
                export_formula=zp_config.export_formula,
            )
        except (FetchError, AuthError):
            logger.exception(
                "Zonneplan price fetch failed — retaining existing payload on %s",
                output_topic,
            )
            return None

        # --- Step 5: Publish. ---
        publish_prices(
            client,
            output_topic,
            steps,
            signal_mimir=self._config.signal_mimir,
            mimir_trigger_topic=self._config.mimir_trigger_topic,
        )
        # Each step covers one hour in "hourly" mode or 15 minutes in
        # "quarter_hourly" mode. len(steps) alone is a step count, not hours —
        # it must be scaled by the step duration to report true horizon_hours.
        step_hours = 0.25 if zp_config.price_interval == "quarter_hourly" else 1.0
        return CycleResult(horizon_hours=len(steps) * step_hours)


def main() -> None:
    """Parse arguments, load config, and start the Zonneplan prices daemon."""
    parser = argparse.ArgumentParser(
        description="Zonneplan electricity price fetcher for mimirheim"
    )
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ZonneplanPricesDaemon(load_helper_config(args.config, ZonneplanPricesConfig, logger)).run()


if __name__ == "__main__":
    main()
