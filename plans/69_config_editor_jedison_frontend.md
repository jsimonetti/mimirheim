# Plan 69 — config-editor: Jedison frontend rewrite

## Purpose

Delete the hand-rolled schema walker and the three bespoke tab renderers in
`static/app.js`, and render every entry through Jedison against the
`/api/registry` / `/api/entry/<id>` / `/api/save` / `/api/preview` /
`/api/reload` endpoints plan 68 built. Add the Content-Security-Policy
header. Delete the deprecated endpoint aliases plan 68 kept alive.

Read `mimirheim_helpers/config_editor/SPEC.md` §4, §5, §6, §9, §11, and §12
in full before starting, and `mimirheim_helpers/config_editor/IMPLEMENTATION_DETAILS.md`
in full for the exact library API shapes referenced below (enum sources,
templates and placeholders, and navigation/grouping in particular). This
plan implements that specification; it does not re-derive it.

---

## Branch

Requires plan 68 done first — this plan depends on the new endpoints and
the migrated field vocabulary existing in the bundled schemas.

Do not push until the user has reviewed the result.

---

## Prerequisites

Plan 68 merged. `mimirheim_helpers/config_editor/schemas/bundled/*.schema.json`
exist and carry the migrated vocabulary (`title`, `x-category`,
`x-enumSource`, `x-format: "mimir-topic-placeholder"` where applicable), and
`GET /api/registry` / `GET /api/entry/<id>` / `POST /api/save` /
`POST /api/preview` / `POST /api/reload` all work.

```bash
uv run pytest
uv run pytest mimirheim_helpers/config_editor/tests
```

Record baseline counts.

---

## Decisions

### 1. Vendored library

`static/vendor/jedison.umd.js` is already vendored (SHA-256
`dba01c6151538e8444c7ed05169b9b842b3f24589a8fb31feb86018f1b7daac6`, version
1.21.0). Before starting, check whether a newer 1.x release exists. If so,
do not blindly bump — re-verify the specific behaviours documented in
`IMPLEMENTATION_DETAILS.md` that a changelog entry could affect
(particularly anything touching `x-enumSource`, `x-watch`/`x-template`, or
`RefParser`) against the new version before vendoring it, and update
`IMPLEMENTATION_DETAILS.md` and the checksum together.

### 2. `mimir-topic-placeholder` custom editor

A small, separately-defined JS function registered via `customEditors` (per
`IMPLEMENTATION_DETAILS.md`'s custom-editor priority pattern: custom editors
always take priority over built-ins). It reads `instance.parent.getKey()` and
`context.mqtt_topic_prefix` to render a live-computed string in the
underlying `<input>`'s `placeholder` attribute, and never calls
`setValue()` unless the user types into the field (SPEC §6). Write this as
one exported function so it can be exercised from a Python-side integration
test's rendered-HTML assertions (see TDD workflow) without needing a JS test
runner.

### 3. Nav and entry construction

Root nav built from `GET /api/registry`: `x-format: "nav-vertical"`, one
item per entry, ordered by `category` then `order` (SPEC §3). A disabled,
non-`required` entry renders a placeholder with an enable action; no
Jedison instance is constructed for it (SPEC §9). Opening an entry's tab
calls `GET /api/entry/<id>` and constructs exactly one instance for it.

### 4. Mandatory `RefParser` setup, one shared helper

Every instance construction goes through one shared function that builds
`new Jedison.RefParser({fetch: undefined})`, awaits `dereference()` on the
entry's schema, then constructs `new Jedison.Create(...)` (SPEC §5, §11).
This is not duplicated per call site — there is exactly one entry point,
so it cannot be skipped by a future call site added carelessly.

### 5. `isDirty` reset after construction

Immediately after constructing an entry's instance from its freshly-loaded
value, walk the tree and reset every `isDirty` flag to `false`, per SPEC
§5. Write this as one function called from the same place `RefParser`
setup happens (Decision 4), not scattered per field type.

### 6. CSP header

Added in `server.py` in this plan, not plan 68, because the correct policy
depends on how the new frontend actually loads scripts and styles. Start
from the tightest policy that blocks all inline and remote script
execution, then loosen only what the running Jedison + custom editor setup
demonstrably needs, verified by the acceptance test in Scope below — never
loosen speculatively.

