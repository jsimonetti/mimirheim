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
`/etc/s6-overlay/s6-rc.d/`. All services are launched automatically when the
container starts:

| Service | Config file | Python module |
|---|---|---|
| `mimirheim` | `/config/mimirheim.yaml` | `mimirheim` |
| `scheduler` | `/config/scheduler.yaml` | `scheduler` |
| `nordpool` | `/config/nordpool.yaml` | `nordpool` |
| `pv-fetcher` | `/config/pv-fetcher.yaml` | `pv_fetcher` |
| `pv-ml-learner` | `/config/pv-ml-learner.yaml` | `pv_ml_learner` |
| `baseload-ha` | `/config/baseload-ha.yaml` | `baseload_ha` |
| `baseload-ha-db` | `/config/baseload-ha-db.yaml` | `baseload_ha_db` |
| `baseload-static` | `/config/baseload-static.yaml` | `baseload_static` |
| `reporter` | `/config/reporter.yaml` | `reporter` |

### Missing config behaviour

Every helper service (all services except `mimirheim`) checks whether its config
file exists at start. If the file is absent the service prints a message and
calls `sleep infinity` — it idles without consuming resources or spamming
logs. Restart the container (or the individual service) once the config file
has been bind-mounted.

The `mimirheim` solver behaves differently: it calls `sleep 5` and exits, which
causes s6-overlay to restart it shortly afterwards. This produces a
steady retry loop so that `mimirheim` starts automatically once
`/config/mimirheim.yaml` appears, without any manual intervention.

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

Bind-mount a directory containing your YAML config files. Any config file you
do not supply is simply ignored by its service (see above).

```sh
docker run -d \
  --name mimirheim \
  -v /path/to/your/configs:/config:ro \
  -e TZ=Europe/Amsterdam \
  mimirheim
```

Only `/config/mimirheim.yaml` is required for the solver to operate. Add the other
config files incrementally as you enable more helpers.

### Running services in separate containers

If you prefer to run each service in its own container — for instance to apply
separate resource limits or restart policies — reuse the **same image** for all
containers. This guarantees that every service uses identical versions of every
shared library, which avoids subtle incompatibilities when, for example, the
`mimirheim` MQTT schema changes.

s6-overlay does not expose a way to selectively disable services via `CMD`
because the entrypoint is `/init` and `CMD` is not used. Instead, override the
entrypoint to bypass s6-overlay entirely and invoke a single Python module
directly:

```sh
# mimirheim solver only
docker run -d \
  -v /path/to/configs:/config:ro \
  -e TZ=Europe/Amsterdam \
  --entrypoint /app/.venv/bin/python \
  mimirheim -m mimirheim --config /config/mimirheim.yaml

# Nordpool fetcher only
docker run -d \
  -v /path/to/configs:/config:ro \
  -e TZ=Europe/Amsterdam \
  --entrypoint /app/.venv/bin/python \
  mimirheim -m nordpool --config /config/nordpool.yaml

# Reporter only
docker run -d \
  -v /path/to/configs:/config:ro \
  -v /path/to/dump/dir:/dumps:ro \
  -v /path/to/output/dir:/output \
  -e TZ=Europe/Amsterdam \
  --entrypoint /app/.venv/bin/python \
  mimirheim -m reporter --config /config/reporter.yaml
```

When running this way, s6-overlay is not involved: there is no automatic
service restart. Use your orchestrator's restart policy (`--restart unless-stopped`
in Docker, `restartPolicy` in Kubernetes) to get the same behaviour.

---

## Build

```sh
docker build -t mimirheim -f container/Dockerfile .
```

Multi-platform (requires `docker buildx`):

```sh
docker buildx build --platform linux/amd64,linux/arm64 -t mimirheim -f container/Dockerfile .
```
