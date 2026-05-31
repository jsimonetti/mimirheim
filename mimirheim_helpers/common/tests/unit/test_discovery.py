"""Unit tests for helper_common.discovery.

Verifies that publish_trigger_discovery() correctly:
- Publishes exactly one button entity when stats_topic is None.
- Publishes button + four sensor entities when stats_topic is set.
- Deletes previously published sensor topics when stats_topic is removed.
- Publishes a forecast sensor entity when forecast_sensor=True.
- Deletes the forecast sensor topic when forecast_sensor=False.
- _all_possible_helper_discovery_topics() returns exactly the union of all
  topics that could ever be published or deleted (invariant test).
- All payloads are retained, QoS 1.
- All payloads reference the same device identifiers block.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import pytest

from helper_common.discovery import (
    _active_helper_discovery_topics,
    _all_possible_helper_discovery_topics,
    publish_trigger_discovery,
)

_TOOL_NAME = "nordpool_prices"
_TOOL_LABEL = "Nordpool Prices"
_TRIGGER_TOPIC = "mimir/input/tools/prices/trigger"
_STATS_TOPIC = "mimir/input/tools/prices/stats"
_OUTPUT_TOPIC = "mimir/input/prices"
_PREFIX = "homeassistant"


def _make_client() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# Button-only (no stats_topic)
# ---------------------------------------------------------------------------


class TestButtonOnly:
    def test_publishes_only_button_when_stats_topic_none(self) -> None:
        """When stats_topic is None and forecast_sensor is False, six calls are
        made: five sensor topic deletions (4 stats + 1 forecast) followed by
        one button publish."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=None,
        )
        # 4 stats sensor deletions + 1 forecast sensor deletion + 1 button publish
        assert client.publish.call_count == 6
        topics = {c.args[0] for c in client.publish.call_args_list}
        assert f"{_PREFIX}/button/{_TOOL_NAME}/config" in topics

    def test_button_payload_has_required_keys(self) -> None:
        """The button payload contains name, unique_id, command_topic,
        payload_press, retain, and device keys."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=None,
        )
        # The button publish is the call whose topic contains "/button/".
        button_call = next(
            c for c in client.publish.call_args_list
            if "/button/" in c.args[0]
        )
        payload = json.loads(button_call.args[1])
        for key in ("name", "unique_id", "command_topic", "payload_press", "retain", "device"):
            assert key in payload, f"Missing key {key!r} in button payload"
        assert payload["command_topic"] == _TRIGGER_TOPIC


# ---------------------------------------------------------------------------
# Button + stats sensors
# ---------------------------------------------------------------------------


class TestWithStatsSensors:
    def test_five_publishes_when_stats_topic_set(self) -> None:
        """When stats_topic is set but forecast_sensor is False, six calls are
        made: one forecast sensor deletion + one button + four stats sensors."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
        )
        # 1 forecast deletion + 1 button + 4 stats sensors
        assert client.publish.call_count == 6

    def test_stats_sensor_topics_have_correct_structure(self) -> None:
        """The four stats sensor discovery topics follow the pattern
        {prefix}/sensor/{tool_name}_{sensor_id}/config."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
        )
        topics = {c.args[0] for c in client.publish.call_args_list}
        for sensor_id in ("last_run_ts", "duration_s", "horizon_hours", "exit_message"):
            expected = f"{_PREFIX}/sensor/{_TOOL_NAME}_{sensor_id}/config"
            assert expected in topics, f"Missing expected sensor topic {expected!r}"

    def test_all_payloads_are_retained_qos1(self) -> None:
        """Every publish call uses qos=1 and retain=True."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
        )
        for c in client.publish.call_args_list:
            assert c.kwargs.get("qos") == 1, f"Expected qos=1: {c}"
            assert c.kwargs.get("retain") is True, f"Expected retain=True: {c}"

    def test_stats_sensors_share_device_block_with_button(self) -> None:
        """All published payloads reference the same device identifiers block."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
        )
        device_ids_seen: list[list[str]] = []
        for c in client.publish.call_args_list:
            raw = c.args[1]
            if raw is None:
                continue
            payload = json.loads(raw)
            if "device" in payload:
                device_ids_seen.append(payload["device"]["identifiers"])
        assert len(set(tuple(d) for d in device_ids_seen)) == 1, (
            "All payloads must reference the same device identifiers"
        )
        assert device_ids_seen[0] == [_TOOL_NAME]

    def test_stats_sensor_state_topic_is_stats_topic(self) -> None:
        """Each stats sensor's state_topic is the configured stats_topic."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
        )
        for c in client.publish.call_args_list:
            raw = c.args[1]
            if raw is None:
                continue
            payload = json.loads(raw)
            if "state_topic" in payload:
                assert payload["state_topic"] == _STATS_TOPIC


