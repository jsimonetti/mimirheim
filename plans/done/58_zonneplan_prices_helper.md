# Step 58 — Zonneplan prices helper

## Purpose

A standalone daemon that fetches electricity prices from the Zonneplan API and
publishes them to the mimirheim prices input topic in the same format as the
Nordpool helper.

Zonneplan is a Dutch energy supplier that provides dynamic (hourly) electricity
prices via an app API. Unlike Nordpool, the prices returned are all-in consumer
prices, not raw spot prices. The API requires an email-based activation step
that cannot be fully automated (the user must click a link in an email), but
the daemon handles the auth flow itself — no separate CLI or container exec is
required.

---

## Relevant IMPLEMENTATION_DETAILS sections

None — this is a standalone helper package outside the mimirheim core, following
the same conventions as `mimirheim_helpers/prices/nordpool/`.

---

## API reference

All calls go to `https://app-api.zonneplan.nl/`. Required headers:

```
Content-Type: application/json;charset=utf-8
x-app-version: 5.10.1
x-app-environment: production
```

### Authentication flow

Step 1 — Request login email:
```
POST /auth/request
Body: {"email": "user@example.com"}
Response: {"data": {"uuid": "<auth-request-uuid>"}}
```

Step 2 — User clicks the activation link in the email. This is an interactive
step that cannot be automated. The link contains a one-time password.

Step 3 — Poll for activation (repeat until `is_activated == true`):
```
GET /auth/request/<auth-request-uuid>
Response when pending:  {"data": {"is_activated": false}}
Response when activated: {"data": {"is_activated": true, "password": "<otp>"}}
```

Step 4 — Exchange one-time password for tokens:
```
POST /oauth/token
Body: {
  "grant_type": "one_time_password",
  "email": "user@example.com",
  "password": "<otp>"
}
Response: {
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

### Token refresh

```
POST /oauth/token
Body: {"grant_type": "refresh_token", "refresh_token": "<refresh_token>"}
Response: same shape as above
```

### Account discovery

```
GET /user-accounts/me
Authorization: Bearer <access_token>
Response: {
  "data": {
    "address_groups": [{
      "connections": [{"uuid": "<connection-uuid>", "market_segment": "electricity", ...}]
    }]
  }
}
```

### Price data

```
GET /connections/<connection-uuid>/summary
Authorization: Bearer <access_token>
Response: {
  "data": {
    "price_per_hour": [
      {
        "datetime": "2026-05-28T08:00:00.000000Z",
        "electricity_price": 1546185,
        "electricity_price_excl_tax": 437704,
        "tariff_group": "low",
        "solar_percentage": 0,
        "solar_yield": 0,
        "sustainability_score": 1213
      },
      ...
    ]
  }
}
```

All integer price fields use the same scale as the rest of the Zonneplan API:
`raw_value × 0.0000001 = EUR/kWh`.

Fields present in every entry:

| Field | Type | Meaning |
|---|---|---|
| `datetime` | ISO 8601 string | Start of the hour (UTC) |
| `electricity_price` | int | All-in import price (incl. tax) |
| `electricity_price_excl_tax` | int | Import price excluding tax |
| `tariff_group` | str | `"low"` or `"high"` peak group |
| `solar_percentage` | int | Renewable mix percentage (0–100) |
| `solar_yield` | int | Solar contribution |
| `sustainability_score` | int | Zonneplan sustainability index |

Only `datetime`, `electricity_price`, and `electricity_price_excl_tax` are
exposed as formula variables. The remaining fields are available in the raw
response but not surfaced to formulas — they are irrelevant to pricing.

There is no per-hour export (production) price in the summary. Export price
must be operator-configured via formula.

---

## Package location and layout

```
mimirheim_helpers/prices/zonneplan/
  zonneplan_prices/
    __init__.py
    __main__.py        # ZonneplanPricesDaemon entry point
    auth.py            # Auth flow logic: request email, poll, exchange OTP
    config.py          # Pydantic config models
    api.py             # HTTP client: token refresh, GET summary
    fetcher.py         # Transforms price_per_hour → mimirheim step list
    publisher.py       # MQTT publish (identical pattern to nordpool publisher.py)
    token.py           # Token load / save / expiry; pending-auth state file
  tests/
    __init__.py
    test_config.py
    test_fetcher.py
    test_token.py
    test_api.py
    test_auth.py
  README.md
  pyproject.toml
