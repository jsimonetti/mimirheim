"""Checked MQTT publishing for mimirheim helper daemons.

``paho`` reports publish failures in the return code of the ``MQTTMessageInfo``
it hands back. It does not raise. Every helper ignored that return code and
logged success unconditionally, so a publish that never left the process was
indistinguishable from one the broker acknowledged.

What that costs depends on the QoS, and the two cases are genuinely different.
Probed against a live broker, publishing while disconnected:

    while-down rc: qos0=4 qos1=4   queued=1
    broker delivered: [('probe/q1', 'qos1-while-down'), ('probe/marker', ...)]

At QoS 1 paho stores the message and redelivers it on reconnect, so
``MQTT_ERR_NO_CONN`` is a delay, not a loss. At QoS 0 nothing is stored and the
message is simply gone.

The window is narrow but real: a helper's cycle is started by a trigger that
arrives over MQTT, so it is connected when the cycle begins. If the connection
drops during the fetch -- seconds for a price API, longer for a database query
-- the forecast is queued and arrives on reconnect while the QoS 0 solve
trigger is dropped. mimirheim then holds fresh data it does not know arrived.

This module has no imports from any specific helper tool.
"""
from __future__ import annotations

import logging
from typing import Any

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class PublishError(Exception):
    """Raised when an MQTT publish did not reach the broker and was not queued.

    Helper cycles run inside ``HelperDaemon._on_message``, which catches any
    exception, logs the traceback and records a failed cycle. Raising is
    therefore the way to make a dropped publish visible without taking the
    daemon down.
    """


def publish_checked(
    client: Any,
    topic: str,
    payload: Any,
    *,
    qos: int,
    retain: bool,
    description: str,
) -> None:
    """Publish to ``topic`` and fail loudly if the message was dropped.

    Args:
        client: A paho MQTT client.
        topic: Topic to publish to.
        payload: Message payload, in any form ``client.publish`` accepts.
        qos: MQTT quality of service. Determines whether an unsent message is
            treated as queued or as lost.
        retain: Whether the broker should retain the message.
        description: Short human-readable name for what is being published,
            used in the error and log messages (e.g. ``"price forecast"``).

    Raises:
        PublishError: If paho reported a failure that means the message was
            dropped. At QoS 1 and above, ``MQTT_ERR_NO_CONN`` is excluded: paho
            queues those and redelivers them on reconnect, so it is logged as a
            delay instead.
    """
    info = client.publish(topic, payload, qos=qos, retain=retain)
    if info.rc == mqtt.MQTT_ERR_SUCCESS:
        return

    if qos > 0 and info.rc == mqtt.MQTT_ERR_NO_CONN:
        logger.warning(
            "Broker unreachable; %s for %r is queued and will be delivered on "
            "reconnect.",
            description,
            topic,
        )
        return

    raise PublishError(
        f"Failed to publish {description} to {topic!r}: "
        f"{mqtt.error_string(info.rc)}"
    )
