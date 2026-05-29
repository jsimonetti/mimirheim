# zonneplan\_prices

Fetches hourly electricity prices from the Zonneplan API and publishes them to
an MQTT topic in the format expected by mimirheim. Intended to be used alongside
the mimirheim solver as a drop-in replacement for the Nordpool prices helper.

Zonneplan is a Dutch energy supplier that publishes dynamic (hourly) all-in
consumer prices. Unlike Nordpool, no API key is needed — authentication uses
an email OTP flow that the daemon handles automatically.

---

## How it works

1. On each trigger message the daemon checks whether a valid access token is
   available on disk.
2. If no token exists, or the token has expired and cannot be refreshed, the
   daemon sends a login email to the configured address and begins polling for
   activation. **The user must click the link in the email once.**
3. Once authenticated, the daemon fetches the `price_per_hour` list from
   `GET /connections/{uuid}/summary`, applies the configured price formulas,
   and publishes the result retained to the output topic.
4. The token is persisted to disk and automatically refreshed on subsequent
   cycles — the email link is only needed once per token lifetime.

---

## Configuration

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: zonneplan-prices

trigger_topic: mimir/input/tools/prices/trigger
output_topic: mimir/input/prices   # optional; defaults to {mimir_topic_prefix}/input/prices

zonneplan:
  email: your@email.com            # required for first-time authentication
  token_file: /data/zonneplan_token.json   # must be on a persistent volume
  import_formula: "price"          # optional; see Formulas section
  export_formula: "0.0"            # optional; see Formulas section

# Optional — trigger a mimirheim solve after each successful price fetch.
signal_mimir: false
mimir_trigger_topic: mimir/trigger

# Optional — publish HA MQTT discovery (creates a button in Home Assistant).
ha_discovery:
  enabled: true
  discovery_prefix: homeassistant
  device_name: "Zonneplan Prices"

# Optional — publish per-cycle statistics.
stats_topic: mimir/stats/zonneplan
```

### Formulas

Both `import_formula` and `export_formula` are Python expressions evaluated
per price step. Three variables are available:

| Variable | Type | Description |
|---|---|---|
| `price` | float | All-in import price incl. VAT, EUR/kWh |
| `price_excl_tax` | float | Import price excl. VAT, EUR/kWh |
| `ts` | datetime | Step start time (UTC-aware) |

Examples:

```yaml
import_formula: "price"                               # pass through all-in price (default)
import_formula: "price_excl_tax * 1.21 + 0.05"        # add fixed markup to excl-VAT price
import_formula: "price + (0.05 if ts.hour < 7 else 0.10)"  # time-of-day tariff
export_formula: "price_excl_tax * 0.8"                # 80% of excl-VAT price
```

---

## Docker deployment

The token file **must be on a persistent Docker volume**. If it lives inside the
container filesystem it is lost on every container restart and the email auth
flow repeats from scratch each time.

Mount the directory containing `config.yaml` as a volume and set `token_file`
to a path within it:

```yaml
# config.yaml
zonneplan:
  token_file: /data/zonneplan_token.json
```

```yaml
# docker-compose.yml (excerpt)
volumes:
  - ./data:/data
```

---

## Authentication flow detail

The daemon handles authentication without any CLI commands:

1. **First run with no token**: The daemon logs a WARNING and sends a login
   email. Check your inbox and click the activation link.
2. **Polling**: While waiting for the click, every trigger cycle logs a
   WARNING (`Still waiting for Zonneplan activation — click the link...`).
3. **After activation**: The daemon saves the token and immediately continues
   to fetch prices. No restart is needed.
4. **Subsequent runs**: The access token is refreshed automatically. The login
   email is only sent again if the refresh token itself expires (typically
   after weeks or months).

The pending-auth state is persisted to a `_pending.json` file alongside the
token file. If the container restarts mid-authentication, the daemon resumes
polling the same activation UUID rather than sending a new email.

---

## Output format

Each step in the published JSON array:

```json
{
  "ts": "2026-05-28T10:00:00+00:00",
  "import_eur_per_kwh": 0.154619,
  "export_eur_per_kwh": 0.0,
  "confidence": 1.0
}
```

The payload is published retained. mimirheim reads it on the configured prices
input topic.
