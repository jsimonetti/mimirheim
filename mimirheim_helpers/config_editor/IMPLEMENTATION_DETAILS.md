# config-editor — Jedison integration implementation details

This document is authoritative for how config-editor's frontend integrates
with the Jedison form-rendering library: exact API behaviour, required setup
that is easy to get silently wrong, and the reasoning behind architectural
choices `SPEC.md` states without justifying. It plays the same role for this
directory that root `IMPLEMENTATION_DETAILS.md` plays for mimirheim's
internal architecture generally. Jedison is used nowhere outside
config-editor's `static/` frontend; this document has no bearing on
mimirheim core or any helper package.

Where this document and `SPEC.md` overlap, `SPEC.md` is the contract
(*what* to build); this document is *how* the library actually behaves
underneath that contract, at a level of detail the contract deliberately
does not carry.

---

## Vendored library

`config_editor/static/vendor/jedison.umd.js`, version 1.21.0, SHA-256
`dba01c6151538e8444c7ed05169b9b842b3f24589a8fb31feb86018f1b7daac6` (matches
the registry `shasum` for `jedison-1.21.0.tgz`). `LICENSE` and
`CHANGELOG.md` from the same release are vendored alongside it. MIT
licensed, single maintainer (`germanbisurgi`, also maintainer of
`json-editor`, of which Jedison is the successor).

Before bumping the vendored version, re-check every section below against
the new release's `CHANGELOG.md` — several of these behaviours are recent
(1.17.0 through 1.21.0) and could plausibly change again.

---

## Dereferencing is mandatory and fails silently if skipped

Jedison does not dereference `$ref` unless the caller supplies its own
`RefParser` and `await`s `dereference()` before constructing the instance:

```js
const refParser = new Jedison.RefParser({ fetch: undefined });
await refParser.dereference(schema);
const jedison = new Jedison.Create({ schema, data, refParser, ... });
```

`Create`'s default `refParser` option is `null`. Without one,
`createInstance()` never calls `.expand()`, so every `$ref` node resolves to
an effectively empty schema (`originalSchema: {}}`) — every constraint is
silently discarded, fields still render generically, and `getErrors()`
always reports clean. This is invisible without specifically checking
`instance.originalSchema` or `instance.getErrors()`. Since every real
Pydantic-generated schema is `$defs`/`$ref`-based throughout, this is not an
edge case — it is the default case whenever this step is missed.

`fetch: undefined` is not optional either: `RefParser`'s default `fetch`
option is `fetch.bind(globalThis)`, so by default it will attempt a real
network request for any `$ref` whose value looks like a URL. With
`fetch: undefined`, `collectRefs` simply fails to resolve that ref and
leaves it `null` (a "Missing refs" console warning, not a network call).

`RefParser` also has a built-in cycle detector
(`findRecursiveRefs`/`markRecursiveSchemas`, since 1.0.0):
`refParser.hasRefCycles()` / `.cycles` reports self-referential `$defs`
correctly without false-flagging unrelated same-named definitions in a
different namespace, provided the namespacing scheme below is used.

---

## Enum sources (`x-enumSource`)

The default resolution for any `string` field with `x-enumSource` set and no
`x-format` is already a native `<select>` (editor class
`EditorStringSelect`). Radio buttons are opt-in
(`x-format: "radios"`, editor class `EditorStringRadios`), never the
fallback.

`x-enumSource` pointed at an `additionalProperties`-shaped map (no fixed
`properties`, e.g. `pv_arrays`) auto-detects it and uses `Object.keys()` of
the current value with no extra configuration:

```js
getEnumSourceValues() {
  return this.enumSourceValues !== undefined
    ? (isArray(this.enumSourceValues) ? this.enumSourceValues
       : isObject(this.enumSourceValues) ? Object.keys(this.enumSourceValues) : [])
    : (schema.enum || []);
}
```

The list updates live: `jedison.watch(path, cb)` fires on any `setValue()`
at the watched path, and `EditorStringSelect.refreshOptions()` re-reads the
map and repopulates `<option>` elements with no reload.

