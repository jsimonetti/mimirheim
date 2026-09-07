# config-editor — schema contract specification

This document is authoritative for the config-editor's schema contract: what
a schema file is, how it is discovered, what vocabulary it may use, how
documents compose, how validation and saving work, and the complete HTTP API.
It plays the same role for this directory that root `README.md` plays for
mimirheim's external behaviour and `IMPLEMENTATION_DETAILS.md` plays for its
internal architecture.

---

## Contents

1. [Schema file format](#1-schema-file-format)
2. [Discovery](#2-discovery)
3. [The `x-mimirheim` root envelope](#3-the-x-mimirheim-root-envelope)
4. [Field vocabulary](#4-field-vocabulary)
5. [Document composition](#5-document-composition)
6. [Cross-references](#6-cross-references)
7. [Validation model](#7-validation-model)
8. [Save semantics](#8-save-semantics)
9. [Enabled state](#9-enabled-state)
10. [Rejection rules](#10-rejection-rules)
11. [Security](#11-security)
12. [HTTP API](#12-http-api)
13. [Limits](#13-limits)

---

## 1. Schema file format

A schema file is a single JSON document with the extension `.schema.json`.
Its filename stem (the part before `.schema.json`) is its **id** and must
match `^[a-z0-9][a-z0-9-]*$`. The id is how the registry, the HTTP API, and
error messages refer to the entry; it need not match the YAML filename it
edits (that mapping is explicit, via `x-mimirheim.file`, see §3).

The schema must be valid JSON Schema in the dialect Pydantic 2's
`model_json_schema()` emits. A schema file does not need to declare
`$schema` explicitly. If it does, it must be
`https://json-schema.org/draft/2020-12/schema`; anything else is rejected
(§10).

The root of the schema must have `"type": "object"` and, following the
project-wide `ConfigDict(extra="forbid")` convention, should set
`"additionalProperties": false`. The root must carry an `x-mimirheim`
envelope (§3).

---

## 2. Discovery

Schemas are found by scanning two directories, in this order:

1. **Bundled**: `config_editor/schemas/bundled/*.schema.json`, packaged
   inside the config-editor wheel. Generated from Pydantic models by
   `scripts/generate_schema_json.py`, committed, and drift-tested the same
   way `mimirheim/config/schema.json` is.
2. **Drop-in**: `<config_dir>/schemas/*.schema.json`, user-supplied. This is
   the entire extension mechanism — a third-party schema goes through
   exactly the same discovery, validation, and rendering code as a bundled
   one. `mimirheim.yaml` itself is not special-cased.

Discovery never imports Python modules to determine what schemas exist.
Whether a helper's package is installed is determined by checking whether
`x-mimirheim.python_package` (§3) is importable.

### Collision handling

Ids are unique across both directories combined. Collisions are rejected,
never merged and never allowed to shadow one another:

- **A drop-in id colliding with a bundled id is rejected.** The bundled
  schema always wins. Silently overriding a bundled helper would let a
  stale user file break a supported helper across an upgrade with no
  visible cause.
- **A drop-in id colliding with another drop-in id**: files are processed in
  sorted filename order within the drop-in directory; the first schema to
  claim an id is accepted, every later one with the same id is rejected.
  This is deterministic across restarts.
- **A bundled id colliding with another bundled id** cannot happen at
  runtime by construction — each bundled schema is generated 1:1 from one
  Pydantic model with a deliberately chosen filename, and the generator's
  own drift test catches a duplicate before it is committed. If it happens
  anyway, that is a packaging bug, not user input, and the server should
  fail loudly at startup rather than silently pick one.

A schema rejected for any reason (collision or otherwise, §10) never
prevents another schema from loading, and never crashes the server. It is
recorded as a problem, surfaced through `GET /api/registry` and
`POST /api/reload` (§12), and logged.

---

## 3. The `x-mimirheim` root envelope

The one custom vocabulary that survives §4's "zero custom field keys" goal.
It describes the file's relationship to mimirheim — which file it writes,
how it is grouped and ordered, whether its package is installed — not how
to render anything. No form-rendering library can supply this; it has
nothing to do with forms.

```json
"x-mimirheim": {
  "file": "nordpool.yaml",
  "category": "prices",
  "order": 10,
  "python_package": "nordpool",
  "python_model": "nordpool.config:NordpoolConfig",
  "exclusive_group": null,
  "required": false,
  "docs_url": "https://github.com/.../wiki/Helpers/Nordpool"
}
```

| Key | Type | Required | Meaning |
|---|---|---|---|
| `file` | string | yes | The YAML filename this schema edits, e.g. `"nordpool.yaml"`. Must match `^[a-z0-9][a-z0-9_-]*\.yaml$`, must contain no path separator, and must not equal `"config-editor.yaml"` (config-editor does not edit its own live configuration). |
| `category` | string | yes | One of a closed set used for nav grouping and ordering: `core`, `prices`, `pv`, `baseload`, `scheduling`, `reporting`, `other`. Any other value is rejected. |
| `order` | integer | no (default `0`) | Sort key within `category`. |
| `python_package` | string | yes | The importable package name that determines whether this helper is installed. For `mimirheim.yaml` itself this is the literal string `"mimirheim"`, always considered installed. |
| `python_model` | string | no | `"module.path:ClassName"` dotted path for a second Pydantic validation pass (§7). Bundled-only (§11); ignored with a logged warning on a drop-in. |
| `exclusive_group` | string or null | no (default `null`) | Entries sharing a group name are mutually exclusive: enabling one deletes every other member's YAML file as part of the same transactional save (§8). The three baseload schemas share `exclusive_group: "baseload"`. |
| `required` | boolean | no (default `false`) | `true` only for `mimirheim.yaml`. A required entry's file must exist for mimirheim to run; the editor never offers to disable (delete) it, only to edit it. |
| `docs_url` | string or null | no | Must be an `https://` URL if present. |

Every key not needed by the implementation should be dropped rather than
carried out of habit; this table is the complete list, not a starting point.

---

## 4. Field vocabulary

Schema authors use standard JSON Schema plus Jedison's own vocabulary. There
is no mimirheim-specific field-level vocabulary beyond the root
`x-mimirheim` envelope (§3):

- **Field titles** use the standard `title` keyword.
- **Basic vs. advanced grouping**: the enclosing object sets
  `x-format: "categories-vertical"` and
  `x-categoryOrder: ["Basic", "Advanced"]`. A field with no `x-category`
  falls into the default "Basic" group; a field belonging in the advanced
  group sets `x-category: "Advanced"`.
- **Enum sources** (e.g. selecting one of `pv_arrays` or `static_loads` by
  key) use `x-enumSource` pointed at the relevant `context` path (§6), with
  no `x-format` override. This resolves to a native `<select>`, not radio
  buttons.
- **Auto-derived display placeholders that need the entry's own map key**
  (e.g. an MQTT topic containing `{array_key}`) use a custom editor selected
  via `x-format: "mimir-topic-placeholder"` (§6). Do not use `x-template` for
  these.
- **Auto-derived values with no per-entry key component** use `x-watch`
  (document-absolute paths only) and `x-template` (Mustache `{{ }}` syntax).

`x-format` is Jedison's own generic "which editor renders this field"
extension point — the same mechanism that selects `"radios"`,
`"tom-select"`, and `"categories-vertical"`. Giving it a mimirheim-specific
value for the placeholder-display case is using the tool as designed,
backed by a registered custom editor. No other custom `json_schema_extra`
key exists.

---

## 5. Document composition

Each schema entry, including `mimirheim.yaml` itself, is rendered as its own
independent Jedison instance. There is no merged, cross-schema document.

Every entry other than `mimirheim.yaml` is given a synthesized, read-only
`context` subtree:

```json
{
  "<the entry's own schema properties, unmodified>": "...",
  "context": {
    "type": "object",
    "readOnly": true,
    "properties": {
      "mqtt_topic_prefix": { "type": "string" },
      "pv_arrays": { "type": "object", "additionalProperties": { "type": "object" } },
      "static_loads": { "type": "object", "additionalProperties": { "type": "object" } }
    }
  }
}
```

`context` is a snapshot built from the last successfully loaded
`mimirheim.yaml` at the moment the entry is opened. It is identical in shape
for every non-`mimirheim.yaml` entry. `mimirheim.yaml`'s own entry has no
`context` key — it is the source, not a consumer, of this data. `context` is
never included in what gets written back to disk (§8).

Because `context` is a snapshot rather than a live link, a change made in
`mimirheim.yaml` while another entry's editor is already open is not
reflected there until that entry is reloaded.

### Mechanics

An entry's instance is constructed only when its tab is opened
(`GET /api/entry/<id>`, §12).

Every schema, for every entry, must be dereferenced via a `Jedison.RefParser`
(constructed with `fetch: undefined`, §11) and an awaited `dereference()`
call before the instance is constructed. Skipping this silently discards
every constraint in the schema: fields still render, but `getErrors()`
always reports clean. A canary test must assert that a known-invalid value
on a real bundled schema produces at least one error (§11).

- `instance.getValue()` on the entry's root returns that file's data
  directly; strip the injected `context` key before writing to YAML.
- `instance.getErrors()` on the entry's root is the authoritative validity
  check for that file.
- `instance.isDirty` is set by any field using `x-watch` + `x-template`
  (§4, §6) as soon as the instance is constructed, before the user has
  touched anything. **Immediately after constructing an entry's instance
  from its freshly-loaded value, walk the tree and reset every `isDirty`
  flag to `false` before rendering it to the user.**

---

## 6. Cross-references

### Enum sources: `pv_arrays` and `static_loads`

`x-enumSource` pointed at `#/context/pv_arrays` or `#/context/static_loads`
resolves to a native `<select>` with no further configuration, and
enumerates an `additionalProperties`-shaped map's keys automatically. Do not
set `x-format` to `radios`.

Because `context` is a snapshot (§5), a rename made in `mimirheim.yaml`
while a helper's editor is already open does not appear in that helper's
picker until it is reloaded.

### Topic templates

`x-watch` targets must be **document-absolute paths** (e.g.
`#/context/mqtt_topic_prefix`) — unlike `x-enumSource`, `x-watch` performs no
relative-path resolution, and a relative path silently resolves to nothing.

**Do not use `x-template` for any field whose derivation needs its own map
key** — the common shape, e.g. a topic templated as
`{mqtt_topic_prefix}/input/pv/{array_key}/forecast`. `x-template`'s value
computation cannot reach an item's own key. Keep the field's actual value
`null` to mean "auto-derive at runtime," exactly as the runtime validator
already expects. For **display only**, use a small custom Jedison editor,
selected via `x-format: "mimir-topic-placeholder"`, that reads
`instance.parent.getKey()` (a public property) and
`context.mqtt_topic_prefix` to render a live-computed placeholder string in
the input's `placeholder` attribute — and never calls `setValue()` on the
user's behalf.

`x-watch` + `x-template` is appropriate for the rare field whose derivation
needs no per-entry key component. Template syntax is Mustache-style
`{{ expr }}` and supports a `{{ expr || 'default' }}` fallback, interpolated
correctly into the middle of a string.

---

## 7. Validation model

Two passes, in two different runtimes:

1. **Client-side**, live, as the user types: Jedison's own validator, driven
   by the dereferenced schema. This is UX feedback, not a trust boundary.
2. **Server-side, on submit**, in order:
   - `jsonschema` (Python) validates the submitted dict against the same
     schema, for every entry, bundled or drop-in.
   - If, and only if, the entry is bundled and its schema declares
     `x-mimirheim.python_model`, a second pass constructs the named Pydantic
     model from the submitted dict. This preserves cross-field and
     custom-validator checks JSON Schema cannot express. A drop-in's
     `python_model` is ignored with a logged warning (§3, §10); drop-ins get
     structural validation only. This limitation must be stated plainly to
     third-party schema authors, not left as a footnote.

`instance.hasNestedValidationErrors()` is a UI-state convenience and must
never be used as the save-time validity check. `instance.getErrors()` is
always live and always accurate; it is the only API used to gate a save.

---

## 8. Save semantics

Config is one system, not N independent files. The unit of a save is **the
dirty set**, not every registered entry — files the user did not touch are
never rewritten, which preserves their comments and avoids spurious diffs.

An entry counts as dirty if, after the post-construction `isDirty` reset
(§5), its instance's `isDirty` is `true` at submit time. Since `context` is
read-only and is never part of what gets written back, it can never itself
make an entry dirty.

**Save is validate-all-then-write-all:**

1. For every dirty entry, run the full validation model (§7).
2. If any dirty entry fails, reject the entire save. Write nothing. Return
   per-entry-scoped error lists (§12) — never a flat, semicolon-joined
   string.
3. If every dirty entry passes, write each one via the existing
   comment-preserving, atomic (`os.replace`) mechanism, one file at a time.
4. If an entry's `x-mimirheim.exclusive_group` is set, enabling or saving it
   deletes every other group member's YAML file as part of the same
   transactional save. The frontend must warn the user which files will be
   deleted before they confirm.

**True atomicity across multiple files is not available.** `os.replace` is
atomic per file; a crash between two file renames in the same save leaves a
partial write. This window is accepted; a journal is not worth building for
this tool.

**A file loaded from disk that already fails validation is loaded anyway.**
Its nav entry is marked invalid (this comes free from Jedison's native
validation badge propagation), its errors are shown inline, and the user can
fix and save. Unknown keys are never silently dropped.

---

## 9. Enabled state

File presence on disk is the sole source of truth. Enabling a helper writes
its YAML file (from the submitted config, or from the model's own Pydantic
defaults if it did not exist); disabling deletes it.

This is orthogonal to Jedison entirely. A disabled, non-`required` entry has
**no Jedison instance constructed for it at all** — its nav slot shows a
plain "not enabled" placeholder with an enable action, not a document with
an internal `enabled: false` flag. There is no "absent subtree" state to
represent, because a disabled entry has no subtree.

---

## 10. Rejection rules

Every rejection reason must: name the offending key or file, state an
actionable fix, never contain an absolute filesystem path, and never be
empty. A schema failing any of these is excluded; every other valid schema
still loads (§2).

| # | Rule |
|---|---|
| 1 | Schema file exceeds the maximum size (§13). |
| 2 | File is not valid JSON. |
| 3 | Schema fails meta-schema validation against `config_editor/schemas/_meta/mimirheim-helper-schema.meta.json`. |
| 4 | Root `type` is not `"object"`. |
| 5 | `x-mimirheim` is missing, or malformed (missing required key, wrong type, or an unknown key inside the envelope). |
| 6 | `x-mimirheim.file` fails the pattern `^[a-z0-9][a-z0-9_-]*\.yaml$`. |
| 7 | `x-mimirheim.file` contains a path separator. |
| 8 | `x-mimirheim.file` equals `"config-editor.yaml"`. |
| 9 | `x-mimirheim.category` is not one of the closed set (§3). |
| 10 | Id collision — with a bundled schema, or with an earlier-loaded drop-in (§2). |
| 11 | Any `$ref` in the schema is non-local (does not start with `#/`). |
| 12 | Schema nesting depth exceeds the limit (§13). |
| 13 | Total property count (root plus all `$defs`) exceeds the limit (§13). |

**Not a rejection:** `x-mimirheim.python_model` present on a drop-in schema.
The schema still loads; the model is simply never used, and a warning is
logged (§3, §11).

---

## 11. Security

**Cross-entry `context` access is not a security boundary.** A hostile
drop-in schema has no egress path: no `<script>` execution, CSP blocks it
(below), and remote `$ref` is rejected at load (§10, rule 11). The worst a
hostile schema can do is show the user their own data, already visible
elsewhere in their own configuration. Path restriction on what a schema may
reference inside `context` is deliberately not implemented and should not
be reintroduced.

**`x-mimirheim.python_model` is bundled-only, with no override.** Importing
a dotted path named inside a user-supplied file is arbitrary code execution.
No configuration relaxes this for drop-ins.

**`Jedison.RefParser` must be constructed with `fetch: undefined`.** Its
default `fetch` option will attempt a network request for a `$ref` whose
value looks like a URL. §10 rule 11 (server-side rejection of any non-local
`$ref` at schema-load time) is the primary control; `fetch: undefined` on
the client is defense-in-depth for a malformed schema that slips past it.
Without this explicit opt-out, "no internet access at runtime" is not
actually enforced.

**A Content-Security-Policy header must be present** and must block all
inline and remote script execution while Jedison still renders correctly
(acceptance test: with devtools set to offline, no request leaves the
origin).

**No schema-derived string is ever assigned through `innerHTML`.** A
schema's `title`, `description`, or any other string is always inserted via
`textContent` or an equivalent escaping API.

**The `RefParser.dereference()` step (§5) is a correctness invariant, not
only a performance one.** Skipping it does not fail loudly — it silently
disables all validation for every field, everywhere, with no visible
symptom (`getErrors()` returns clean). A canary test must assert that a
known-invalid value on a real bundled schema produces at least one error,
to catch a regression here.

---

## 12. HTTP API

All responses are `application/json`. Status codes: `200` success, `400`
malformed JSON or bad `Content-Length`, `403` disallowed IP, `404` unknown
entry id or path, `422` validation failure.

| Method | Path | Request | Response |
|---|---|---|---|
| `GET` | `/` | — | Single-page editor frontend. |
| `GET` | `/static/<file>` | — | Frontend static assets. |
| `GET` | `/api/registry` | — | Every discovered entry: `id`, its `x-mimirheim` fields, `enabled` (file exists), and load-time rejection problems for entries that failed to load. Builds the nav rail in one call. |
| `GET` | `/api/entry/<id>` | — | `{"schema": <dereferenced schema with injected context>, "value": <current file content, or Pydantic defaults if not enabled>, "enabled": bool}` for exactly one entry. Fetched when the user opens that entry's tab. |
| `POST` | `/api/save` | `{"entries": {"<id>": {"enabled": true, "config": {...}} \| {"enabled": false}, ...}}` | `{"ok": true}` or `{"ok": false, "errors": {"<id>": [...]}}`. Transactional over the submitted entries per §8. |
| `POST` | `/api/preview` | Same shape as `/api/save`. | A computed YAML diff per touched file. Never writes anything. Must share the exact merge/write implementation `/api/save` uses, so a preview can never differ from what saving would actually do. |
| `POST` | `/api/reload` | — | Re-runs discovery (§2) without restarting the process. Returns the same shape as `GET /api/registry`. |

---

## 13. Limits

| Limit | Value | Reason |
|---|---|---|
| Max schema file size | 256 KiB | Generous headroom over the largest bundled schema; bounds a hostile or broken drop-in. |
| Max schema nesting depth | 20 | Bounds the dereference/namespace walk against pathological recursion in a drop-in. |
| Max property count per schema (root + all `$defs`) | 2000 | Generous headroom above the largest bundled schema; bounds validation and render cost. |
| Max POST request body | 1 MiB | The largest legitimate payload is a full config as JSON, a few tens of KB. |

No hard cap is placed on the number of entries inside one named map
(`pv_arrays`, `static_loads`, etc.) within a single open entry; each entry's
render cost is bounded only by Jedison's own per-item construction cost for
that one file.
