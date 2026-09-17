# mimirheim_shared never owns an MQTT connection; a Config Owner shares its one connection and accepts graceful-only Descriptor clearing when its last-will slot is already spent

MQTT permits exactly one last-will (LWT) per connection — confirmed against
paho-mqtt's own implementation, where `will_set()` overwrites a single
stored topic/payload pair rather than adding to a list, mirroring the single
Will Topic/Message field in the MQTT CONNECT packet (v3.1.1 and v5 alike).
Mimirheim core's primary connection already spends that one slot clearing
`config.outputs.availability` on ungraceful disconnect — a documented,
tested, Home-Assistant-consumed contract that predates the Config Service
protocol.

We first gave the Config Service protocol's `describe()` step its own
second, dedicated connection so the Descriptor topic could get a real,
crash-safe last-will without touching availability's. We reversed that: an
extra persistent broker connection (its own TLS handshake, its own
reconnect/retry surface, its own thing to monitor) is not worth it just to
buy crash-safety for a secondary, lower-stakes retained topic, when a
cheap, already-precedented fallback exists — explicit clearing on graceful
shutdown, the same pattern mimirheim core's primary connection already uses
for availability's "offline" publish before disconnecting.

Mimirheim core now shares its one existing connection for the Config
Service protocol. `config.outputs.availability`'s last-will is untouched.
No last-will is ever registered for the Descriptor topic: it is published
retained on connect and explicitly cleared (empty retained publish) on
graceful shutdown only. The trade-off this accepts: an ungraceful
disconnect (a crash) leaves the retained Descriptor stale on the broker
until the process restarts and republishes it. That is a deliberate,
disclosed limit, not a defect to chase.

`mimirheim_shared.config_service` itself is unaffected by this reversal —
it was already pure data (`descriptor_topic()`, `build_descriptor()`,
`descriptor_payload()`, `CLEARING_PAYLOAD`) with no connection ownership,
and stays that way. What changed is only which of a Config Owner's own
connections calls those functions, and whether that Config Owner also
calls `client.will_set()` for the Descriptor topic.

This is core's problem specifically, not the general one. No helper in
this codebase registers a last-will today. A helper Config Owner with no
pre-existing last-will can give its own Descriptor a real, crash-safe
native LWT on its own single connection with no conflict at all — the
graceful-only fallback above is what a Config Owner reaches for
specifically when its one last-will slot is already spoken for, not the
default every Config Owner should assume it needs.

A per-publish MQTT v5 Message Expiry Interval could bound the crash-time
staleness window without a second connection or touching the availability
last-will. Deliberately deferred: it requires opting the connection into
MQTT v5 (currently unset, so paho defaults to v3.1.1) and depends on broker
support that is unverified here, including the in-process amqtt broker the
integration test suite uses.

## Considered Options

- mimirheim_shared owns a connection-managing Config Service client
  (constructs its own `paho.Client`, connects, registers the last-will
  internally). Rejected: it cannot know whether the Config Owner's primary
  connection already has a last-will registered, so it cannot decide
  correctly whether a second connection is needed.
- A second, dedicated connection per Config Owner purely to give the
  Descriptor its own native last-will (the first version of this ADR).
  Rejected: an extra persistent broker connection is a real, ongoing cost
  (TLS handshake, reconnect surface, one more thing to monitor) to buy
  crash-safety for a topic where "stale until the next restart" is an
  acceptable, disclosed limit. Graceful-shutdown clearing is cheap and
  already precedented by how `config.outputs.availability` itself is
  handled on clean shutdown.
- Repointing the one available last-will slot at the Descriptor topic
  instead of availability. Rejected: it would regress availability's
  existing, external, documented, tested crash-safety to gain the same for
  the newer, lower-stakes Descriptor topic — the wrong topic to give up
  crash-safety for.
