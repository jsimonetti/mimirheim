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
import sys
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

from helper_common.config import apply_mqtt_env_overrides
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon

from zonneplan_prices.api import AuthError, FetchError, ZonneplanClient
from zonneplan_prices.auth import attempt_auth
from zonneplan_prices.config import ZonneplanPricesConfig
from zonneplan_prices.fetcher import fetch_prices
from zonneplan_prices.publisher import publish_prices
from zonneplan_prices.token import is_token_valid, load_token, save_token

logger = logging.getLogger(__name__)


def _load_config(path: str) -> ZonneplanPricesConfig:
    """Load and validate the YAML configuration file.

    Args:
        path: Filesystem path to the config.yaml file.

    Returns:
        Validated ZonneplanPricesConfig instance.

    Raises:
        SystemExit: If the file cannot be read or fails validation.
    """
    try:
        raw = yaml.safe_load(Path(path).read_text())
        apply_mqtt_env_overrides(raw)
        return ZonneplanPricesConfig.model_validate(raw)
    except Exception:
        logger.exception("Failed to load configuration from %s", path)
        sys.exit(1)


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

    # The electricity connection UUID is discovered on the first successful
    # fetch and cached here for all subsequent cycles. The UUID is stable for
    # the lifetime of the daemon — it identifies the account's electricity
    # contract and never changes at runtime.
    _connection_uuid: str | None = None

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        """Fetch current Zonneplan prices and publish them.

        On auth or fetch failure the error is logged and the existing retained
        payload on the output topic is left unchanged. The daemon never crashes;
        it returns None and waits for the next trigger.

        Args:
            client: Connected paho MQTT client.

        Returns:
            CycleResult with the number of price steps as horizon_hours, or
            None if the cycle did not complete successfully.
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

        # --- Step 4: Resolve connection UUID (discovered once, then cached). ---
        api_client = ZonneplanClient(access_token=token["access_token"])
        if self._connection_uuid is None:
            try:
                self._connection_uuid = api_client.get_connection_uuid()
                logger.info("Discovered Zonneplan connection UUID: %s", self._connection_uuid)
            except FetchError:
                logger.exception(
                    "Failed to discover Zonneplan connection UUID — retaining "
                    "existing payload on %s",
                    output_topic,
                )
                return None
        connection_uuid = self._connection_uuid

        # --- Step 5: Fetch prices. ---
        try:
            steps = fetch_prices(
                client=api_client,
                connection_uuid=connection_uuid,
                import_formula=zp_config.import_formula,
                export_formula=zp_config.export_formula,
            )
        except (FetchError, AuthError):
            logger.exception(
                "Zonneplan price fetch failed — retaining existing payload on %s",
                output_topic,
            )
            return None

        # --- Step 6: Publish. ---
        publish_prices(
            client,
            output_topic,
            steps,
            signal_mimir=self._config.signal_mimir,
            mimir_trigger_topic=self._config.mimir_trigger_topic,
        )
        return CycleResult(horizon_hours=len(steps))


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

    ZonneplanPricesDaemon(_load_config(args.config)).run()


if __name__ == "__main__":
    main()
