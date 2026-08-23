# helper_common — Agent Instructions

This package is the shared infrastructure every other helper builds on. It
ships in the single `mimirheim` wheel and is not a daemon: it has no entry
point and is never run on its own.

It is also the only helper package that other helper packages import. Treat a
change here as a change to all of them.

---

## Source of truth

Before writing any code, read:

- `AGENTS.md` in the repo root — the code standards, testing discipline and
  environment rules that apply to every package here.
- `IMPLEMENTATION_DETAILS.md` in the repo root — mimirheim's architectural
  conventions.
- [wiki/Developer/Helper-API.md](../../wiki/Developer/Helper-API.md) — the MQTT
  contract every helper implements.

---

## Dependencies

There is one `pyproject.toml`, at the repo root, and this package has no extra
of its own. Its dependencies (`paho-mqtt`, `pydantic`, `pyyaml`) are core
`mimirheim` dependencies, because every helper needs them and the wheel is
built once.

Do not add a dependency here that only one helper needs. A dependency in
`helper_common` is installed for everyone, including users who enabled a single
extra; that is what the per-helper extras exist to avoid.

---

## Environment

Run every command from the repo root:

```bash
uv sync --all-extras                        # core plus every helper dependency
uv run pytest                               # the whole suite
uv run pytest mimirheim_helpers/common/tests
uv run ruff check .                         # must be clean before a change is done
```

There is no `python -m helper_common`.

---

## Project structure

```
mimirheim_helpers/common/
  AGENTS.md            # this file
  helper_common/
    __init__.py        # documents what the package provides
    config.py          # MqttConfig, HomeAssistantConfig, the env overrides,
                       #   and load_helper_config
    cycle.py           # CycleResult, the return type of _run_cycle
    daemon.py          # MqttDaemon and HelperDaemon
    discovery.py       # HA MQTT discovery payloads
    publish.py         # publish_checked and PublishError
    topics.py          # canonical topic names, one function per topic
  tests/
    unit/
      test_daemon.py
      test_daemon_lifecycle.py
      test_discovery.py
      test_load_helper_config.py
      test_publish.py
      test_topics.py
```

---

## The two base classes

`MqttDaemon` is the MQTT lifecycle: paho client construction, TLS, credentials,
connect and disconnect logging, signal handling, clean shutdown, and
`_publish_stats`. It is deliberately concrete, not an ABC — every callback has a
working default, so there is nothing a subclass is obliged to implement.

`HelperDaemon(MqttDaemon)` adds the trigger-driven cycle that the data-input
helpers share: subscription to the trigger topic and `homeassistant/status`, the
retain guard, the five-second debounce, rate-limit suppression via
`CycleResult.suppress_until`, per-cycle stats, and HA discovery. Subclasses
implement one method, `_run_cycle`.

Pick `MqttDaemon` when the daemon is event-driven rather than trigger-driven.
The reporter reacts to dump notifications; `PvLearnerDaemon` has two independent
trigger topics. Neither fits `HelperDaemon`'s single-trigger model.

The scheduler subclasses neither. It is the component that *sends* the triggers
the others consume, so a base class built to receive them is the wrong shape.

---

## Things that are the way they are on purpose

`MqttDaemon.__init__` derives its logger name from the package rather than using
`__name__`, because helpers run as `python -m <pkg>` where `__name__` is
`"__main__"`. Every entry point names its logger explicitly for the same
reason; there is a test enforcing it in
`tests/unit/test_helper_entry_point_logging.py`.

`publish_checked` treats `MQTT_ERR_NO_CONN` as a failure at QoS 0 and as a
delay at QoS 1 or above. That is not an inconsistency: paho queues QoS 1 and
redelivers it on reconnect, and drops QoS 0 on the floor. Do not simplify it to
"any non-zero rc is fatal" — the forecast publishes would then fail every time
the broker blipped, for messages that arrived fine.

`random.uniform` in the HA birth-message handler is deliberate jitter, so a
fleet of helpers does not republish discovery in lockstep when Home Assistant
restarts. It is not a security-relevant use of the module.

`apply_mqtt_env_overrides` treats a null `mqtt:` section as an absent one, so a
config that leaves every broker setting to the Supervisor is valid.

---

## Code standards

The root `AGENTS.md` governs. The points that come up most here:

- Test-driven development. The test exists and fails before the implementation.
- Complete type annotations on every public function and method.
- `model_config = ConfigDict(extra="forbid")` on every Pydantic model.
- Google-style docstrings on all public classes and functions, and a
  module-level docstring on every module.
- Never a bare `except:` or `except Exception:` without logging the full
  traceback.
- No emoticons in code, comments or documentation.
- No imports from `mimirheim/` and none from any specific helper package. This
  package is imported by them, never the other way round.
