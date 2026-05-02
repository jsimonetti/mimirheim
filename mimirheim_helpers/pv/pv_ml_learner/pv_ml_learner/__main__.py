"""Entry point and main daemon class for the mimirheim pv_ml_learner tool.

``PvLearnerDaemon`` subscribes to two MQTT topics at startup and dispatches
to one of two cycles when a message arrives:

- **Training cycle** (triggered by ``config.training.train_trigger_topic``):
  Ingests new HA PV actuals for every configured array, then fetches new KNMI
  observations for the timeframe not yet stored. Builds per-array training
  datasets and retrains XGBoost models when enough calendar months of data are
  available. On success, immediately runs the inference cycle to publish fresh
  forecasts.

- **Inference cycle** (triggered by ``config.training.inference_trigger_topic``):
  Fetches a fresh Meteoserver weather forecast, generates per-hour PV output
  predictions for every configured array, and publishes retained MQTT payloads.

Scheduling (cron, Home Assistant automation, Node-RED, etc.) is the
responsibility of the caller. The daemon itself does not schedule anything.

What this module does not do:
- It does not implement fetching, training, or publishing logic; those are
  delegated to the respective modules.
- It does not read or write the Home Assistant database directly; that is
  ``ha_actuals``'s responsibility.
"""

from __future__ import annotations

import argparse
import logging
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import sqlalchemy as sa

from helper_common.cycle import CycleResult
from helper_common.daemon import MqttDaemon
from helper_common.discovery import publish_trigger_discovery

from pv_ml_learner.config import ArrayConfig, PvLearnerConfig, load_config
from pv_ml_learner.dataset_builder import build_training_rows
from pv_ml_learner.ha_actuals import build_ha_engine, compute_hourly_kwh
from pv_ml_learner.knmi_fetcher import FetchError as KnmiFetchError
from pv_ml_learner.knmi_fetcher import fetch_knmi_hours
from pv_ml_learner.meteoserver_fetcher import FetchError as McFetchError
from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast
from pv_ml_learner.predictor import ModelNotReadyError, predict_forecast
from pv_ml_learner.publisher import publish_forecast
from pv_ml_learner.storage import (
    create_schema,
    get_earliest_actuals_ts,
    get_knmi_range,
    get_latest_actuals_ts,
    get_latest_knmi_ts,
    get_latest_meteoserver_fetch,
    get_pv_actuals_range,
    insert_meteoserver_fetch,
    prune_meteoserver,
    upsert_knmi_hours,
    upsert_pv_actuals,
)
from pv_ml_learner.trainer import InsufficientDataError, train_model

logger = logging.getLogger("pv_ml_learner")

# Home Assistant publishes "online" retained to this topic on startup and on
# MQTT integration reload. Subscribing allows the daemon to re-publish its
# discovery payloads so HA restores the trigger buttons without a full restart.
_HA_STATUS_TOPIC = "homeassistant/status"


def _ingest_pv_actuals_from_ha(
    array_name: str,
    array_cfg: ArrayConfig,
    ha_db_url: str,
    start_ts: int,
) -> list:
    """Read new PV actuals for one array from the HA database.

    Extracted as a module-level function to make ``run_training_cycle``
    testable without a real HA database file.

    Args:
        array_cfg: Configuration for the array being ingested (provides
            entity IDs and the exclusion sensor list).
        ha_db_url: SQLAlchemy connection URL for the Home Assistant recorder
            database (e.g. ``sqlite:////config/home-assistant_v2.db``).
        start_ts: Return only hours strictly after this timestamp.

    Returns:
        List of ``PvActualRow`` objects with ``array_name`` set to
        ``array_name``.
    """
    ha_engine = build_ha_engine(ha_db_url)
    with ha_engine.connect() as ha_conn:
        return compute_hourly_kwh(
            ha_conn,
            entity_ids=array_cfg.sum_entity_ids,
            start_ts=start_ts,
            exclude_limiting_entity_ids=array_cfg.exclude_limiting_entity_ids or None,
            array_name=array_name,
        )


# KNMI data is typically published 24–48 hours after the observation time.
# Requesting data up to this offset before "now" avoids requesting hours that
# have not yet been published by KNMI.
_KNMI_PUBLICATION_DELAY_SECONDS = 48 * 3600

# How far back to fetch KNMI history on the very first run (2 years).
_KNMI_INITIAL_LOOKBACK_SECONDS = 2 * 365 * 24 * 3600

