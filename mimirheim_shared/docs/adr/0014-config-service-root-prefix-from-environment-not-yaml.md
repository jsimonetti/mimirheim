# The Config Service topic root's prefix comes from an environment variable, never from any Config Owner's own YAML, and defaults to "mimir"

The Config Service protocol's topic root was a fixed literal,
`"mimirheim/config-service"`, unrelated to core's own `mqtt.topic_prefix`
field (which defaults to `"mimir"`). A deployment whose MQTT ACLs are
scoped to `mimir/#` — the namespace every business topic already lives
under — would not cover the Config Service protocol at all, without anyone
having deliberately chosen that.

The root's prefix now defaults to `"mimir"`, matching `mqtt.topic_prefix`,
and is overridable by a new `MQTT_PREFIX` environment variable read
directly by `mimirheim_shared.config_service`, independent of any Config
Owner's own parsed configuration.

## Considered Options

- Derive the Config Service root from core's own `mqtt.topic_prefix`
  field. Rejected: the Config Service root must be identical across every
  Config Owner in a deployment for discovery to work at all — core,
  every helper, and the editor all subscribe and publish under it. Tying
  it to one owner's individually-editable YAML field means changing that
  one field (e.g. to avoid a collision with an unrelated MQTT application)
  would silently detach that owner's Config Service traffic from
  everyone else's, with no validation to catch it. An environment
  variable, shared identically by every process in the same container, is
  the only thing that can guarantee agreement by construction.
- Give every helper's own `MqttConfig` a `topic_prefix` field too, so the
  root could be derived consistently from any owner. Rejected: helpers
  don't compose a topic tree from a prefix today — each publishes to
  individually named, explicitly configured topics — so this would add a
  field with no other purpose than feeding this one derivation, for a
  problem the environment variable already solves without touching
  helper schemas at all.