`x-enumSource` targets are resolved *relative to the current instance's
path* (an internal `j(path, ref)` resolver supporting `.`/`..` segments) —
this is the opposite of `x-watch`'s absolute-path-only behaviour below, and
mixing the two up is an easy mistake.

**Custom editor priority**: every built-in editor's `static priority()`
defaults to `0`; `getClass()` tries all `customEditors` first (sorted by
`priority()`, which only matters with more than one registered), then
built-ins in fixed declaration order. A custom editor is never starved by a
built-in — register a `customEditors` entry whose `resolves()` matches a
chosen `x-format` string, and copy the ~15-line
`setupEnumSource()`/`getEnumSourceValues()`/`refreshOptions()` pattern from
`EditorStringSelect` if a searchable/typeahead control is ever wanted for a
very long list. Neither Tom Select (`x-format: "tom-select"`, a
multi-select for arrays of enum values, not a single-value dropdown) nor
Awesomplete (`x-format: "awesomplete"`, a free-text autocomplete unrelated
to `x-enumSource`) fits a single-value constrained picker out of the box.

For `x-watch` (not `x-enumSource`) against the same kind of map, the
dot-path template resolver (`I(data, "a.b.c")`, plain property-path
walking) can only reach a specific, already-known key
(`{{ arrays.value.roof_pv.max_power_kw }}`) — it has no `Object.keys()`/
wildcard support. Enumeration is an `x-enumSource`-only capability.

---

## Templates and computed placeholders (`x-watch` / `x-template`)

`x-template` forces/overwrites the field's value; it is not a
non-destructive hint. Its computation
(`setValueFormTemplate(){const t=lt(this.schema,"template");c(t)&&t&&this.setValue(w(t,this.watched))}`)
unconditionally calls `setValue()`, discarding any existing value
(user-typed or loaded from YAML) at construction time. Template syntax is
Mustache-style `{{ expr }}`, supports a `{{ expr || 'default' }}` fallback,
and interpolates correctly into the middle of an arbitrary string via a
single regex pass over all tokens.

**`x-watch` targets must be document-absolute paths.** Unlike
`x-enumSource`, `x-watch` targets are passed as-is into
`jedison.getInstance(ref)`/`jedison.watch(ref, cb)` with no relative-path
resolution — a relative path silently resolves to nothing
(`this.watched` stays `{}` forever). This means `x-watch` can never express
"a sibling relative to me" inside a `$ref`-shared subschema.

**A dynamic map entry's own key cannot be reached from `x-template`'s value
computation.** Every instance knows its own key
(`this.key = this.path.split(pathSeparator).pop()`, exposed via the public
`getKey()`), and a `functions` constructor option exists that substitutes
arbitrary function results at `{{ functions.name }}` — but
`setValueFormTemplate()` templates only against the `x-watch` bag
(`w(t, this.watched)`), never against `getTemplateData()`, which is what
carries `functions`. A `functions.ownKey` resolver correctly returns the
key when called directly, but renders as an empty segment inside a value
`x-template`. `functions` does work inside `x-titleTemplate` (tab/nav/array-
item titles, which does call `getTemplateData()`) — so
`"x-titleTemplate": "Array {{ functions.ownKey }}"` on a map entry renders
correctly, while the equivalent inside a value `x-template` does not.
`arrayTemplateData`'s synthetic `{i0, i1}` array-index context is likewise
never populated for object-property children, closing that door too.

**Consequence for anything shaped like
`{prefix}/input/pv/{array_key}/forecast`:** do not use `x-template` for a
field whose derivation needs its own map key — there is no native path
today. Use `x-watch` + `x-template` only where no per-entry key is needed
(a fixed cross-field derivation). For the per-entry-key case, keep the
field's actual value semantics unchanged (`null` = auto-derive at runtime,
resolved by the existing Pydantic/runtime logic) and use a small custom
editor, following the priority pattern above, that reads
`instance.parent.getKey()` directly to render a live-computed *placeholder*
string without ever calling `setValue()` unless the user types into the
field. Forcing a value here would also trip the `isDirty` false positive
below on every affected entry, on every load.

---

