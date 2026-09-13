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
labels, help text, documentation links, Tier, grouping, and Conditional
Visibility. Carries no validation rules of its own.
_Avoid_: UI schema, schema extension, x- annotations

**Tier**:
A FormSpec field's classification as Basic or Expert, controlling whether
it is shown by default or hidden behind an Advanced disclosure.
_Avoid_: level, mode (mode implies a single global switch; tiering is
per-field)

**Conditional Visibility**:
A declarative, serializable rule on a FormSpec field — a comparison
against another field's value, optionally combined with AND/OR — that
determines whether the field is shown given the current Candidate Values.
Never expressed as executable code.
_Avoid_: visibility logic, dynamic field

**Candidate Values**:
The set of proposed field values a Config Editor submits to a Config Owner
for validation and writing. Distinct from the values currently on disk.
_Avoid_: form data, payload, submission