### 7. Deprecated aliases deleted

`GET /api/schema`, `GET /api/config`, `POST /api/config`,
`GET /api/helper-configs`, `GET /api/helper-schemas`,
`POST /api/helper-config/<filename>` are deleted from `server.py`, along
with their thin-adapter tests from plan 68, once nothing in `app.js` calls
them.

### 8. Deep-link migration

The old `#helper=<filename>` URL fragment redirects to the new
`?entry=<id>` scheme for one release, so a bookmarked or saved link does
not silently 404.

### 9. No JS test framework

None exists in this repo; adding one is a separate decision, out of scope
here. Verification is a mix of Python-side integration tests against the
served HTML/JS where that is enough (see TDD workflow) and a written manual
checklist executed in light mode, dark mode, and at phone width, since this
editor is usually opened through the Home Assistant companion app.

---

## Scope

### In scope

- `static/app.js` — full rewrite.
- `static/style.css` — rewrite around design tokens, a real spacing scale,
  light and dark via `prefers-color-scheme`.
- `static/index.html` — updated to load the vendored Jedison UMD build and
  the custom editor module.
- `server.py` — add the CSP header; delete the six deprecated aliases and
  their plan-68 adapter tests.
- `mimirheim_helpers/config_editor/AGENTS.md` — remove the "target, not
  current" caveat for the sections of `SPEC.md` this plan completes (§4,
  §6, §11's CSP item, §12's alias removal).

### Deleted from `app.js`

`buildForm`, `buildFieldRow`, `buildSubList`, `buildDictOfModel`,
`collectFormData`, `stripNulls`, `setNestedValue`, `getNestedValue`,
`getDef`, `resolveRef`, `buildHelperForm`, `collectHelperFormData`,
`renderGeneralTab`, `renderHeatingTab`, `renderBaseloadTab`,
`renderHelperTab`, `buildTabBar`, `activateTab`, `syncHelperPrefixes`.

### Ported, not dropped

