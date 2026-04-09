# scheduler — MQTT trigger scheduler

**scheduler** is a standalone daemon that publishes MQTT trigger messages on a cron schedule. It is the central clock for the mimirheim input pipeline: it tells each input tool when to fetch fresh data and tells mimirheim when to run a new solve cycle.

---

## Contents

1. [Purpose](#1-purpose)
2. [Design philosophy](#2-design-philosophy)
3. [Configuration](#3-configuration)
4. [Running](#4-running)
5. [Fault tolerance](#5-fault-tolerance)
6. [Example: full pipeline schedule](#6-example-full-pipeline-schedule)

---

## 1. Purpose

Each mimirheim input tool (nordpool, forecast.solar, homeassistant baseload) subscribes to a trigger topic and acts on demand. The scheduler is the component that decides *when* to demand. It has a simple job: parse cron expressions and publish an empty MQTT message to a configured topic each time a schedule fires.

The scheduler does not know what each tool does. It only knows topics and times. Wiring a schedule to a tool is done by pointing the schedule's `trigger_topic` at the tool's `trigger_topic`. The tools and mimirheim remain fully decoupled from timing.

---

## 2. Design philosophy

### Why MQTT triggers instead of direct subprocess calls

Tools run as independent daemon processes, potentially on different hosts. The scheduler connects them the same way mimirheim connects to everything else: MQTT. A tool that is temporarily down when a trigger fires will miss that trigger (trigger topics are not retained), but the tools are designed to be triggered frequently enough that a missed cycle is not a problem.

### Why not a single combined process

Separating the scheduler from the tools means:

- Each tool can be restarted, updated, or replaced without affecting the scheduler.
- The scheduler can be replaced (e.g. by Home Assistant automations or an external cron daemon) without touching the tools.
- Multiple subscribers can listen on the same trigger topic: if you want to do something additional when prices are refreshed, subscribe to the trigger topic in a Node-RED flow.

---

## 3. Configuration

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: mimir-scheduler
  # username: user
  # password: secret

schedules:
  - "0 14 * * *":      mimir/input/tools/prices/trigger   # day-ahead prices published ~14:00 UTC
  - "5 0 * * *":       mimir/input/tools/prices/trigger   # midnight horizon rollover
  - "0 3,6,9,12,15,18 * * *": mimir/input/tools/pv/trigger  # PV forecast six times per day
  - "0 0 * * *":       mimir/input/tools/baseload/trigger  # base load once daily at midnight
  - "*/15 * * * *":    mimir/input/trigger                 # mimirheim solve every 15 minutes
```

`schedules` is a list of single-key dicts. Each dict has exactly one entry: the cron expression as the key and the MQTT trigger topic as the value. This format has no entry names — comments serve as labels when needed.

| Element | Type | Description |
|---------|------|-------------|
| key | string | Standard five-field cron expression: `minute hour day_of_month month day_of_week`. All fields support `*`, `/`, `,`, and `-` syntax |
| value | string | MQTT topic to publish to when the schedule fires. Publishes an empty payload, not retained, QoS 0 |

Multiple entries may use the same topic with different cron expressions.

### Cron expression reference

| Expression | Meaning |
|------------|---------|
| `*/15 * * * *` | Every 15 minutes |
| `0 14 * * *` | 14:00 UTC every day |
| `0 14 * * 1-5` | 14:00 UTC Monday through Friday |
| `5 0,12 * * *` | 00:05 and 12:05 UTC every day |
| `0 6,9,12,15,18 * * *` | 06:00, 09:00, 12:00, 15:00, 18:00 UTC every day |

The scheduler interprets all times as **UTC**. It does not apply timezone offsets. Configure cron expressions accordingly.

---

## 4. Running

```bash
uv run python -m mimirheim_helpers.scheduler --config mimirheim_helpers/scheduler/config.yaml
```

### Systemd unit example

```ini
[Unit]
Description=mimirheim input scheduler
After=network.target mosquitto.service

[Service]
WorkingDirectory=/opt/mimirheim
ExecStart=/opt/mimirheim/.venv/bin/python -m mimirheim_helpers.scheduler --config /etc/mimirheim/scheduler.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Start the scheduler after all input tool daemons and mimirheim are running. The exact startup order does not matter — MQTT delivery is best-effort and a missed trigger on startup is expected.

---

## 5. Fault tolerance

- **MQTT disconnect**: The scheduler reconnects automatically using paho-mqtt's built-in reconnect. Scheduled triggers that fall during a disconnect are not replayed after reconnection — the next scheduled fire proceeds as normal.
- **Cron parse errors**: Invalid cron expressions in config are caught at startup and raise a `ValidationError` before the process enters its main loop. The process exits with a non-zero code and a clear error message identifying the invalid entry.
- **Clock drift**: The scheduler uses the system clock. If the system clock jumps (e.g. NTP correction), the next scheduled trigger fires at the correct next occurrence of the cron expression rather than at a fixed interval from the last fire.
- **Process crash**: On restart, the scheduler does not attempt to fire triggers that were missed during downtime. It schedules the next future occurrence of each cron expression from the current wall-clock time.

---

## 6. Example: full pipeline schedule

A complete example for a home with rooftop PV, a battery, and Nordpool day-ahead pricing:

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: mimir-scheduler

schedules:
  # Prices: fetch after Nordpool publishes day-ahead (~12:42 CET / 11:42 UTC).
  # Also refresh at midnight so the horizon covers the new day.
  - "0 14 * * *":  mimir/input/tools/prices/trigger
  - "5 0 * * *":   mimir/input/tools/prices/trigger

  # PV forecast: refresh every 3 hours. A pre-dawn update seeds the morning.
  - "0 3,6,9,12,15,18 * * *": mimir/input/tools/pv/trigger

  # Base load: once daily at midnight. Historical averages change slowly.
  - "0 0 * * *":   mimir/input/tools/baseload/trigger

  # mimirheim solve: every 15 minutes. mimirheim only solves when all inputs are ready.
  - "*/15 * * * *": mimir/input/trigger
```

### Typical daily event sequence

| UTC time | Event | Scheduler fires |
|----------|-------|-----------------|
| 00:00 | Midnight | baseload trigger, prices trigger |
| 00:05 | Prices refresh | prices midnight trigger |
| 03:00 | Pre-dawn PV update | pv trigger |
| 06:00–18:00 | Daylight PV updates | pv trigger (every 3 h) |
| 14:00 | Day-ahead prices published | prices trigger |
| Every :00, :15, :30, :45 | Regular solve cycle | mimirheim trigger |
