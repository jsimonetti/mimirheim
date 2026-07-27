# Plan 65 — Zonneplan quarterly prices via the consumer-prices chart endpoint

## Purpose

Zonneplan is moving from hourly-only dynamic prices to also offering
quarter-hourly prices. Investigation (see session notes) of the community
`home-assistant-zonneplan-one` integration confirms this is **not** a
granularity change to the endpoint our fetcher currently calls. Quarterly (and
hourly) prices are both served from a new, separate, non-connection-scoped
endpoint:

```
GET https://app-api.zonneplan.nl/api/consumer-prices/charts/{chart_name}
```

where `chart_name` is `"electricity-hourly"` or `"electricity-quarter-hourly"`.
The response shape is different from the current `/connections/{uuid}/summary`
`price_per_hour` list: entries are nested (`price_tax_included.amount` instead
of flat `electricity_price`) and carry an explicit `start_date` **and**
`end_date` per entry, rather than only a single `datetime`.

This has since been confirmed against the live API with a real access token
(both `electricity-hourly` and `electricity-quarter-hourly` charts fetched via
curl) — the shape below is verified, not merely inferred from the community
integration's source.

Since the connection-scoped `summary` endpoint is used **only** for prices in
this helper (confirmed: `get_summary` and `get_connection_uuid` have no other
callers), and we are only interested in price data, this plan removes the
summary/connection-UUID code path entirely rather than keeping it alongside
the new one.

The price chart to request (hourly vs. quarter-hourly) becomes a
user-configurable field, defaulting to `"hourly"` so existing deployments keep
working unchanged.

---

## Branch

All work happens on a new branch, created from `main` before any file is
touched:

```bash
git switch -c feat/zonneplan-quarterly-prices
```

Do not merge or push until the user has reviewed the result.

---

## Prerequisites

Before starting, confirm the working tree is clean and record the baseline
test count:

```bash
uv run pytest
uv run --extra zonneplan pytest mimirheim_helpers/prices/zonneplan/tests -q
```

If any test is already failing, stop and report before proceeding.

---

## Decisions

### 1. Endpoint and client method

`ZonneplanClient` gets one new method, mirroring the existing style of
`get_summary`:

```python
def get_consumer_prices(self, chart_name: str) -> dict:
    """Fetch a consumer price chart.

    Args:
        chart_name: "electricity-hourly" or "electricity-quarter-hourly".

    Returns:
        The "data" object from the response, containing
        data["chart"]["series"]["prices"].

    Raises:
        FetchError: On HTTP or network failure.
    """
```

Calls `GET {_BASE_URL}/api/consumer-prices/charts/{chart_name}` with the same
`_auth_headers()` Bearer scheme used everywhere else. No connection UUID is
part of the path or payload — this call is account/token scoped only.

`get_summary()` and `get_connection_uuid()` are **deleted** from `api.py`
entirely (see Scope below). `get_connection_uuid()` hits a different endpoint
(`/user-accounts/me`) than `get_summary()`, but both exist solely to support
the connection-scoped summary price lookup, which the new endpoint no longer
needs.

### 2. Response parsing

New raw entry shape, confirmed against the live API (hourly example):

```json
{
  "start_date": "2026-07-26T09:00:00+00:00",
  "end_date": "2026-07-26T10:00:00+00:00",
  "price_tax_included": {"amount": 1308450},
  "price_tax_excluded": {"amount": 199969},
  "tariff_group": "low",
  "sustainability_score": {"permille": 1000}
}
```

The quarterly entries are identical except `start_date`/`end_date` are 15
minutes apart and **`tariff_group` is absent** from quarterly entries (present
only on hourly entries, per the live response). Since `tariff_group` is not
surfaced in our output format regardless (see below), this discrepancy has no
impact — parsing must simply not assume `tariff_group` is present.

`prices` list is read from `data["chart"]["series"]["prices"]` (missing keys at
any level default to `{}` / `[]`, matching the existing `.get(..., [])`
defensive style). The same `_PRICE_SCALE = 0.0000001` applies to
`price_tax_included.amount` and `price_tax_excluded.amount` — confirmed both
against the community integration's HA sensor `value_factor=0.0000001` and
against the live response values (e.g. `1308450 * 0.0000001 = 0.130845` EUR/kWh
for the first hourly entry above, consistent with typical Dutch all-in prices).

