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

## Load semantics

`GET /api/entries/{name}/data` validates an existing on-disk file against
the entry's own model before returning it, and returns the *validated*
model's own dump, not the raw YAML dict. This is deliberate, for two
reasons:

- Pydantic never emits a literal `default` in JSON Schema for a field using
  `default_factory` (every device-map field and most config-section fields
  in `MimirheimConfig` use one). A partial on-disk file that omits such a
  field would otherwise reach the rendering library with that key simply
  absent, and the rendering library has no schema-level default to fall
  back on -- it silently fails to render that section at all rather than
  showing it with its real default values. Validating and re-dumping fills
  every such field in before the response is sent, so every field the model
  defines always has a concrete value in the response.
- An on-disk file that fails validation is rejected outright (HTTP 422, in
  the same `{entry_name, loc, message}` shape `POST /api/save` uses for its
  own errors), not passed through as-is or silently emptied. Fixing a file
  that fails validation is not this editor's job: if it was hand-edited
  into a broken state, that is the operator's mistake to fix; if it fails
  because it predates a Pydantic model change, migrating it forward is the
  responsibility of the owning helper or of mimirheim itself, not something
  this editor should paper over by serving stale or partial data.

This does not weaken "validation always goes through the real Pydantic
model" (see that section above): it extends the same principle to reads,
not only writes. A file that exists on disk but no longer validates is
already broken before this editor ever touches it; refusing to load it is
the same posture `POST /api/save` already takes toward a submission that
fails validation.

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

No transform is currently registered. The adapter is opt-in infrastructure:
it exists so that a field which genuinely needs schema- or data-level
rewriting has somewhere to register that behavior, not so that behavior
exists ahead of a real need. Do not add a transform speculatively; add one
when a specific field's rendering is actually broken, name it in that
field's own `x-mimir-adapter` hint, and document why in this section.

### Transform dispatch

A field opts into a specific adapter behavior by setting
`x-mimir-adapter: <transform-name>` in its `json_schema_extra`. The adapter
maintains an internal mapping from transform names to transform
implementations. Adding a new transform means registering a new name in this
mapping; it does not require changing how any existing transform is selected
or applied. A field with no `x-mimir-adapter` hint is passed through with
only namespace-independent hints (such as a label or grouping key) applied,
with no structural change to its schema or data.

Dispatch is selection by explicit hint only. There is deliberately no
shape-based auto-detection: a transform never fires just because a field
happens to match some structural pattern (for example, every `X | None`
field). An earlier version of this adapter added exactly that -- two
transforms, `nullable-list` and `nullable-scalar`, that fired automatically
on every nullable scalar or array field with no hint required -- and it was
removed. The problem was not the transforms' own behavior; it was that nobody
reviewing a schema change could tell, from the model alone, which fields were
being silently rewritten and why. Every transform that applies today does so
because some field's own `json_schema_extra` says so, visibly, at the point
where a reviewer is already looking.

### Reaching every field in a model's schema, not only its own top-level fields

`adapter.transform_schema` and `adapter.transform_incoming_data` each
operate on one field at a time. Two further entry points reach every field
anywhere in a registered model's schema, including fields nested inside
sub-models:

- `transform_schema_document(model_schema)` runs `transform_schema` over
  the model's own top-level `properties` and, separately, over every entry
  in `$defs` -- every sub-model the model uses, however deeply nested in
  the original Python type graph. This is not an open-ended recursive walk:
  `model_json_schema()` always hoists every referenced sub-model into one
  flat `$defs` map, so "top-level properties, plus each `$defs` entry's own
  properties" already reaches every field in the document, in exactly two
  passes.
- `transform_value_document(field_schema, value, defs)` applies
  `transform_incoming_data` to a submitted value and everything nested
  inside it. Submitted data carries no `$ref` pointers of its own, so this
  function resolves `$ref` and a null-pair `anyOf` against `defs` as it
  descends into an object's `properties`, a named-map field's
  `additionalProperties`, or an array's `items`. Every level's own
  transform is applied last, against that level's original, unresolved
  schema fragment, after its children have already been transformed.

Both whole-document functions are exercised today with no transform
registered, which is why they matter even though nothing currently changes
shape: a field nested inside a sub-model (`mqtt.client_id`,
`BatteryConfig.charge_segments`, and so on) needs `transform_schema_document`/
`transform_value_document` to be reached at all once a transform does exist
for it, not only a top-level field of the registered model itself.

## Namespace convention

Every hint this editor's adapter interprets is placed in a field's
`json_schema_extra` under a key beginning with `x-mimir-`. This includes, at
minimum, the transform-selection hint (`x-mimir-adapter`) and any advisory
hints a transform produces or consumes. A hint under any other name is
assumed to belong to the rendering library itself and is left untouched by
the adapter.

