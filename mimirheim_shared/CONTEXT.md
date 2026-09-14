# Config Service

The protocol and vocabulary for how mimirheim core and every helper expose
their own on-disk configuration for discovery, presentation, and editing.
This context is about the mechanism of configuring a process; it is not
about what any particular configuration field means (that vocabulary
belongs to the context that owns the field, e.g. the solver domain for
battery or PV settings).

## Language

**Config Owner**:
A process — mimirheim core or a helper, whether in-process or running in a
separate container — that holds exactly one configuration file and is the
only entity permitted to validate and write it.
_Avoid_: helper (a Config Owner may be core, not a helper), publisher

**Config Editor**:
The consumer of the Config Service: discovers Config Owners via their
Descriptors, renders forms from their FormSpecs, and submits Candidate
Values for validation and writing. Never writes a configuration file
itself.
_Avoid_: the app, the UI, the editor backend

**Descriptor**:
The payload a Config Owner publishes, retained, describing its
configuration: the validation schema together with its FormSpec.
_Avoid_: schema (ambiguous between the validation shape and the
presentation layer)

**FormSpec**:
The presentation-only counterpart to a Config Owner's validation model:
labels, help text, documentation links, Tier, grouping, Tab placement, and
Conditional Visibility. Carries no validation rules of its own.
_Avoid_: UI schema, schema extension, x- annotations

**Field Shape**:
A FormSpec field's structural kind — scalar, nested object, Named
Collection, Ordered Collection, optional object, or enum select — derived
automatically from the Config Owner's validation model. Distinct from
Tier, which classifies a field's presentation priority, not its structure.
_Avoid_: field type (ambiguous with the validation model's own type),
widget

**Shape Override**:
An explicit FormSpec setting that renders a field with a simpler Field
Shape than the one derived from its validation model, e.g. a nested object
shown as opaque raw text instead of recursed into. Only ever simplifies;
never gives a field structure its validation model doesn't have.
_Avoid_: widget override, type coercion

**Named Collection**:
A Field Shape for a mapping of entries that all validate against the same
nested model, each identified by a name (e.g. one battery per key).
_Avoid_: dict field, map field

**Ordered Collection**:
A Field Shape for a sequence of entries that all validate against the same
nested model, identified by position rather than name (e.g. a battery's
efficiency segments).
_Avoid_: list field, array field

**Tier**:
A FormSpec field's classification as Basic or Expert, controlling whether
it is shown by default or hidden behind an Advanced disclosure.
_Avoid_: level, mode (mode implies a single global switch; tiering is
per-field)

**Tab**:
A FormSpec-level grouping that partitions a Config Owner's fields into
top-level navigable panes. A field with no Tab set belongs to the default
Tab, "General".
_Avoid_: section, page

**Subtab**:
A second, nested level of Tab, scoped within one Tab. The Tab/Subtab
hierarchy is exactly two levels deep; it does not nest further.
_Avoid_: sub-group, nested tab

**Conditional Visibility**:
A declarative, serializable rule on a FormSpec field — a comparison
against another field's value, optionally combined with AND/OR — that
determines whether the field is shown given the current Candidate Values.
Never expressed as executable code.
_Avoid_: visibility logic, dynamic field

**Presence Toggle**:
The control that expresses whether an optional nested object field exists
at all, independent of whatever values its own fields hold once present.
_Avoid_: null checkbox, optional flag

**Suggested Value**:
A FormSpec-authored hint value for a scalar field, shown as static help
text alongside the field rather than pre-filled into it. Distinct from the
Config Owner's own schema default (which does pre-fill the field) and from
Candidate Values (the submission payload); a Suggested Value never
validates and never submits on its own.
_Avoid_: default (reserved for the schema's own default), placeholder

**Candidate Values**:
The set of proposed field values a Config Editor submits to a Config Owner
for validation and writing. Distinct from the values currently on disk.
_Avoid_: form data, payload, submission
