"""Unit tests for helper_common.discovery.

Verifies that publish_trigger_discovery() correctly:
- Publishes exactly one button entity when stats_topic is None.
- Publishes button + four sensor entities when stats_topic is set.
- Deletes previously published sensor topics when stats_topic is removed.
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
_PREFIX = "homeassistant"


def _make_client() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# Button-only (no stats_topic)
# ---------------------------------------------------------------------------


class TestButtonOnly:
    def test_publishes_only_button_when_stats_topic_none(self) -> None:
        """When stats_topic is None, five calls are made: four sensor
        topic deletions (possible - active) followed by one button publish."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=None,
        )
        # 4 sensor deletions + 1 button publish
        assert client.publish.call_count == 5
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
        """When stats_topic is set, five payloads are published: one button
        and four sensor entities."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
        )
        # No deletions because all possible topics are active.
        assert client.publish.call_count == 5

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
        """When stats_topic is None, the four sensor topics are deleted by
        publishing an empty retained payload to each."""
        client = _make_client()
        publish_trigger_discovery(
            client,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=None,
        )
        # 4 deletions + 1 button publish = 5
        assert client.publish.call_count == 5

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
        regardless of stats_topic setting."""
        possible = _all_possible_helper_discovery_topics(
            tool_name=_TOOL_NAME,
            discovery_prefix=_PREFIX,
        )

        # Check with stats_topic set (all possible topics are published).
        client_with = _make_client()
        publish_trigger_discovery(
            client_with,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=_STATS_TOPIC,
        )
        topics_when_active = {c.args[0] for c in client_with.publish.call_args_list}

        # Check with stats_topic None (button published + sensors deleted).
        client_without = _make_client()
        publish_trigger_discovery(
            client_without,
            tool_name=_TOOL_NAME,
            tool_label=_TOOL_LABEL,
            trigger_topic=_TRIGGER_TOPIC,
            stats_topic=None,
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