```

The package is named `zonneplan_prices` (not `zonneplan`) to avoid confusion with
any future `pip install zonneplan` package.

---

## Configuration schema (`config.py`)

```python
class ZonneplanApiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Email address used to trigger the Zonneplan login email.
    # Required for the in-daemon auth flow. When absent and the token file is
    # also absent, the daemon logs an error and refuses to start.
    email: str | None = None

    # Path to the JSON file where OAuth tokens are persisted between restarts.
    # Defaults to zonneplan_token.json next to the config file.
    token_file: str = "zonneplan_token.json"

    # Zonneplan connection UUID to fetch prices for.
    # If omitted, the first electricity connection from /user-accounts/me is used.
    connection_uuid: str | None = None

    # Formula string applied to derive import_eur_per_kwh from each price step.
    # Available variables:
    #   price          — all-in import price incl. tax (EUR/kWh, float)
    #   price_excl_tax — import price excl. tax (EUR/kWh, float)
    #   ts             — step start time (datetime, UTC-aware)
    # Default: pass the all-in price through unchanged.
    import_formula: str = "price"

    # Formula string for the net export price (EUR/kWh).
    # Same variables available as import_formula. Because Zonneplan does not
    # publish a per-hour export price, most operators use a fixed value or a
    # formula based on price_excl_tax.
    # Default: 0.0 (no export revenue modelled).
    export_formula: str = "0.0"


