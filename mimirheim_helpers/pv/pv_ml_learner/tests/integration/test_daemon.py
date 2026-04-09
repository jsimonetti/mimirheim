"""Integration tests for pv_ml_learner.__main__.PvLearnerDaemon.

Each test instantiates the daemon with a real (in-memory) storage database
and a real (but tiny) model — all external I/O (KNMI, Meteoserver, HA) is
patched out. This validates the composition of the pipeline stages without
requiring a live broker or an active HA database.
"""

from __future__ import annotations

import calendar
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pv_ml_learner.config import (
    ArrayConfig,
    HaDiscoveryConfig,
    HomeAssistantConfig,
    HyperparamConfig,
    KnmiConfig,
    MeteoserverConfig,
    PvLearnerConfig,
    StorageConfig,
    TrainingConfig,
)
from pv_ml_learner.storage import KnmiRow, McRow, PvActualRow


def _ts(year: int, month: int, day: int, hour: int) -> int:
    return int(calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0)))


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _knmi_row(hour_utc: int, ghi: float = 200.0) -> KnmiRow:
    return KnmiRow(
        hour_utc=hour_utc, station_id=260, ghi_wm2=ghi,
        wind_ms=3.0, temp_c=12.0, rain_mm=0.5,
    )


def _mc_row(step_ts: int, ghi: float = 200.0) -> McRow:
    return McRow(
        step_ts=step_ts, ghi_wm2=ghi, temp_c=12.0, wind_ms=3.0,
        rain_mm=0.5, cloud_pct=40.0,
    )


def _pv_actual(
    hour_utc: int, kwh: float = 1.5, array_name: str = "main"
) -> PvActualRow:
    return PvActualRow(array_name=array_name, hour_utc=hour_utc, kwh=kwh)


# ---------------------------------------------------------------------------
# Synthetic data sets spanning enough calendar months to satisfy training
# ---------------------------------------------------------------------------


def _synthetic_knmi_rows(n_months: int = 3) -> list[KnmiRow]:
    rows = []
    for month in range(1, n_months + 1):
        for day in range(1, 4):
            for hour in range(8, 18):
                rows.append(_knmi_row(_ts(2024, month, day, hour)))
    return rows


def _synthetic_pv_rows(
    n_months: int = 3, array_name: str = "main"
) -> list[PvActualRow]:
    rows = []
    for month in range(1, n_months + 1):
        for day in range(1, 4):
            for hour in range(8, 18):
                rows.append(
                    _pv_actual(
                        _ts(2024, month, day, hour),
                        kwh=1.5 + hour * 0.05,
                        array_name=array_name,
                    )
                )
    return rows


def _synthetic_mc_rows(n: int = 48) -> list[McRow]:
    base = _ts(2024, 7, 1, 0)
    return [_mc_row(base + i * 3600) for i in range(n)]


# ---------------------------------------------------------------------------
# Config and daemon fixture helpers
# ---------------------------------------------------------------------------


def _fast_grid() -> HyperparamConfig:
    return HyperparamConfig(
        n_estimators=[10], max_depth=[3], learning_rate=[0.1],
        subsample=[1.0], min_child_weight=[1],
    )


