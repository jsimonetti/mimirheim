# mimirheim_shared never owns an MQTT connection

MQTT permits exactly one last-will (LWT) per connection. Mimirheim core's
existing primary connection already registers a last-will that clears
``{prefix}/status/availability`` on ungraceful disconnect. If the Config
Service protocol's ``describe()`` step called ``client.will_set()`` again on
that same connection to clear the Descriptor topic, it would silently replace
the availability last-will rather than add to it — a regression, not an
addition. The same constraint applies to any helper that already registers
its own last-will.

We considered giving ``mimirheim_shared`` its own connection-owning component
(constructing a ``paho.Client``, calling ``connect()``/``loop_start()``, and
registering the Descriptor's last-will on it directly) but rejected it:
whether a Config Owner needs a second, dedicated connection to avoid
clobbering its own last-will — or can safely share its primary one, if it
registers none — is a decision only the Config Owner's own connection-owning
code can make, since only it knows what else that connection's last-will is
already used for.

Instead, ``mimirheim_shared.config_service`` only provides pure data and
derivations: ``descriptor_topic()``, ``build_descriptor()``,
``descriptor_payload()``, and ``CLEARING_PAYLOAD``. A Config Owner's own
connection-management code — mimirheim core's ``ConfigServiceClient``
(``mimirheim/io/config_service.py``), or a helper's own MQTT setup — is
responsible for constructing and owning whichever ``paho.Client`` it uses for
the Config Service protocol, calling ``client.will_set(descriptor_topic(...),
payload=CLEARING_PAYLOAD, retain=True)`` before connecting, and
``client.publish(descriptor_topic(...), descriptor_payload(...), retain=True)``
from its own ``on_connect`` handler. This mirrors the existing pattern in
``mimirheim.io.mqtt_client.MqttClient``, which likewise receives an
already-constructed paho client rather than constructing one itself.

Mimirheim core, specifically, wires ``ConfigServiceClient`` onto a second,
dedicated paho client kept alongside its primary one, so neither last-will
clobbers the other.

## Considered Options

- mimirheim_shared owns a connection-managing Config Service client
  (constructs its own ``paho.Client``, connects, registers the last-will
  internally). Rejected: it cannot know whether the Config Owner's primary
  connection already has a last-will registered, so it cannot decide
  correctly whether a second connection is needed.
