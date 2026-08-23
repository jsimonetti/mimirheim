"""Unit tests for helper_common.publish.publish_checked.

Every helper published its forecast at QoS 1 and then poked mimirheim with a
QoS 0 message, and none of them looked at the return code. Probed against a
live amqtt broker, the two levels behave differently while disconnected:

    while-down rc: qos0=4 qos1=4   queued=1
    broker delivered: [('probe/q1', 'qos1-while-down'), ('probe/marker', ...)]

The QoS 1 message was queued by paho and delivered on reconnect. The QoS 0
message was never queued and never arrived -- while the caller logged that it
had signalled mimirheim.

So MQTT_ERR_NO_CONN means two different things depending on QoS, and this
function has to distinguish them rather than treat every non-zero rc as fatal.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest

from helper_common.publish import PublishError, publish_checked


class _FakeClient:
    """Records publishes and returns a caller-chosen return code."""

    def __init__(self, rc: int = mqtt.MQTT_ERR_SUCCESS) -> None:
        self._rc = rc
        self.calls: list[tuple] = []

    def publish(self, topic, payload=None, qos=0, retain=False):  # noqa: ANN001
        self.calls.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self._rc)


class TestSuccess:
    def test_forwards_every_argument(self) -> None:
        client = _FakeClient()

        publish_checked(
            client, "mimir/input/prices", '[{"kw": 1}]', qos=1, retain=True,
            description="price forecast",
        )

        assert client.calls == [("mimir/input/prices", '[{"kw": 1}]', 1, True)]

    def test_returns_none_on_success(self) -> None:
        client = _FakeClient()

        assert (
            publish_checked(
                client, "t", b"", qos=0, retain=False, description="trigger"
            )
            is None
        )


class TestQos0IsLostWhenDisconnected:
    """A QoS 0 publish that paho could not send is gone. That is an error."""

    def test_raises_on_no_conn(self) -> None:
        client = _FakeClient(rc=mqtt.MQTT_ERR_NO_CONN)

        with pytest.raises(PublishError):
            publish_checked(
                client, "mimir/input/trigger", b"", qos=0, retain=False,
                description="mimirheim trigger",
            )

    def test_error_names_the_topic_and_the_reason(self) -> None:
        client = _FakeClient(rc=mqtt.MQTT_ERR_NO_CONN)

        with pytest.raises(PublishError) as exc:
            publish_checked(
                client, "mimir/input/trigger", b"", qos=0, retain=False,
                description="mimirheim trigger",
            )

        message = str(exc.value)
        assert "mimir/input/trigger" in message
        assert "mimirheim trigger" in message
        assert mqtt.error_string(mqtt.MQTT_ERR_NO_CONN) in message

    @pytest.mark.parametrize(
        "rc",
        [mqtt.MQTT_ERR_QUEUE_SIZE, mqtt.MQTT_ERR_PAYLOAD_SIZE, mqtt.MQTT_ERR_INVAL],
        ids=["queue-full", "payload-too-large", "invalid"],
    )
    def test_raises_on_any_other_failure(self, rc: int) -> None:
        client = _FakeClient(rc=rc)

        with pytest.raises(PublishError):
            publish_checked(
                client, "t", b"", qos=0, retain=False, description="trigger"
            )


class TestQos1IsQueuedWhenDisconnected:
    """paho stores a QoS 1 message and redelivers it, so NO_CONN is not a loss."""

    def test_no_conn_does_not_raise(self) -> None:
        client = _FakeClient(rc=mqtt.MQTT_ERR_NO_CONN)

        publish_checked(
            client, "mimir/input/prices", "[]", qos=1, retain=True,
            description="price forecast",
        )

    def test_no_conn_is_logged_as_queued(self, caplog: pytest.LogCaptureFixture) -> None:
        client = _FakeClient(rc=mqtt.MQTT_ERR_NO_CONN)

        with caplog.at_level(logging.WARNING):
            publish_checked(
                client, "mimir/input/prices", "[]", qos=1, retain=True,
                description="price forecast",
            )

        assert "queued" in caplog.text.lower()
        assert "mimir/input/prices" in caplog.text

    @pytest.mark.parametrize(
        "rc",
        [mqtt.MQTT_ERR_QUEUE_SIZE, mqtt.MQTT_ERR_PAYLOAD_SIZE],
        ids=["queue-full", "payload-too-large"],
    )
    def test_other_failures_still_raise_at_qos_1(self, rc: int) -> None:
        """Only NO_CONN is benign at QoS 1; a full queue really did drop it."""
        client = _FakeClient(rc=rc)

        with pytest.raises(PublishError):
            publish_checked(
                client, "mimir/input/prices", "[]", qos=1, retain=True,
                description="price forecast",
            )
