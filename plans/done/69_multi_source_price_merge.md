# 69 — Multi-source price merge

Status: agreed, ready to implement.

## 1. Problem

`inputs.prices` is a single topic today. `ReadinessState` stores exactly one
`list[PriceStep]`, and `resample_prices()` (`core/forecast.py`) resamples that
one list to the 15-minute grid. Nordpool and Zonneplan are alternatives, not
composable sources: both default to the same topic and each retained publish
fully overwrites whatever was there.

Goal: accept multiple price topics, resample each independently, then merge
per 15-minute step by highest confidence — so a lower-confidence predictive
source (e.g. a future "day-after-day-ahead" ML helper) can extend the horizon
without ever being able to override a real day-ahead price.

## 2. Decisions

| Decision | Value | Why |
|---|---|---|
| Config type | `list[str]`, coercing a bare `str` input to a one-element list, `default_factory=list` | Matches the request; internal representation is always `list[str]` so every downstream consumer (readiness, mqtt_client) handles one type, not a union |
| Empty-list default | `[]` before post-init, then defaulted to `[{prefix}/input/prices]` if still empty after config load | Preserves today's single-topic default behaviour for anyone who sets nothing |
| Duplicate topics in the list | Rejected at validation | Ambiguous priority; a copy-paste config bug, not a supported case |
| Tie-break on equal confidence | List order = priority; earlier entries win ties | User decision. Also fixes a real ambiguity: Nordpool and Zonneplan both report confidence 1.0, and the array format now lets someone configure both at once |
| Missing price source at solve time | Solve proceeds on whatever price sources have reported, as long as merged coverage clears `min_horizon_hours` | User decision. Consistent with the existing coverage-based (not presence-based) readiness philosophy for forecast series |
| Forward extrapolation across sources | Disallowed. A source can only compete for step `t` if `t` falls within its own real timestamp range (`first_ts <= t <= last_ts`) | See §3. Without this, a short-coverage confidence-1.0 source (e.g. Nordpool's 24h) would hold its last value forward at fixed confidence 1.0 forever, permanently outranking a longer-horizon predictive source at every step beyond Nordpool's own data — exactly the failure mode the merge exists to prevent |
| Leading-edge backward fill | Kept, but only as a fallback tier used when **no** source has real coverage of `t` yet | Preserves today's single-source behaviour (extend the earliest known price backward) for the case where even the longest source hasn't started publishing yet |
| `resample_prices()` | Removed, replaced by `merge_price_sources()` (single-source case is `merge_price_sources([one_list], ...)`) | Two near-identical resampling implementations is duplication a plan should not introduce; existing `resample_prices` tests migrate as the single-source regression baseline |
| Confidence decay for a future ML helper | Stays out of mimirheim core, computed by that helper before publishing (per `IMPLEMENTATION_DETAILS/06`) | Not revisiting this documented boundary. Core only ever picks the winning step's own reported confidence, never blends or decays it |

## 3. Merge algorithm

`sources: list[list[PriceStep]]`, ordered by config priority (index 0 =
highest priority on ties). For each output step `k` in `range(n_steps)`,
`t = solve_start + k * 15min`:

1. **Tier 1 — real coverage.** For each source `i`, find `active` = the
   step with the latest `.ts <= t` (existing hold-previous scan). Source `i`
   is a tier-1 candidate only if `active is not None and t <= last_ts(source i)`.
   This is the change from today's per-series scan: it blocks forward
   extrapolation past a source's own last known timestamp.
2. **Tier 2 — leading-edge fallback.** Used only if tier 1 is empty across
   *every* source (i.e. `t` is before every source's `first_ts`). Each source
   with any data at all contributes its own first step as a candidate —
   identical to today's single-series `if active is None: active = sorted_steps[0]`.
3. **Selection.** From whichever tier is non-empty, pick the candidate with
   the highest `confidence`; ties broken by lowest source index (config
   order). Emit that candidate's `import_eur_per_kwh`, `export_eur_per_kwh`,
   `confidence`.

Invariant relied on, not defensively checked: at least one source has real
tier-1 coverage of every `t` in `[solve_start, horizon_end)`, because
`horizon_end` (see below) is bounded by the furthest-reaching source, and
readiness already guarantees at least one price source with future coverage
before `snapshot()` runs. A step with zero candidates in both tiers cannot
occur inside a horizon computed this way and is not guarded against, per the
project's "don't handle unreachable states" convention.

New function, `core/forecast.py`:

```python
def merge_price_sources(
    sources: list[list[PriceStep]],
    solve_start: datetime,
    n_steps: int,
) -> tuple[list[float], list[float], list[float]]:
```

## 4. Horizon computation must union, not intersect, across price sources

`compute_horizon_steps()` takes the *minimum* coverage across every series
passed to it — correct for "PV and load must both be covered," wrong for
price sources, where the entire point is that a longer-horizon source should
extend the usable horizon, not be capped by a shorter higher-confidence one.

New function, `core/forecast.py`:

```python
def compute_price_horizon_steps(
    solve_start: datetime,
    sources: list[list[PriceStep]],
) -> int:
```

Reuses the existing `_last_ts_at_or_after()` helper per source, but takes the
**maximum** of the per-source results (skipping sources with no data) instead
of the minimum, then converts to a step count exactly like
`compute_horizon_steps()` does. Returns 0 if no source has any future data.

`ReadinessState._compute_horizon_steps()` changes from "one flat set of
forecast topics, intersect all of them" to:

```
price_steps  = compute_price_horizon_steps(solve_start, <PriceStep lists present in _entries for self._prices_topics>)
if price_steps == 0: return 0
if pv_topics or load_topics present:
    other_steps = compute_horizon_steps(solve_start, <PV/load series present>)
    return min(price_steps, other_steps)
else:
    return price_steps
```

The `else` branch matters: `compute_horizon_steps()` called with zero series
returns 0 by its own contract ("empty lists immediately short-circuit to
0" — actually zero *args*, same short-circuit), which would wrongly zero the
whole result for a battery+grid-only config with no PV arrays or static
loads. This is a real edge case, not hypothetical — covered by a dedicated
test.

PV and load topics keep today's all-required, intersection-based semantics
unchanged. Only price topics get union coverage and partial-source
tolerance — that's what was asked for and agreed; do not generalize it to PV
or load topics as part of this plan.

## 5. Files

- `mimirheim/config/schema.py` — `InputsConfig.prices: list[str]`, `mode="before"` validator coercing `str -> [str]`, duplicate-topic validator, post-init default (`if not self.inputs.prices: self.inputs.prices = [_topics.prices_topic(p)]`).
- `mimirheim/config/schema.json` — regenerate.
- `mimirheim/core/bundle.py` — `PriceStep` docstring says "the MQTT prices topic" (singular); update to reflect multiple topics being merged.
- `mimirheim/core/forecast.py` — add `merge_price_sources()`, `compute_price_horizon_steps()`; remove `resample_prices()`.
- `mimirheim/core/readiness.py` — `_prices_topic: str` -> `_prices_topics: list[str]`; remove prices from the flat `_forecast_topics` set, handle separately in `_compute_horizon_steps()`, `not_ready_reason()`, `_check_gaps()`, `snapshot()`.
- `mimirheim/io/mqtt_client.py` — register `parse_price_steps` for every topic in `config.inputs.prices`, not just one.
- `tests/unit/test_config_schema.py` — update `test_inputs_prices_derived_from_prefix` / `test_inputs_prices_explicit_override_preserved` to list form; add: bare-string coercion, explicit list, empty-list defaulting, duplicate-topic rejection.
- `tests/unit/test_forecast.py` — migrate `resample_prices` tests to `merge_price_sources` with a single source (regression baseline); add multi-source tests (see §6); add `compute_price_horizon_steps` tests.
- `tests/unit/test_readiness.py` — multi-topic price readiness (one topic never received, solve still proceeds); merged coverage still too short blocks; zero-PV/zero-load edge case; `snapshot()` produces correctly merged bundle fields.
- `tests/unit/test_mqtt_client.py` — subscribes/dispatches on every configured price topic.
- `README.md` — §5 Confidence Model: document the merge rule (highest confidence per step, source-order tie-break) and that `inputs.prices` is now a list.
- `wiki/Reference/Config-Mimirheim.md` — regenerate via `scripts/extract_config_docs.py`.
- `wiki/Reference/MQTT-Topics.md`, `wiki/Configuration.md` — update prose describing a single prices topic.
- `IMPLEMENTATION_DETAILS/07_solvebundle_and_per_device_input_models.md` — document the merge algorithm (this is where `PriceStep`/readiness resampling design already lives).
- `IMPLEMENTATION_DETAILS/14_mqtt_topic_naming_convention_and_auto_derivation.md` — `inputs.prices` row currently documents one derived topic; describe list-with-default semantics.

## 6. Tests first

- `test_config_schema`: bare string coerces to one-element list; explicit list preserved in given order; empty list (or omitted `inputs`) defaults to `[derived_topic]`; duplicate topics raise; `extra="forbid"` still holds.
- `test_forecast`:
  - `merge_price_sources` with one source reproduces every existing `resample_prices` case exactly (constant-within-period, step-changes-at-boundary, export+confidence carried through, unsorted input, leading-edge backward fill).
  - Two sources, disjoint confidence, no overlap in time: each step takes whichever source covers it.
  - Two sources overlapping in time, source A confidence 1.0 / short horizon, source B confidence 0.4 / long horizon: A wins every step within its own real range; B wins every step beyond A's `last_ts`, at B's own (lower) confidence — the key regression test for the forward-extrapolation bug this design avoids. Assert explicitly that a step beyond A's last_ts does **not** carry A's confidence.
  - Equal confidence at the same step from two sources: earlier list index wins.
  - Leading edge before any source has data: tier-2 fallback picks the correct (highest-confidence, then earliest-index) source.
  - `compute_price_horizon_steps`: union beats intersection — a short high-confidence source plus a long low-confidence source yields the long source's coverage, not the short one's; empty source list / all-empty sources returns 0.
- `test_readiness`:
  - Two price topics configured; only one has ever published; merged coverage clears `min_horizon_hours` → `is_ready()` True, `snapshot()` succeeds using only the published one.
  - Two price topics configured, both published, but merged (unioned) coverage is still short → `is_ready()` False, `not_ready_reason()` mentions the shortfall.
  - Config with no PV arrays and no static loads (battery + grid only), one price topic → readiness driven by price coverage alone, not zeroed by the empty PV/load intersection.
  - `snapshot()`: two overlapping price sources with different confidence produce a bundle whose `horizon_prices`/`horizon_confidence` match the expected per-step winner.
- `test_mqtt_client`: `config.inputs.prices = ["a/prices", "b/prices"]` results in both topics subscribed and both dispatched to `parse_price_steps`.

## 7. Acceptance

`uv run pytest` green, `uv run ruff check .` clean, `test_schema_json_is_up_to_date` passing after `schema.json` regeneration, `wiki/Reference/Config-Mimirheim.md` regenerated and committed.

## 8. Known and out of scope

- The actual "day-after-day-ahead" ML prediction helper package (fetcher, confidence-decay-over-time policy, its own `mimirheim_helpers/prices/<name>/` package) is not part of this plan. This plan only builds the core capability it depends on. Confidence decay policy for that helper is a separate design decision for whoever builds it, constrained only by "must not itself overwrite/merge — mimirheim core does that."
- Per-source internal-gap warnings (`find_gaps`/`_check_gaps`) still run per topic independently; no change to gap-detection logic itself, only to how many topics it iterates over.
- The Jedison config-editor (separate repo, renders `schema.json` into a form UI) will need a list-of-strings widget for this field instead of a single text input. Cross-repo, not tracked here — flagging so it isn't a surprise when `schema.json`'s `inputs.prices` type changes from `string` to `array`.
- No UI/CLI convenience for reordering priority after the fact beyond editing the YAML list order.
