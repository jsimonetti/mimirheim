"""Unit tests for baseload_static.config.

Covers schema validation for all Pydantic models used by the static baseload tool.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from baseload_static.config import MqttConfig, StaticBaseloadConfig, BaseloadConfig


_VALID_CONFIG: dict = {
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "client_id": "baseload-static-test",
    },
    "trigger_topic": "mimir/input/tools/baseload/trigger",
    "output_topic": "mimir/input/base",
    "baseload": {
        "profile_kw": [0.5] * 24,
        "horizon_hours": 48,
    },
    "signal_mimir": False,
}


class TestMqttConfig:
    def test_valid_minimal(self) -> None:
        cfg = MqttConfig(host="broker", client_id="id")
        assert cfg.port == 1883

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            MqttConfig(host="broker", client_id="id", unknown="x")


class TestStaticBaseloadConfig:
    def test_valid_24_element_profile(self) -> None:
        cfg = StaticBaseloadConfig(profile_kw=[0.3] * 24)
        assert len(cfg.profile_kw) == 24
        assert cfg.horizon_hours == 48

    def test_single_element_profile_accepted(self) -> None:
        cfg = StaticBaseloadConfig(profile_kw=[1.0])
        assert cfg.profile_kw == [1.0]

    def test_empty_profile_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaticBaseloadConfig(profile_kw=[])

    def test_profile_above_168_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaticBaseloadConfig(profile_kw=[0.1] * 169)

    def test_horizon_hours_default_is_48(self) -> None:
        cfg = StaticBaseloadConfig(profile_kw=[0.5])
        assert cfg.horizon_hours == 48

    def test_weekly_profiles_accepted_with_all_7_days(self) -> None:
        weekly = {i: [float(i)] * 24 for i in range(7)}
        cfg = StaticBaseloadConfig(weekly_profiles_kw=weekly)
        assert cfg.profile_kw is None
        assert len(cfg.weekly_profiles_kw) == 7

    def test_weekly_profiles_without_profile_kw_fails_when_days_missing(self) -> None:
        # Only 6 days provided; day 6 is absent and there is no fallback.
        weekly = {i: [0.3] * 24 for i in range(6)}
        with pytest.raises(ValidationError, match="Missing"):
            StaticBaseloadConfig(weekly_profiles_kw=weekly)

    def test_neither_profile_kw_nor_weekly_profiles_rejected(self) -> None:
        with pytest.raises(ValidationError, match="profile_kw"):
            StaticBaseloadConfig()

    def test_weekly_profiles_invalid_key_rejected(self) -> None:
        # Key 7 is not a valid weekday (0–6).
        weekly = {i: [0.3] * 24 for i in range(7)}
        weekly[7] = [0.3] * 24
        with pytest.raises(ValidationError, match="Invalid keys"):
            StaticBaseloadConfig(weekly_profiles_kw=weekly)

    def test_weekly_profile_empty_sub_profile_rejected(self) -> None:
        weekly = {i: [0.3] * 24 for i in range(7)}
        weekly[3] = []
        with pytest.raises(ValidationError):
            StaticBaseloadConfig(weekly_profiles_kw=weekly)

    def test_weekly_profiles_with_fallback_profile_kw(self) -> None:
        # Providing only some weekdays is valid when profile_kw is the fallback.
        cfg = StaticBaseloadConfig(
            profile_kw=[0.3] * 24,
            weekly_profiles_kw={5: [0.5] * 24, 6: [0.6] * 24},
        )
        assert cfg.weekly_profiles_kw[5][0] == 0.5

    def test_horizon_hours_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaticBaseloadConfig(profile_kw=[0.5], horizon_hours=0)

    def test_horizon_hours_above_168_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StaticBaseloadConfig(profile_kw=[0.5], horizon_hours=169)

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            StaticBaseloadConfig(profile_kw=[0.5], unknown="x")


class TestBaseloadConfig:
    def test_valid_full_config(self) -> None:
        cfg = BaseloadConfig.model_validate(_VALID_CONFIG)
        assert len(cfg.baseload.profile_kw) == 24
        assert cfg.baseload.horizon_hours == 48

    def test_signal_mimir_without_explicit_trigger_uses_derived_topic(self) -> None:
        """When signal_mimir is True and no explicit trigger topic is given, the derived topic is used."""
        cfg = BaseloadConfig.model_validate({**_VALID_CONFIG, "signal_mimir": True})
        assert cfg.signal_mimir is True
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_signal_mimir_with_trigger_topic_accepted(self) -> None:
        good = {
            **_VALID_CONFIG,
            "signal_mimir": True,
            "mimir_trigger_topic": "mimir/input/trigger",
        }
        cfg = BaseloadConfig.model_validate(good)
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_rejects_unknown_top_level_fields(self) -> None:
        bad = {**_VALID_CONFIG, "unexpected": True}
        with pytest.raises(ValidationError):
            BaseloadConfig.model_validate(bad)