# The KNMI hourly data API silently truncates responses at roughly 10 000 rows.
# With ~50 % of hours being night (Q == -1, dropped), the effective cap is about
# 5 000–8 000 usable rows — roughly one year of data. Chunking by this many
# seconds ensures each API call stays well within the limit.
_KNMI_CHUNK_SECONDS = 365 * 24 * 3600  # one year per request


def _fetch_knmi_chunked(
    station_id: int,
    start_ts: int,
    end_ts: int,
) -> list:
    """Fetch KNMI observations in year-sized chunks to stay within the API row limit.

    The KNMI hourly data API silently truncates responses when the requested
    range produces more than roughly 10 000 rows. A multi-year fetch without
    chunking therefore returns an incomplete dataset without any error or
    warning. This function splits the range into one-year slices and
    concatenates the results.

    Args:
        station_id: KNMI station number (e.g. 260 for De Bilt).
        start_ts: Start of the window as a UTC Unix timestamp (inclusive).
        end_ts: End of the window as a UTC Unix timestamp (inclusive).

    Returns:
        Combined list of ``KnmiRow`` objects from all chunks, sorted by
        ``hour_utc`` ascending.

    Raises:
        FetchError: If any individual chunk fetch fails.
    """
    all_rows = []
    chunk_start = start_ts
    while chunk_start < end_ts:
        chunk_end = min(chunk_start + _KNMI_CHUNK_SECONDS - 1, end_ts)
        rows = fetch_knmi_hours(station_id, chunk_start, chunk_end)
        all_rows.extend(rows)
        logger.debug(
            "KNMI chunk %d–%d: fetched %d rows.", chunk_start, chunk_end, len(rows)
        )
        chunk_start = chunk_end + 1
    return all_rows


