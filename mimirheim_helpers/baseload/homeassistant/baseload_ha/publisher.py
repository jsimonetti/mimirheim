"""Publish base load forecast steps to an MQTT topic.

This module handles only the MQTT publish side. It has no HTTP, config, or
scheduling dependencies.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

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
    client.publish(output_topic, payload, qos=1, retain=True)
    logger.info("Published %d base load steps to %s", len(steps), output_topic)

    if signal_mimir:
        client.publish(mimir_trigger_topic, "", qos=0, retain=False)
        logger.debug("Signalled mimirheim via %s", mimir_trigger_topic)