This project's configuration models are not required to use this namespace
today. A field that needs rendering-library-specific treatment with no
shape change (a category, a masked password input, and so on) is authored
using Jedison's own native vocabulary directly in that field's
`json_schema_extra` -- there is currently no translation layer between this
editor's own hints and Jedison's. Introducing one, and migrating existing
native hints into the `x-mimir-` namespace behind it, is deferred until a
second rendering library is actually in scope and the indirection earns its
keep.

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

Three points confirmed against the vendored bundle and Jedison's own docs,
not assumed from documentation alone, because each one broke real rendering
in browser testing before being fixed:

- Jedison never dereferences `$ref`/`$defs` on its own. Every registered
  model's schema is built almost entirely of `$ref`s to `$defs` (any nested
  Pydantic sub-model), so a `Jedison.RefParser` must be created and awaited
  (`refParser.dereference(schema)`) before `Jedison.Create()`, or every
  nested field renders as a meaningless generic type-switcher instead of
  its real fields.
- `Jedison.Theme()` is a bare base class; `Jedison.ThemeBootstrap5()` is
  what actually applies Bootstrap 5's form-control styling. Both ship in
  the same vendored UMD bundle.
- Jedison's "Add property" button is unconditionally suppressed whenever a
  schema's `additionalProperties` is exactly `false` (every `extra="forbid"`
  model in this project), independent of the `objectAdd` option -- so no
  per-field override is needed to hide it on a plain closed object.
  `static/app.js` additionally sets a global `objectAdd: false` at the
  `Create()` level, which also suppresses the "add a new named entry"
  control a dict-typed field (e.g. `batteries: dict[str, BatteryConfig]`)
  needs, since `additionalProperties` there is a schema, not `false`, and
  is therefore not covered by the automatic suppression above. A dict-typed
  field that wants that control back sets `x-addPropertyContent` (its
  custom add-button label); `jedison_mapping.to_jedison_object_schema`
  derives the matching `x-objectAdd: True` override for that same field
  automatically from `x-addPropertyContent`'s presence, so the two hints
  never need to be set by hand together. See `jedison_mapping.py`'s own
  module docstring for why this one derivation is not gated behind
  `x-mimir-adapter` like every other transform in this editor.

### Editor and option selection reference

This section records, for every JSON Schema shape this project's real
models actually contain, what Jedison's own default editor does, what (if
anything) this project overrides, and why -- including shapes deliberately
left at Jedison's default. It is the durable record step 71_7's
investigation produced; a future change to a specific field's rendering
should consult this before re-researching Jedison from scratch. Every claim
below was either confirmed against Jedison's own documentation
(`https://germanbisurgi.github.io/jedison-docs/`), the vendored bundle's
own source (`static/vendor/jedison/jedison.umd.js`, minified but not
obfuscated beyond variable renaming -- string literals survive and are
greppable), or a real, running instance of this editor inspected in a
browser.

**Plain, closed object (an `extra="forbid"` record with no dict-typed
field).** Jedison's default object editor renders a fieldset containing one
editor per property, with no override needed: no `x-objectAdd` hint is ever
set, because Jedison already suppresses its "Add property" button whenever a
schema's `additionalProperties` is exactly `false` -- true for every
`extra="forbid"` model in this project -- independent of any `objectAdd`
option (confirmed against the vendored bundle during earlier hand-testing;
see "Rendering library" above).

