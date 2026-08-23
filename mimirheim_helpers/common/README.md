# helper_common — Shared infrastructure for the helper daemons

**helper_common** is the library every other helper is built on. It is not a daemon: it has no entry point, no config file of its own, and nothing to run.

It exists so that "connect to the broker, validate the config, react to a trigger, publish the result" is written once. When a helper's `AGENTS.md` says `helper_common` is a deliberate shared dependency rather than a layering violation, this is what it means.

This README is for someone changing a helper or writing a new one. The user-facing half — the config sections every helper inherits from here — is documented in [wiki/Helpers/Common.md](../../wiki/Helpers/Common.md).

---

## Contents

1. [What is in here](#1-what-is-in-here)
2. [The two base classes](#2-the-two-base-classes)
3. [Writing a new helper](#3-writing-a-new-helper)
4. [Configuration](#4-configuration)
5. [Publishing](#5-publishing)
6. [Topics](#6-topics)
7. [Testing](#7-testing)

---

## 1. What is in here

| Module | Provides |
| --- | --- |
| `config.py` | `MqttConfig`, `HomeAssistantConfig`, `mqtt_env_overrides`, `apply_mqtt_env_overrides`, `load_helper_config` |
| `daemon.py` | `MqttDaemon`, `HelperDaemon` |
| `cycle.py` | `CycleResult`, the return type of `_run_cycle` |
| `publish.py` | `publish_checked`, `PublishError` |
| `discovery.py` | Home Assistant MQTT discovery payloads |
| `topics.py` | One function per canonical mimirheim topic |

It imports nothing from `mimirheim/` and nothing from any specific helper. The dependency runs one way only.

---

## 2. The two base classes

### `MqttDaemon`

The MQTT lifecycle: paho client construction, TLS, credentials, connect and disconnect logging, signal handling, clean shutdown, and `_publish_stats`.

Deliberately concrete rather than abstract. Every callback has a working default, so there is nothing a subclass is obliged to implement, and declaring it `abc.ABC` with no abstract methods would enforce nothing while implying otherwise.

Override `_on_connect`, `_on_disconnect` or `_on_message` as needed. Call `super()._on_connect(...)` first, so a refused connection is reported before you act on it.

### `HelperDaemon(MqttDaemon)`

Adds the trigger-driven cycle the data-input helpers share:

- Subscription to `trigger_topic` and `homeassistant/status`.
- A retain guard. The broker replays retained messages on every subscribe, and a replayed trigger is a past request, not a new one.
- A five-second debounce.
- Rate-limit suppression: return a `CycleResult` with `suppress_until` set and every trigger is dropped until that UTC time passes.
- Per-cycle stats to `stats_topic`, when configured.
- HA discovery on connect and on the HA birth message.

Subclasses implement one method, `_run_cycle(client)`, and set `TOOL_NAME`.

### Choosing between them

Pick `HelperDaemon` if the daemon is "one trigger topic in, one forecast out". That is nordpool, zonneplan, pv_fetcher and all three baseload variants.

Pick `MqttDaemon` when that shape does not fit. The reporter is event-driven and has no trigger; `PvLearnerDaemon` has two independent trigger topics, one to train and one to infer.

The scheduler subclasses neither, and that is not an oversight. It is the component that *sends* the triggers the others consume, so a base class built to receive them is the wrong shape.

---

## 3. Writing a new helper

```python
class MyDaemon(HelperDaemon):
    TOOL_NAME = "my_tool"          # stable; changing it duplicates the HA entity

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        steps = fetch_something()
        publish_my_forecast(client, self._config.output_topic, steps)
        return CycleResult(horizon_hours=len(steps))
```

Then an entry point:

```python
logger = logging.getLogger("my_tool")   # not __name__ -- see below

def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="...")
    MyDaemon(load_helper_config(args.config, MyConfig, logger)).run()
```

Four things that are easy to get wrong:

**Name the logger explicitly.** Helpers run as `python -m <pkg>`, where `__name__` is `"__main__"`. A module-scope `logging.getLogger(__name__)` in a `__main__.py` therefore logs under `__main__` while the base class logs under the package name, so the same daemon appears under two names and filtering by helper misses half its output. There is a test enforcing this in `tests/unit/test_helper_entry_point_logging.py`.

**Do not catch broadly in `_run_cycle`.** The base class already catches, logs the full traceback, and records `CycleResult(success=False)`. Swallowing an exception yourself turns a failed cycle into a silently successful one. Let specific exceptions propagate.

**Quiet chatty third-party loggers.** `httpx` logs a line per request at INFO, which for `pv_ml_learner` would also put the Meteoserver API key in the log because it travels in the URL.

**Register any new extra in two places.** The tool's own entry under `[project.optional-dependencies]`, and the `helpers` meta-extra that the container build installs.

---

## 4. Configuration

`MqttConfig` and `HomeAssistantConfig` are the shared models. Every helper composes them rather than defining broker fields itself, which is why TLS and Supervisor credentials behave identically everywhere.

`load_helper_config(path, model_cls, logger)` is the whole startup sequence: read the YAML, apply the environment overrides, validate, and on any failure log the traceback and exit 1. Five helpers used to carry a byte-identical private copy of it.

Failure is terminal on purpose. A daemon cannot do useful work with a configuration it could not read, and exiting lets the supervisor restart it once the file is fixed.

`mqtt_env_overrides()` maps the five Supervisor variables onto `MqttConfig` fields:

| Environment variable | Field |
| --- | --- |
| `MQTT_HOST` | `host` |
| `MQTT_PORT` | `port` |
| `MQTT_USERNAME` | `username` |
| `MQTT_PASSWORD` | `password` |
| `MQTT_SSL` | `tls` (the string `"true"`) |

The environment wins over the file, so an add-on install needs no broker credentials in YAML. A bare `mqtt:` key with nothing under it is treated as absent, so a config that leaves every broker setting to the Supervisor is valid.

This mapping is defined once. The config editor needs the same values to show which fields the Supervisor controls, and calls this rather than keeping its own copy — it used to, and the two had already drifted over what an unparseable `MQTT_PORT` should do.

---

## 5. Publishing

Use `publish_checked`, not `client.publish`. paho reports failures in the return code of the `MQTTMessageInfo` it hands back; it does not raise. Ignoring that return code is how every helper came to log a successful publish for a message that never left the process.

```python
publish_checked(
    client, topic, payload,
    qos=1, retain=True,
    description="price forecast",
)
```

The QoS matters, and `publish_checked` distinguishes the two cases rather than treating every non-zero rc as fatal:

- **QoS 1 and above**: paho stores the message and redelivers it on reconnect, so `MQTT_ERR_NO_CONN` is a delay. It is logged and execution continues.
- **QoS 0**: nothing is stored. The message is gone, so it raises `PublishError`.

Do not simplify that to "any non-zero rc is fatal". Every forecast publish would then fail whenever the broker blipped, for messages that arrive perfectly well a moment later.

`PublishError` propagating out of `_run_cycle` is the intended path: `HelperDaemon` catches it, logs the traceback and records a failed cycle, without taking the daemon down.

---

## 6. Topics

`topics.py` has one function per canonical mimirheim topic, each taking the configured prefix:

```python
topics.prices_topic("mimir")                      # mimir/input/prices
topics.baseload_forecast_topic("mimir", "base_load")
topics.dump_available_topic("mimir")
```

Derive defaults through these rather than formatting strings in a config model. A helper that hardcodes `"mimir/input/..."` breaks for anyone who changed `mqtt.topic_prefix`, and it breaks quietly — the publish succeeds, onto a topic nothing is listening to.

---

## 7. Testing

Run everything from the repo root:

```bash
uv sync --all-extras
uv run pytest mimirheim_helpers/common/tests
uv run ruff check .
```

A change here affects every helper, so run the full suite before calling it done, not just this package's.

Two things worth knowing when writing tests against this code:

- A bare `MagicMock()` as an MQTT client returns a mock from `publish()`, so `info.rc` is a truthy mock and no return-code check can ever fail. Set `client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS`.
- Patching `threading.Event` with a factory that itself calls `threading.Event()` recurses. Bind the real class at module scope first.
