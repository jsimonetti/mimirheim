"""Unit tests for PvLearnerDaemon._publish_discovery().

Verifies that the train and infer trigger buttons are grouped under the same
HA device when ha_discovery.enabled is True, that forecast sensors share the
same device when forecast_sensor is True, and that no discovery is published
when ha_discovery.enabled is False.
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from pv_ml_learner.config import PvLearnerConfig


def _base_config_dict(*, ha_enabled: bool = True, device_name: str = "PV ML Learner", forecast_sensor: bool = False) -> dict:
    return {
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "pv-learner-test",
        },
        "signal_mimir": False,
        "knmi": {"station_id": 260},
        "meteoserver": {
            "api_key": "test-key",
            "latitude": 52.10,
            "longitude": 5.18,
        },
        "homeassistant": {
            "db_url": "sqlite:////config/home-assistant_v2.db",
        },
        "arrays": {
            "main": {
                "peak_power_kwp": 5.2,
                "output_topic": "mimir/input/pv_forecast/main",
                "sum_entity_ids": ["sensor.solaredge_energy_today"],
                "model_path": "/data/model.joblib",
                "metadata_path": "/data/model_meta.json",
            }
        },
        "storage": {"db_path": "/data/pv_ml_learner.db"},
        "training": {
            "train_trigger_topic": "mimir/tools/pv_ml_learner/train",
            "inference_trigger_topic": "mimir/tools/pv_ml_learner/infer",
        },
        "ha_discovery": {
            "enabled": ha_enabled,
            "device_name": device_name,
            "forecast_sensor": forecast_sensor,
        },
    }


def _make_config(*, ha_enabled: bool = True, device_name: str = "PV ML Learner") -> PvLearnerConfig:
    """Return a minimal PvLearnerConfig for testing _publish_discovery()."""
    return PvLearnerConfig.model_validate(_base_config_dict(ha_enabled=ha_enabled, device_name=device_name))


def _make_config_with_forecast(*, device_name: str = "PV ML Learner") -> PvLearnerConfig:
    """Return a config with forecast_sensor=True."""
    return PvLearnerConfig.model_validate(_base_config_dict(ha_enabled=True, device_name=device_name, forecast_sensor=True))


def _make_config_multi_array(*, device_name: str = "PV ML Learner") -> PvLearnerConfig:
    """Return a config with two arrays and forecast_sensor=True."""
    raw = _base_config_dict(ha_enabled=True, device_name=device_name, forecast_sensor=True)
    raw["arrays"]["dak"] = {
        "peak_power_kwp": 3.0,
        "output_topic": "mimir/input/pv_forecast/dak",
        "sum_entity_ids": ["sensor.dak_energy"],
        "model_path": "/data/dak_model.joblib",
        "metadata_path": "/data/dak_meta.json",
    }
    return PvLearnerConfig.model_validate(raw)


class TestPublishDiscovery:
    def test_train_and_infer_buttons_share_device_id(self) -> None:
        """Both publish_trigger_discovery calls must use the same device_id so
        HA groups them under one device card."""
        config = _make_config(ha_enabled=True)
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        assert mock_pub.call_count >= 2
        call_kwargs = [c.kwargs for c in mock_pub.call_args_list[:2]]
        device_ids = [k["device_id"] for k in call_kwargs]
        assert device_ids[0] == device_ids[1] == "pv_ml_learner"

    def test_device_identifier_is_pv_ml_learner(self) -> None:
        """The shared device_id is always 'pv_ml_learner', independent of device_name."""
        config = _make_config(ha_enabled=True, device_name="My Custom PV Learner")
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        for c in mock_pub.call_args_list[:2]:
            assert c.kwargs["device_id"] == "pv_ml_learner"

    def test_device_label_uses_configured_device_name(self) -> None:
        """device_label passed to both calls equals ha_discovery.device_name."""
        config = _make_config(ha_enabled=True, device_name="My PV Learner")
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        for c in mock_pub.call_args_list[:2]:
            assert c.kwargs["device_label"] == "My PV Learner"

    def test_button_tool_names_are_distinct(self) -> None:
        """The two calls use different tool_names, giving distinct entity paths."""
        config = _make_config(ha_enabled=True)
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        tool_names = [c.kwargs["tool_name"] for c in mock_pub.call_args_list[:2]]
        assert len(set(tool_names)) == 2
        assert "pv_ml_learner_train" in tool_names
        assert "pv_ml_learner_infer" in tool_names

    def test_no_discovery_published_when_ha_disabled(self) -> None:
        """When ha_discovery.enabled is False, _publish_discovery() is a no-op."""
        config = _make_config(ha_enabled=False)
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        mock_pub.assert_not_called()
        client.publish.assert_not_called()


class TestForecastSensorGrouping:
    """Verify that per-array forecast sensors share the pv_ml_learner device
    and do not publish redundant trigger buttons."""

    def test_forecast_sensor_calls_share_device_id(self) -> None:
        """With forecast_sensor=True, all publish_trigger_discovery calls
        (train, infer, per-array) use device_id='pv_ml_learner'."""
        config = _make_config_with_forecast()
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        for c in mock_pub.call_args_list:
            assert c.kwargs.get("device_id") == "pv_ml_learner", (
                f"Call {c.kwargs.get('tool_name')!r} missing device_id='pv_ml_learner'"
            )

    def test_per_array_call_has_no_trigger_topic(self) -> None:
        """The per-array forecast call passes trigger_topic=None, so no extra
        button entity is added to the shared device."""
        config = _make_config_with_forecast()
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        array_calls = [c for c in mock_pub.call_args_list if "main" in c.kwargs.get("tool_name", "")]
        assert array_calls, "Expected a per-array call for 'main'"
        for c in array_calls:
            assert c.kwargs.get("trigger_topic") is None, (
                f"Per-array call should have trigger_topic=None, got {c.kwargs.get('trigger_topic')!r}"
            )

    def test_total_call_count_with_forecast(self) -> None:
        """With one array and forecast_sensor=True, there are exactly 3 calls:
        train, infer, and the per-array forecast sensor."""
        config = _make_config_with_forecast()
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        assert mock_pub.call_count == 3

    def test_multi_array_produces_one_call_per_array(self) -> None:
        """With two arrays, there are 4 calls: train, infer, main forecast, dak forecast."""
        config = _make_config_multi_array()
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        assert mock_pub.call_count == 4
        tool_names = {c.kwargs["tool_name"] for c in mock_pub.call_args_list}
        assert "pv_ml_learner_train" in tool_names
        assert "pv_ml_learner_infer" in tool_names
        assert "pv_ml_learner_main" in tool_names
        assert "pv_ml_learner_dak" in tool_names

    def test_no_forecast_calls_without_forecast_sensor_flag(self) -> None:
        """When forecast_sensor=False (default), only 2 calls are made."""
        config = _make_config(ha_enabled=True)
        client = MagicMock()
        with patch("pv_ml_learner.__main__.publish_trigger_discovery") as mock_pub:
            from pv_ml_learner.__main__ import PvLearnerDaemon
            daemon = PvLearnerDaemon.__new__(PvLearnerDaemon)
            daemon._config = config
            daemon._publish_discovery(client)

        assert mock_pub.call_count == 2
