"""Tests for pv_ml_learner.config.

All tests confirm the Pydantic schema accepts valid input and rejects invalid
input. No I/O is performed; all tests operate on in-memory dicts only.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _valid_config() -> dict:
    """Return a minimal but fully valid raw config dict."""
    return {
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "pv-learner",
            "username": None,
            "password": None,
        },
        "signal_mimir": False,
        "knmi": {
            "station_id": 260,
        },
        "meteoserver": {
            "api_key": "test-key",
            "latitude": 52.10,
            "longitude": 5.18,
            "forecast_horizon_hours": 48,
        },
        "homeassistant": {
            "db_path": "/config/home-assistant_v2.db",
        },
        "arrays": [
            {
                "name": "main",
                "peak_power_kwp": 5.2,
                "output_topic": "mimir/input/pv_forecast/main",
                "sum_entity_ids": ["sensor.solaredge_energy_today"],
                "model_path": "/data/pv_ml_learner_main.joblib",
                "metadata_path": "/data/pv_ml_learner_main_meta.json",
            }
        ],
        "storage": {
            "db_path": "/data/pv_ml_learner.db",
        },
        "training": {
            "train_trigger_topic": "mimir/input/tools/pv_ml_learner/train",
            "inference_trigger_topic": "mimir/input/tools/pv_ml_learner/infer",
            "min_months_required": 12,
            "n_cv_splits": 5,
        },
        "ha_discovery": {
            "enabled": False,
        },
    }


class TestValidConfig:
    def test_valid_full_config_parses(self) -> None:
        from pv_ml_learner.config import PvLearnerConfig

        cfg = PvLearnerConfig.model_validate(_valid_config())
        assert cfg.meteoserver.api_key == "test-key"
        assert cfg.knmi.station_id == 260
        assert cfg.training.min_months_required == 12

    def test_exclude_limiting_entity_ids_absent_defaults_to_empty_list(self) -> None:
        """When exclude_limiting_entity_ids is omitted from an array it defaults to []."""
        from pv_ml_learner.config import PvLearnerConfig

        cfg = PvLearnerConfig.model_validate(_valid_config())
        assert cfg.arrays[0].exclude_limiting_entity_ids == []

    def test_exclude_limiting_entity_ids_present_parses(self) -> None:
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        raw["arrays"][0]["exclude_limiting_entity_ids"] = [
            "binary_sensor.solaredge_export_limited"
        ]
        cfg = PvLearnerConfig.model_validate(raw)
        assert cfg.arrays[0].exclude_limiting_entity_ids == [
            "binary_sensor.solaredge_export_limited"
        ]

    def test_signal_mimir_with_trigger_topic_parses(self) -> None:
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        raw["signal_mimir"] = True
        raw["mimir_trigger_topic"] = "mimir/input/trigger"
        cfg = PvLearnerConfig.model_validate(raw)
        assert cfg.signal_mimir is True


class TestRejectedConfig:
    def test_min_months_required_below_1_rejected(self) -> None:
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        raw["training"]["min_months_required"] = 0
        with pytest.raises(ValidationError):
            PvLearnerConfig.model_validate(raw)

    def test_missing_meteoserver_api_key_rejected(self) -> None:
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        del raw["meteoserver"]["api_key"]
        with pytest.raises(ValidationError):
            PvLearnerConfig.model_validate(raw)

    def test_empty_sum_entity_ids_rejected(self) -> None:
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        raw["arrays"][0]["sum_entity_ids"] = []
        with pytest.raises(ValidationError):
            PvLearnerConfig.model_validate(raw)

    def test_duplicate_array_names_rejected(self) -> None:
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        second_array = dict(raw["arrays"][0])
        second_array["output_topic"] = "mimir/input/pv_forecast/second"
        second_array["model_path"] = "/data/second.joblib"
        second_array["metadata_path"] = "/data/second_meta.json"
        # Both arrays share the same name "main" — must be rejected.
        raw["arrays"].append(second_array)
        with pytest.raises(ValidationError):
            PvLearnerConfig.model_validate(raw)

    def test_signal_mimir_without_explicit_trigger_uses_derived_topic(self) -> None:
        """When signal_mimir is True and no explicit trigger topic is given, the derived topic is used."""
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        raw["signal_mimir"] = True
        cfg = PvLearnerConfig.model_validate(raw)
        assert cfg.signal_mimir is True
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_unknown_field_rejected(self) -> None:
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        raw["unexpected_field"] = "should fail"
        with pytest.raises(ValidationError):
            PvLearnerConfig.model_validate(raw)


class TestHiooTopicDerivation:
    def test_array_output_topic_derived_from_name(self) -> None:
        """When output_topic is omitted, it is derived from mimir_topic_prefix and array name."""
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        del raw["arrays"][0]["output_topic"]
        cfg = PvLearnerConfig.model_validate(raw)
        assert cfg.arrays[0].output_topic == "mimir/input/pv/main/forecast"

    def test_array_output_topic_derived_custom_prefix(self) -> None:
        """Derivation respects a custom mimir_topic_prefix."""
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        raw["mimir_topic_prefix"] = "mymimir"
        del raw["arrays"][0]["output_topic"]
        cfg = PvLearnerConfig.model_validate(raw)
        assert cfg.arrays[0].output_topic == "mymimir/input/pv/main/forecast"

    def test_explicit_output_topic_not_overwritten(self) -> None:
        """An explicitly set output_topic is not overwritten by derivation."""
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        # output_topic is explicitly set in _valid_config
        cfg = PvLearnerConfig.model_validate(raw)
        assert cfg.arrays[0].output_topic == "mimir/input/pv_forecast/main"

    def test_mimir_trigger_topic_derived_from_prefix(self) -> None:
        """mimir_trigger_topic is derived from mimir_topic_prefix when not set."""
        from pv_ml_learner.config import PvLearnerConfig

        cfg = PvLearnerConfig.model_validate(_valid_config())
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_mimir_trigger_topic_custom_prefix(self) -> None:
        """mimir_trigger_topic derivation uses a custom prefix."""
        from pv_ml_learner.config import PvLearnerConfig

        raw = _valid_config()
        raw["mimir_topic_prefix"] = "mymimir"
        cfg = PvLearnerConfig.model_validate(raw)
        assert cfg.mimir_trigger_topic == "mymimir/input/trigger"

