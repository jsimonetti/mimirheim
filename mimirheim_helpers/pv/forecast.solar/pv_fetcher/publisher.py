"""MQTT publisher for the PV forecast pipeline.

This module formats the mimirheim-compatible PV forecast payload and publishes it
to the configured output topic. It is responsible only for serialisation and
delivery — it does not fetch data or compute confidence values.

What this module does not do:
- It does not call the forecast.solar API.
- It does not compute confidence values.
- It does not import from mimirheim.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("pv_fetcher.publisher")


def publish_array(
    client: Any,
    output_topic: str,
    steps: list[dict],
    *,
    signal_mimir: bool,
    mimir_trigger_topic: str | None = None,
) -> None:
    """Publish a PV forecast payload and optionally trigger a mimirheim solve.

    Serialises ``steps`` to JSON and publishes it retained to ``output_topic``.
    If ``signal_mimir`` is True, publishes an empty non-retained message to
    ``mimir_trigger_topic`` afterward.

    The steps list must already be in mimirheim format — a list of dicts with keys
    ``ts``, ``kw``, and ``confidence``.

    Args:
        client: A paho-mqtt ``Client`` instance.
        output_topic: MQTT topic to publish the forecast payload to. Published
            retained with QoS 1.
        steps: Forecast steps in mimirheim format. Typically produced by
            ``confidence.apply_confidence()``.
        signal_mimir: Whether to publish an empty trigger to ``mimir_trigger_topic``
            after the forecast is published.
        mimir_trigger_topic: mimirheim's trigger topic. Required when
            ``signal_mimir`` is True; ignored otherwise.
    """
    payload = json.dumps(steps)
    client.publish(output_topic, payload, qos=1, retain=True)
    logger.debug("Published %d forecast steps to %s", len(steps), output_topic)

    if signal_mimir:
        client.publish(mimir_trigger_topic, payload=b"", qos=0, retain=False)
        logger.debug("Published mimirheim trigger to %s", mimir_trigger_topic)