def _make_config(
    tmp_path: Path, *, array_names: list[str] | None = None
) -> PvLearnerConfig:
    """Return a valid PvLearnerConfig backed by tmp_path model files."""
    if array_names is None:
        array_names = ["main"]

    from helper_common.config import MqttConfig as _MqttConfig

    arrays = [
        ArrayConfig(
            name=name,
            peak_power_kwp=5.0,
            output_topic=f"mimir/input/pv_forecast/{name}",
            sum_entity_ids=[f"sensor.pv_{name}_energy"],
            model_path=str(tmp_path / f"model_{name}.joblib"),
            metadata_path=str(tmp_path / f"metadata_{name}.json"),
        )
        for name in array_names
    ]

    return PvLearnerConfig(
        mqtt=_MqttConfig(host="localhost", port=1883, client_id="pv-learner"),
        signal_mimir=False,
        knmi=KnmiConfig(station_id=260),
        meteoserver=MeteoserverConfig(
            api_key="test-key", latitude=52.1, longitude=5.2,
            forecast_horizon_hours=48,
        ),
        homeassistant=HomeAssistantConfig(db_path="/config/home-assistant_v2.db"),
        arrays=arrays,
        storage=StorageConfig(db_path=str(tmp_path / "pv_ml_learner.db")),
        training=TrainingConfig(
            train_trigger_topic="mimir/input/tools/pv_ml_learner/train",
            inference_trigger_topic="mimir/input/tools/pv_ml_learner/infer",
            min_months_required=3,
            n_cv_splits=2,
            hyperparams=_fast_grid(),
        ),
        ha_discovery=HaDiscoveryConfig(enabled=False),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTrainingCycle:
    def test_training_creates_model_file(self, tmp_path: Path) -> None:
        """run_training_cycle writes a model file when enough data is present."""
        from pv_ml_learner.__main__ import PvLearnerDaemon

        cfg = _make_config(tmp_path)
        daemon = PvLearnerDaemon(cfg)
        client = MagicMock()

        knmi_rows = _synthetic_knmi_rows(n_months=3)
        pv_rows = _synthetic_pv_rows(n_months=3, array_name="main")
        mc_rows = _synthetic_mc_rows()

        with (
            patch("pv_ml_learner.__main__._fetch_knmi_chunked", return_value=knmi_rows),
            patch(
                "pv_ml_learner.__main__._ingest_pv_actuals_from_ha",
                return_value=pv_rows,
            ),
            patch(
                "pv_ml_learner.__main__.fetch_meteoserver_forecast",
                return_value=mc_rows,
            ),
            patch("pv_ml_learner.__main__.publish_forecast"),
        ):
            daemon.run_training_cycle(client)

        assert Path(cfg.arrays[0].model_path).exists()

    def test_insufficient_data_does_not_raise(self, tmp_path: Path) -> None:
        """run_training_cycle logs a warning but does not raise when data is sparse."""
        from pv_ml_learner.__main__ import PvLearnerDaemon

        cfg = _make_config(tmp_path)
        daemon = PvLearnerDaemon(cfg)
        client = MagicMock()

        # Only 1 month of data — threshold is 3.
        knmi_rows = _synthetic_knmi_rows(n_months=1)
        pv_rows = _synthetic_pv_rows(n_months=1, array_name="main")
        mc_rows = _synthetic_mc_rows()

        with (
            patch("pv_ml_learner.__main__._fetch_knmi_chunked", return_value=knmi_rows),
            patch(
                "pv_ml_learner.__main__._ingest_pv_actuals_from_ha",
                return_value=pv_rows,
            ),
            patch(
                "pv_ml_learner.__main__.fetch_meteoserver_forecast",
                return_value=mc_rows,
            ),
            patch("pv_ml_learner.__main__.publish_forecast"),
        ):
            daemon.run_training_cycle(client)

        # Model file must not exist — training was skipped due to insufficient data.
        assert not Path(cfg.arrays[0].model_path).exists()

    def test_multi_array_training_creates_all_model_files(
        self, tmp_path: Path
    ) -> None:
        """Each array gets its own model file written by run_training_cycle."""
        from pv_ml_learner.__main__ import PvLearnerDaemon

        cfg = _make_config(tmp_path, array_names=["east", "west"])
        daemon = PvLearnerDaemon(cfg)
        client = MagicMock()

        knmi_rows = _synthetic_knmi_rows(n_months=3)
        east_pv = _synthetic_pv_rows(n_months=3, array_name="east")
        west_pv = _synthetic_pv_rows(n_months=3, array_name="west")

        def _fake_ingest(array_cfg, ha_db_path, start_ts):
            return east_pv if array_cfg.name == "east" else west_pv

        mc_rows = _synthetic_mc_rows()

        with (
            patch("pv_ml_learner.__main__._fetch_knmi_chunked", return_value=knmi_rows),
            patch(
                "pv_ml_learner.__main__._ingest_pv_actuals_from_ha",
                side_effect=_fake_ingest,
            ),
            patch(
                "pv_ml_learner.__main__.fetch_meteoserver_forecast",
                return_value=mc_rows,
            ),
            patch("pv_ml_learner.__main__.publish_forecast"),
        ):
            daemon.run_training_cycle(client)

        for array_cfg in cfg.arrays:
            assert Path(array_cfg.model_path).exists(), (
                f"Model file missing for array {array_cfg.name}"
            )


class TestInferenceCycle:
    def test_inference_publishes_per_array(self, tmp_path: Path) -> None:
        """run_inference_cycle calls publish_forecast exactly once per array."""
        from pv_ml_learner.__main__ import PvLearnerDaemon
        from pv_ml_learner.dataset_builder import build_training_rows
        from pv_ml_learner.trainer import train_model

        cfg = _make_config(tmp_path, array_names=["east", "west"])
        daemon = PvLearnerDaemon(cfg)
        client = MagicMock()

        # Pre-train models for both arrays so inference can proceed.
        knmi_rows = _synthetic_knmi_rows(n_months=3)
        for array_cfg in cfg.arrays:
            pv_rows = _synthetic_pv_rows(n_months=3, array_name=array_cfg.name)
            training_rows = build_training_rows(knmi_rows, pv_rows)
            train_model(
                training_rows,
                cfg.training,
                array_cfg.model_path,
                array_cfg.metadata_path,
            )

        mc_rows = _synthetic_mc_rows()
        published_topics: list[str] = []

        def _fake_publish(client, topic, steps, **kwargs):
            published_topics.append(topic)

        with (
            patch(
                "pv_ml_learner.__main__.fetch_meteoserver_forecast",
                return_value=mc_rows,
            ),
            patch("pv_ml_learner.__main__.publish_forecast", side_effect=_fake_publish),
        ):
            daemon.run_inference_cycle(client)

        expected = {a.output_topic for a in cfg.arrays}
        assert set(published_topics) == expected

    def test_meteoserver_failure_skips_publishing(self, tmp_path: Path) -> None:
        """A Meteoserver fetch failure causes the inference cycle to return early."""
        from pv_ml_learner.__main__ import PvLearnerDaemon
        from pv_ml_learner.meteoserver_fetcher import FetchError

        cfg = _make_config(tmp_path)
        daemon = PvLearnerDaemon(cfg)
        client = MagicMock()

        with (
            patch(
                "pv_ml_learner.__main__.fetch_meteoserver_forecast",
                side_effect=FetchError("network error"),
            ),
            patch("pv_ml_learner.__main__.publish_forecast") as mock_pub,
        ):
            daemon.run_inference_cycle(client)

        mock_pub.assert_not_called()

    def test_model_not_ready_skips_one_array(self, tmp_path: Path) -> None:
        """An array with no model is skipped; other arrays still publish."""
        from pv_ml_learner.__main__ import PvLearnerDaemon
        from pv_ml_learner.dataset_builder import build_training_rows
        from pv_ml_learner.trainer import train_model

        cfg = _make_config(tmp_path, array_names=["east", "west"])
        daemon = PvLearnerDaemon(cfg)
        client = MagicMock()

        # Only train the "east" array; "west" has no model.
        knmi_rows = _synthetic_knmi_rows(n_months=3)
        east_cfg = cfg.arrays[0]
        east_pv = _synthetic_pv_rows(n_months=3, array_name="east")
        training_rows = build_training_rows(knmi_rows, east_pv)
        train_model(
            training_rows,
            cfg.training,
            east_cfg.model_path,
            east_cfg.metadata_path,
        )

        mc_rows = _synthetic_mc_rows()
        published_topics: list[str] = []

        def _fake_publish(client, topic, steps, **kwargs):
            published_topics.append(topic)

        with (
            patch(
                "pv_ml_learner.__main__.fetch_meteoserver_forecast",
                return_value=mc_rows,
            ),
            patch("pv_ml_learner.__main__.publish_forecast", side_effect=_fake_publish),
        ):
            daemon.run_inference_cycle(client)

        # Only "east" should have published; "west" has no model.
        assert published_topics == [east_cfg.output_topic]