`tariff_group` and `sustainability_score` are not currently surfaced in our
output format and are not added — out of scope (see Scope below).

### 3. Staleness filter — use `end_date`, not a fixed time-floor

This directly addresses the regression from the earlier (reverted) attempt to
switch the old fetcher's cutoff from an hour-floor to a 15-minute floor: at the
time, floor-to-15 could exclude the only available in-progress hourly entry
before new data arrived, producing an empty price list for a period (observed:
no data between 12:00 and 15:00).

The new endpoint gives us the authoritative interval end for every entry, so
we no longer need to guess a step size at all:

```python
now = datetime.now(tz=timezone.utc)
steps = [e for e in raw_entries if e["end_date"] > now]
```

An entry is included as long as its interval has not fully elapsed yet,
regardless of whether it is an hourly or quarter-hourly block, and regardless
of how far into the interval "now" currently is. This is strictly more
permissive than either of the old fixed-floor approaches and cannot reproduce
the earlier regression.

### 4. Configurable chart source

`ZonneplanApiConfig` gets a new field:

```python
price_interval: Literal["hourly", "quarter_hourly"] = Field(
    default="hourly",
    description=(
        "Price data resolution requested from Zonneplan. 'hourly' matches "
        "current dynamic pricing; 'quarter_hourly' requests 15-minute prices "
        "once available on the account."
    ),
    json_schema_extra={"ui_label": "Price interval", "ui_group": "basic"},
)
```

