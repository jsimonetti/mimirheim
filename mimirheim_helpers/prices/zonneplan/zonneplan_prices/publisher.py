"""Publish Zonneplan price steps to an MQTT topic.

This module handles only the MQTT publish side. It has no HTTP, config, or
scheduling dependencies.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


def _normalise_zeros(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace any float value equal to 0.0 (including -0.0) with the integer 0.

    Python's ``json.dumps`` serialises ``-0.0`` as ``"-0.0"`` and ``0.0`` as
    ``"0.0"``, both of which look odd in dashboards. Converting them to the
    integer ``0`` produces the cleaner ``"0"`` in the JSON output.

    Args:
        steps: List of price step dicts as built by the fetcher.

    Returns:
        A new list with the same structure; original dicts are not mutated.
    """
    return [
        {k: 0 if isinstance(v, float) and v == 0.0 else v for k, v in step.items()}
        for step in steps
    ]


def publish_prices(
    client: mqtt.Client,
    output_topic: str,
    steps: list[dict[str, Any]],
    *,
    signal_mimir: bool,
    mimir_trigger_topic: str | None = None,
) -> None:
    """Publish a list of price steps retained to output_topic.

    Args:
        client: A connected paho MQTT client instance.
        output_topic: Topic to publish the JSON price array to. The message is
            retained so mimirheim always has the last known prices available on
            reconnect.
        steps: Sorted list of price step dicts. An empty list is a valid payload
            when no prices are available — it clears the retained message.
        signal_mimir: If True, also publish an empty non-retained message to
            ``mimir_trigger_topic`` to trigger an immediate mimirheim solve cycle.
        mimir_trigger_topic: The mimirheim trigger topic. Required when
            ``signal_mimir`` is True.

    Raises:
        ValueError: If ``signal_mimir`` is True but ``mimir_trigger_topic`` is
            not provided.
    """
    if signal_mimir and not mimir_trigger_topic:
        raise ValueError(
            "mimir_trigger_topic must be set when signal_mimir is True"
        )

    payload = json.dumps(_normalise_zeros(steps))
    client.publish(output_topic, payload, qos=1, retain=True)
    logger.info("Published %d price steps to %s", len(steps), output_topic)

    if signal_mimir:
        client.publish(mimir_trigger_topic, "", qos=0, retain=False)
        logger.debug("Signalled mimirheim via %s", mimir_trigger_topic)