**Named-map (dict-typed) fields -- the priority investigation target.**
`MimirheimConfig.batteries` and its siblings (`pv_arrays`, `ev_chargers`,
`deferrable_loads`, `static_loads`, `hybrid_inverters`, `thermal_boilers`,
`space_heating_hps`, `combi_heat_pumps`) are Pydantic's `dict[str,
SomeConfig]`, rendered as `{"type": "object", "additionalProperties":
{"$ref": ...}}` with no fixed `properties` at all -- a different shape from
a closed record, and one `x-objectAdd` deliberately does not target (see
above). Confirmed empirically (bundle source, then a real browser session
against a running instance of this editor):

- Jedison's default object editor handles this shape correctly out of the
  box, with no hint needed: its "add a new named entry" flow is the same
  "Add property" button as a closed record's, but the key it resolves a new
  entry's schema against comes from `additionalProperties` (a schema, not
  `false`) whenever no fixed `properties` entry matches the typed key —
  this is exactly what lets `additionalProperties` remain a schema (not
  `false`) that a dict-typed field needs, and what makes the "Add property"
  button appear at all for these fields.
- Clicking "Add property" reveals an inline text input (labelled "Add
  property" by default, customisable via `x-addPropertyContent`) where the
  user types the new key. Confirmed in a real browser: typing a name and
  submitting creates a full child form for that entry, correctly built from
  the item model's own schema.
- Jedison has no hook that customises this quick-add input's own
  placeholder or help text (only its short label, via
  `x-addPropertyContent`); Jedison's `x-info` option, rendered next to a
  field's own heading with `variant: "modal"` (the only variant actually
  wired to display anything, confirmed against the vendored bundle), is the
  closest real equivalent for guidance on a new entry's naming convention.
  No field currently sets it.

**Plain string.** Jedison's default string editor (a single-line text
input) is adequate for every plain string field in this project's real
models except one: `mqtt.password` (and its `helper_common.MqttConfig`
counterpart), which carries `"x-format": "password"` directly in
`json_schema_extra` (a Jedison-native key, not `x-mimir-` namespaced --
see "Namespace convention"). Confirmed against the vendored bundle: the
default string editor reads a
field's `x-format` (not the JSON-Schema-standard `format` keyword) and, if
it matches one of a fixed list of HTML input types that includes
`"password"`, renders `<input type="password">`. No other string field in
this project's real models (grepped for a plausible secret-like name --
`api_key`, `token`, `secret`, `passwd`) needs the same treatment, and no
field is long enough to justify a `textarea` variant.

**Plain number/integer.** Jedison's default number editor already reflects
JSON Schema `minimum`/`maximum` (Pydantic's mapping of a field's `ge`/`le`)
as native HTML `min`/`max` attributes, because `useConstraintAttributes`
defaults to `true` in Jedison's own `Create()` defaults (confirmed against
the vendored bundle) -- no override, global or per-field, is needed. A
`gt`/`lt`-constrained field (JSON Schema `exclusiveMinimum`/
`exclusiveMaximum`) is *not* reflected as a native constraint attribute: the
default number editor's constraint-attribute code only reads
`minimum`/`maximum`. This is a real, confirmed gap (`SpaceHeatingConfig`'s
`elec_power_kw` and `cop` are the two affected real fields), left
unaddressed: the field still validates correctly server-side regardless
(per "Why validation always goes through the real Pydantic model"), the
only loss is a client-side HTML constraint hint, and fixing it would need a
new hint translation for two fields, which is not a high-confidence,
broadly-applicable improvement in the sense step 71_7 was scoped for.

**Plain boolean.** No real field in any currently registered model is typed
`bool | None`; every boolean field is a plain, required `bool` with a
default. Jedison's default boolean editor is adequate; nothing about a
plain `bool` field's rendering was found to need an override.

**`enum` / `Literal`-typed fields.** `SocTopicConfig.unit: Literal["kwh",
"percent"]` is the only real `Literal` field in scope (grepped across every
file step 71_6 already covered). Confirmed against the vendored bundle:
Jedison's string-editor-with-options resolves automatically whenever a
string schema carries a non-empty `enum`, with no `x-format` or other hint
required, and renders it as a native `<select>`. No override needed.

**Plain `X | None` (a nullable scalar or array with no other alternative).**
Confirmed against the vendored bundle and in a real browser session: Jedison
renders an untransformed `anyOf` between a concrete branch and a null branch
as a generic switcher control with one option per branch, and because
neither branch in this shape carries its own title, Jedison's own
switcher-building code merges the *field's* title into every branch that
lacks one -- so the switcher's two options end up carrying the literal same
label, with nothing to indicate what either one does. This is a real,
currently unaddressed defect, left at Jedison's default: `X | None` is one
of the most common field shapes in this project's real models (dozens of
fields per model), and collapsing it away for every such field
automatically, with no explicit hint, is exactly the "transform fires
without a reviewer being able to see why from the model alone" problem
"Transform dispatch" above describes -- see that section for why this
project no longer does that. Fixing a specific field that is actually
causing a usability problem, by registering a transform and naming it in
that field's own `x-mimir-adapter` hint, is the way to address this; doing
so for every nullable field pre-emptively is not.

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
- Every `X | None` field (a nullable scalar or array with no other
  alternative, including a None-or-submodel field such as
  `BatteryConfig.inputs`) renders as Jedison's default, unhelpful anyOf
  switcher -- see "Editor and option selection reference" above. This is a
  real, confirmed usability defect, deliberately left unaddressed until a
  specific field's rendering is a genuine problem worth registering a
  transform for; see "The adapter" above for why this project no longer
  fixes this class of shape pre-emptively across every field at once. A
  None-or-submodel field in particular would need its own investigation
  into how (or whether) Jedison can render an object editor with an
  explicit "unset this section" affordance, since `null` there is a real,
  reachable, meaningful state ("opt out entirely") that a naive
  collapse-to-non-null-branch would make impossible to express again in the
  form, unlike an empty list or an empty string.
- A pre-existing, unrelated browser console error
  (`TypeError: Cannot convert undefined or null to object` inside Jedison's
  own `removeNotListedPropertiesFromValue`, triggered while building the
  default value for a None-or-submodel field such as `BatteryConfig.inputs`
  that has no explicit `default` key of its own) was observed during manual
  verification. It does not visibly break rendering or data submission for
  any field tested. Recorded here so a future investigation into the
  None-or-submodel gap does not have to rediscover it from scratch.