This follows the existing `Literal[...]` + `json_schema_extra` convention used
elsewhere (e.g. `baseload_ha/config.py`'s `unit` field) — no special UI widget
marker is needed; the config editor derives a dropdown from the `Literal`
automatically.

A small mapping from config value to the real Zonneplan `chart_name` string
lives in `fetcher.py`, next to `_PRICE_SCALE`:

```python
_CHART_NAMES: dict[str, str] = {
    "hourly": "electricity-hourly",
    "quarter_hourly": "electricity-quarter-hourly",
}
```

Default is `"hourly"` so no existing `config.yaml` needs to change.

### 5. `fetch_prices` signature change

`connection_uuid` is removed from `fetch_prices`; `price_interval` is added:

```python
def fetch_prices(
    *,
    client: ZonneplanClient,
    price_interval: str,
    import_formula: str,
    export_formula: str,
) -> list[dict[str, Any]]:
```

This is a breaking signature change to an internal function with no other
callers besides `__main__.py` and its own tests, both updated in this plan.

### 6. `__main__.py` — remove connection UUID resolution

`ZonneplanPricesDaemon._run_cycle`'s "Step 4: Resolve connection UUID" block is
deleted entirely, along with the `_connection_uuid` class attribute. Step 5
becomes:

```python
steps = fetch_prices(
    client=api_client,
    price_interval=zp_config.price_interval,
    import_formula=zp_config.import_formula,
    export_formula=zp_config.export_formula,
)
```

No other helper package imports `zonneplan_prices.api`, so deleting
`get_summary`/`get_connection_uuid` is confirmed safe — this must be
re-verified with a repo-wide grep as the first implementation step, before any
deletion.

### 7. No config migration needed for existing users

`connection_uuid` was never a user-facing config value (it was discovered
automatically). Removing it requires no `config.yaml` changes. The only new,
optional field is `price_interval`, defaulting to `"hourly"`.

---

## Scope

### In scope

- `zonneplan_prices/api.py` — delete `get_summary()`, delete
  `get_connection_uuid()`; add `get_consumer_prices(chart_name)`.
- `zonneplan_prices/config.py` — add `price_interval` field to
  `ZonneplanApiConfig`.
- `zonneplan_prices/fetcher.py` — rewrite `fetch_prices()`: new client call,
  new response parsing (`start_date`/`end_date`, nested `amount` fields), new
  `end_date`-based staleness filter, `_CHART_NAMES` mapping, signature change
  (`price_interval` replaces `connection_uuid`).
- `zonneplan_prices/__main__.py` — remove `_connection_uuid` attribute and
  Step 4 connection-UUID resolution block; update Step 5 call site; update
  module/class docstrings that mention connection UUID discovery.
- `tests/test_api.py` — delete `TestGetSummary`, delete
  `TestGetConnectionUuid`; add `TestGetConsumerPrices`.
- `tests/test_fetcher.py` — rewrite fixtures and all tests for the new
  response shape, new signature, and new staleness rule (see TDD workflow).
- `tests/test_config.py` — add tests for `price_interval` (default, both
  valid values, invalid value rejected).
- `README.md` (this helper) — update "How it works" step 3, config example
  (`price_interval`), and any other summary/connection_uuid references.

### Not in scope

- `tariff_group` / `sustainability_score` fields — not surfaced downstream.
- Any change to `mimirheim/core/forecast.py` resampling — quarterly step data
  already flows correctly through the existing step-function resampler (see
  prior investigation).
- ETag/rate-limit handling matching the community integration's `_async_get`
  (no evidence yet that the new endpoint requires it; can be revisited if the
  live API proves stricter).
- Any other helper package.

### Files deleted

None outright (no standalone test files exist solely for summary/connection
UUID — those tests are removed from within `test_api.py`, which remains).

---

## TDD workflow

### Step 0 — safety check before deleting anything

```bash
grep -rn "get_summary\|get_connection_uuid" --include=*.py mimirheim_helpers/ mimirheim/
```

Confirm zero matches outside `zonneplan_prices/api.py`,
`zonneplan_prices/__main__.py`, and `zonneplan_prices/tests/test_api.py`
before proceeding.

### Step 1 — write failing tests for `get_consumer_prices`

In `tests/test_api.py`, add `TestGetConsumerPrices` with:
- `test_calls_correct_url_with_bearer_token` — asserts the request URL is
  `f"{_BASE_URL}/api/consumer-prices/charts/electricity-hourly"` for
  `chart_name="electricity-hourly"`, and that the Bearer header is set;
  asserts the returned value is the `"data"` object.
- `test_raises_fetch_error_on_http_failure` — HTTP 503 → `FetchError`.

Run tests — new tests fail (method does not exist yet).

### Step 2 — implement `get_consumer_prices`, delete `get_summary`/`get_connection_uuid`

In `api.py`, add `get_consumer_prices()` per the Decisions section. Delete
`get_summary()` and `get_connection_uuid()`. Delete the now-obsolete
`TestGetSummary` and `TestGetConnectionUuid` classes from `test_api.py`.

Run `uv run --extra zonneplan pytest mimirheim_helpers/prices/zonneplan/tests/test_api.py -v`
— all green.

### Step 3 — write failing tests for the new fetcher behaviour

Rewrite `tests/test_fetcher.py`:
- Replace `_make_raw_entry` with `_make_price_entry(start, end, price_raw,
  price_excl_raw)` returning the nested shape.
- Replace `_make_client` to stub `client.get_consumer_prices.return_value =
  {"chart": {"series": {"prices": entries}}}`.
- Update every `fetch_prices(...)` call site: replace `connection_uuid="conn-1"`
  with `price_interval="hourly"`.
- Rewrite the past/future filtering tests to use `end_date` per the Decisions
  §3 rule. Include:
  - `test_expired_entry_excluded` — `end_date` in the past → excluded.
  - `test_in_progress_entry_included` — `start_date` in the past, `end_date`
    in the future (the exact regression scenario from the earlier revert:
    e.g. `start=12:00, end=13:00`, `now=12:47`) → included.
  - `test_future_entry_included`.
- Add `test_chart_name_mapping_hourly` and
  `test_chart_name_mapping_quarter_hourly` — assert
  `client.get_consumer_prices` was called with `"electricity-hourly"` /
  `"electricity-quarter-hourly"` respectively for each `price_interval` value.
- Add `test_quarterly_steps_all_returned_distinctly` — four 15-minute entries
  within one hour, all in the future, all four appear as separate steps.
- Keep (adapted to the new entry shape) the existing coverage for: price
  scale, price_excl_tax scale, import formula, export formula, confidence
  always 1.0, `ts` is ISO 8601 UTC, `FetchError` propagation, steps sorted by
  `ts`, empty list handling.

Run tests — all fail (fetcher still uses the old implementation).

### Step 4 — implement the new fetcher

Rewrite `fetch_prices()` in `fetcher.py` per Decisions §2, §3, §4, §5. Update
the module docstring's endpoint description and output format example.

Run `uv run --extra zonneplan pytest mimirheim_helpers/prices/zonneplan/tests/test_fetcher.py -v`
— all green.

### Step 5 — config field

Add `price_interval` to `ZonneplanApiConfig` in `config.py`. Add tests to
`tests/test_config.py`:
- `test_price_interval_defaults_to_hourly`
- `test_price_interval_accepts_quarter_hourly`
- `test_price_interval_rejects_invalid_value`

Run `uv run --extra zonneplan pytest mimirheim_helpers/prices/zonneplan/tests/test_config.py -v`
— all green.

### Step 6 — wire up `__main__.py`

Remove `_connection_uuid` attribute and the Step 4 connection-UUID resolution
block from `_run_cycle`. Update the Step 5 (now the final) `fetch_prices` call
to pass `price_interval=zp_config.price_interval` instead of
`connection_uuid=connection_uuid`. Update the class/module docstrings that
describe the auth-flow steps (renumber "Step 5"/"Step 6" as needed) and remove
mentions of connection UUID discovery.

There is no dedicated daemon-level test file for `_run_cycle` in this helper;
no test file changes are required here beyond what Steps 1–5 already cover.

### Step 7 — full helper test run

```bash
uv run --extra zonneplan pytest mimirheim_helpers/prices/zonneplan/tests -v
```

All tests green. Record the final count and compare to the Step 5 (README
step numbering, not plan step) baseline noted in Prerequisites — the net test
count should grow (new tests added) with zero regressions elsewhere.

### Step 8 — full repo test run

```bash
uv run pytest
```

Confirm no other suite is affected (expected: unaffected, since this helper is
a standalone package).

### Step 9 — documentation

Update `mimirheim_helpers/prices/zonneplan/README.md`:
- "How it works" step 3: replace the `GET /connections/{uuid}/summary`
  description with the new `GET /api/consumer-prices/charts/{chart_name}`
  description, mentioning the configurable interval.
- Configuration example: add `price_interval: hourly` (commented as optional,
  default shown) to the `zonneplan:` block.
- Remove any other references to `summary` / connection UUID discovery in the
  authentication flow description, if present.

---

## Acceptance criteria

- [ ] `get_summary` and `get_connection_uuid` no longer exist anywhere in the
      codebase.
- [ ] `ZonneplanClient.get_consumer_prices(chart_name)` exists, calls the
      correct URL, and is covered by tests for both success and HTTP-failure
      paths.
- [ ] `fetch_prices()` no longer takes `connection_uuid`; takes
      `price_interval` instead.
- [ ] Staleness filtering uses `end_date` per entry, not a fixed time-floor.
      A regression test exists reproducing the earlier "in-progress entry
      dropped too early" scenario and asserts it is now included.
- [ ] `price_interval` is a configurable field on `ZonneplanApiConfig`,
      defaulting to `"hourly"`, accepting `"hourly"` and `"quarter_hourly"`,
      rejecting any other value.
- [ ] `__main__.py` no longer performs connection UUID discovery.
- [ ] All existing and new tests pass:
      `uv run --extra zonneplan pytest mimirheim_helpers/prices/zonneplan/tests -v`
- [ ] Full repo suite unaffected: `uv run pytest`.
- [ ] README updated to describe the new endpoint and the `price_interval`
      config field.
- [ ] All work is on `feat/zonneplan-quarterly-prices`, not merged or pushed
      without explicit approval.

---

## Open risks (updated after live verification)

- ~~No live JSON example of the new endpoint has been captured~~ — **resolved**:
  both `electricity-hourly` and `electricity-quarter-hourly` charts were
  fetched live with a real access token and match the shape assumed above
  (aside from the `tariff_group` discrepancy noted in Decisions §2, which has
  no impact).
- ~~Whether the account/token used by this daemon is already entitled to
  quarter-hourly data is unknown~~ — **resolved**: the live quarterly fetch
  succeeded, confirming entitlement.
- Whether Zonneplan enforces stricter rate limiting on this endpoint than on
  the old summary endpoint is still unknown; no ETag/conditional-request
  handling is added in this plan.

The remaining risk is accepted per the user's explicit instruction, not a
blocker to starting implementation.
