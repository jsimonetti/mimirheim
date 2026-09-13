# Config Service protocol is MQTT-uniform for every Config Owner

Helpers can run in-process or in a separate container, and mimirheim core
itself also needs to be editable. We considered giving in-process Config
Owners a direct-call shortcut (e.g. entry-point-discovered Python
callables) alongside a remote MQTT path for out-of-process helpers, but
rejected the two-path design: every Config Owner, including core, publishes
its Descriptor and accepts Candidate Values over MQTT only, with no
in-process shortcut. This keeps the protocol identical regardless of
deployment topology, since helpers and the Config Editor are expected to
eventually move into a separate repository, and MQTT is already a hard
runtime dependency of mimirheim core.

## Considered Options

- Local direct call (entry-points) for in-process Config Owners, MQTT for
  remote ones. Rejected: two code paths to maintain, and the local
  shortcut would have to be removed or reimplemented once helpers split
  into a separate repository.
