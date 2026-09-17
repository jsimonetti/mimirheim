# A Config Owner validates its Broker Settings independently of, and before, the rest of its configuration

Every Config Owner previously validated its entire configuration file as one
Pydantic model in a single pass, exiting on any failure — including a
failure in a field with nothing to do with MQTT. This meant a Config Owner
could not become reachable over the Config Service protocol, the mechanism
by which an operator is meant to fix a bad configuration, unless the whole
file already validated: a chicken-and-egg problem for anyone editing
configuration only through the Config Editor.

Startup now validates Broker Settings (the `mqtt:` section, environment-
overridden) on their own, first. Only a Broker Settings failure remains
fatal (`sys.exit(1)`): without a broker connection there is no way to
receive a fix at all, so waiting in place buys nothing. A failure anywhere
else in the file no longer exits the process; see ADR-0010 and ADR-0011 for
what happens instead.

## Considered Options

- Keep single-pass validation and let the Config Editor write directly to
  disk out-of-band (e.g. a shared volume mount) when a Config Owner is
  down. Rejected: reintroduces a second, non-MQTT configuration path that
  ADR-0001 already rejected for protocol uniformity, and does not work at
  all for a helper running in a genuinely separate container with no
  filesystem shared with the editor.
- Validate the whole file leniently, accepting whatever top-level sections
  happen to parse rather than singling out Broker Settings. Rejected:
  Broker Settings gate everything else — no MQTT, no Config Service
  protocol, nothing recoverable — so they earn a qualitatively different,
  always-fatal failure mode that no other section has.