- MQTT env redaction round-trip and read-only rendering of Supervisor-
  supplied values (already served correctly by the plan-68 endpoints; this
  plan's job is to render what they return, not to reimplement redaction).
- Exclusive-group switching, with a warning listing which files will be
  deleted before the user confirms a save.
- Reports mode.
- Deep linking (Decision 8).

### Out of scope

- A JS test framework.
- Any further backend change beyond the CSP header and alias deletion.
- The drop-in authoring CLI, preview-endpoint sharing at the UI level
  beyond calling it, and the wiki page — plan 70.

---

## TDD workflow

Because there is no JS test framework, steps alternate between Python-side
integration tests (which can genuinely be written test-first) and frontend
implementation work verified manually against a running dev server. Do not
skip the manual verification steps — they are the actual acceptance gate
for anything a Python test cannot see.

### Step 0 — safety check

```bash
grep -n "nordpool\|zonneplan\|pv_fetcher\|pv_ml_learner\|baseload_static\|baseload_ha\|reporter\|scheduler" mimirheim_helpers/config_editor/config_editor/static/app.js
```

Confirm zero matches before this plan starts touching the file (a helper
name should never have appeared in `app.js` even before this rewrite; this
is a pre-condition check, not new coverage).

### Step 1 — CSP header, test-first

Add a Python-side test asserting every response carries a
`Content-Security-Policy` header with the intended directives. Implement
the header in `server.py`. Green. (The "does Jedison actually still render
under this policy" half of this check is manual — see Step 6.)

### Step 2 — nav skeleton

Implement fetching `GET /api/registry` and rendering the `nav-vertical`
shell (Decision 3), with disabled entries showing a placeholder + enable
action and no instance constructed. Manual check against the dev server:
every bundled entry appears, ordered by category then order; a disabled
helper shows "not enabled", not a form.

### Step 3 — entry construction

Implement `GET /api/entry/<id>` fetch, the shared `RefParser` + dereference
+ `Jedison.Create` helper (Decision 4), and mounting into the DOM. Manual
check: opening `mimirheim.yaml`'s tab renders a real form with real values.

### Step 4 — `isDirty` reset

Implement Decision 5. Manual check, using the browser console: immediately
after opening an entry with at least one `x-watch`/`x-template` field
(SPEC §6's "no per-entry key" case, if any exist after plan 68's
migration — otherwise skip this specific check and note it), confirm
`isDirty` reads `false`, not `true`, right after load.

### Step 5 — `mimir-topic-placeholder` custom editor

Implement Decision 2. Manual check: open `pv-fetcher.yaml` (or whichever
entry has a `pv_arrays` output-topic field), confirm the placeholder text
shows the live-computed topic string, confirm typing into the field
overrides it and the typed value is what gets submitted, confirm the field
submits `null` if left untouched.

Add a Python-side integration test that renders the served `index.html` +
`app.js` is syntactically loadable (a smoke-level check only — this cannot
substitute for the manual check above, and should not attempt to).

### Step 6 — CSP verification against the real frontend

With the frontend now functional end to end, verify the CSP header from
Step 1 against actual behaviour: open devtools, set network to offline,
reload the editor, confirm no request leaves the origin and no CSP
violation is logged for legitimate Jedison/custom-editor operation. Adjust
the policy only if something legitimate is blocked, and only just enough to
unblock it.

### Step 7 — save, preview, exclusive groups, reports mode, deep links

Implement `POST /api/save` wiring (including the pre-save warning for
exclusive-group deletions), `POST /api/preview`, reports mode, and the
`#helper=<filename>` → `?entry=<id>` redirect (Decision 8). Manual checks
for each: saving a dirty entry persists it and clears its dirty indicator;
enabling `baseload-ha.yaml` while `baseload-static.yaml` exists warns before
deleting it; an old bookmarked `#helper=nordpool.yaml` link lands on the
correct entry.

### Step 8 — delete deprecated aliases

Delete the six aliases from `server.py` and their plan-68 adapter tests,
now that nothing calls them.

```bash
grep -n "api/schema\|api/config\|api/helper-config" mimirheim_helpers/config_editor/config_editor/static/app.js
```

Zero matches.

### Step 9 — style rewrite

Rewrite `style.css` around design tokens and a spacing scale; implement
light/dark via `prefers-color-scheme`. Manual check in both modes.

### Step 10 — manual verification checklist

Execute, and record the result of, each item:

- [ ] Light mode, desktop width: every entry renders, saves, and validates.
- [ ] Dark mode, desktop width: same.
- [ ] Phone width (the primary real-world case, via the HA companion app):
      nav, form, and save button are all usable without horizontal scroll.
- [ ] A schema whose `title` contains `<img src=x onerror=alert(1)>`
      renders as literal text, not executed markup.
- [ ] With devtools set to offline, no request leaves the origin at any
      point during normal use.
- [ ] `grep` for any helper name in `app.js` returns nothing.

### Step 11 — full test runs

```bash
uv run pytest mimirheim_helpers/config_editor/tests
uv run pytest
```

All green.

---

## Acceptance criteria

- [ ] Every function listed under "Deleted from `app.js`" no longer exists
      anywhere in the codebase.
- [ ] `grep` for any helper package name in `app.js` returns nothing.
- [ ] A schema whose `title` contains injected markup renders as literal
      text (no `innerHTML` assignment of schema-derived strings anywhere).
- [ ] With devtools set to offline, no request leaves the origin during
      normal use, and Jedison still renders and functions correctly under
      the shipped CSP header.
- [ ] The `mimir-topic-placeholder` custom editor shows a live-computed
      placeholder without forcing a value, and a user-typed value is what
      gets submitted.
- [ ] `isDirty` is `false` immediately after any entry is opened, even for
      entries containing `x-watch`/`x-template` fields.
- [ ] Exclusive-group deletion warns the user which files will be deleted
      before a save that triggers it.
- [ ] The six deprecated aliases no longer exist in `server.py`.
- [ ] An old `#helper=<filename>` link redirects correctly.
- [ ] The manual verification checklist (Step 10) is fully checked off.
- [ ] `uv run pytest mimirheim_helpers/config_editor/tests` and
      `uv run pytest` both green.
- [ ] All work is on `feat/config-editor-jedison-frontend`, not merged or
      pushed without explicit approval.