class PvLearnerDaemon(MqttDaemon):
    """Main daemon orchestrating training, inference, and MQTT publishing.

    Subclasses ``MqttDaemon`` for MQTT lifecycle management (client
    construction, connect/disconnect, signal handling, ``_publish_stats``).
    Unlike the single-trigger ``HelperDaemon``, this daemon subscribes to two
    independent trigger topics and dispatches to two different cycles.

    Attributes:
        _config: Validated static configuration.
        _engine: SQLAlchemy engine for the pv_ml_learner SQLite database.
        _last_trigger_at: Monotonic timestamp of the last accepted trigger,
            used for per-topic debounce.
    """

    def __init__(self, config: PvLearnerConfig) -> None:
        # SQLite engine must be ready before MqttDaemon.__init__ constructs the
        # paho client (which calls _on_connect on the network thread).
        Path(config.storage.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._engine = sa.create_engine(
            f"sqlite:///{config.storage.db_path}",
            connect_args={"check_same_thread": False},
        )
        with self._engine.begin() as conn:
            create_schema(conn)

        # Per-topic debounce: keyed by topic string.
        self._last_trigger_at: dict[str, float] = {}

        super().__init__(config)
        logger.info("Storage schema ready at %s.", config.storage.db_path)

    # ------------------------------------------------------------------
    # MqttDaemon overrides
    # ------------------------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        super()._on_connect(client, userdata, flags, reason_code, properties)
        if reason_code != 0:
            return
        cfg = self._config
        client.subscribe(cfg.training.train_trigger_topic, qos=1)
        client.subscribe(cfg.training.inference_trigger_topic, qos=1)
        client.subscribe(_HA_STATUS_TOPIC, qos=1)
        logger.info(
            "Subscribed to train topic %s and inference topic %s.",
            cfg.training.train_trigger_topic,
            cfg.training.inference_trigger_topic,
        )
        self._publish_discovery(client)

        # Startup: attempt training for any array that has no model yet.
        missing_models = [
            (name, a) for name, a in cfg.arrays.items() if not Path(a.model_path).exists()
        ]
        if missing_models:
            names = [name for name, _ in missing_models]
            logger.info(
                "No trained model found for array(s) %s; attempting immediate training.",
                names,
            )
            try:
                self.run_training_cycle(client)
            except Exception:
                logger.error("Startup training failed.\n%s", traceback.format_exc())

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: Any,
    ) -> None:
        topic = message.topic

        # HA birth message must be checked before the retain guard because
        # the birth message itself is retained.
        if topic == _HA_STATUS_TOPIC:
            if message.payload == b"online":
                logger.info("Received HA birth message; re-publishing discovery.")
                self._publish_discovery(client)
            return

        # Drop retained messages (broker replays them on every re-subscribe).
        if message.retain:
            logger.debug("Ignoring retained message on %r.", topic)
            return

        # Per-topic debounce: two triggers within 5 s on the same topic are
        # collapsed into one to avoid double-firing when HA resends rapidly.
        now_mono = time.monotonic()
        last = self._last_trigger_at.get(topic)
        if last is not None and now_mono - last < 5.0:
            logger.debug("Trigger debounced (%.1f s since last) on %r.", now_mono - last, topic)
            return
        self._last_trigger_at[topic] = now_mono

        cfg = self._config
        start_ts = datetime.now(tz=timezone.utc)
        start_mono = now_mono

        if topic == cfg.training.train_trigger_topic:
            logger.info("Received training trigger on %s; running training cycle.", topic)
            success = False
            horizon_hours: float | None = None
            try:
                n_steps = self.run_training_cycle(client)
                success = True
                if n_steps is not None:
                    horizon_hours = float(n_steps)
            except Exception:
                logger.error("Training cycle failed.\n%s", traceback.format_exc())
            finally:
                self._publish_stats(
                    start_ts,
                    time.monotonic() - start_mono,
                    CycleResult(success=success, horizon_hours=horizon_hours),
                )
        elif topic == cfg.training.inference_trigger_topic:
            logger.info("Received inference trigger on %s; running inference cycle.", topic)
            success = False
            horizon_hours: float | None = None
            try:
                n_steps = self.run_inference_cycle(client)
                success = True
                if n_steps is not None:
                    horizon_hours = float(n_steps)
            except Exception:
                logger.error("Inference cycle failed.\n%s", traceback.format_exc())
            finally:
                self._publish_stats(
                    start_ts,
                    time.monotonic() - start_mono,
                    CycleResult(success=success, horizon_hours=horizon_hours),
                )
        else:
            logger.warning("Received message on unexpected topic %s; ignoring.", topic)

    # ------------------------------------------------------------------
    # HA discovery
    # ------------------------------------------------------------------

    def _publish_discovery(self, client: mqtt.Client) -> None:
        """Publish retained HA MQTT discovery payloads for both trigger buttons.

        Two button entities are created: one that triggers a training cycle and
        one that triggers an inference cycle. Both belong to the same HA device.
        Publishing is idempotent; calling this on every broker (re-)connect or
        on receiving an HA birth message is safe.

        Args:
            client: The connected paho MQTT client.
        """
        cfg = self._config
        ha = cfg.ha_discovery
        if not ha.enabled:
            return
        prefix = ha.discovery_prefix
        label = ha.device_name
        publish_trigger_discovery(
            client,
            tool_name="pv_ml_learner_train",
            tool_label=f"{label} Train",
            trigger_topic=cfg.training.train_trigger_topic,
            stats_topic=cfg.stats_topic,
            discovery_prefix=prefix,
        )
        publish_trigger_discovery(
            client,
            tool_name="pv_ml_learner_infer",
            tool_label=f"{label} Infer",
            trigger_topic=cfg.training.inference_trigger_topic,
            stats_topic=cfg.stats_topic,
            discovery_prefix=prefix,
        )
        logger.debug("Published HA discovery for train and infer trigger buttons.")

    # ------------------------------------------------------------------
    # Training cycle
    # ------------------------------------------------------------------

    def run_training_cycle(self, client: mqtt.Client) -> int | None:
        """Ingest new HA actuals and KNMI observations, then retrain all arrays.

        Steps:

        1. Ingest new HA PV actuals for every configured array. This must happen
           before the KNMI step so that the KNMI fetch start can be aligned to
           the actual PV data history rather than an arbitrary lookback window.
        2. Fetch KNMI observations for the range not yet stored. The lower bound
           is determined by what is already held (never backfills). On a first
           run the lower bound is aligned to the earliest PV actual in the
           database.
        3. Retrain each array using the combined data now in local storage.
        4. Run the inference cycle to publish fresh forecasts.

        If the dataset for an array spans fewer calendar months than
        ``config.training.min_months_required``, that array's training step is
        skipped and its current model (if any) is left unchanged.

        Args:
            client: Connected paho MQTT client, passed through to the
                inference cycle for publishing.

        Returns:
            The number of hourly forecast steps from the inference cycle run
            at the end of training, or ``None`` if no array produced a forecast.
        """
        cfg = self._config
        now_ts = int(time.time())

        # -----------------------------------------------------------------
        # 1. Ingest new HA PV actuals for every configured array.
        #    Must happen before the KNMI step so the KNMI fetch start can
        #    be aligned to the PV actuals history.
        # -----------------------------------------------------------------
        for array_name, array_cfg in cfg.arrays.items():
            self._ingest_array_actuals(array_name, array_cfg)

        # -----------------------------------------------------------------
        # 2. Fetch KNMI observations for the range not yet stored.
        #    Lower bound: the hour immediately after the latest KNMI row we
        #    already hold — never backfill or re-fetch existing data.
        #    On a first run (no KNMI stored yet), start from the earliest PV
        #    actual so that KNMI coverage aligns with the PV history.
        #    Upper bound: now minus the KNMI publication delay.
        # -----------------------------------------------------------------
        try:
            with self._engine.connect() as conn:
                latest_knmi = get_latest_knmi_ts(conn)
                earliest_pv_ts = get_earliest_actuals_ts(conn)

            if latest_knmi is not None:
                # Extend forward only — never go back before what we already have.
                knmi_start = latest_knmi + 3600
            elif earliest_pv_ts is not None:
                # First run: align KNMI history to the start of PV actuals.
                knmi_start = earliest_pv_ts
            else:
                # No PV data at all yet; fall back to the standard lookback.
                knmi_start = now_ts - _KNMI_INITIAL_LOOKBACK_SECONDS

            knmi_end = now_ts - _KNMI_PUBLICATION_DELAY_SECONDS

            if knmi_start < knmi_end:
                logger.info(
                    "Fetching KNMI observations for station %d from %d to %d.",
                    cfg.knmi.station_id,
                    knmi_start,
                    knmi_end,
                )
                knmi_rows = _fetch_knmi_chunked(cfg.knmi.station_id, knmi_start, knmi_end)
                with self._engine.begin() as conn:
                    n = upsert_knmi_hours(conn, knmi_rows)
                logger.info("Upserted %d KNMI rows.", n)
            else:
                logger.info("KNMI data is already up to date.")
        except KnmiFetchError:
            logger.error(
                "KNMI fetch failed; continuing with existing data.\n%s",
                traceback.format_exc(),
            )

        # -----------------------------------------------------------------
        # 3. Retrain each array using the combined KNMI + PV actuals now in
        #    the database.
        # -----------------------------------------------------------------
        for array_name, array_cfg in cfg.arrays.items():
            self._retrain_array(array_name, array_cfg, now_ts)

        # -----------------------------------------------------------------
        # 4. Publish fresh forecasts using the new (or unchanged) models.
        # -----------------------------------------------------------------
        return self.run_inference_cycle(client)

    def _ingest_array_actuals(self, array_name: str, array_cfg: ArrayConfig) -> None:
        """Ingest new PV actuals from HA for a single array into local storage.

        Reads only hours not yet stored (those after the latest known actuals
        timestamp) and upserts the results. Errors are caught and logged; the
        daemon continues so that the remaining arrays are not affected.

        Args:
            array_name: The array's key from the ``arrays`` map.
            array_cfg: Configuration for the array to ingest.
        """
        cfg = self._config
        try:
            with self._engine.connect() as conn:
                latest_actuals = get_latest_actuals_ts(conn, array_name)
            start_ts = latest_actuals if latest_actuals is not None else 0

            pv_rows = _ingest_pv_actuals_from_ha(
                array_name, array_cfg, cfg.homeassistant.db_url, start_ts
            )
            with self._engine.begin() as conn:
                n = upsert_pv_actuals(conn, pv_rows)
            logger.info("Array %s: upserted %d PV actual rows.", array_name, n)
        except Exception:
            logger.error(
                "Array %s: HA actuals ingest failed; continuing with existing data.\n%s",
                array_name,
                traceback.format_exc(),
            )

    def _retrain_array(self, array_name: str, array_cfg: ArrayConfig, now_ts: int) -> None:
        """Retrain the model for a single array using data already in local storage.

        Queries the full KNMI and PV actuals history from the database (no I/O),
        builds training rows, and calls ``train_model``. If fewer calendar months
        than ``config.training.min_months_required`` are available, training is
        skipped and the existing model file (if any) is left unchanged.

        Args:
            array_name: The array's key from the ``arrays`` map.
            array_cfg: Configuration for the array to retrain.
            now_ts: Current Unix timestamp used as the upper bound for data queries.
        """
        cfg = self._config
        try:
            with self._engine.connect() as conn:
                knmi_all = get_knmi_range(conn, 0, now_ts)
                pv_all = get_pv_actuals_range(conn, array_name, 0, now_ts)

            training_rows = build_training_rows(knmi_all, pv_all)
            logger.info(
                "Array %s: built %d training rows from %d KNMI + %d PV actuals.",
                array_name,
                len(training_rows),
                len(knmi_all),
                len(pv_all),
            )

            train_model(
                training_rows,
                cfg.training,
                array_cfg.model_path,
                array_cfg.metadata_path,
            )
            logger.info("Array %s: model training complete.", array_name)

        except InsufficientDataError as exc:
            logger.warning("Array %s: skipping training: %s", array_name, exc)
        except Exception:
            logger.error(
                "Array %s: training failed.\n%s",
                array_name,
                traceback.format_exc(),
            )

    # ------------------------------------------------------------------
    # Inference cycle
    # ------------------------------------------------------------------

    def run_inference_cycle(self, client: mqtt.Client) -> int | None:
        """Fetch a fresh Meteoserver forecast, predict for all arrays, and publish.

        Fetches one Meteoserver forecast (shared across all arrays), then for
        each configured array: loads the trained model, generates per-hour
        predictions, and publishes to the array's configured output topic.

        Skips publishing for any array whose model is not ready, but continues
        with remaining arrays.

        Args:
            client: Connected paho MQTT client used to publish each forecast.

        Returns:
            The number of hourly forecast steps published by the array with the
            most steps, or ``None`` if no array produced a forecast (e.g. all
            models not yet trained).
        """
        cfg = self._config

        try:
            mc_rows = fetch_meteoserver_forecast(
                api_key=cfg.meteoserver.api_key,
                latitude=cfg.meteoserver.latitude,
                longitude=cfg.meteoserver.longitude,
                horizon_hours=cfg.meteoserver.forecast_horizon_hours,
            )
        except McFetchError:
            logger.error(
                "Meteoserver fetch failed; skipping this inference cycle.\n%s",
                traceback.format_exc(),
            )
            return

        fetch_ts = int(time.time())
        with self._engine.begin() as conn:
            insert_meteoserver_fetch(conn, fetch_ts, mc_rows)
            prune_meteoserver(conn)

        max_steps: int | None = None
        for array_name, array_cfg in cfg.arrays.items():
            n = self._run_array_inference(client, array_name, array_cfg, mc_rows)
            if n is not None and (max_steps is None or n > max_steps):
                max_steps = n

        # Optionally signal mimirheim that all forecasts have been published.
        if cfg.signal_mimir and cfg.mimir_trigger_topic:
            client.publish(cfg.mimir_trigger_topic, payload="", retain=False)
            logger.info("Published mimirheim trigger to %s.", cfg.mimir_trigger_topic)

        return max_steps

    def _run_array_inference(
        self,
        client: mqtt.Client,
        array_name: str,
        array_cfg: ArrayConfig,
        mc_rows: list,
    ) -> int | None:
        """Run inference and publish the forecast for a single array.

        Args:
            client: Connected paho MQTT client.
            array_name: The array's key from the ``arrays`` map.
            array_cfg: Configuration for the array to predict and publish.
            mc_rows: Meteoserver forecast rows fetched this cycle.

        Returns:
            Number of hourly forecast steps published, or ``None`` if the
            model is not ready or inference failed.
        """
        cfg = self._config

        try:
            steps = predict_forecast(
                mc_rows,
                array_cfg.model_path,
                array_cfg.metadata_path,
                peak_power_kwp=array_cfg.peak_power_kwp,
            )
        except ModelNotReadyError as exc:
            logger.warning(
                "Array %s: model not ready; skipping forecast publish. %s",
                array_name,
                exc,
            )
            return None
        except Exception:
            logger.error(
                "Array %s: inference failed.\n%s",
                array_name,
                traceback.format_exc(),
            )
            return None

        publish_forecast(
            client,
            array_cfg.output_topic,
            steps,
            signal_mimir=False,  # mimirheim signal is sent once per cycle after all arrays
            mimir_trigger_topic=None,
        )
        logger.info(
            "Array %s: published %d forecast steps to %s.",
            array_name,
            len(steps),
            array_cfg.output_topic,
        )
        return len(steps)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    # run() is inherited from MqttDaemon. It connects the client, starts the
    # network loop, and blocks until SIGTERM/SIGINT. Startup training (for
    # arrays with no model) is triggered from _on_connect.


def main() -> None:
    """Parse CLI arguments, load config, and start the daemon."""
    parser = argparse.ArgumentParser(
        description=(
            "mimirheim PV ML learner — train an XGBoost PV forecast model on KNMI "
            "and HA data, and publish forecasts via MQTT."
        )
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
    # httpx logs full request URLs at INFO level, which would expose the
    # Meteoserver API key in log files.  Suppress it to WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    PvLearnerDaemon(load_config(args.config)).run()


if __name__ == "__main__":
    main()