class ZonneplanPricesConfig(BaseModel):
    """Root configuration for the zonneplan_prices daemon.

    Mirrors NordpoolConfig in structure so that HelperDaemon's autodiscovery
    and stats machinery works without any changes to the base class.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig
    mimir_topic_prefix: str = "mimir"

    # Topic the daemon subscribes to. A message here fires one fetch cycle.
    trigger_topic: str

    # Retained price payload destination. Defaults to
    # '{mimir_topic_prefix}/input/prices' when None.
    output_topic: str | None = None

    zonneplan: ZonneplanApiConfig

    # Optional HA MQTT discovery settings. When present, HelperDaemon
    # publishes a button entity under the discovery prefix on every connect
    # and on every HA birth message (homeassistant/status). No extra code
    # is needed in the subclass — the base class handles it via TOOL_NAME.
    ha_discovery: HomeAssistantConfig | None = None

    # Optional topic where per-cycle run statistics are published.
    stats_topic: str | None = None

    signal_mimir: bool = False
    mimir_trigger_topic: str | None = None

    @model_validator(mode="after")
    def _check_mimir_trigger(self) -> "ZonneplanPricesConfig":
        if self.signal_mimir and not self.mimir_trigger_topic:
            raise ValueError(
                "mimir_trigger_topic must be set when signal_mimir is True"
            )
        return self
```

---

## Token and pending-auth state (`token.py`)

Two files live alongside each other on disk:

**Token file** (`token_file`, default `zonneplan_token.json`):
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "token_type": "Bearer",
  "expires_at": "2026-05-06T10:00:00+00:00"
}
```
`expires_at` is computed at write time as `now + expires_in - 60s`.

**Pending-auth file** (`<token_file_stem>_pending.json`):
```json
{
  "uuid": "<auth-request-uuid>",
  "email": "user@example.com",
  "requested_at": "2026-05-06T09:50:00+00:00"
}
```
Stored when a login email has been sent but not yet activated. On restart, the
daemon resumes polling the same UUID instead of sending a new email. The pending
file is deleted on successful activation.

**The one operational gap not yet in the plan** is that both files must be on a Docker volume. If they live inside the container filesystem they're lost on every restart and the auth flow repeats from scratch every time. The README must document this under a "Docker deployment" section, and the default `token_file` path should be chosen so it naturally lands on a volume mount (e.g. the same directory as `config.yaml`).

`token.py` exposes:
- `load_token(path: Path) -> dict | None`
- `save_token(path: Path, token: dict) -> None` — writes file with `expires_at`.
- `is_token_valid(token: dict) -> bool` — True if `expires_at` is in the future.
- `load_pending(path: Path) -> dict | None`
- `save_pending(path: Path, uuid: str, email: str) -> None`
- `delete_pending(path: Path) -> None`
- `is_pending_fresh(pending: dict) -> bool` — True if `requested_at` is < 4 minutes
  ago (OTP links expire after ~5 minutes; 4 minutes gives a safety margin to
  still complete the token exchange before expiry).

---

## API client (`api.py`)

Synchronous HTTP client (uses `requests`). Wraps the minimal Zonneplan API calls
needed by this helper:

```python
class ZonneplanClient:
    def request_login_email(self, email: str) -> str:
        """POST /auth/request. Returns the auth-request UUID.
        Raises FetchError on HTTP or network failure.
        """

    def poll_activation(self, uuid: str) -> dict | None:
        """GET /auth/request/{uuid}.
        Returns the token dict if activated, None if still pending.
        Raises AuthError if the request has expired.
        """

    def get_summary(self, connection_uuid: str) -> dict:
        """GET /connections/{connection_uuid}/summary.
        Raises FetchError on HTTP or network failure.
        """

    def refresh_token(self, refresh_token: str) -> dict:
        """POST /oauth/token with grant_type=refresh_token.
        Raises AuthError if the refresh token is expired or invalid.
        """

    def get_connection_uuid(self) -> str:
        """GET /user-accounts/me and return the first electricity connection UUID.
        Raises FetchError if no electricity connection is found.
        """
```

The client holds the current access token in memory and injects it as
`Authorization: Bearer <token>` on each request. Token refresh (when the access
token is near expiry) is handled by `_run_cycle` before calling `get_summary`.

---

## In-daemon authentication flow (`auth.py`)

No separate CLI is needed. `auth.py` provides a single function called by
`_run_cycle` when a valid token is unavailable:

```python
def attempt_auth(
    *,
    client: ZonneplanClient,
    email: str,
    token_path: Path,
    pending_path: Path,
    poll_window_seconds: int = 30,
) -> dict | None:
    """Attempt one round of the Zonneplan email auth flow.

    Called by _run_cycle when no valid token is present. Each call does at most
    poll_window_seconds of polling so the MQTT thread is not blocked for long.

    On the first call with no pending file: sends the login email and starts
    polling. On subsequent calls (pending file exists and is fresh): resumes
    polling. If the pending file is stale (OTP expired): deletes it and sends
    a new login email.

    Returns the new token dict if activated within this call's poll window,
    or None if the user has not yet clicked the link.

    Raises AuthError only on unrecoverable failures (e.g. bad email address).
    """
```

### Logging during auth

```
[WARNING] No Zonneplan token found. Sending login email...
[WARNING] Login email sent. Check your inbox and click the activation link.
          Polling for activation (up to 30s this cycle)...
[WARNING] Still waiting for Zonneplan activation (polling). Click the link in
          your email. Will retry on the next trigger.
[INFO]    Zonneplan activation confirmed. Token saved to zonneplan_token.json.
```

If `email` is not configured and no token file exists, the daemon logs at ERROR
level and returns immediately:
```
[ERROR] No Zonneplan token found and no email address configured. Set
        zonneplan.email in config.yaml to enable automatic authentication.
```

---

## Fetcher (`fetcher.py`)

```python
def fetch_prices(
    *,
    client: ZonneplanClient,
    connection_uuid: str,
    import_formula: str,
    export_formula: str,
) -> list[dict[str, Any]]:
    """Fetch price steps from Zonneplan and return the mimirheim-format list.

    Returns:
        Sorted list of step dicts with keys:
        - ts: ISO 8601 UTC timestamp (start of hour).
        - import_eur_per_kwh: all-in import price after import_formula.
        - export_eur_per_kwh: export price after export_formula.
        - confidence: always 1.0 (Zonneplan prices are confirmed).

    Only steps at or after the current UTC hour are included, matching the
    Nordpool helper's truncation behaviour.

    Raises:
        FetchError: on API failure.
        AuthError: if the refresh token is expired and cannot be used.
    """
```

Price transformation per step. Given a raw `price_per_hour` entry:

```python
price          = entry["electricity_price"] * 0.0000001        # EUR/kWh incl. tax
price_excl_tax = entry["electricity_price_excl_tax"] * 0.0000001 # EUR/kWh excl. tax
ts             = datetime.fromisoformat(entry["datetime"])       # UTC datetime
```

All three variables are passed into both `import_fn` and `export_fn` as keyword
arguments. The compiled lambda signature is:

```python
lambda ts, price, price_excl_tax: <expr>
```

This allows formulas like:
- `"price"` — all-in import, pass through
- `"price_excl_tax * 1.21 + 0.05"` — add fixed markup to excl-tax price

---

## Daemon (`__main__.py`)

A `HelperDaemon` subclass, identical in structure to `NordpoolDaemon`:

```python
class ZonneplanPricesDaemon(HelperDaemon):
    TOOL_NAME = "zonneplan_prices"

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        config = self._config.zonneplan
        token_path = Path(config.token_file)
        pending_path = token_path.with_stem(token_path.stem + "_pending")

        # 1. Load token.
        token = load_token(token_path)

        # 2. If no valid token, attempt the auth flow.
        if not token or not is_token_valid(token):
            if not token or token is None:
                # Try refresh first if we have a stale token.
                try:
                    token = api_client.refresh_token(token["refresh_token"])
                    save_token(token_path, token)
                except AuthError:
                    token = None

            if not token:
                if not config.email:
                    logger.error(
                        "No Zonneplan token found and no email configured. "
                        "Set zonneplan.email in config.yaml."
                    )
                    return None
                token = attempt_auth(
                    client=api_client,
                    email=config.email,
                    token_path=token_path,
                    pending_path=pending_path,
                )
                if not token:
                    return None  # still waiting for user to click link

        # 3. Resolve connection_uuid (from config or auto-discover).
        # 4. Call fetch_prices().
        # 5. Call publish_prices().
        # 6. Return CycleResult(horizon_hours=len(steps)).
```

The daemon never crashes on auth failure. On each trigger it makes at most one
network call (or a short polling window) before returning. The existing retained
MQTT payload is left unchanged until a successful fetch.

---

## Status reporting

The HA discovery payload creates a trigger button in HA. During the auth waiting
period the logs contain WARNING-level messages on every trigger cycle so the
operator knows exactly what the daemon is waiting for. Once authenticated the
logs return to INFO-level normal operation.

---

## Output format

Identical to Nordpool. Each step:
```json
{
  "ts": "2026-05-06T09:00:00+00:00",
  "import_eur_per_kwh": 0.213450,
  "export_eur_per_kwh": 0.0,
  "confidence": 1.0
}
```

---

## Tests to write first (TDD)

### `tests/test_config.py`

- Happy path: minimal config with required fields validates.
- `signal_mimir=True` without `mimir_trigger_topic` raises `ValidationError`.
- Unknown field raises `ValidationError` (extra="forbid").
- `import_formula` with syntax error raises `ValidationError`.

### `tests/test_token.py`

- `load_token` returns None for absent file.
- `load_token` returns None for malformed JSON.
- `save_token` writes a file with `expires_at` computed correctly.
- `is_token_valid` returns True for future `expires_at`.
- `is_token_valid` returns False for past `expires_at`.
- `load_pending` returns None for absent file.
- `save_pending` writes a file with `requested_at` set to now.
- `is_pending_fresh` returns True when `requested_at` is 1 minute ago.
- `is_pending_fresh` returns False when `requested_at` is 6 minutes ago.

### `tests/test_fetcher.py`

- A `price_per_hour` list with 5 entries, all in the future, returns 5 steps.
- Steps from before the current hour are excluded.
- `electricity_price` (raw int) is multiplied by 0.0000001 to produce `price`.
- `electricity_price_excl_tax` (raw int) is multiplied by 0.0000001 to produce
  `price_excl_tax`.
- `import_formula="price * 1.1"` is applied correctly.
- `import_formula="price_excl_tax * 1.21 + 0.05"` produces the correct result.
- `export_formula="price_excl_tax * 0.8"` is applied to the export price.
- An empty `price_per_hour` list returns an empty list.
- `FetchError` raised by the client propagates unchanged.

### `tests/test_auth.py`

- When no pending file exists, `attempt_auth` calls `request_login_email` and
  writes a pending file.
- When a fresh pending file exists, `attempt_auth` does not send a new email.
- When a fresh pending file exists (simulating a container restart mid-auth),
  `attempt_auth` resumes polling the existing UUID without sending a new email.
- When a stale pending file exists, `attempt_auth` deletes it and sends a new email.
- When `poll_activation` returns a token, `attempt_auth` saves the token, deletes
  the pending file, and returns the token.
- When `poll_activation` returns None throughout the window, `attempt_auth`
  returns None.
- When `email` is None and no token exists, `_run_cycle` logs at ERROR and returns
  None without calling `request_login_email`.

### `tests/test_api.py`

- `get_summary` calls the correct URL with the Bearer token header.
- `refresh_token` HTTP failure raises `AuthError`.
- `get_summary` HTTP failure raises `FetchError`.
- `request_login_email` returns the UUID from the response.
- `poll_activation` returns None when `is_activated` is false.
- `poll_activation` returns a token dict when `is_activated` is true.

---

## Implementation sequence

1. Write all tests first — they will fail.
2. `token.py` — load/save/expiry + pending-auth state (tests pass).
3. `config.py` — Pydantic models with formula compilation (tests pass).
4. `api.py` — HTTP client (tests pass with `responses` or `unittest.mock`).
5. `auth.py` — in-daemon auth flow using `api.py` and `token.py` (tests pass).
6. `fetcher.py` — price transformation (tests pass).
7. `publisher.py` — copy from nordpool with name changes.
8. `__main__.py` — daemon wiring.
9. `README.md` — document auth flow, config schema, output format.
10. `pyproject.toml` — add to workspace under `[prices.zonneplan]` optional group.

---

## Acceptance criteria

- `uv run pytest mimirheim_helpers/prices/zonneplan/tests/ -q` is green.
- Running the daemon with a valid token file fetches prices and publishes a
  non-empty retained payload to the configured output topic.
- Running the daemon with `email` configured and no token file: logs a WARNING
  about the sent email, polls for activation, and does not crash.
- Running the daemon without a token file and without `email` configured: logs
  an ERROR with a clear message and does not crash.
- After the user clicks the activation link, the next trigger cycle completes
  authentication, saves the token, and publishes prices — no container restart
  or exec required.
- The output payload conforms to the mimirheim prices input format documented
  in README.md §Input topics / prices.