## `$defs` merging across independently-generated schemas

Not needed by the architecture `SPEC.md` §5 specifies (each entry keeps its
own model's `$defs`, never combined with another schema's), but kept here
because it works and may be revisited if cross-file live reactivity is ever
worth its cost (see "Why per-entry documents" below).

Two independently-generated schemas can define different models under the
same `$defs` name — a real example: mimirheim's own `MqttConfig` (8
properties) and a helper's independently-generated `MqttConfig` (7
properties, missing `topic_prefix`) share the def name with different
shapes. A naive `{...defsA, ...defsB}` merge lets one clobber the other.

Working scheme: prefix every `$defs` name by a per-schema namespace
(e.g. `mimir__`, `nordpool__`) and rewrite every `"$ref": "#/$defs/Name"` to
`"#/$defs/<ns>__Name"` via a recursive tree walk, once per source schema,
before merging the `$defs` maps. This correctly isolates same-named defs
from different sources, and `RefParser.hasRefCycles()`/`.cycles` correctly
attributes a self-referential cycle to the right namespaced def without
false-flagging an unrelated same-named def in a different namespace.

---

## Validation and dirty-state APIs

Two different APIs answer "is this subtree valid", and they are not
interchangeable:

- **`instance.getErrors()`** — always live, always accurate, scoped to
  exactly that instance's own value and schema; calls the validator
  directly, independent of any UI/DOM state. This is the correct API for a
  save-time validity check.
- **`instance.hasNestedValidationErrors()`** — a pure UI-state read
  (`this.ui.showingValidationErrors`, recursively through children), not a
  live computation. It only updates after that field's own DOM `"change"`
  event has run `showValidationErrors(this.getErrors())`. Setting a value
  on a *parent* object instance rather than the specific leaf field does
  not update this flag, because the parent's `setValue()` does not dispatch
  the leaf's `"change"` listener. **Never use this as a save-gate** — it is
  a UI convenience only.

`instance.isDirty` is a genuine public property: initialised `false`,
flipped `true` by that instance's own `setValue()`, and propagated to every
ancestor (`this.parent.isDirty = true` on the `"notifyParent"` bubble).
**There is no reset method** — it only ever transitions `false → true`; the
application must explicitly reset it if it wants "unsaved changes since
last load/save" semantics for a long-lived instance.

**False-positive trap**: because `x-template` forces a `setValue()` at
construction for every field that carries one, and `setValue()`
unconditionally sets `isDirty = true`, every entry with a templated field
becomes "dirty" immediately on load, before the user has touched anything.
An "unsaved changes" indicator built on `isDirty` must explicitly walk the
tree and reset it right after initial construction, or it will falsely
claim every document with any `x-template` field is dirty from the moment
it opens.

---

## Navigation and grouping

`x-format: "nav-vertical"` on an object schema (editor class
`EditorObjectNav`, resolves for `x-format` matching
`/^nav-(horizontal|vertical)$/`) renders one nav tab per direct child
property — this is the entry rail, one item per config file.

`x-format: "categories-vertical"` on an enclosing object plus
`x-category: "Advanced"` on individual fields (editor class
`EditorObjectCategories`) implements Basic/Advanced grouping. Fields with no
`x-category` fall back to `x-categoriesDefaultLabel` (default literally
`"Basic"`) — only fields that need the advanced group need tagging.
`x-categoryOrder: ["Basic", "Advanced"]` controls which tab is active by
default; since categories are tabs, not accordions, "Advanced starts
collapsed" is achieved by putting `"Basic"` first in this list, not by a
separate collapse mechanism.

