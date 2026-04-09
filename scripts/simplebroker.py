from __future__ import annotations
import argparse
import asyncio
import logging
import signal
from typing import Any
from amqtt.broker import Broker

logging.basicConfig(level=logging.WARNING, format="%(message)s")
logging.getLogger("amqtt.broker").setLevel(logging.INFO)

"""Development MQTT broker for local mimirheim testing.

This module starts a minimal MQTT broker with anonymous access and no ACL checks.
It is intended for local developer workflows only. It does not provide durable
message storage or production-grade security controls.
"""

class LoggingBroker(Broker):
    """MQTT broker that logs every published topic update to stdout."""

    async def broadcast_message(  # type: ignore[override]
        self,
        session: Any,
        topic: str,
        data: bytes,
        qos: Any = None,
        retain: bool = False,
    ) -> Any:
        """Log publish traffic and forward to the standard broker flow.

        Args:
            session: Publisher session object from the broker.
            topic: MQTT topic name.
            data: Raw payload bytes.
            qos: MQTT QoS level.
            retain: Whether the message is retained.

        Returns:
            The result from the base broker implementation.
        """
        try:
            payload = data.decode("utf-8")
        except UnicodeDecodeError:
            payload = data.hex()

        print(
            f"PUBLISH topic='{topic}' qos={qos} retain={retain} payload='{payload}'",
            flush=True,
        )
        return await super().broadcast_message(session, topic, data, qos=qos, retain=retain)


async def run_broker(host: str, port: int) -> None:
    """Start the development MQTT broker and keep it running.

    Args:
        host: TCP bind host.
        port: TCP bind port.
    """
    config: dict[str, Any] = {
        "listeners": {
            "default": {
                "type": "tcp",
                "bind": f"{host}:{port}",
            }
        },
        "plugins": ["amqtt.plugins.authentication.AnonymousAuthPlugin"],
    }

    broker = LoggingBroker(config)
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await broker.start()
    print(f"MQTT broker started on {host}:{port}", flush=True)

    try:
        await stop_event.wait()
    finally:
        await broker.shutdown()
        print("MQTT broker stopped", flush=True)


def main() -> None:
    """Parse CLI arguments and run the broker."""
    parser = argparse.ArgumentParser(description="Run a simple open MQTT broker.")
    parser.add_argument("--port", type=int, default=1883, help="Bind port (default: 1883)")
    args = parser.parse_args()

    asyncio.run(run_broker("127.0.0.1", args.port))


if __name__ == "__main__":
    main()