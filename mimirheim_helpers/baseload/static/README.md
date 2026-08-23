# baseload_static — Base load forecast from a fixed profile

**baseload_static** is a daemon that publishes a predefined kilowatt load profile to the mimirheim base load input topic. It contacts nothing: the entire forecast is written out in the config file.

Use it when you have no usable consumption history, when the history you have is dominated by loads mimirheim already schedules, or as a known-good baseline while diagnosing one of the other two base load helpers.

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [Output format](#4-output-format)
5. [Running](#5-running)
6. [Fault tolerance](#6-fault-tolerance)
7. [Scheduling](#7-scheduling)
8. [Choosing between the three base load helpers](#8-choosing-between-the-three-base-load-helpers)

---

## 1. Purpose

mimirheim needs a base load forecast — the household consumption it cannot control — on `{prefix}/input/baseload/{name}/forecast` before it can decide what to do with the loads it can control. This tool fills that input by:

1. Waiting for a message on its trigger topic.
2. Building an hourly forecast from the configured profile, starting at the current UTC hour.
3. Publishing it retained to the output topic.
4. Optionally publishing to mimirheim's trigger topic so a new solve runs immediately.

There is no external system in that list, which is the point. Nothing can be unreachable, rate-limited or slow.

---

## 2. How it works

### Trigger model

The daemon subscribes to one MQTT trigger topic and does not poll on a timer. The message content is ignored; its arrival fires one cycle. A separate scheduler (see `mimirheim_helpers/scheduler/`) publishes to that topic on whatever schedule suits you.

Because the output is retained, mimirheim always has the last published forecast available on reconnect, so the schedule only has to be frequent enough to keep the horizon rolling forward.

### Profile selection

Each step picks its value from the most specific profile available:

1. If `weekly_profiles_kw` has an entry for that step's UTC weekday, that profile is used.
2. Otherwise `profile_kw` is used.

Within the chosen profile the value is selected by wall-clock UTC hour, modulo the profile length:

```
kw = profile[ts.hour % len(profile)]
```

The modulo is what makes a profile independent of when the tool runs. With 24 values, `profile[0]` is always served at midnight UTC and `profile[12]` always at noon, whether the cycle fires at 03:00 or 21:00. A 1-value profile gives a flat constant load. A 168-value profile gives a full week, though `weekly_profiles_kw` is usually the clearer way to express that.

Any length from 1 to 168 is accepted, but lengths that do not divide evenly into 24 are rarely what you want: a 3-value profile repeats eight times a day, and which value lands at midnight depends on nothing you can see in the config.

### Horizon

`horizon_hours` steps are produced, starting at the current UTC hour with minutes and seconds discarded. The profile is tiled to fill the window, so 24 values cover a 48-hour horizon by repeating twice.

All timestamps are UTC. There is no local-time handling anywhere in this tool — a profile written for local habits needs its own offset applied by you, once, when you write it.

---

## 3. Configuration

`mimirheim_helpers/examples/baseload-static.yaml` is the annotated reference configuration, and it validates as shipped.

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: baseload-static-daemon

trigger_topic: mimir/input/tools/baseload/trigger

baseload:
  horizon_hours: 48
  profile_kw:
    - 0.30  # 00:00 UTC
    - 0.28  # 01:00 UTC
    # ... 24 values in total
    - 0.38  # 23:00 UTC

signal_mimir: false
```

### Field reference

| Key | Type | Description |
| --- | --- | --- |
| `mqtt.host` | string | Broker hostname or IP address |
| `mqtt.port` | integer 1–65535 | Broker port. Default `1883`; use `8883` with TLS |
| `mqtt.client_id` | string | Client identifier, unique on the broker. Defaults to `mimir-baseload` |
| `mqtt.username` | string, optional | Broker username. Omit for anonymous access |
| `mqtt.password` | string, optional | Broker password |
| `mqtt.tls` | boolean | Enable TLS. Default `false`. The port is not what decides this |
| `mqtt.tls_allow_insecure` | boolean | Skip certificate verification when `tls` is true. No effect when it is false |
| `trigger_topic` | string | A message here fires one publish cycle. Required |
| `mimir_topic_prefix` | string | mimirheim's `mqtt.topic_prefix`. Default `mimir`. Used to derive the two topics below |
| `mimir_static_load_name` | string | mimirheim's `static_loads` device name. Default `base_load` |
| `output_topic` | string, optional | Where the forecast is published, retained. Defaults to `{mimir_topic_prefix}/input/baseload/{mimir_static_load_name}/forecast` |
| `baseload.profile_kw` | list of 1–168 floats | The repeating profile, in kW. Required unless `weekly_profiles_kw` covers all seven weekdays |
| `baseload.weekly_profiles_kw` | map of 0–6 to list | Per-weekday profiles. Keys are Python weekday numbers, `0`=Monday … `6`=Sunday |
| `baseload.horizon_hours` | integer 1–168 | Hourly steps to publish. Default `48` |
| `signal_mimir` | boolean | Publish to `mimir_trigger_topic` after publishing. Default `false` |
| `mimir_trigger_topic` | string, optional | mimirheim's trigger topic. Defaults to `{mimir_topic_prefix}/input/trigger` |
| `ha_discovery` | section, optional | Home Assistant MQTT discovery. See [wiki/Helpers/Common.md](../../../wiki/Helpers/Common.md) |
| `stats_topic` | string, optional | Retained per-cycle JSON statistics |

Broker settings can also come from the environment — `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_SSL` — which the Home Assistant Supervisor injects. Those take precedence over the file, so an add-on install does not need broker credentials written into YAML. A bare `mqtt:` key with nothing under it is valid when the environment supplies everything.

### Validation rules worth knowing

- At least one of `profile_kw` and `weekly_profiles_kw` must be set.
- `weekly_profiles_kw` keys must be `0`–`6`. Anything else is rejected by name.
- Each `weekly_profiles_kw` entry must itself have 1–168 values.
- If `profile_kw` is omitted, `weekly_profiles_kw` must cover all seven weekdays. The error names the missing ones.
- Unknown keys are rejected. There is no silent typo.

---

## 4. Output format

A JSON array published retained to `output_topic`, one object per hour:

```json
[
  {
    "ts": "2026-08-24T13:00:00+00:00",
    "kw": 0.28
  },
  {
    "ts": "2026-08-24T14:00:00+00:00",
    "kw": 0.27
  }
]
```

- `ts` is UTC ISO 8601 with a `+00:00` offset, marking the start of the hour.
- `kw` is the forecast power in kilowatts, rounded to four decimal places.
- There is no `confidence` field. mimirheim treats an absent confidence as `1.0`, which is the honest reading here: a static profile is exactly as certain as you decided it was.

The steps above come from a three-value profile `[0.30, 0.28, 0.27]` starting at 13:00, which is why the first value served is `0.28` — `13 % 3 == 1`.

---

## 5. Running

```bash
uv run python -m baseload_static --config mimirheim_helpers/examples/baseload-static.yaml
```

The module path is `baseload_static`. The package is published at the top level of the `mimirheim` wheel, so there is no dotted path under `mimirheim_helpers`.

This helper needs no extra: it uses only `paho-mqtt`, `pydantic` and `pyyaml`, which a core `mimirheim` install already has.

### Systemd unit example

```ini
[Unit]
Description=mimirheim static base load publisher
After=network.target mosquitto.service

[Service]
WorkingDirectory=/opt/mimirheim
ExecStart=/opt/mimirheim/.venv/bin/python -m baseload_static --config /etc/mimirheim/baseload_static.yaml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## 6. Fault tolerance

- **Broker unreachable at startup**: the connect raises and the process exits, so the supervisor restarts it. There is nothing useful a daemon can do without a broker.
- **Broker drops mid-cycle**: paho reconnects on its own. The retained forecast is queued at QoS 1 and delivered on reconnect. The QoS 0 solve trigger is not queued, so a failed one is reported rather than logged as a success.
- **Bad config**: validation happens once at startup, and the process exits 1 with the offending field named. It does not start with half a profile.
- **Retained trigger messages are ignored.** The broker replays them on every subscribe, and a replayed trigger is a past request, not a new one.
- **Triggers within five seconds of the last one are debounced**, so a stuck automation cannot spin the daemon.

There is no fetch to fail, which removes most of the failure modes the other two base load helpers have.

---

## 7. Scheduling

Publish to `trigger_topic` from the mimirheim scheduler, a Home Assistant automation, or cron. Once or twice a day is plenty: the profile does not change, and the only thing a cycle accomplishes is rolling the horizon forward and refreshing the retained payload.

Trigger it again after editing the profile, or the retained payload keeps serving the old one until the next scheduled cycle.

---

## 8. Choosing between the three base load helpers

| | Reads | Use when |
| --- | --- | --- |
| `baseload_static` | nothing | You have no history, or you want a predictable baseline |
| `baseload_ha` | the HA recorder REST API | HA is reachable over HTTP and you want it to learn from actuals |
| `baseload_ha_db` | the HA recorder database directly | You need longer history than the API will serve, or outlier filtering |

All three publish to the same topic, so **only one may be enabled**. The config editor enforces this: enabling one deletes the other two config files.

Running two by hand is not caught by anything. They will overwrite each other's retained payload, and which forecast mimirheim sees depends on which fired last.