Validation-invalid state propagates natively through every nesting level:
both `EditorObjectNav` and `EditorObjectCategories` tabs get a
`.jedi-nav-warning` badge whenever `childInstance.hasNestedValidationErrors()`
is true (governed by `x-navWarning`/`x-navWarningMessage`), bubbling all the
way to the entry rail with no custom code. `deprecated: true` on a field
schema adds a `jedi-deprecated` CSS class to its container (since 1.21.0,
issue #72) — a usable hook for anything that needs to style a deprecated
field differently.

Neither dirty state nor "enabled" (a helper's file existing vs. not, an
app-level concept Jedison has no notion of) has a native nav indicator.
Both need the application to listen for `jedison.on("instance-change", ...)`
(or poll `isDirty`) and manually toggle a CSS class on the corresponding tab
element.

---

## `applyOverlay`

`applyOverlay(schema, {actions: [{target, update|remove}]})` (since 1.17.0)
applies OpenAPI-Overlay-style structural edits to a schema copy without
touching the original. Its `target` selector is a small hand-rolled
JSONPath subset supporting only literal property names, `.*` wildcards, and
bracket selectors — there is no predicate/filter syntax
(`[?(...)]`; the parser explicitly rejects `(`). It cannot express "every
field where some condition holds", only "the field at this exact known
path". Not a good mechanism for a bulk, schema-wide vocabulary translation
(that belongs at schema-generation time, in the Pydantic model or a
generation-script post-process step, both of which already know every
field's exact key). Good for small, targeted, environment- or
deployment-specific presentation patches applied at request time without
touching the committed schema, where the exact path is already known ahead
of time.

---

## Why per-entry documents, not one merged document

`SPEC.md` §5 specifies one independent Jedison instance per schema entry,
each given a synthesized read-only `context` subtree, rather than merging
every schema into one document. The reasoning:

- `x-watch`/`x-enumSource` only ever resolve within the single Jedison
  instance currently constructed (`jedison.getInstance()`/`jedison.watch()`
  operate on `this.instances`, private to one `Create()` call) — a
  `context` subtree populated from the last-loaded `mimirheim.yaml` gives a
  helper's instance the same `x-enumSource: "#/context/pv_arrays"`
  resolution a merged document would, at the cost of same-session live
  cross-file reactivity (a rename in `mimirheim.yaml` is not reflected in
  an already-open helper's picker until that helper reloads).
- The `$defs` namespacing/rewriting scheme above works, but is an ongoing
  maintenance surface: every helper added or model changed needs the merge
  step re-verified. Per-entry needs none of this; each schema stays exactly
  what `model_json_schema()` produces.
- Measured render cost (Chromium, `performance.now()` around the
  synchronous `new Jedison.Create(...)` call) for a realistic four-schema
  merged document (30 `pv_arrays` entries, 15 `static_loads` entries, all
  other `MimirheimConfig` sections, plus three full helper schemas) was
  ≈4.7 seconds first paint, with `refParser.dereference()` itself
  negligible (≈3ms) — the cost is entirely instance/DOM construction.
  `EditorObjectNav`/`EditorObjectCategories` build every child eagerly; a
  merged document has no way to defer this to "when the user actually
  clicks that file's tab". Per-entry only ever constructs the instance for
  the file currently open, and this cost does not grow as more helpers are
  added to the merged alternative's document.

The merge mechanics above are kept on file, not deleted, in case live
cross-file reactivity is ever judged worth this cost and maintenance
surface explicitly — but the current architecture does not default to it.

---

## Known defect: nullable, length-bounded array crashes on construction

A schema shaped like `anyOf: [{type: "array", maxItems: N, ...}, {type: "null"}]`
with a current value of `null` crashes at construction
(`EditorArray.refreshAddBtn` reads `.length` of `null`), because the
`anyOf`/`oneOf` switcher (`EditorMultiple`) mis-detects which branch fits a
`null` value when `maxItems` is present (`minItems` alone does not trigger
it). This is not contrived: `baseload_static.config.BaseloadConfig.profile_kw`
(`anyOf` array with `maxItems: 168` / `null`, default `null`) hits it
exactly.

No data-level fix is available — coercing `null` to `[]` would violate
`minItems: 1` and the model's own "at least one profile source" rule. The
workaround is to strip `maxItems` from the *presentation* schema handed to
Jedison only, leaving the authoritative constraint enforced server-side by
Pydantic at save time, exactly as the rest of the two-pass validation model
already works. File this upstream
(`github.com/germanbisurgi/jedison`) before shipping any implementation
that touches nullable, length-bounded arrays without this workaround
already in place.
