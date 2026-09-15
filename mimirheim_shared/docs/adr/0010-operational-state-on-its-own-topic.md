# A Config Owner's Operational State is published on its own topic, not folded into the Descriptor or into per-cycle diagnostics

A Config Owner that fails full validation (ADR-0009) still needs to tell
the Config Editor, and any MQTT-based monitoring, "I'm here, but not
running." Two existing mechanisms looked like they might already cover
this and both turned out not to.

Operational State is instead published retained on its own well-known
topic, `mimirheim/config-service/<owner_id>/state`, alongside the existing
Descriptor, validate_and_write, and get_current_values topics, carrying
`{"state": "awaiting_configuration" | "operational", "detail": <string or
null>}`. It is published and cleared in lockstep with the Descriptor, using
the same last-will/graceful-clearing split ADR-0005 already established
for it.

## Considered Options

- Add a state field to the Descriptor payload itself. Rejected: the
  glossary already draws a deliberate line between the Descriptor
  (validation schema plus FormSpec — what a Config Owner *can* be
  configured as) and runtime state; folding the two together blurs a
  distinction the rest of the protocol relies on.
- Reuse `helper_common.cycle.CycleResult.exit_message`, published on a
  helper's existing `stats_topic`. Rejected: `stats_topic` is a field on
  the Config Owner's own fully-validated configuration object, which by
  definition does not exist while validation is failing — there is no
  reliable topic name to publish to. Mimirheim core has no equivalent
  field or topic at all. `exit_message` remains scoped to its original
  purpose (why the last operational cycle failed), unrelated to this.
