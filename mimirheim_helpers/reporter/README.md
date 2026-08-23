# reporter — HTML report archive for mimirheim solves

**reporter** is a daemon that turns mimirheim's solve dumps into a browsable archive of HTML reports, one per solve, with charts of what the solver decided and why.

It is the only helper that produces nothing mimirheim consumes. Every other helper feeds an input topic; this one reads what came out and renders it for a human.

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [Output](#4-output)
5. [Running](#5-running)
6. [Serving the reports](#6-serving-the-reports)
7. [Fault tolerance](#7-fault-tolerance)
8. [Security notes](#8-security-notes)

---

## 1. Purpose

When `reporting.enabled` is set in the mimirheim configuration, mimirheim writes a dump file pair after every successful solve — `{ts}_input.json` and `{ts}_output.json` — and publishes a small JSON pointer to an MQTT topic. This tool turns those pairs into reports:

1. A notification arrives naming a new dump pair.
2. Both files are read and rendered into `{ts}_report.html`.
3. `inventory.js` is updated so the index page lists the new report.
4. Old reports beyond the retention limit are deleted.

The result is a directory of static files. Nothing needs a running web server to *produce*, only to *serve*.

---

## 2. How it works

### Event-driven, not polling

The daemon subscribes to `reporting.notify_topic` and does nothing until a notification arrives. It never scans the dump directory on a timer.

This is why it extends `MqttDaemon` rather than `HelperDaemon`: it has no trigger topic, no cycle, and no output topic. It only subscribes.

### Startup

Before entering the MQTT loop, `run()` does four things:

1. Creates `output_dir` if it does not exist.
2. Copies `index.html`, `index.css` and `plotly.min.js` into it, without overwriting files that are already there — so you can customise `index.html` and keep it.
3. **Catch-up scan.** Every dump pair on disk with no corresponding report is rendered, newest first. This is how a missed notification is recovered; there is no need for retained notifications or a durable subscription.
4. Rebuilds `inventory.js` from disk and runs one garbage-collection pass.

A restart therefore reconciles itself against whatever is on disk, which is what makes losing a notification a non-event.

### Retention

`max_reports` caps the number of `*_report.html` files. When the count is exceeded, the oldest are deleted and their entries removed from `inventory.js`. `0` means unlimited.

Only reports are deleted. The dump files in `dump_dir` belong to mimirheim, which has its own `max_dumps`, and this tool never touches them.

---

## 3. Configuration

`mimirheim_helpers/examples/reporter.yaml` is the annotated reference configuration, and it validates as shipped.

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: mimirheim-reporter

reporting:
  dump_dir: /data/mimirheim/dumps
  output_dir: /data/mimirheim/reports
  max_reports: 100
  notify_topic: mimir/status/dump_available
```

### Field reference

| Key | Type | Description |
| --- | --- | --- |
| `mqtt.host` | string | Broker hostname or IP address |
| `mqtt.port` | integer 1–65535 | Broker port. Default `1883`; use `8883` with TLS |
| `mqtt.client_id` | string | Client identifier, unique on the broker. Defaults to `mimir-reporter` |
| `mqtt.username` | string, optional | Broker username. Omit for anonymous access |
| `mqtt.password` | string, optional | Broker password |
| `mqtt.tls` | boolean | Enable TLS. Default `false` |
| `mqtt.tls_allow_insecure` | boolean | Skip certificate verification when `tls` is true |
| `mimir_topic_prefix` | string | mimirheim's `mqtt.topic_prefix`. Default `mimir`. Used to derive `notify_topic` |
| `reporting.dump_dir` | path | Where mimirheim writes its dump pairs. Must be readable here. Required |
| `reporting.output_dir` | path | Where reports are written. Created if absent. Required |
| `reporting.max_reports` | integer ≥ 0 | Reports to retain. Default `100`. `0` means unlimited |
| `reporting.notify_topic` | string, optional | Topic to subscribe to. Defaults to `{mimir_topic_prefix}/status/dump_available` |

Broker settings can also come from the environment — `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_SSL` — which the Home Assistant Supervisor injects, and which take precedence over the file.

Unknown keys are rejected. `chart_publishing` and `ha_discovery` used to live here and have been removed: the economic summary is published by mimirheim core on `outputs.last_solve`, and the chart series are available as Home Assistant MQTT sensor attributes. A config that still has either section will not start. See [wiki/Helpers/Reporter.md](../../wiki/Helpers/Reporter.md) for the apexcharts-card migration.

### It must see the same directory as mimirheim

`dump_dir` is documented as "shared with the mimirheim container", and the paths in the notification payload are absolute. Both sides must agree on what that path is. If you bind-mount the dumps at a different location in each container, the reporter refuses the notification and logs which path fell outside its configured `dump_dir` — see [Security notes](#8-security-notes).

---

## 4. Output

Everything lands in `output_dir`:

| File | Written | Purpose |
| --- | --- | --- |
| `{ts}_report.html` | per solve | The report. Self-contained apart from `plotly.min.js` |
| `inventory.js` | on every change | The report list, consumed by `index.html` |
| `index.html` | once, if absent | The index page. Customise freely; it is not overwritten |
| `index.css` | once, if absent | Its stylesheet |
| `plotly.min.js` | once, if absent | Copied out of the installed `plotly` package |

`{ts}` is the solve timestamp with the colons in the time part replaced by hyphens, so `2026-04-02T14:00:00Z` becomes `2026-04-02T14-00-00Z_report.html`.

`inventory.js` assigns a `window.MIMIRHEIM_REPORTS` array, newest first, with the metrics pre-computed so the index renders without fetching anything:

```javascript
// Auto-generated by mimirheim-reporter. Do not edit manually.
window.MIMIRHEIM_REPORTS = [
  {
    "ts": "2026-04-02T14:00:00Z",
    "file": "2026-04-02T14-00-00Z_report.html",
    "strategy": "minimize_cost",
    "solve_status": "optimal"
  }
];
```

Assigning to a global rather than serving JSON is deliberate: `inventory.py` states the reason as letting `index.html` build its table "without requiring a running web server", which is what makes opening the index straight off the disk work.

---

## 5. Running

```bash
uv run python -m reporter --config mimirheim_helpers/examples/reporter.yaml
```

The module path is `reporter`. The package is published at the top level of the `mimirheim` wheel, so there is no dotted path under `mimirheim_helpers`.

Install the extra, which brings `plotly`:

```bash
uv pip install "mimirheim[reporter]"
```

### Systemd unit example

```ini
[Unit]
Description=mimirheim report generator
After=network.target mosquitto.service

[Service]
WorkingDirectory=/opt/mimirheim
ExecStart=/opt/mimirheim/.venv/bin/python -m reporter --config /etc/mimirheim/reporter.yaml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## 6. Serving the reports

This tool serves nothing over HTTP. `output_dir` is a directory of static files; point anything at it.

The simplest option is the config editor, which proxies the directory when `reporter.yaml` is present in its config directory: `GET /reports` serves the index and `GET /reports/{file}` serves a report. It also serves the dump JSON at `GET /reports/dumps/{ts}_input.json`, so the download links in a report work through the proxy. Note that the config editor does not authenticate.

Otherwise nginx, caddy, a Home Assistant static-file add-on, or `python -m http.server` in that directory all work. Opening `index.html` straight off the disk works too; only the dump download links need the proxy.

---

## 7. Fault tolerance

Nothing in the notification path is allowed to kill the daemon. Every one of these is logged and skipped:

- A payload that is not JSON, or is missing `ts`, `input_path` or `output_path`.
- A dump file named in the payload that does not exist, or cannot be parsed.
- A render that raises. One unrenderable dump does not stop the next.

That breadth is deliberate: `_on_notification` runs on the paho callback thread, and an exception escaping it would take the network loop down with it.

- **Missed notifications** are recovered by the catch-up scan on the next start.
- **A report that already exists** is not re-rendered, but its inventory entry is refreshed, which repairs an `inventory.js` left incomplete by a crash.
- **Broker unreachable at startup**: the connect raises and the process exits, so the supervisor restarts it.

---

## 8. Security notes

`ts`, `input_path` and `output_path` all arrive in an MQTT payload. Anyone who can publish to the broker sets them, and on a shared Home Assistant broker that is a wider set than mimirheim itself. Two checks exist because of it:

- **`ts` is validated, not just transformed.** It becomes a filename, so it must match the character set an ISO 8601 timestamp is made of and must not contain a `..` segment. Before that check existed, a notification carrying `ts = "../escaped/PWNED"` wrote an HTML file outside `output_dir`.
- **Both dump paths must resolve inside `dump_dir`.** Otherwise a crafted notification could have this daemon read any file the process can reach and embed it in a report — which, served through the config editor, hands it to anyone who can reach that port.

Rejections are logged at warning level naming the offending value, so a genuine misconfiguration is diagnosable rather than silent.

Reports contain your consumption, production, prices and tariffs. Treat `output_dir` as private, and do not expose whatever serves it to an untrusted network.
