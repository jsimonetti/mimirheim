# Step 68 — Condense code comments and docstrings

## Why

Inline comments in the MIP-heavy files (`battery.py`, `model_builder.py`, `ev.py`, and
similar) have grown very large, because CLAUDE.md previously mandated a full three-part
explanation (physical quantity + units, why the bound/constraint exists, what breaks if
removed) inline for every non-trivial variable and constraint. Much of this rationale is
general design knowledge, not something a reader needs re-explained at every call site.
This step shrinks the comments while guaranteeing no information is lost: any rationale
previously available only inline ends up documented in IMPLEMENTATION_DETAILS.md's
per-section files (or README.md for user-facing behaviour) before the inline version is
shortened.

This directly modifies a CLAUDE.md rule ("Comment every non-trivial constraint and
variable"). That edit is included in this step so the codebase and its governing
document stay consistent.

## Relevant IMPLEMENTATION_DETAILS sections

All of them, structurally — see "Step 0" below. Content-wise, most new subsections land
in `IMPLEMENTATION_DETAILS/08_mip_model_design.md`.

## Step 0 — Split IMPLEMENTATION_DETAILS.md into an index + per-section files (done)

`IMPLEMENTATION_DETAILS.md` (1353 lines, 14 numbered sections) is now a table of
contents; each section is one-to-two sentences with a link to
`IMPLEMENTATION_DETAILS/NN_slug.md`. Verified byte-for-byte content-preserving via a
normalized diff (heading levels shifted, blank/separator lines stripped) against the
pre-split file. `CLAUDE.md`'s "Source of truth" paragraph updated to describe the index.
Pre-existing `§N` references in `plans/done/*.md`, `CLAUDE.md`, and source comments were
left untouched and spot-checked to still resolve correctly through the index.

## Step 1 — Update CLAUDE.md's comment rule (done)

`CLAUDE.md`'s "Comment every non-trivial constraint and variable" section now describes
the short-inline-plus-pointer convention: physical quantity + units + terse (1-2 line)
why inline; anything needing more than ~3 lines of rationale goes into the relevant
`IMPLEMENTATION_DETAILS/NN_slug.md` subsection, referenced by name (matching the
existing `plans/done/` pointer convention, e.g. `IMPLEMENTATION_DETAILS.md §8,
subsection "Piecewise efficiency (battery and EV)"`).

## Step 2 — Per-file condensation pass (done)

For each file below: find comment blocks longer than ~3 lines stating non-obvious
design rationale; check if that rationale is already documented (several already are,
see call-outs); if not, add a subsection to the relevant `IMPLEMENTATION_DETAILS/`
file; shrink the inline comment to the approved convention; trim docstrings in the same
file (Google-style structure unchanged, prose tightened); run `uv run ruff check .` and
`uv run pytest` and confirm both stay exactly as green as before the file was touched.

**Batch A — core MIP files:** `mimirheim/devices/battery.py`,
`mimirheim/core/model_builder.py`, `mimirheim/devices/ev.py`

**Batch B — remaining device/core files:** `mimirheim/devices/hybrid_inverter.py`,
`mimirheim/devices/pv.py`, `mimirheim/devices/deferrable_load.py`,
`mimirheim/devices/thermal_boiler.py`, `mimirheim/devices/combi_heat_pump.py`,
`mimirheim/devices/space_heating.py`, `mimirheim/devices/grid.py`,
`mimirheim/core/objective.py`, `mimirheim/core/solver_backend.py`,
`mimirheim/core/battery_care.py`, `mimirheim/core/post_process.py`,
`mimirheim/core/building_thermal.py`

**Batch C — non-MIP protocol/state files:** `mimirheim/core/control_arbitration.py`,
`mimirheim/core/readiness.py`, `mimirheim/io/mqtt_client.py`,
`mimirheim/io/mqtt_publisher.py`, `mimirheim/io/ha_discovery.py`

**Known duplication (condense with a pointer, minimal new writing):**
`control_arbitration.py` mirrors `IMPLEMENTATION_DETAILS/09_arbitration_engine_and_closed_loop_enforcer_selection.md`;
`battery_care.py`'s SOC-ratchet comments mirror README.md's "Periodic full charge
(`soc_ratchet`)" subsection (lines 749-856).

**Confirmed new content required** (write into
`IMPLEMENTATION_DETAILS/08_mip_model_design.md` before condensing): the anti-roundtrip
shared direction binary (`model_builder.py:431-457`), the `active[t]` idle-state binary
reasoning (`battery.py`, `ev.py`), the EV availability-gate rationale
(`ev.py:311-326`). Other Batch B/C files get checked against the docs per-file.

## Deferred (out of scope for this step)

`mimirheim/config/schema.py` — its docstrings feed the auto-generated
`wiki/Reference/Config-*.md` pages via `scripts/extract_config_docs.py`; needs its own
check of what that script reads before any trimming.

## Results

All 20 files in Batches A-C processed. Net change: -2698/+413 lines across
source files (most of it comment/docstring volume), while adding 10 new
subsections to `IMPLEMENTATION_DETAILS/08_mip_model_design.md` and one to
`IMPLEMENTATION_DETAILS/02_solver_backend.md` covering rationale that had
been duplicated inline or was previously undocumented. `mqtt_publisher.py`
and `post_process.py` were reviewed and left largely untouched: their dense
comments document genuinely unique, non-duplicated concurrency/precedence
correctness rules (similar in profile to `battery_care.py`), not repeated
explanations, so condensing further risked losing load-bearing information
for a small token saving.

## Acceptance criteria

- `uv run ruff check .` clean.
- `uv run pytest` green (pre-existing unrelated failures in
  `mimirheim_helpers/pv/open-meteo` excluded — confirmed present before this step
  started).
- Every comment block removed or shortened has its full rationale traceable to a
  specific `IMPLEMENTATION_DETAILS/` file or README.md subsection.
- Manual diff review confirms no code logic changed, only comments/docstrings.