# ---------------------------------------------------------------------------
# Stale topic cleanup (stats_topic removed)
# ---------------------------------------------------------------------------


class TestStaleCleanup:
    def test_stale_sensors_deleted_when_stats_topic_none(self) -> None:
        """When stats_topic is None, all five sensor topics are deleted:
        four stats sensors + the forecast sensor."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=None,
        )
        # 5 deletions + 1 button publish = 6
        assert client.publish.call_count == 6

        deleted_topics = {
            c.args[0]
            for c in client.publish.call_args_list
            if c.args[1] is None
        }
        for sensor_id in ("last_run_ts", "duration_s", "horizon_hours", "exit_message"):
            expected = f"{_PREFIX}/sensor/{_TOOL_NAME}_{sensor_id}/config"
            assert expected in deleted_topics, (
                f"Expected deletion of {expected!r} when stats_topic is None"
            )
        forecast_topic = f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config"
        assert forecast_topic in deleted_topics, (
            "Expected deletion of forecast sensor topic when forecast_sensor is False"
        )

    def test_button_topic_not_deleted(self) -> None:
        """The button topic is always in the active set and must never be deleted."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=None,
        )
        button_topic = f"{_PREFIX}/button/{_TOOL_NAME}/config"
        deletion_topics = {
            c.args[0]
            for c in client.publish.call_args_list
            if c.args[1] is None
        }
        assert button_topic not in deletion_topics


# ---------------------------------------------------------------------------
# Invariant: _all_possible matches publish + delete targets
# ---------------------------------------------------------------------------


class TestInvariant:
    def test_all_possible_covers_publish_and_delete_targets(self) -> None:
        """_all_possible_helper_discovery_topics() returns exactly the union of
        topics that publish_trigger_discovery() would either publish or delete,
        regardless of stats_topic or forecast_sensor setting."""
        possible = _all_possible_helper_discovery_topics(
            tool_name=_TOOL_NAME,
            discovery_prefix=_PREFIX,
        )

        # Check with all features on (stats + forecast sensor): all possible topics published.
        client_with = _make_client()
        publish_trigger_discovery(
            client_with,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
            forecast_sensor=True,
            output_topic=_OUTPUT_TOPIC,
        )
        topics_when_active = {c.args[0] for c in client_with.publish.call_args_list}

        # Check with all features off (button + all sensor deletions).
        client_without = _make_client()
        publish_trigger_discovery(
            client_without,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=None,
            forecast_sensor=False,
        )
        topics_when_inactive = {c.args[0] for c in client_without.publish.call_args_list}

        assert possible == topics_when_active
        assert possible == topics_when_inactive

    def test_active_topics_subset_of_possible(self) -> None:
        """_active_helper_discovery_topics() is always a subset of
        _all_possible_helper_discovery_topics()."""
        possible = _all_possible_helper_discovery_topics(tool_name=_TOOL_NAME)
        active_with = _active_helper_discovery_topics(
            tool_name=_TOOL_NAME, stats_topic=_STATS_TOPIC
        )
        active_without = _active_helper_discovery_topics(
            tool_name=_TOOL_NAME, stats_topic=None
        )
        assert active_with.issubset(possible)
        assert active_without.issubset(possible)

    def test_custom_discovery_prefix_applied(self) -> None:
        """A non-default discovery_prefix is used for both possible and active sets."""
        possible = _all_possible_helper_discovery_topics(
            tool_name=_TOOL_NAME, discovery_prefix="custom"
        )
        for topic in possible:
            assert topic.startswith("custom/"), f"Expected 'custom/' prefix: {topic!r}"


# ---------------------------------------------------------------------------
# Forecast sensor
# ---------------------------------------------------------------------------


