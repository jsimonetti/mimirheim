# A Restart Request always exits the process; it never reloads or reconstructs a Config Owner's operational state in place

Once a validate_and_write (or a manual edit) puts a valid configuration on
disk, a Config Owner still needs to be told to pick it up — nothing watches
the file for changes, and a Config Owner does not re-validate on its own.
We add a general-purpose `restart_request`/`restart_response` pair to the
Config Service protocol, distinct from Save: on receipt, a Config Owner
clears its Descriptor and Operational State, disconnects gracefully, and
exits, relying on the container supervisor (`ENABLE_<SERVICE>`-gated per
ADR-0013) to start a fresh process against whatever is currently on disk.

## Considered Options

- In-process reload: tear down and reconstruct a Config Owner's own
  operational object graph (MQTT subscriptions, trigger topics, discovery
  payloads, the solve loop's device set) from the new configuration without
  exiting. Rejected: every Config Owner would need individually auditing
  for whether its own object graph can be safely torn down and rebuilt
  live — a materially larger and more error-prone surface than exiting and
  letting the existing process boundary and supervisor do the same job.
- Make every successful validate_and_write restart automatically, with no
  separate action. Rejected explicitly: Save and Restart are distinct
  actions with different blast radii — a save an operator wants to review
  before bouncing the process, versus one they want applied immediately —
  matching the Config Editor's existing separate (if previously stubbed)
  Restart control.
