"""MQTT publisher for the PV ML forecast pipeline.

This module serialises a list of ``ForecastStep`` objects into the mimirheim
payload format and publishes it to the configured MQTT topic, retained at
QoS 1.  It mirrors the structure of ``pv_fetcher.publisher`` but operates on
``ForecastStep`` dataclasses rather than pre-built dicts.

What this module does not do:
- It does not call any external API.
- It does not compute forecasts or confidence values.
- It does not import from mimirheim.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pv_ml_learner.predictor import ForecastStep

logger = logging.getLogger(__name__)


def publish_forecast(
    client: Any,
    output_topic: str,
    steps: list[ForecastStep],
    *,
    signal_mimir: bool,
    mimir_trigger_topic: str | None = None,
) -> None:
    """Publish a PV forecast payload and optionally trigger a mimirheim solve.

    Serialises ``steps`` to JSON in mimirheim's ``PowerForecastStep`` format and
    publishes the payload retained to ``output_topic``.  If ``signal_mimir`` is
    ``True``, also publishes an empty non-retained message to
    ``mimir_trigger_topic`` to prompt an immediate re-solve.

    This call raises ``ValueError`` before any MQTT interaction if
    ``signal_mimir`` is ``True`` but ``mimir_trigger_topic`` is ``None``.

    Args:
        client: A paho-mqtt ``Client`` instance.
        output_topic: MQTT topic for the forecast payload.
        steps: Predicted PV output steps.
        signal_mimir: Whether to publish an MQTT trigger to ``mimir_trigger_topic``
            after the forecast is published.
        mimir_trigger_topic: mimirheim's trigger topic.  Must be provided when
            ``signal_mimir`` is ``True``.

    Raises:
        ValueError: If ``signal_mimir`` is ``True`` and ``mimir_trigger_topic``
            is ``None``.
    """
    if signal_mimir and mimir_trigger_topic is None:
        raise ValueError(
            "signal_mimir is True but mimir_trigger_topic is None. "
            "Set mimir_trigger_topic in the configuration."
        )

    payload_list = [
        {
            "ts": step.ts.isoformat(),
            "kw": step.kw,
            "confidence": step.confidence,
        }
        for step in steps
    ]
    payload_str = json.dumps(payload_list)

    client.publish(output_topic, payload_str, qos=1, retain=True)
    logger.debug("Published %d forecast steps to %s.", len(steps), output_topic)

    if signal_mimir:
        client.publish(mimir_trigger_topic, payload=b"", qos=0, retain=False)
        logger.debug("Published mimirheim trigger to %s.", mimir_trigger_topic)
