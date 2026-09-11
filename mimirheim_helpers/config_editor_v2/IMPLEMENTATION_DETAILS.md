# config-editor-v2 — Implementation Details

## Purpose

config-editor-v2 is a web-based configuration editor for mimirheim and its
helpers. It renders an edit form for every registered configuration file from
that file's Pydantic model, validates edits against the real Pydantic model
before writing anything, and writes each configuration file back to disk
independently.

The defining property of this editor is that it carries no knowledge of any
specific configuration's shape. It does not know what a battery is, what a
price helper is, or that mimirheim exists as a distinct concept from its
helpers. Everything it needs to render a field, group fields, or treat a field
specially is read from that field's JSON Schema, specifically from a small set
of vendor-namespaced extension keys. Domain knowledge lives entirely in the
Pydantic models being edited; config-editor-v2 only interprets schema and
hints.

config-editor-v2 is a self-contained helper, independent of any other
configuration editor that may exist in this project. It does not share code,
process, port, or registry state with any other editor implementation. It can
be developed, tested, and run without affecting any other component.

## Non-goals

- Registering configuration models that live outside this project's own
  packages. A model must be importable from within this helper's Python
  environment to be registered. Packages that run in a different process or
  container, with their own dependencies, are not supported by this design.
- Presenting mutually exclusive fields (two fields where providing one implies
  the other must be absent) as a single grouped choice control. Such fields
  are rendered as independent fields; a conflict between them is caught by the
  underlying Pydantic model's own validation at save time.
- Enforcing a non-zero minimum length for a list-typed field that is allowed to
  be entirely absent. Such a field's true minimum, when it exists, is surfaced
  to the user as an advisory hint only; the authoritative check happens when
  the real Pydantic model validates the submitted data.
- Supporting more than one configuration source per named device slot, where
  the underlying domain model does not yet support multiple simultaneous
  instances of that slot. Any such limitation is inherited from the
  configuration model being edited, not introduced by this editor.

## Registry

Every top-level configuration file this editor can produce is described by one
entry in a static, hand-maintained registry, defined in Python inside this
helper's own package (`registry.py`). Each entry specifies:

- A human-readable name, shown in the editor's navigation.
- The YAML filename the entry writes to.
- A fully qualified dotted import path to the Pydantic model class that
  defines and validates that file's contents.

The registry is a plain list, not a dynamically discovered set. A new
configuration source is added to this editor by adding one entry to this list.
This is deliberate: a plugin-discovery mechanism (for example, Python entry
points) provides no benefit here, because every configuration source this
editor can register today is installed as part of the same Python package as
the editor itself. There is exactly one installable unit, so there is nothing
to discover at runtime that isn't already known when the registry list is
written.

A registry entry's model must be a normal, importable Pydantic class. The
registry does not accept a pre-serialized JSON Schema in place of an import
path, for the reason described in the next section.

## Why validation always goes through the real Pydantic model

JSON Schema, on its own, cannot express every constraint these configuration
models enforce. Constraints observed in practice include:

- Ordering relationships between fields (for example, one field must be less
  than or equal to another).
- Mutual exclusivity or mutual requirement between two fields.
- Numeric relationships that must hold across several fields at once.
- Validation that runs code against a field's value — for example, compiling a
  user-supplied formula string and rejecting the value if it does not compile.

None of these can be expressed as a JSON Schema constraint; several of them
(the last case in particular) cannot be expressed as data-shape validation of
any kind, because the check depends on executing code against the value, not
inspecting its shape.

Consequently, this editor never treats JSON Schema validation, or its own
adapter transforms, as sufficient to guarantee a configuration is valid. The
editor's own schema and data transforms exist only to make editing usable in
the browser. The only check that determines whether a save succeeds is a call
to the real Pydantic model's validation. This is also why every registry entry
must supply an import path rather than a static schema: without the class
itself, this editor has no way to run that validation at all.

## Save semantics

Each registered configuration file is written independently, as its own YAML
file, using an atomic write that preserves any existing comments in the file
on disk.

A save operation validates every registered configuration together before
writing any of them. If any one registered configuration fails validation, no
file is written — not even the files whose content did validate successfully.

This means the editor must:

- Attribute every validation failure clearly to the specific registered entry
  and field it came from, so a user can navigate directly to the problem
  without guessing which section is at fault.
- Never let a registered configuration that has never been touched by a user
  in a given save permanently block saving other, unrelated configurations.

