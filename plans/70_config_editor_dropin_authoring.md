# Plan 70 — config-editor: drop-in authoring experience

## Purpose

Make third-party schemas usable by someone who has never read mimirheim's
source: a CLI to validate a drop-in schema without starting the server, a
preview endpoint that shows what a save would actually write, a
review-changes step in the UI before saving, and a wiki page written for an
external author.

Read `mimirheim_helpers/config_editor/SPEC.md` §2, §7, §10, and §12 before
starting. This plan does not change discovery, the field vocabulary, or the
HTTP endpoints those sections define — it only makes what plans 68 and 69
built reachable and understandable from outside the repository.

---

## Branch

Requires plans 68 and 69 done.

Do not push until the user has reviewed the result.

---

## Prerequisites

Plans 68 and 69 merged. `registry.py`'s discovery and validation, and the
Jedison frontend, both work end to end.

```bash
uv run pytest
uv run pytest mimirheim_helpers/config_editor/tests
```

Record baseline counts.

---

## Decisions

### 1. `--validate-schemas` CLI command

Added to `config_editor/__main__.py` (or a new `config_editor/cli.py`
imported from it — whichever keeps `__main__.py`'s existing entry-point
shape intact). Calls the exact same `registry.py` discovery function the
running server uses — not a reimplementation — so the CLI and the server
can never disagree about what is valid. Prints one line per discovered
schema file (`OK <id>` or `REJECTED <id>: <reason>`), exits non-zero if any
file is rejected.

```bash
uv run python -m config_editor --validate-schemas --config-dir /config
```

### 2. Preview endpoint shares the write path's merge logic

`POST /api/preview` (already specified in SPEC §12; plan 68 built the
endpoint's shape but this plan is where its actual diff computation is
implemented and exercised). It calls the identical composition/write
function `POST /api/save` uses, up to but not including the final
`os.replace` call, and returns a computed YAML diff per touched file. This
guarantees a preview can never differ from what saving would actually do,
because it is not a separate code path that could drift from the real one.

### 3. Review-changes step in the UI before saving

Before a save is submitted, the frontend calls `POST /api/preview` and
shows the diff to the user for confirmation. This matters specifically
because the save path deletes keys absent from the submitted config, which
is not obvious from looking at a form — a user could otherwise delete a
field's value without realising the corresponding YAML key disappears
entirely rather than reverting to a default.

### 4. Wiki page

`wiki/Developer/Custom-Config-Schemas.md`, written for an author who has
never read mimirheim's source. Structure: what a schema file is and where
it goes (SPEC §1, §2), the complete `x-mimirheim` envelope (SPEC §3) with a
worked example, the field vocabulary (SPEC §4) with a worked example for
each blessed keyword, the structural-validation-only limitation for
drop-ins stated plainly as a limitation (SPEC §7), how to use
`--validate-schemas` and the preview endpoint while iterating, and a
complete worked example schema reproduced inline.

### 5. Filesystem watching is out of scope

A reload endpoint (`POST /api/reload`, already built in plan 68) plus a
button in the UI to call it are enough. A watcher thread inside a Home
Assistant add-on container is unneeded complexity for a workflow that is
already "save the file, click reload."

### 6. Worked examples live under `examples/schemas/`

`examples/schemas/solaredge.schema.json` — a complete, valid third-party
schema exercising the full blessed vocabulary: a named map of device
objects, an enum, `x-category`-based advanced grouping, an `x-enumSource`
cross-reference into `context.pv_arrays`, and the
`mimir-topic-placeholder` custom editor. It must pass
`--validate-schemas` and the meta-schema, checked by a test so it cannot
silently rot.

`examples/schemas/broken-example.schema.json.txt` — deliberately invalid
(missing `x-mimirheim`), with the `.txt` extension so the discovery scanner
ignores it as a live schema. Used by the wiki page's troubleshooting
section and by a test asserting the exact, documented rejection reason
string `--validate-schemas` produces for it.

---

## Scope

### In scope

- `config_editor/__main__.py` (or a new `cli.py`) — `--validate-schemas`.
- `config_editor/registry.py` — the shared merge/diff function used by both
  `/api/save` and `/api/preview`, if plan 68 did not already factor it out
  that way; if it did, this plan only adds the diff-formatting layer.
- `config_editor/server.py` — `POST /api/preview`'s diff computation.
- `static/app.js` — the review-changes confirmation step before a save is
  submitted.