class TestForecastSensor:
    def test_forecast_sensor_published_when_enabled(self) -> None:
        """When forecast_sensor=True and output_topic is provided, a discovery
        payload is published to {prefix}/sensor/{tool_name}_forecast/config."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=True,
            output_topic=_OUTPUT_TOPIC,
        )
        topics = {c.args[0] for c in client.publish.call_args_list}
        assert f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config" in topics

    def test_forecast_sensor_not_published_when_disabled(self) -> None:
        """When forecast_sensor=False, no payload is published to the forecast topic."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=False,
            output_topic=_OUTPUT_TOPIC,
        )
        published_topics = {
            c.args[0] for c in client.publish.call_args_list if c.args[1] is not None
        }
        assert f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config" not in published_topics

    def test_forecast_sensor_deleted_when_disabled(self) -> None:
        """When forecast_sensor=False, the forecast sensor topic receives a
        None payload (stale-topic deletion)."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=False,
        )
        deleted_topics = {
            c.args[0] for c in client.publish.call_args_list if c.args[1] is None
        }
        assert f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config" in deleted_topics

    def test_forecast_sensor_not_published_without_output_topic(self) -> None:
        """When forecast_sensor=True but output_topic is None (not supplied),
        no forecast payload is published."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=True,
        )
        published_topics = {
            c.args[0] for c in client.publish.call_args_list if c.args[1] is not None
        }
        assert f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config" not in published_topics

    def test_forecast_sensor_payload_has_required_keys(self) -> None:
        """The forecast sensor payload contains all required HA discovery keys."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=True,
            output_topic=_OUTPUT_TOPIC,
        )
        forecast_call = next(
            c for c in client.publish.call_args_list
            if c.args[0] == f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config"
        )
        payload = json.loads(forecast_call.args[1])
        for key in (
            "name", "unique_id", "state_topic", "json_attributes_topic",
            "value_template", "unit_of_measurement", "entity_category",
            "enabled_by_default", "device",
        ):
            assert key in payload, f"Missing key {key!r} in forecast sensor payload"

    def test_forecast_sensor_state_topic_is_output_topic(self) -> None:
        """state_topic and json_attributes_topic both equal the supplied output_topic."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=True,
            output_topic=_OUTPUT_TOPIC,
        )
        forecast_call = next(
            c for c in client.publish.call_args_list
            if c.args[0] == f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config"
        )
        payload = json.loads(forecast_call.args[1])
        assert payload["state_topic"] == _OUTPUT_TOPIC
        assert payload["json_attributes_topic"] == _OUTPUT_TOPIC

    def test_forecast_sensor_enabled_by_default_is_true(self) -> None:
        """The forecast sensor is enabled by default so users see it immediately."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=True,
            output_topic=_OUTPUT_TOPIC,
        )
        forecast_call = next(
            c for c in client.publish.call_args_list
            if c.args[0] == f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config"
        )
        payload = json.loads(forecast_call.args[1])
        assert payload["enabled_by_default"] is True

    def test_forecast_sensor_default_unit_is_kw(self) -> None:
        """Default unit_of_measurement is 'kW' (for power-type helpers)."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=True,
            output_topic=_OUTPUT_TOPIC,
        )
        forecast_call = next(
            c for c in client.publish.call_args_list
            if c.args[0] == f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config"
        )
        payload = json.loads(forecast_call.args[1])
        assert payload["unit_of_measurement"] == "kW"
        assert payload.get("device_class") == "power"

    def test_forecast_sensor_eur_per_kwh_has_no_device_class(self) -> None:
        """When unit='EUR/kWh' and device_class=None, no device_class key is
        present in the payload."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=True,
            output_topic=_OUTPUT_TOPIC,
            forecast_unit="EUR/kWh",
            forecast_device_class=None,
        )
        forecast_call = next(
            c for c in client.publish.call_args_list
            if c.args[0] == f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config"
        )
        payload = json.loads(forecast_call.args[1])
        assert payload["unit_of_measurement"] == "EUR/kWh"
        assert "device_class" not in payload

    def test_forecast_sensor_shares_device_block_with_button(self) -> None:
        """The forecast sensor payload references the same device block as the button."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            forecast_sensor=True,
            output_topic=_OUTPUT_TOPIC,
        )
        button_call = next(c for c in client.publish.call_args_list if "/button/" in c.args[0])
        forecast_call = next(
            c for c in client.publish.call_args_list
            if c.args[0] == f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config"
        )
        button_device = json.loads(button_call.args[1])["device"]
        forecast_device = json.loads(forecast_call.args[1])["device"]
        assert button_device == forecast_device

    def test_all_possible_includes_forecast_sensor_topic(self) -> None:
        """_all_possible_helper_discovery_topics() includes the forecast sensor topic."""
        possible = _all_possible_helper_discovery_topics(
            tool_name=_TOOL_NAME,
            discovery_prefix=_PREFIX,
        )
        assert f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config" in possible

    def test_active_includes_forecast_sensor_when_enabled(self) -> None:
        """_active_helper_discovery_topics() includes the forecast sensor topic
        when forecast_sensor=True."""
        active = _active_helper_discovery_topics(
            tool_name=_TOOL_NAME,
            stats_topic=None,
            forecast_sensor=True,
        )
        assert f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config" in active

    def test_active_excludes_forecast_sensor_when_disabled(self) -> None:
        """_active_helper_discovery_topics() does not include the forecast sensor
        topic when forecast_sensor=False."""
        active = _active_helper_discovery_topics(
            tool_name=_TOOL_NAME,
            stats_topic=None,
            forecast_sensor=False,
        )
        assert f"{_PREFIX}/sensor/{_TOOL_NAME}_forecast/config" not in active