An entry the user did not submit in a given save (its name is absent from
the save's submitted data entirely, not merely submitted as an empty object)
is handled as follows:

- If its model happens to validate with no data at all -- every field has a
  default -- it is included in the save and (re)written with those defaults.
- If it does not, the entry is silently excluded from that save: it is
  neither validated as a failure nor written. It simply is not part of this
  save.

This distinction matters because every real configuration model currently
registered in this editor requires at least an `mqtt` block, and most also
require a `trigger_topic`, neither of which has -- or should have -- a
universal default: an MQTT broker address and a trigger topic are
inherently site-specific. Treating an untouched entry's inevitable
`ValidationError` against `{}` as a blocking failure, as an earlier version
of this logic did, meant a user could never save anything at all unless
every registered helper's every required field was filled in simultaneously
in the same save, even a user who only wants to configure `mimirheim.yaml`
and does not use any of the other registered helpers. Excluding a
genuinely untouched entry from the save entirely, rather than trying and
failing to default it, is therefore not an arbitrary leniency: it is the
only way an editor covering several independently optional helpers can let
a user save the subset of configurations they actually use.

The "all files validated together before any is written" guarantee is
unaffected by this: it still holds for every entry that is either submitted
in the save or capable of validating against its own defaults. An entry
excluded as untouched-with-no-defaults never reaches that guarantee at all,
because it was never part of the save to begin with.

## The adapter

The adapter is the only part of this editor that has any knowledge of the
concrete UI rendering library in use. Everywhere else, "schema" means the
underlying model's JSON Schema; the adapter is what turns that schema, and the
data flowing to and from it, into whatever shape the rendering library
expects.

The adapter performs two operations on each registered model:

1. **Schema transform.** Starting from `model_json_schema()`, produce the
   schema document the rendering library will actually render. This transform
   is driven exclusively by explicit hints placed in each field's
   `json_schema_extra`, never by inferring intent from a field's structural
   shape. All hints the adapter reads are namespaced under the `x-mimir-`
   prefix; hints outside that namespace are assumed to belong to the rendering
   library directly and are passed through unmodified.
2. **Data transform.** Convert data between the on-disk/validated
   representation (whatever `model_dump()` produces and `model_validate()`
   accepts) and the representation the rendering library produces when a user
   submits the form. This transform must be the exact inverse of whatever the
   schema transform changed, so that data submitted by the form, once passed
   back through the data transform, is acceptable input to the real Pydantic
   model's validation.

### Transform dispatch

A field opts into a specific adapter behavior by setting
`x-mimir-adapter: <transform-name>` in its `json_schema_extra`. The adapter
maintains an internal mapping from transform names to transform
implementations. Adding a new transform means registering a new name in this
mapping; it does not require changing how any existing transform is selected
or applied. A field with no `x-mimir-adapter` hint is passed through with only
namespace-independent hints (such as a label or grouping key) applied, with no
structural change to its schema or data.

### The `nullable-list` transform

This transform handles a field typed such that its value is either `None` or a
non-empty list — that is, a JSON Schema shaped as an `anyOf` between an array
with a minimum item count and a null type. Without this transform, a
schema-driven rendering library sees an `anyOf` and is expected to ask the user
to choose which of the two alternatives ("array" or "null") they are filling
in, which is not a meaningful choice to present: the user is simply choosing
whether to provide any entries at all.

The transform's outgoing schema change:

- The `anyOf` between an array-with-minimum and null is replaced with a plain
  array schema, with no enforced minimum item count. The rendering library
  therefore presents an ordinary list editor that starts empty and accepts
  zero or more entries, with no special-cased "null" state visible to the
  user.
- If the original field declared a real minimum length greater than zero, that
  number is preserved in the rewritten schema as a separate, non-enforcing
  hint, intended only for optional in-form messaging (for example, a note that
  reads "at least two entries required if any are provided"). This hint is
  never expressed as the rewritten schema's own minimum item count, since doing
  so would reintroduce enforcement the transform is specifically designed to
  remove at this layer.

The transform's incoming (submitted) data change:

- If the submitted value for this field is an empty list, it is converted to
  `None` before the data is handed to the real Pydantic model for validation.
- Any non-empty submitted list is passed through unchanged.

This incoming-data conversion is required for correctness, not only for
appearance. Some models pair two nullable-list fields as alternatives to each
other, where providing one and leaving the other absent is required, and
providing both, or neither, is rejected. That check is written against
Python's `is not None`, which treats an empty list as "provided." Left
unconverted, a user who fills in one of the two alternative fields and leaves
the other's list editor empty would submit an empty list for the untouched
field, which reads as "both fields provided" to that check and produces an
incorrect validation error. Converting an empty submitted list back to `None`
before validation is what makes leaving a list editor empty equivalent, from
the real model's point of view, to never having set that field at all.

Because this transform intentionally does not enforce the field's true
minimum on the client side, a user can submit a list that is non-empty but
still shorter than the real required minimum. This is only caught when the
real Pydantic model validates the save, per the save semantics described
above. This is an accepted trade-off: the alternative is reintroducing an
enforced structural constraint the transform exists specifically to remove
from the client-facing schema.

This transform does not attempt to present two related nullable-list fields
(an alternative pair, where exactly one of the pair should be populated) as a
single grouped choice control. Both fields are rendered independently, and a
conflict between them is caught only by the real model's own validation at
save time, surfaced as a save-time error rather than prevented in the form.

## Namespace convention

Every hint this editor's adapter interprets is placed in a field's
`json_schema_extra` under a key beginning with `x-mimir-`. This includes, at
minimum, the transform-selection hint (`x-mimir-adapter`) and any advisory
hints a transform produces or consumes (such as the non-enforcing minimum
length hint described above). A hint under any other name is assumed to belong
to the rendering library itself and is left untouched by the adapter.

This project's configuration models are not required to use this namespace
today. Adopting this convention for a given model's existing hints, where that
model has any, is migration work performed when that model is first registered
with this editor, not a precondition for this editor's own implementation.

## Rendering library

This editor renders forms using Jedison, themed with Bootstrap. Both are
vendored directly into this helper's static assets rather than fetched from a
package registry at build or run time. Vendored code is checked in alongside
the license file each vendored library ships under, so that redistribution
terms are satisfied without depending on the vendored library's original
hosting remaining available.

Adopting a rendering library with vendored assets is a deliberate choice for
this helper. It is not a requirement placed on any other component in this
project, and no other component is expected to take on a client-side
dependency as a result of this decision.

### Jedison hint mapping

The Jedison-specific half of the adapter (`jedison_mapping.py`) maps this
editor's `x-mimir-` hints to the concrete schema attributes Jedison's
schema-driven form renderer reads. It runs after `adapter.transform_schema`
and any field-specific transform (such as `nullable-list`) have already
applied to a field's schema.

Two source hint names are introduced for this mapping, following the
`x-mimir-` namespace convention already established by `x-mimir-adapter` and
`x-mimir-min-length-hint`:

- `x-mimir-label`: this field's display label.
- `x-mimir-group`: the name of the section this field belongs to.

The mapping:

| Source | Destination |
|---|---|
| `x-mimir-label` on a field | Jedison's native `title` key on that field |
| `x-mimir-group` on a field | Jedison's `x-category` key on that field |
| Any field in a model carrying `x-mimir-group` | Jedison's `x-format` key, set once on the model's top-level object schema, to `"categories-vertical"` |
| `x-mimir-min-length-hint` on a field (written by the `nullable-list` transform) | Appended to Jedison's native `description` key on that field, rather than overwriting any existing description |

`"categories-vertical"` is this mapping's chosen default for `x-format`
(Jedison also supports `"categories-horizontal"`). Vertical category lists
degrade better on narrow viewports than a horizontal tab strip, which has to
scroll or wrap once there are more than a few sections; nothing in the
current design requires the horizontal layout instead.

Because grouping requires seeing every field in a model at once (to decide
the parent object schema's `x-format`), it cannot be resolved by a function
that only sees one field's schema in isolation. The label and advisory-hint
concerns, which are per-field, are handled by `to_jedison_schema(field_schema)`.
Grouping, which requires the whole model schema, is handled by a second
function, `to_jedison_object_schema(model_schema)`, which applies
`to_jedison_schema` to every field under `properties` and then resolves
grouping across all of them.

A field with none of these three hints is passed through unchanged. An
`x-mimir-` key this mapping does not recognise (including `x-mimir-adapter`,
which belongs to the rendering-library-agnostic half of the adapter, not to
this mapping) is left in place rather than dropped, so a future mapping
addition is additive. A hint outside the `x-mimir-` namespace is assumed to
already be in Jedison's own vocabulary and is left untouched, per the
"Namespace convention" section above.

## Deployment

This editor runs as its own process, independent of any other configuration
editor. If more than one configuration editor is running against the same
configuration directory at the same time, each editor writes files
independently and neither is aware of the other's writes; operators running
more than one editor concurrently are responsible for avoiding conflicting
edits to the same file from two editors at once.

## Open follow-up work

The following are known, deliberately unaddressed by this document. They are
not required for this editor to function as specified above, and are recorded
here so they are not lost, not because they block current work.

- Registering a configuration model that is not installed in this editor's own
  Python environment (for example, a model that runs in a separate process or
  container with its own dependencies) is unsupported. Supporting it would
  require the owning process to perform its own validation and writing,
  reachable through some request the editor makes to it, rather than this
  editor importing and validating the model directly. This is a materially
  different design, not an extension of the registry described above.
- Some configuration domains today enforce "exactly one of several sources"
  where the intended behavior is for multiple sources to contribute
  additively at once. Where that mismatch exists, it is a defect in the
  underlying domain model, not in this editor, and correcting it is out of
  scope for this document. This editor's registry and adapter design do not
  assume, and are not blocked by, any particular resolution of that mismatch.
- Presenting an alternative-pair of nullable-list fields as a single grouped
  toggle, rather than two independently rendered fields, is a possible future
  enhancement to the adapter's hint vocabulary. It is not implemented by the
  `nullable-list` transform described above.
- Enforcing a "zero, or at least N" constraint for a nullable-list field at the
  adapter layer, rather than leaving a too-short non-empty submission to be
  caught only at save time, is a possible future enhancement to the
  `nullable-list` transform. It is not implemented by the version described
  above.
