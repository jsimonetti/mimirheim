# Step 70 - EpexPredictor prices helper

## Purpose

A standalone daemon that fetches EPEX day-ahead spot price predictions from
the EpexPredictor API (https://epexpredictor.batzill.com) and publishes them
to the mimirheim prices input topic, in the same format as the Nordpool
helper.

Unlike Nordpool, EpexPredictor is explicitly a modeled forecast, not a
guaranteed source: the operator's README states "There are no guarantees on
availability or correctness of the data." Its `/prices` response includes a
`knownUntil` timestamp marking the boundary between real EPEX auction results
(relayed from energy-charts.info / ENTSO-E) and the model's own predicted
tail, which currently extends about a week ahead. This helper is meant to
augment true day-ahead sources (Nordpool, a supplier feed) by extending the
mimirheim price horizon further into the future, at progressively lower
confidence, using the same per-step confidence field and multi-source merge
mechanism that `plans/done/69_multi_source_price_merge.md` already built into
mimirheim core (`inputs.prices` is a list of topics, merged per step by
highest confidence; see `mimirheim/core/forecast.py`
`merge_price_sources()`).

Design decision confirmed with the user: confidence decay is anchored to
fetch time, exactly like the open-meteo PV helper, not to the API's
`knownUntil` boundary. Every step's confidence is a function of how many
hours ahead of the fetch it lies, regardless of whether that step happens to
fall within the API's "known" (real) window or its predicted tail. This is
simpler and matches the literal instruction to reuse the PV helper's decay
format and defaults. One consequence worth flagging: within `knownUntil`,
EpexPredictor is relaying real auction data, so a step there would otherwise
get the same confidence penalty as a step in the predicted tail at the same
horizon distance, even though it is factually as reliable as Nordpool's own
output at that step.

To address that consequence without abandoning the fetch-time anchor, a
second, independent knob is added: `confidence_decay.known_until_confidence`.
When set, it overrides the decay-band result for any step at or before the
API's `knownUntil` timestamp with a single fixed confidence value, leaving
every step after `knownUntil` on the normal fetch-time bands unchanged. When
left at its default of `null`, behaviour is exactly as originally decided:
the bands apply uniformly to every step regardless of `knownUntil`. This
keeps the fetch-time anchor as the base mechanism (per the user's decision)
while giving the operator an explicit, opt-in way to tell the helper "trust
the real portion of this feed at this fixed level," for example `1.0` to
match Nordpool's treatment of confirmed day-ahead prices, so this helper's
real steps can compete on equal footing in the multi-source merge instead of
being structurally outranked by Nordpool on every overlapping step.

---

## Relevant IMPLEMENTATION_DETAILS sections

None directly. This is a standalone helper package outside the mimirheim
core, following the same conventions as
`mimirheim_helpers/prices/nordpool/` and `mimirheim_helpers/pv/open-meteo/`.
No changes to mimirheim core are needed: the multi-source price merge
(`inputs.prices: list[str]`, `merge_price_sources()`,
`compute_price_horizon_steps()`) already landed in the "prepare for multiple
price sources" commit and requires no further work to support a second price
topic.

---

## API reference

Base URL: `https://epexpredictor.batzill.com`. Full spec at
`https://epexpredictor.batzill.com/openapi.json` (version 0.1.0 at time of
writing). No authentication.

### `GET /prices`

Verbose endpoint, used by this helper (the `/prices_short` endpoint drops
`knownUntil` and returns unix-timestamp arrays instead of ISO objects; not
used here).

Query parameters relevant to this helper:

| Param | Type | Meaning |
|---|---|---|
| `hours` | int | How many hours ahead to predict. Default `-1` (all available, currently about 7 days / 168 hourly steps). Maps to `horizon_hours` config. |
| `region` | enum | Bidding zone: `DE`, `AT`, `BE`, `NL`, `SE1`, `SE2`, `SE3`, `SE4`, `DK1`, `DK2`, `ES`, `PT`. Maps to `area` config. |
| `unit` | enum | `CT_PER_KWH` (API default), `EUR_PER_KWH`, `EUR_PER_MWH`. This helper always requests `EUR_PER_KWH`, hardcoded, not configurable, to match the Nordpool helper's output unit. |
| `hourly` | bool | Averages each clock hour into one step when true. Default false (quarter-hourly). Maps to `price_interval` config (`"hourly"` -> true, `"quarter_hourly"` -> false). |
| `timezone` | str | Timezone for output timestamps. API default `Europe/Berlin`. This helper always requests `UTC`, hardcoded, so `startsAt` is already UTC and needs no conversion. |
| `surcharge` | float | Fixed EUR/kWh add-on. API default `0.0`. **Never set by this helper.** Always sent as `0.0` (or omitted). Markup belongs in `import_formula` / `export_formula`, per explicit user instruction. |
| `taxPercent` | float | Percentage tax add-on. API default `0.0`. **Never set by this helper**, same reasoning as `surcharge`. |
| `evaluation` | bool | Switches every value to model-generated, for offline model evaluation. Never used by this helper (always false / omitted). |
| `startTs` | datetime | Start of the returned window. Not exposed; this helper relies on the API's own "now" default and additionally applies the same past-step truncation Nordpool uses, defensively. |

Response body (`PricesModel`):

```json
{
  "prices": [
    {"startsAt": "2026-09-10T11:00:00Z", "total": 0.0988},
    {"startsAt": "2026-09-10T11:15:00Z", "total": 0.1043}
  ],
  "knownUntil": "2026-09-10T21:45:00Z"
}
```

`total` is already in the requested unit (EUR/kWh, since this helper always
requests `unit=EUR_PER_KWH`). `startsAt` is an ISO 8601 timestamp with a `Z`
suffix when `timezone=UTC` is requested; `datetime.fromisoformat()` on
Python 3.11+ parses `Z` directly into a UTC-aware datetime. `knownUntil` is
parsed the same way and returned alongside the price steps (see `fetcher.py`
below): it is the sole input to the `known_until_confidence` override and is
otherwise not logged or published as its own field.

Empirically verified against the live API (2026-09-10, region `NL`):
`hours=-1&hourly=true` returns 169 hourly steps spanning a full week, with
`knownUntil` about 10-11 hours ahead of the request time. Confirms the "real
data" window is short (roughly the standard day-ahead publication horizon)
relative to the multi-day predicted tail this helper exists to expose.

Error handling: non-2xx responses (network failure, 5xx, or a 422 from a
malformed request the config validator failed to catch) are wrapped in a
local `FetchError`, exactly like the Nordpool and Zonneplan helpers.

---

## Package location and layout

```
mimirheim_helpers/prices/epexpredictor/
  epexpredictor_prices/
    __init__.py
    __main__.py        # EpexPredictorPricesDaemon entry point
    config.py           # Pydantic config models
    fetcher.py           # HTTP GET, raw (ts, price) extraction, FetchError
    series.py            # ConfidenceDecay + confidence_for_step + build_price_steps
    publisher.py          # Identical pattern to nordpool/publisher.py
  tests/
    unit/
      test_config.py
      test_fetcher.py
      test_series.py
      test_main.py
      test_publisher.py
  README.md
  AGENTS.md
```

The package is named `epexpredictor_prices` (not `epexpredictor`) to avoid
confusion with any future upstream Python client of the same name, matching
the reasoning already documented for `zonneplan_prices`.

No auth flow and no persisted token file are needed: the API is
unauthenticated, so the package has no `token.py` or `auth.py` equivalents to
Zonneplan.

---

## Configuration schema (`config.py`)

```python
from __future__ import annotations

from typing import Callable, Literal
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from helper_common.config import HomeAssistantConfig, MqttConfig
import helper_common.topics as _topics

# Default formulas: pass the raw predicted price through unchanged.
_DEFAULT_IMPORT_FORMULA = "price"
_DEFAULT_EXPORT_FORMULA = "price"

# EPEX day-ahead has been quoted on a 15-minute market time unit for most
# areas since October 2025, matching the Nordpool helper's default and
# reasoning.
_DEFAULT_PRICE_INTERVAL = "quarter_hourly"

# Default request horizon. The API will return about a week of hourly steps
# when asked for everything it has (hours=-1), but only the first several
# hours of that are real day-ahead data; the rest is an increasingly
# speculative model prediction. 72 hours mirrors the open-meteo PV helper's
# forecast_days=3 default: enough to meaningfully extend past a same-day
# Nordpool/supplier horizon without requesting a full week of low-confidence
# data by default.
_DEFAULT_HORIZON_HOURS = 72

_AREA_CODES = Literal[
    "DE", "AT", "BE", "NL", "SE1", "SE2", "SE3", "SE4", "DK1", "DK2", "ES", "PT",
]


def _compile_formula(formula: str) -> Callable[[datetime, float], float]:
    """Compile a price formula string into a callable.

    Identical contract to the Nordpool helper's ``_compile_formula``: a
    Python expression referencing ``price`` (predicted EUR/kWh) and ``ts``
    (UTC-aware datetime), evaluated with full Python access. The config file
    is operator-controlled and treated as executable code; do not load config
    from untrusted sources.
    """
    ...  # identical body to nordpool/config.py::_compile_formula


class EpexPredictorApiConfig(BaseModel):
    """EpexPredictor-specific fetch and pricing parameters.

    Attributes:
        area: EPEX bidding zone code, one of the zones EpexPredictor
            supports (DE, AT, BE, NL, SE1-4, DK1-2, ES, PT). Rejected at
            config load time if outside this set, rather than surfacing as a
            422 from the API at the first fetch.
        base_url: API base URL. Defaults to the public
            ``https://epexpredictor.batzill.com`` instance. Override to point
            at a self-hosted instance; the project is open source and its
            own API description explicitly invites self-hosting, mirroring
            the same knob on the open-meteo PV helper's ``open_meteo.base_url``.
        horizon_hours: How many hours ahead to request. Passed straight
            through as the API's ``hours`` parameter. The API may return
            fewer steps near the edge of its own predicted range; the
            configured value is a ceiling, not a guarantee.
        import_formula: Python expression for the all-in import price in
            EUR/kWh. Available variables: ``price`` (predicted EUR/kWh),
            ``ts`` (datetime, UTC). This helper never asks the API to add a
            surcharge or tax itself (see fetcher.py); any markup belongs
            here.
        export_formula: Python expression for the net export price in
            EUR/kWh. Same variables as import_formula.
        price_interval: ``"quarter_hourly"`` (default) or ``"hourly"``.
            Mirrors the Nordpool helper's field of the same name and the
            same rationale: set to ``"hourly"`` only when your supplier
            bills a single dynamic price per whole hour.
    """

    model_config = ConfigDict(extra="forbid")

    area: _AREA_CODES = Field(..., json_schema_extra={"ui_label": "EPEX area", "ui_group": "basic"})
    base_url: str = Field(
        default="https://epexpredictor.batzill.com",
        description="API base URL. Change for a self-hosted EpexPredictor instance.",
        json_schema_extra={"ui_label": "API base URL", "ui_group": "advanced"},
    )
    horizon_hours: int = Field(
        default=_DEFAULT_HORIZON_HOURS,
        ge=1,
        json_schema_extra={"ui_label": "Horizon (hours)", "ui_group": "basic"},
    )
    import_formula: str = Field(default=_DEFAULT_IMPORT_FORMULA, json_schema_extra={"ui_label": "Import price formula", "ui_group": "basic"})
    export_formula: str = Field(default=_DEFAULT_EXPORT_FORMULA, json_schema_extra={"ui_label": "Export price formula", "ui_group": "basic"})
    price_interval: Literal["hourly", "quarter_hourly"] = Field(
        default=_DEFAULT_PRICE_INTERVAL,
        json_schema_extra={"ui_label": "Price interval", "ui_group": "basic"},
    )

    @field_validator("import_formula", "export_formula", mode="after")
    @classmethod
    def _validate_formula(cls, v: str) -> str:
        _compile_formula(v)
        return v


class ConfidenceDecayConfig(BaseModel):
    """Confidence values assigned to forecast steps by how far ahead they are.

    The four band fields have identical shape and identical starting
    defaults to the open-meteo PV helper's ``ConfidenceDecayConfig``, per
    explicit instruction: these numbers describe how much a
    day-or-more-ahead forecast can generally be trusted, not anything
    specific to weather or price. Anchored to fetch time, not to the API's
    own ``knownUntil`` real/predicted boundary (see Purpose above for the
    reasoning). ``known_until_confidence`` is an addition specific to this
    helper, with no PV equivalent, layered on top of that base mechanism.

    Attributes:
        hours_0_to_6: Confidence for steps 0-6 hours ahead. Default 0.90.
        hours_6_to_24: Confidence for steps 6-24 hours ahead. Default 0.75.
        hours_24_to_48: Confidence for steps 24-48 hours ahead. Default 0.55.
        hours_48_plus: Confidence for steps more than 48 hours ahead. Default 0.35.
        known_until_confidence: When set, overrides the decay-band result
            for any step at or before the API's ``knownUntil`` timestamp
            (real EPEX auction data already published) with this fixed
            value. Steps after ``knownUntil`` are never affected and keep
            using the bands above. Null (default): no override, the bands
            apply uniformly to every step including real data, matching the
            fetch-time-anchor design. Set to 1.0 to treat the real portion
            of this feed as confirmed data, the same way the Nordpool
            helper always reports confidence 1.0.
    """

    model_config = ConfigDict(extra="forbid")

    hours_0_to_6: float = Field(default=0.90, ge=0.0, le=1.0, json_schema_extra={"ui_label": "Confidence 0-6 h", "ui_group": "advanced"})
    hours_6_to_24: float = Field(default=0.75, ge=0.0, le=1.0, json_schema_extra={"ui_label": "Confidence 6-24 h", "ui_group": "advanced"})
    hours_24_to_48: float = Field(default=0.55, ge=0.0, le=1.0, json_schema_extra={"ui_label": "Confidence 24-48 h", "ui_group": "advanced"})
    hours_48_plus: float = Field(default=0.35, ge=0.0, le=1.0, json_schema_extra={"ui_label": "Confidence 48+ h", "ui_group": "advanced"})
    known_until_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Overrides the confidence of every step at or before the API's "
            "knownUntil timestamp with this fixed value. Null (default) "
            "applies the decay bands uniformly, including to real data."
        ),
        json_schema_extra={"ui_label": "Known-until confidence override", "ui_group": "advanced"},
    )


class EpexPredictorPricesConfig(BaseModel):
    """Root configuration for the epexpredictor_prices daemon.

    Mirrors NordpoolConfig in every structural respect so HelperDaemon's
    autodiscovery and stats machinery works unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig
    mimir_topic_prefix: str = "mimir"
    trigger_topic: str
    output_topic: str | None = None
    epexpredictor: EpexPredictorApiConfig
    confidence_decay: ConfidenceDecayConfig = Field(default_factory=ConfidenceDecayConfig)
    ha_discovery: HomeAssistantConfig | None = None
    stats_topic: str | None = None
    signal_mimir: bool = False
    mimir_trigger_topic: str | None = None

    @model_validator(mode="after")
    def _derive_mimir_topics(self) -> "EpexPredictorPricesConfig":
        p = self.mimir_topic_prefix
        if self.output_topic is None:
            self.output_topic = _topics.prices_topic(p)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(p)
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "EpexPredictorPricesConfig":
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir-epexpredictor"
        return self
```

Note: `output_topic` defaults to the same canonical
`{prefix}/input/prices` topic Nordpool defaults to. An operator running both
helpers together to extend the horizon must set one of the two
`output_topic` values explicitly so they land on different entries of the
`inputs.prices` list; this is exactly the scenario
`plans/done/69_multi_source_price_merge.md` built the list-of-topics support
for, and it must be called out in this helper's README (see below), not
silently defaulted around.

---

## Fetcher (`fetcher.py`)

```python
class FetchError(Exception):
    """Raised when the EpexPredictor API call fails unrecoverably."""


@dataclass
class FetchResult:
    """Raw prices and the API's real/predicted boundary for one fetch.

    Attributes:
        steps: Sorted (UTC-aware step start time, price in EUR/kWh) pairs.
        known_until: The API's ``knownUntil`` timestamp (UTC-aware): steps at
            or before this instant are real EPEX auction data, steps after
            it are the model's own prediction.
    """

    steps: list[tuple[datetime, float]]
    known_until: datetime


def fetch_raw_prices(
    *,
    area: str,
    horizon_hours: int,
    price_interval: str,
    base_url: str,
) -> FetchResult:
    """Fetch raw predicted spot prices from EpexPredictor.

    Always requests unit=EUR_PER_KWH and timezone=UTC, and always sends
    surcharge=0.0 and taxPercent=0.0 (or omits them) regardless of any
    caller input, since this helper has no config surface for either: markup
    and tax belong exclusively in import_formula / export_formula.

    Only steps at or after the current UTC hour are returned, matching the
    Nordpool helper's truncation behaviour, applied defensively in addition
    to whatever window the API itself returns by default. This truncation
    never drops ``known_until`` itself: it is read from the response's
    ``knownUntil`` field independently of the (possibly truncated) ``prices``
    list.

    Args:
        area: EPEX bidding zone code.
        horizon_hours: Hours ahead to request (the API's ``hours`` param).
        price_interval: "quarter_hourly" or "hourly"; maps to the API's
            ``hourly`` boolean.
        base_url: API base URL, public or self-hosted. Requests are made
            against ``f"{base_url}/prices"``.

    Returns:
        A ``FetchResult`` with the sorted price steps and ``known_until``.

    Raises:
        FetchError: On HTTP failure, network failure, or a malformed
            response body (missing "prices" or "knownUntil" key, unparsable
            timestamp).
    """
```

Uses `requests` (synchronous), matching the Zonneplan helper's choice: there
is no async client library pulling this helper toward `aiohttp` the way
`pynordpool` does for the Nordpool helper.

---

## Series / confidence (`series.py`)

Mirrors `pv_openmeteo/series.py` in structure and in the confidence-band
defaults and logic (`confidence_for_step`, `<` comparisons at the 6/24/48
hour boundaries, boundary hours falling into the higher band), adapted to
the price step shape (two price fields instead of one power field) and to
apply the import/export formulas:

```python
@dataclass
class ConfidenceDecay:
    hours_0_to_6: float
    hours_6_to_24: float
    hours_24_to_48: float
    hours_48_plus: float

    def confidence_for_step(self, step_ts: datetime, fetch_time: datetime) -> float:
        ...  # identical logic to pv_openmeteo.series.ConfidenceDecay


def build_price_steps(
    raw: list[tuple[datetime, float]],
    *,
    fetch_time: datetime,
    decay: ConfidenceDecay,
    import_formula: str,
    export_formula: str,
    known_until: datetime,
    known_until_confidence: float | None = None,
) -> list[dict]:
    """Apply the import/export formulas and confidence decay to raw prices.

    For each step, confidence is ``known_until_confidence`` when it is not
    None and the step's timestamp is at or before ``known_until``; otherwise
    it falls back to ``decay.confidence_for_step()``, unconditionally. This
    is the only place the override takes effect: a step after
    ``known_until`` always uses the fetch-time band, regardless of the
    override setting.

    Args:
        raw: Sorted (UTC-aware ts, predicted EUR/kWh) pairs from fetcher.py.
        fetch_time: Reference instant for the confidence bands.
        decay: Confidence values per horizon band.
        import_formula: Python expression string, same variables as Nordpool.
        export_formula: Python expression string, same variables as Nordpool.
        known_until: The API's real/predicted boundary for this fetch, as
            returned in ``FetchResult.known_until``.
        known_until_confidence: Fixed confidence to use for every step at or
            before ``known_until``. None (default): no override, every step
            uses the decay bands, matching the original fetch-time-only
            design.

    Returns:
        List of dicts, each with ``ts`` (ISO 8601 UTC string, matching
        ``datetime.isoformat()`` on an aware UTC datetime, i.e. a "+00:00"
        suffix, not "Z" - the same format Nordpool publishes),
        ``import_eur_per_kwh``, ``export_eur_per_kwh`` (both rounded to 6
        decimals, same as Nordpool), and ``confidence``.
    """
```

`_compile_formula` is imported from `config.py`, exactly as
`nordpool/fetcher.py` does today.

---

## Daemon (`__main__.py`)

A `HelperDaemon` subclass, structurally identical to `NordpoolDaemon`:

```python
class EpexPredictorPricesDaemon(HelperDaemon):
    TOOL_NAME = "epexpredictor_prices"
    FORECAST_VALUE_TEMPLATE = "{{ value_json[0].import_eur_per_kwh | default(0) | round(4) }}"
    FORECAST_UNIT = "EUR/kWh"
    FORECAST_DEVICE_CLASS = None
    FORECAST_ATTRIBUTES_TEMPLATE = PRICE_FORECAST_ATTRIBUTES_TEMPLATE

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        config = self._config
        fetch_time = datetime.now(tz=timezone.utc)
        try:
            result = fetch_raw_prices(
                area=config.epexpredictor.area,
                horizon_hours=config.epexpredictor.horizon_hours,
                price_interval=config.epexpredictor.price_interval,
                base_url=config.epexpredictor.base_url,
            )
        except FetchError:
            logger.exception(
                "EpexPredictor fetch failed - retaining existing payload on %s",
                config.output_topic,
            )
            return
        decay = ConfidenceDecay(
            hours_0_to_6=config.confidence_decay.hours_0_to_6,
            hours_6_to_24=config.confidence_decay.hours_6_to_24,
            hours_24_to_48=config.confidence_decay.hours_24_to_48,
            hours_48_plus=config.confidence_decay.hours_48_plus,
        )
        steps = build_price_steps(
            result.steps,
            fetch_time=fetch_time,
            decay=decay,
            import_formula=config.epexpredictor.import_formula,
            export_formula=config.epexpredictor.export_formula,
            known_until=result.known_until,
            known_until_confidence=config.confidence_decay.known_until_confidence,
        )
        publish_prices(
            client,
            config.output_topic,
            steps,
            signal_mimir=config.signal_mimir,
            mimir_trigger_topic=config.mimir_trigger_topic,
        )
        step_hours = 0.25 if config.epexpredictor.price_interval == "quarter_hourly" else 1.0
        return CycleResult(horizon_hours=len(steps) * step_hours)
```

`publisher.py` is a straight copy of `nordpool/publisher.py` with the import
path updated; no behavioural change is needed since the output shape
(`ts`, `import_eur_per_kwh`, `export_eur_per_kwh`, `confidence`) is identical.

---

## Output format

Identical to Nordpool. Each step:

```json
{
  "ts": "2026-09-10T09:00:00+00:00",
  "import_eur_per_kwh": 0.213450,
  "export_eur_per_kwh": 0.213450,
  "confidence": 0.90
}
```

---

## Tests to write first (TDD)

### `tests/unit/test_config.py`

- Happy path: minimal config with required fields (`area`, formulas defaulted) validates.
- Unknown `area` value raises `ValidationError` (Literal enum).
- `base_url` omitted defaults to `"https://epexpredictor.batzill.com"`; an explicit override is preserved unchanged.
- `horizon_hours <= 0` raises `ValidationError`.
- `import_formula` / `export_formula` with a syntax error raises `ValidationError`.
- Unknown top-level or nested field raises `ValidationError` (`extra="forbid"`).
- `confidence_decay` omitted entirely defaults to 0.90 / 0.75 / 0.55 / 0.35, with `known_until_confidence` defaulting to `None`.
- `confidence_decay` with explicit band overrides is respected.
- `known_until_confidence` outside `[0.0, 1.0]` raises `ValidationError`; a value inside the range (including `1.0`) is preserved.
- `output_topic` / `mimir_trigger_topic` default derivation from `mimir_topic_prefix`, same as Nordpool's existing tests.
- `mqtt.client_id` defaults to `"mimir-epexpredictor"` when not set.

### `tests/unit/test_fetcher.py`

Mock `requests.get` (or use the `responses` library, matching whichever
Zonneplan already depends on).

- Request always includes `unit=EUR_PER_KWH` and `timezone=UTC` regardless of config.
- Request always includes `surcharge=0.0` and `taxPercent=0.0` (or omits both), and there is no code path that can set them to anything else - this is the regression test enforcing the "never use the API for surcharges or taxes" instruction.
- Request URL is built from the passed `base_url` (default and a custom self-hosted override both tested).
- `region` query param equals the configured `area`.
- `hours` query param equals the configured `horizon_hours`.
- `price_interval="hourly"` sends `hourly=true`; `"quarter_hourly"` sends `hourly=false`.
- A response with several `prices` entries returns a `FetchResult` whose `steps` are sorted (datetime, float) pairs, datetime UTC-aware.
- `FetchResult.known_until` is parsed from the response's `knownUntil` field as a UTC-aware datetime.
- Entries whose `startsAt` is before the current UTC hour are excluded from `steps`; `known_until` itself is unaffected by this truncation even when it falls before the current hour.
- An empty `prices` list returns a `FetchResult` with `steps == []` and `known_until` still parsed from the response.
- A non-2xx HTTP response raises `FetchError`.
- A response body missing the `prices` or `knownUntil` key raises `FetchError`.
- A network-level exception (e.g. `requests.ConnectionError`) raises `FetchError`.

### `tests/unit/test_series.py`

- `confidence_for_step`: 0h ahead -> 0.90; 5.99h -> 0.90; 6h -> 0.75; 23.99h -> 0.75; 24h -> 0.55; 47.99h -> 0.55; 48h -> 0.35; 200h -> 0.35 (same boundary semantics as `pv_openmeteo.series.ConfidenceDecay`).
- Custom (non-default) `ConfidenceDecay` values are honoured.
- `build_price_steps` applies `import_formula` and `export_formula` independently per step (e.g. `"price * 1.1"` and `"0.0"`).
- Output `ts` is `datetime.isoformat()` on a UTC-aware datetime (`"+00:00"` suffix), not the API's original `"Z"`-suffixed string - explicit assertion this matches Nordpool's timestamp format byte for byte given the same instant.
- `import_eur_per_kwh` / `export_eur_per_kwh` rounded to 6 decimals.
- With `known_until_confidence=None` (default): confidence is computed purely from `fetch_time` and the step's own `ts`, regardless of `known_until` - a step far in the future produces the lowest band even when `known_until` is set even further out. This is the regression test for the original anchor-to-fetch-time decision, now that `known_until` is passed into the function.
- With `known_until_confidence` set (e.g. `1.0`): a step at or before `known_until` gets exactly that value, even when the fetch-time band for its horizon distance would otherwise be lower (or higher).
- With `known_until_confidence` set: a step strictly after `known_until` still uses the normal fetch-time band, unaffected by the override.
- Boundary: a step exactly at `known_until` receives the override (inclusive comparison).
- An empty input list returns `[]`.

### `tests/unit/test_publisher.py`

Copy of `nordpool/tests/unit/test_publisher.py` with import paths updated;
no new behaviour to test since `publisher.py` is an unmodified copy.

### `tests/unit/test_main.py`

- A successful fetch cycle publishes retained steps to `output_topic` and returns a `CycleResult` with `horizon_hours` computed from `len(steps) * step_hours`.
- `FetchError` from the fetcher is caught, logged, and the daemon returns `None` without publishing (existing retained payload untouched).
- `signal_mimir=True` triggers a publish to `mimir_trigger_topic` after prices are published.

---

## Implementation sequence

1. Write all tests first - they will fail.
2. `config.py` - Pydantic models with formula compilation (tests pass).
3. `fetcher.py` - HTTP GET and raw price extraction (tests pass, mocked HTTP).
4. `series.py` - confidence decay and formula application (tests pass).
5. `publisher.py` - copy from nordpool with import path changes (tests pass).
6. `__main__.py` - daemon wiring (tests pass).
7. `README.md` - document configuration, output format, and the "set a distinct output_topic when running alongside another price source" note.
8. `AGENTS.md` - copy of nordpool's, package name and paths updated.
9. `mimirheim_helpers/examples/epexpredictor.yaml` - example config, modeled on `mimirheim_helpers/examples/nordpool.yaml`.
10. Root `pyproject.toml`:
    - `[project.optional-dependencies]`: new `epexpredictor = ["requests>=2.34.2"]` extra (reuse whatever `requests` constraint the Zonneplan extra already pins); add to the `helpers` meta-extra.
    - `[tool.hatch.build.targets.wheel]` members: add `mimirheim_helpers/prices/epexpredictor/epexpredictor_prices`.
    - test discovery / `testpaths`: add `mimirheim_helpers/prices/epexpredictor/tests`.
11. `wiki/Helpers/EpexPredictor.md` - setup guide, modeled on `wiki/Helpers/Nordpool.md`, explicitly documenting the confidence-decay-by-fetch-time design and the "this is a modeled source, combine it with a real day-ahead source via inputs.prices for best results" framing.

---

## Acceptance criteria

- `uv run pytest mimirheim_helpers/prices/epexpredictor/tests/ -q` is green.
- `uv run ruff check .` is clean.
- Running the daemon against the live API with a valid `area` fetches prices and publishes a non-empty retained payload to the configured `output_topic`.
- The output payload conforms to the mimirheim prices input format documented in README.md, section "Input topics / prices", byte-for-byte compatible with what the Nordpool helper produces for the same fields (unit, timestamp format).
- Confidence values follow the configured decay bands, anchored to fetch time, and are never used to smuggle a surcharge or tax adjustment - the only two config-driven levers for that are `import_formula` and `export_formula`.
- No mimirheim core files are modified; this plan is additive-only at the helper-package level.

---

## Known and out of scope

- **Logging `knownUntil`.** The value is parsed and used as the input to `known_until_confidence`, but is not otherwise logged or published as its own field (e.g. in the stats payload). Could be added later as a diagnostic log line if the real/predicted boundary becomes operationally interesting on its own, but that is a separate change.
- **`evaluation` mode.** Not exposed. It exists for the API operator's own model-evaluation tooling, not for production price fetching.
- The Jedison config-editor (separate repo) will pick up this helper's schema the same way it picks up every other helper's; no work tracked here, consistent with how `plans/done/69_multi_source_price_merge.md` treated the same cross-repo boundary.