# ---------------------------------------------------------------------------
# Device ID parameter
# ---------------------------------------------------------------------------


class TestDeviceId:
    def test_button_device_block_uses_tool_name_when_device_id_not_supplied(self) -> None:
        """When device_id is not supplied, device identifiers == [tool_name]."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
        )
        button_call = next(c for c in client.publish.call_args_list if "/button/" in c.args[0])
        payload = json.loads(button_call.args[1])
        assert payload["device"]["identifiers"] == [_TOOL_NAME]
        assert payload["device"]["name"] == _TOOL_LABEL

    def test_button_device_block_uses_device_id_when_supplied(self) -> None:
        """When device_id is supplied, device identifiers == [device_id]."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            device_id="shared_device",
            device_label="Shared Device",
        )
        button_call = next(c for c in client.publish.call_args_list if "/button/" in c.args[0])
        payload = json.loads(button_call.args[1])
        assert payload["device"]["identifiers"] == ["shared_device"]
        assert payload["device"]["name"] == "Shared Device"

    def test_device_label_falls_back_to_tool_label_when_not_supplied(self) -> None:
        """When device_id is supplied but device_label is not, name falls back to tool_label."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            device_id="shared_device",
        )
        button_call = next(c for c in client.publish.call_args_list if "/button/" in c.args[0])
        payload = json.loads(button_call.args[1])
        assert payload["device"]["identifiers"] == ["shared_device"]
        assert payload["device"]["name"] == _TOOL_LABEL

    def test_two_calls_with_same_device_id_produce_same_device_block(self) -> None:
        """Two separate calls with the same device_id produce payloads whose
        device blocks are identical — causing HA to group them under one device card."""
        client_a = _make_client()
        client_b = _make_client()
        publish_trigger_discovery(
            client_a,
            tool_name="pv_ml_learner_train",
            tool_label="Train",
            trigger_topic="mimir/tools/train/trigger",
            device_id="pv_ml_learner",
            device_label="PV ML Learner",
        )
        publish_trigger_discovery(
            client_b,
            tool_name="pv_ml_learner_infer",
            tool_label="Infer",
            trigger_topic="mimir/tools/infer/trigger",
            device_id="pv_ml_learner",
            device_label="PV ML Learner",
        )
        button_a = json.loads(next(c for c in client_a.publish.call_args_list if "/button/" in c.args[0]).args[1])
        button_b = json.loads(next(c for c in client_b.publish.call_args_list if "/button/" in c.args[0]).args[1])
        assert button_a["device"] == button_b["device"]

    def test_stats_sensor_device_block_uses_device_id_when_supplied(self) -> None:
        """Stats sensors published with a device_id use the same device block."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
            device_id="shared_device",
            device_label="Shared Device",
        )
        for c in client.publish.call_args_list:
            raw = c.args[1]
            if raw is None:
                continue
            payload = json.loads(raw)
            if "device" in payload:
                assert payload["device"]["identifiers"] == ["shared_device"]

    def test_topic_names_are_still_based_on_tool_name_not_device_id(self) -> None:
        """The MQTT topic paths use tool_name, not device_id."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            device_id="shared_device",
        )
        touched_topics = {c.args[0] for c in client.publish.call_args_list}
        assert f"{_PREFIX}/button/{_TOOL_NAME}/config" in touched_topics
        assert f"{_PREFIX}/button/shared_device/config" not in touched_topics
