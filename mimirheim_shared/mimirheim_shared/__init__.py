"""mimirheim_shared: the Config Service protocol shared by every Config Owner.

A Config Owner is mimirheim core or a helper, in-process or running in its
own container, that holds exactly one configuration file. This package
holds the pieces every Config Owner and the Config Editor need in common:

- ``formspec``: presentation-only types (label, description, help text,
  Tier, grouping) kept separate from a Config Owner's pydantic validation
  model.
- ``visibility``: the declarative, JSON-serializable Conditional Visibility
  condition type and its evaluator.
- ``atomic_write``: the atomic, comment-preserving YAML write utility used
  by every Config Owner's own ``validate_and_write`` implementation.
- ``alignment``: the spec/model alignment assertion each Config Owner's test
  suite calls to verify its FormSpec fully describes its validation model.

This package does not implement the MQTT transport itself (the ``describe``/
``validate_and_write`` request-response exchange); that is layered on top of
these types in a later step. It also does not validate or write any
Config Owner's configuration on its own behalf: ownership of a
configuration file always stays with the Config Owner that holds it.
"""
