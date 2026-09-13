# Config Service protocol lives in a new, narrowly-scoped mimirheim_shared package

The Config Service protocol (the MQTT contract, FormSpec types, and the
atomic comment-preserving config-writing utility) must be usable by both
mimirheim core and every helper. Core must not depend on helper packages,
and helpers should not need to pull in core's solver dependencies just to
become a Config Owner. We gave the protocol its own package instead of
placing it inside mimirheim core (which would force lightweight, possibly
split-repo helpers to depend on the solver) or inside the existing
helper-only common library (which core must not depend on). Its scope is
kept narrow — protocol only — and does not absorb the existing helper
common library's other responsibilities.
