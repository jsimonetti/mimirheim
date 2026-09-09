"""MQTT publisher for the Open-Meteo PV forecast pipeline.

This module formats the mimirheim-compatible PV forecast payload and publishes
it. It is responsible only for serialisation and delivery.

What this module does not do:
- It does not call the Open-Meteo API.
- It does not compute confidence values.
- It does not import from mimirheim.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from helper_common.publish import publish_checked

logger = logging.getLogger("pv_openmeteo.publisher")


def publish_forecast(client: Any, output_topic: str, steps: list[dict]) -> None:
    """Publish one array's forecast payload.

    The payload is published retained at QoS 1 so that a mimirheim instance
    restarting between fetch cycles finds a forecast waiting on the topic
    instead of running its first solve blind.

    The steps list must already be in mimirheim format: a list of dicts with
    keys ``ts``, ``kw`` and ``confidence``.

    Args:
        client: A paho-mqtt ``Client`` instance.
        output_topic: MQTT topic for the forecast payload.
        steps: Forecast steps in mimirheim format, as produced by
            ``series.apply_confidence()``.
    """
    publish_checked(
        client,
        output_topic,
        json.dumps(steps),
        qos=1,
        retain=True,
        description="PV forecast",
    )


def publish_trigger(client: Any, trigger_topic: str) -> None:
    """Ask mimirheim to run a solve now.

    The message is empty and not retained: it is an event, and a retained
    trigger would fire again on every reconnect.

    Args:
        client: A paho-mqtt ``Client`` instance.
        trigger_topic: mimirheim's trigger topic.
    """
    publish_checked(
        client,
        trigger_topic,
        b"",
        qos=0,
        retain=False,
        description="mimirheim solve trigger",
    )
