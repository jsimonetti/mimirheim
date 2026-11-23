"""Publish the weather-dependent base load forecast and model statistics over MQTT.

This module handles only the MQTT publish side. It has no database, HTTP,
config or modelling dependencies.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

from helper_common.publish import publish_checked

logger = logging.getLogger(__name__)


def publish_forecast(
    client: mqtt.Client,
    output_topic: str,
    steps: list[dict[str, Any]],
    *,
    signal_mimir: bool,
    mimir_trigger_topic: str | None = None,
) -> None:
    """Publish a list of base load forecast steps retained to output_topic.

    Args:
        client: A connected paho MQTT client instance.
        output_topic: Topic to publish the JSON forecast array to. The message
            is retained so mimirheim always has a forecast available on reconnect.
        steps: Timestamped step dicts with "ts" and "kw" keys.
        signal_mimir: If True, also publish an empty non-retained message to
            mimir_trigger_topic to trigger an immediate mimirheim solve cycle.
        mimir_trigger_topic: The mimirheim trigger topic. Required when signal_mimir
            is True.

    Raises:
        ValueError: If signal_mimir is True but mimir_trigger_topic is not provided.
    """
    if signal_mimir and not mimir_trigger_topic:
        raise ValueError(
            "mimir_trigger_topic must be set when signal_mimir is True"
        )

    payload = json.dumps(steps)
    publish_checked(
        client,
        output_topic,
        payload,
        qos=1,
        retain=True,
        description="base load forecast",
    )
    logger.info("Published %d base load steps to %s", len(steps), output_topic)

    if signal_mimir:
        publish_checked(
            client,
            mimir_trigger_topic,
            "",
            qos=0,
            retain=False,
            description="mimirheim solve trigger",
        )
        logger.debug("Signalled mimirheim via %s", mimir_trigger_topic)


def publish_model_stats(client: mqtt.Client, topic: str, stats: dict[str, Any]) -> None:
    """Publish model fit statistics retained to ``topic``.

    This is separate from the daemon's run statistics topic, whose fixed
    payload the shared helper daemon publishes after every cycle.

    Args:
        client: A connected paho MQTT client instance.
        topic: Model statistics topic.
        stats: JSON-serialisable statistics of the last successful fit.
    """
    publish_checked(
        client,
        topic,
        json.dumps(stats),
        qos=1,
        retain=True,
        description="model statistics",
    )
    logger.debug("Published model statistics to %s", topic)