- `wiki/Developer/Custom-Config-Schemas.md` (new).
- `examples/schemas/solaredge.schema.json` (new) and
  `examples/schemas/broken-example.schema.json.txt` (new), each with a test.
- `mimirheim_helpers/config_editor/README.md` — §2 "How it works" gets a
  line describing the review-changes/preview step before a save commits
  (Decision 3); §5 "Running" gets a `--validate-schemas` entry pointing to
  the new wiki page.
- `mimirheim_helpers/config_editor/AGENTS.md` — remove the remaining
  "target, not current" caveat for `SPEC.md`, since all of it is now
  implemented; `SPEC.md` and `README.md` together fully describe the
  running system.

### Out of scope

- Any change to discovery, the field vocabulary, or the HTTP endpoint
  shapes — all frozen as of plan 68/69.
- A schema-authoring GUI.
- Filesystem watching (Decision 5).

---

## TDD workflow

### Step 0 — safety check

```bash
uv run python -m config_editor --validate-schemas --config-dir /config
```

Run against the current bundled schemas with no drop-ins present, confirm
it reports every bundled entry `OK` and exits zero, before adding any new
code.

### Step 1 — `--validate-schemas`, test-first

Add a test invoking the CLI (via `subprocess` or direct function call,
whichever the existing `__main__.py` test pattern uses) against a temp
config directory containing one valid drop-in and one invalid drop-in
(missing `x-mimirheim`). Assert: the valid one reports `OK`, the invalid one
reports the exact rejection reason, exit code is non-zero.

Implement. Green.

### Step 2 — preview endpoint diff computation, test-first

Add a test: `POST /api/preview` with a dirty entry returns a diff showing
the changed lines, and asserts the actual file on disk is byte-for-byte
unchanged afterward. Add a test for the "save would delete a key" case
specifically (Decision 3's motivating scenario): submitting a config that
omits a previously-present optional field shows that key's removal in the
diff.

Implement, factoring the shared merge logic out of `/api/save` if it is not
already a separate function. Green.

### Step 3 — review-changes UI step

Implement the frontend confirmation step (Decision 3). Manual check:
attempting to save with an omitted field shows the diff, including the
deletion, before the save is committed; cancelling leaves the file
untouched.

### Step 4 — worked examples

Write `solaredge.schema.json` and `broken-example.schema.json.txt`. Add
tests: the valid example passes `--validate-schemas` and the meta-schema;
the broken example produces the exact documented rejection reason string.

### Step 5 — wiki page and README updates

Write `wiki/Developer/Custom-Config-Schemas.md` per Decision 4's structure.
Update `README.md`'s "How it works" (review-changes/preview step) and
"Running" (`--validate-schemas`, pointing to the new wiki page) sections.

### Step 6 — acceptance walkthrough

From a clean container (or an equivalent clean state — a fresh `/config`
directory with no drop-ins), follow the wiki page literally, with no other
file open, using only what it says: write a drop-in schema, validate it,
preview a save, save it, confirm the resulting YAML file and the rendered
form both match expectations. Any step the document does not carry alone on
its own is a documentation bug — fix the document, not this step.

### Step 7 — full test runs

```bash
uv run pytest mimirheim_helpers/config_editor/tests
uv run pytest
```

All green.

---

## Acceptance criteria

- [ ] `--validate-schemas` uses the same discovery/validation code path the
      running server uses — not a separate implementation.
- [ ] `POST /api/preview` never mutates the filesystem, including for the
      disable case, and shares its merge logic with `POST /api/save`.
- [ ] A save that would delete a key absent from the submitted config shows
      that deletion in the preview diff before the user confirms.
- [ ] `examples/schemas/solaredge.schema.json` passes `--validate-schemas`
      and the meta-schema, checked by a test.
- [ ] `examples/schemas/broken-example.schema.json.txt` produces the exact
      rejection reason documented in the wiki page, checked by a test.
- [ ] `wiki/Developer/Custom-Config-Schemas.md` exists and the Step 6
      walkthrough was actually performed and passed, not merely written.
- [ ] `README.md`'s "How it works" and "Running" sections describe the
      review-changes step and `--validate-schemas`.
- [ ] No filesystem watcher was added.
- [ ] `uv run pytest mimirheim_helpers/config_editor/tests` and
      `uv run pytest` both green.
- [ ] All work is on `feat/config-editor-dropin-authoring`, not merged or
      pushed without explicit approval.
