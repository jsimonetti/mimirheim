# mimirheim container

All-in-one Docker image that runs the mimirheim solver and every input helper under
[s6-overlay](https://github.com/just-containers/s6-overlay).

---

## Image structure

### Runtime process manager: s6-overlay

The image uses s6-overlay as its init system. s6-overlay starts all services
in parallel under a single container process tree and restarts any service that
exits unexpectedly. The entrypoint is `/init` (the s6-overlay init binary);
there is no meaningful `CMD`.

### Services

Each service is a separate s6 `longrun` service definition under
`/etc/s6-overlay/s6-rc.d/`. Every service is started by s6, but a service
only execs its Python module if its `ENABLE_<SERVICE>` environment variable
is set to the exact string `"true"`; otherwise it logs a message and stays
down (see [Enabling services](#enabling-services-enable_service) below).

| Service | Enable variable | Config file | Python module |
|---|---|---|---|
| `mimirheim` | *(always runs)* | `/config/mimirheim.yaml` | `mimirheim` |
| `config-editor` | `ENABLE_CONFIG_EDITOR` | `/config/config-editor.yaml` | `config_editor` |
| `scheduler` | `ENABLE_SCHEDULER` | `/config/scheduler.yaml` | `scheduler` |
| `nordpool` | `ENABLE_NORDPOOL` | `/config/nordpool.yaml` | `nordpool` |
| `zonneplan` | `ENABLE_ZONNEPLAN` | `/config/zonneplan.yaml` | `zonneplan_prices` |
| `epexpredictor` | `ENABLE_EPEXPREDICTOR` | `/config/epexpredictor.yaml` | `epexpredictor_prices` |
| `pv-fetcher` | `ENABLE_PV_FETCHER` | `/config/pv-fetcher.yaml` | `pv_fetcher` |
| `pv-openmeteo` | `ENABLE_PV_OPENMETEO` | `/config/pv-openmeteo.yaml` | `pv_openmeteo` |
| `pv-ml-learner` | `ENABLE_PV_ML_LEARNER` | `/config/pv-ml-learner.yaml` | `pv_ml_learner` |
| `baseload-ha` | `ENABLE_BASELOAD_HA` | `/config/baseload-ha.yaml` | `baseload_ha` |
| `baseload-ha-db` | `ENABLE_BASELOAD_HA_DB` | `/config/baseload-ha-db.yaml` | `baseload_ha_db` |
| `baseload-static` | `ENABLE_BASELOAD_STATIC` | `/config/baseload-static.yaml` | `baseload_static` |
| `reporter` | `ENABLE_REPORTER` | `/config/reporter.yaml` | `reporter` |

### Enabling services (`ENABLE_<SERVICE>`)

A service's `ENABLE_<SERVICE>` variable must equal the literal string
`"true"` for it to start at all; unset, `"false"`, or any other value
leaves it down. There is no default-enabled fallback for any of the 12
gated services — `mimirheim` itself is the only exception, and always
runs. See [wiki: Environment Variables](https://github.com/jsimonetti/mimirheim/wiki/Environment-Variables)
for the full reference, including the equivalent `MQTT_*` variables that
supply broker credentials the same way.

> **Upgrading from an older version?** See
> [Upgrading: `ENABLE_<SERVICE>` is now required](#upgrading-enable_service-is-now-required)
> below — this is a breaking change for existing plain-Docker/Compose
> deployments.

### Missing or invalid config behaviour

A service that is enabled but whose config file is missing, unreadable, or
fails validation does not idle or crash-loop: it still execs Python,
connects to MQTT using whatever Broker Settings it can piece together from
its file and the `MQTT_*` environment variables, and enters **Awaiting
Configuration** — it serves only the Config Service protocol (Descriptor,
Operational State, `get_current_values`, `validate_and_write`,
`restart_request`) and runs none of its own function. Use the config
editor (`ENABLE_CONFIG_EDITOR=true`) to write a corrected file, then
restart the service so it picks the change up — no Config Owner reloads
its configuration while running. See IMPLEMENTATION_DETAILS.md.

The one broker-settings variable every service needs regardless — from its
own YAML or from `MQTT_HOST`/`MQTT_PORT`/etc. — is `mqtt.host`: without a
usable broker to connect to, a service cannot serve the Config Service
protocol either, and exits instead.

### Shared venv

All packages share a single Python virtual environment at `/app/.venv`. This
avoids duplicating shared dependencies (pydantic, paho-mqtt, pyyaml) and
guarantees that every service uses exactly the same version of every library.

### Example configs

Annotated example configuration files for every service are baked into the
image at `/app/examples/`. Copy them to your config directory as a starting
point:

```sh
docker run --rm mimirheim ls /app/examples/
docker run --rm -v /path/to/configs:/out mimirheim \
    sh -c "cp /app/examples/* /out/"
```

---

## Running the image

### All-in-one (recommended)

Bind-mount a directory containing your YAML config files, and set an
`ENABLE_<SERVICE>` variable for each service you want to run (see
[Enabling services](#enabling-services-enable_service) above). A service
you do not enable stays down regardless of whether its config file exists;
a service you enable but do not supply a config file for comes up in
Awaiting Configuration instead of running.

```sh
docker run -d \
  --name mimirheim \
  -v /path/to/your/configs:/config \
  -e TZ=Europe/Amsterdam \
  -e MQTT_HOST=192.0.2.10 \
  -e MQTT_PORT=1883 \
  -e MQTT_USERNAME=mimir \
  -e MQTT_PASSWORD=changeme \
  -e ENABLE_CONFIG_EDITOR=true \
  -e ENABLE_NORDPOOL=true \
  -e ENABLE_PV_FETCHER=true \
  -e ENABLE_BASELOAD_STATIC=true \
  -e ENABLE_SCHEDULER=true \
  -p 8099:8099 \
  mimirheim
```

`mimirheim` itself always runs and needs no `ENABLE_*` variable. The
`MQTT_*` variables above are optional if every config file already has its
own `mqtt:` section with matching credentials; they let you skip writing
broker credentials into every file individually, and are required if a
service's config file is missing entirely (see
[Missing or invalid config behaviour](#missing-or-invalid-config-behaviour)).
Add or drop `ENABLE_*` variables to change which helpers run; see the wiki
page linked above for the complete list.

`/config` is mounted read-write, not `:ro`. Two services in the all-in-one image
write there: the config editor rewrites the YAML files in place, and zonneplan
persists its refreshed OAuth token to `/config/zonneplan_token.json` by default.
Both write atomically — create a temporary file in the directory, then rename it
over the target — so write permission is needed on the directory itself, not
only on the files. Mount `:ro` and the config editor returns an error on save
and zonneplan cannot persist a refreshed token.

The single-service examples below keep `:ro`, because none of the services shown
there writes to `/config`.

### Running services in separate containers

If you prefer to run each service in its own container — for instance to apply
separate resource limits or restart policies — reuse the **same image** for all
containers. This guarantees that every service uses identical versions of every
shared library, which avoids subtle incompatibilities when, for example, the
`mimirheim` MQTT schema changes.

s6-overlay does not expose a way to selectively disable services via `CMD`
because the entrypoint is `/init` and `CMD` is not used. Instead, override the
entrypoint to bypass s6-overlay entirely and invoke a single Python module
directly. `ENABLE_<SERVICE>` variables have no effect in this mode — the gate
lives in each service's s6 `run` script, which is never invoked here, so the
module always execs. `MQTT_*` variables still work identically, since the
module reads them itself:

```sh
# mimirheim solver only
docker run -d \
  -v /path/to/configs:/config:ro \
  -e TZ=Europe/Amsterdam \
  -e MQTT_HOST=192.0.2.10 \
  -e MQTT_PORT=1883 \
  --entrypoint /app/.venv/bin/python \
  mimirheim -m mimirheim --config /config/mimirheim.yaml

# Nordpool fetcher only
docker run -d \
  -v /path/to/configs:/config:ro \
  -e TZ=Europe/Amsterdam \
  -e MQTT_HOST=192.0.2.10 \
  -e MQTT_PORT=1883 \
  --entrypoint /app/.venv/bin/python \
  mimirheim -m nordpool --config /config/nordpool.yaml

# Reporter only
docker run -d \
  -v /path/to/configs:/config:ro \
  -v /path/to/dump/dir:/dumps:ro \
  -v /path/to/output/dir:/output \
  -e TZ=Europe/Amsterdam \
  -e MQTT_HOST=192.0.2.10 \
  -e MQTT_PORT=1883 \
  --entrypoint /app/.venv/bin/python \
  mimirheim -m reporter --config /config/reporter.yaml
```

When running this way, s6-overlay is not involved: there is no automatic
service restart. Use your orchestrator's restart policy (`--restart unless-stopped`
in Docker, `restartPolicy` in Kubernetes) to get the same behaviour.

---

## Upgrading: `ENABLE_<SERVICE>` is now required

**This is a breaking change for plain-Docker and Compose deployments.**
Older versions of this image started a helper service whenever its config
file existed, regardless of any `ENABLE_<SERVICE>` variable. That
file-presence gate has been removed entirely: every service now starts only
when its own `ENABLE_<SERVICE>` variable is set to the literal string
`"true"` (`mimirheim` itself is unaffected — it was never gated this way).

If you are upgrading and relied on a config file's mere presence to enable
a helper, set the corresponding variable(s) below for every helper you
already have a config file for, or that helper silently stops running:

```
ENABLE_BASELOAD_HA=true
ENABLE_BASELOAD_HA_DB=true
ENABLE_BASELOAD_STATIC=true
ENABLE_CONFIG_EDITOR=true
ENABLE_EPEXPREDICTOR=true
ENABLE_NORDPOOL=true
ENABLE_PV_FETCHER=true
ENABLE_PV_ML_LEARNER=true
ENABLE_PV_OPENMETEO=true
ENABLE_REPORTER=true
ENABLE_SCHEDULER=true
ENABLE_ZONNEPLAN=true
```

Set only the ones matching services you actually have a config file for
today; there is no harm in a service being enabled with no config file
(it comes up in Awaiting Configuration instead), but there is no need to
set variables for services you were not already running. Home Assistant
add-on installs are unaffected: the Supervisor already sets every
`ENABLE_<SERVICE>` variable explicitly on every start, independent of this
change.

---

## Build

```sh
docker build -t mimirheim -f container/Dockerfile .
```

Multi-platform (requires `docker buildx`):

```sh
docker buildx build --platform linux/amd64,linux/arm64 -t mimirheim -f container/Dockerfile .
```
