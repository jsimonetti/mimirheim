# Home Assistant Add-on Repository — Conversion Plan

## Overview

This plan describes how to convert the existing mimirheim container into a
Home Assistant add-on (app) distributed via a dedicated Supervisor repository.
It covers the full path from the current s6-overlay container to a published,
dual-channel (stable/beta), multi-architecture (amd64/aarch64) add-on with
automated CI/CD via GitHub Actions.

---

## Reference material

- [HA add-on configuration reference](https://developers.home-assistant.io/docs/add-ons/configuration)
- [HA app publishing guide](https://developers.home-assistant.io/docs/apps/publishing)
- [HA apps-example repository](https://github.com/home-assistant/apps-example)
- [HA builder actions](https://github.com/home-assistant/builder)

---

## Target repository layout

### Source layout (inside `hioo`)

All add-on source files live inside the `hioo` monorepo under `hassio-repository/`.
This keeps the Dockerfile, run scripts, and add-on configs version-locked to the
Python package source they package.

The HA Supervisor requires `repository.yaml` at the **root** of the GitHub
repository URL that users add. Because `hioo` is a Python project repo, adding
`repository.yaml` to its root would be incorrect. Instead, a CI job in `hioo`
deploys the `hassio-repository/` subdirectory to a separate, dedicated GitHub
repository (`simonetti/hassio-addons`) on every push to `main` and on every
version tag. That dedicated repo is what users add to HA.

This pattern is commonly called a *subtree push deployment*: one source repo,
one user-facing repo, no manual duplication.

```
hioo/ (source repo)
  hassio-repository/                  <-- becomes root of simonetti/hassio-addons
    repository.yaml
    README.md
    .github/
      workflows/
        builder.yaml          # change-detection dispatcher
        build-app.yaml        # reusable per-app build using HA builder actions
    mimirheim/                # stable add-on
      config.yaml
      Dockerfile
      translations/
        en.yaml
      CHANGELOG.md
      DOCS.md
      icon.png
      logo.png
    mimirheim-beta/           # testing / edge add-on
      config.yaml
      Dockerfile
      translations/
        en.yaml
      CHANGELOG.md
      DOCS.md
      icon.png
      logo.png
  container/
    Dockerfile                <-- existing container (standalone Docker)
    etc/
      s6-overlay/ ...
      cont-init.d/ ...
```

### Published layout (`simonetti/hassio-addons` — user-facing)

```
simonetti/hassio-addons  (deployed by CI from hassio-repository/)
  repository.yaml
  README.md
  .github/workflows/
  mimirheim/
  mimirheim-beta/
```

The add-on directories (`mimirheim/`, `mimirheim-beta/`) are self-contained.
Each is an independent HA app. They both build from the same adapted Dockerfile
but use different image tags and `stage` values.

---

## Step 1 — Create the GitHub repositories

1. Create the user-facing HA repository: `github.com/simonetti/hassio-addons`.
   This repository is managed entirely by CI — do not commit to it manually.
   Users add its URL to HA Supervisor: `https://github.com/simonetti/hassio-addons`.

2. All source editing happens in `hioo`. The `hassio-repository/` directory is
   the source of truth. A CI job (see Step 8) pushes its contents to the root
   of `simonetti/hassio-addons` on every build.

3. Grant the `hioo` repository's Actions runner write access to
   `simonetti/hassio-addons`. Create a GitHub personal access token (or a
   deploy key) with `contents: write` scope on `hassio-addons` and add it as a
   secret named `HASSIO_ADDONS_DEPLOY_KEY` in the `hioo` repository settings.

**`hassio-repository/repository.yaml`**:

```yaml
name: Mimirheim Add-on Repository
url: https://github.com/simonetti/hassio-addons
maintainer: Simonetti <info@simonetti.nl>
```

---

## Step 2 — Add-on config.yaml

Each add-on directory must contain a `config.yaml`. The stable and beta
variants are identical except where noted.

### `mimirheim/config.yaml` (stable)

```yaml
name: Mimirheim
version: "0.1.0"
slug: mimirheim
description: >-
  MILP energy optimiser for home battery, PV, and EV scheduling against dynamic
  electricity prices.
url: https://github.com/simonetti/hioo
arch:
  - aarch64
  - amd64
startup: application
boot: auto
init: false
image: ghcr.io/simonetti/mimirheim
stage: stable

# addon_config mounts the add-on's private config directory to /config
# inside the container — the same path all s6 services already read from.
# No path override is needed.
#
# homeassistant_config (read-only) is mounted at /homeassistant so helpers
# that read the HA config directory (e.g. baseload-ha-db) can reach it.
#
# share (read-write) is mounted at /share. The HA Samba add-on exposes this
# directory over SMB, so reports written here are directly browsable from
# any device on the network. Dumps are written to /data (always available,
# private to the add-on — not in the map list).
map:
  - type: addon_config
    read_only: false
  - type: homeassistant_config
    read_only: true
    path: /homeassistant
  - type: share
    read_only: false

# The config-editor web UI is served through HA ingress rather than a
# directly exposed port. Ingress authenticates users via their HA session
# before proxying to the container, making a separate auth layer unnecessary.
ingress: true
ingress_port: 8099
ingress_entry: /

services:
  - mqtt:need

options:
  # Helpers are all disabled by default. Enable each one once you have placed
  # its corresponding YAML config file in the add-on config directory.
  enable_nordpool: false
  enable_pv_fetcher: false
  enable_pv_ml_learner: false
  enable_baseload_ha: false
  enable_baseload_ha_db: false
  enable_baseload_static: false
  enable_scheduler: false
  enable_reporter: false
  # Config editor defaults to enabled; disable if not needed.
  enable_config_editor: false

schema:
  enable_nordpool: bool
  enable_pv_fetcher: bool
  enable_pv_ml_learner: bool
  enable_baseload_ha: bool
  enable_baseload_ha_db: bool
  enable_baseload_static: bool
  enable_scheduler: bool
  enable_reporter: bool
  enable_config_editor: bool
```

### `mimirheim-beta/config.yaml` (testing / edge)

Identical to the stable variant with these differences:

```yaml
name: Mimirheim (Beta)
slug: mimirheim-beta
image: ghcr.io/simonetti/mimirheim
# beta tracks the :edge tag; see image_tag override in Dockerfile ARG or
# via the workflow tag strategy — both addons use the same image name,
# differentiated only by the tag they declare in their config.yaml version field.
stage: experimental
```

**Important note on versions and image tags:** The `version` field in
`config.yaml` is the tag the Supervisor will pull from the container registry.
For the stable release, `version` must match the published release tag (e.g.,
`"0.4.2"`). For the beta add-on, `version` is set to `"edge"` so the
Supervisor always pulls `:edge`, which is rebuilt on every push to `main`.

---

## Step 3 — Dockerfile adaptation

The current `container/Dockerfile` uses `python:3.12-slim-bookworm` as its
base and installs s6-overlay manually. For HA add-ons the base image must be
one of the images provided by the Home Assistant project, which already include
s6-overlay, bashio, and tempio.

### Base image change

Replace:
```dockerfile
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
```

With:
```dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.12-alpine3.21
FROM ${BUILD_FROM} AS builder
```

The `BUILD_FROM` argument is substituted by the HA builder action to the
architecture-appropriate image. Using `base-python` instead of `base` provides
Python 3.12 on Alpine 3.21 without manual installation.

### Remove manual s6-overlay installation

The entire s6-overlay download and installation block in Stage 2 is removed.
s6-overlay is already present in the HA base image.

### Replace apt with apk

The current image uses `apt-get`. Alpine uses `apk`. Changes:

```dockerfile
# Before (Debian):
RUN apt-get update && apt-get install -y --no-install-recommends xz-utils wget

# After (Alpine):
RUN apk add --no-cache wget xz
```

All other `apt-get` calls must be converted to `apk add --no-cache` equivalents.

### Add required OCI and HA labels

Add these labels to Stage 2 in the final image:

```dockerfile
ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_DESCRIPTION
ARG BUILD_NAME
ARG BUILD_REF
ARG BUILD_REPOSITORY
ARG BUILD_VERSION

LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.revision="${BUILD_REF}" \
    org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.version="${BUILD_VERSION}"
```

These build-args are injected automatically by the HA builder action.

### Config path — backwards-compatible symlink

The current container reads config from `/config/`. HA add-ons mount the
add-on config directory at `/addon_configs/<slug>/` inside the container. To
preserve full backwards compatibility without touching any s6 service run
scripts, a `cont-init.d` script creates `/config` as a symlink to the correct
add-on config path at container startup.

When running as a plain Docker container, `ADDON_SLUG` is unset and no
symlink is created; `/config` remains the bind-mounted volume as before.

The s6 service run scripts (`container/etc/s6-overlay/s6-rc.d/*/run`) require
no changes.

### Non-root user

HA add-on containers run as root by default. The `app` user created in the
current Dockerfile is not required. Remove:
```dockerfile
RUN groupadd -r -g 1000 app && useradd -r -u 1000 -g app -d /app -s /sbin/nologin app
```

And remove all `--chown=app:app` flags from `COPY` instructions.

### VOLUME directive

Remove `VOLUME ["/config"]`. HA manages volume mounts via the `map` field in
`config.yaml`, not via Dockerfile VOLUME declarations.

### Full Stage 2 structure after changes

```dockerfile
# ---------------------------------------------------------------------------
# Stage 2: final HA add-on image
# ---------------------------------------------------------------------------
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.12-alpine3.21
FROM ${BUILD_FROM}

ARG BUILD_ARCH BUILD_DATE BUILD_DESCRIPTION BUILD_NAME BUILD_REF BUILD_REPOSITORY BUILD_VERSION

# Install runtime system dependencies (Alpine).
RUN apk add --no-cache libstdc++ libgcc

# Copy the venv from the builder stage.
COPY --from=builder /app/.venv /app/.venv

# Copy example configurations.
COPY mimirheim/config/example.yaml /app/examples/mimirheim.yaml
COPY mimirheim_helpers/examples/ /app/examples/

# Copy s6-overlay service definitions (same structure as current container).
COPY container/etc/ /etc/

# Remove stray notification-fd file.
RUN rm -f /etc/s6-overlay/s6-rc.d/mimirheim/notification-fd

# Make all run scripts executable.
RUN chmod +x \
    /etc/s6-overlay/s6-rc.d/mimirheim/run \
    /etc/s6-overlay/s6-rc.d/nordpool/run \
    ... (same list as current Dockerfile)

EXPOSE 8099

LABEL \
    io.hass.name="${BUILD_NAME}" \
    ... (full label block as above)

# s6-overlay is provided by the HA base image.
ENTRYPOINT ["/init"]
```

---

## Step 4 — Directory mapping: config, data, and share

`addon_config` mounts the add-on's private config directory to `/config`
inside the container — the same path all existing s6 run scripts read from.
No path override is needed.

The `homeassistant_config` map entry (with `path: /homeassistant`) exposes
the main HA config directory read-only at `/homeassistant`. The
`baseload-ha-db` helper reads the HA SQLite database from this directory;
all other helpers do not need it and will ignore it.

**`/data`** is always mounted read-write by the Supervisor without any map
entry. It is private to the add-on and survives upgrades and restarts. The
pv-ml-learner stores its trained XGBoost model files here, which is
appropriate — they are internal artefacts that should not be user-browsable
or modified by hand:

- `arrays.<name>.model_path`: `/data/pv_ml_learner_<name>.joblib`
- `arrays.<name>.metadata_path`: `/data/pv_ml_learner_<name>_meta.json`

The pv-ml-learner example config already uses these `/data` paths, so no
change to the helper config is needed.

**`/share`** is the HA shared network directory, exposed by the
[Samba add-on](https://github.com/home-assistant/addons/tree/master/samba)
over SMB. The `share:rw` map entry makes it writable from inside the
container. Both debug dumps and HTML reports are written here so users can
inspect them directly from any device on the network:

- `debug.dump_dir`: `/share/mimirheim/dumps`
- `reporting.output_dir`: `/share/mimirheim/reports`

Both directories must be created before services start. The
`00-options-env.sh` cont-init script handles this (see Step 6).

**Note on pv-ml-learner `db_path`**: the example config currently uses
`/config/home-assistant_v2.db`. Under the HA add-on, the HA config directory
is mounted at `/homeassistant`, so users must set `db_path` to
`/homeassistant/home-assistant_v2.db` in their `pv-ml-learner.yaml`. The
example config shipped in the image should reflect this updated path.

---

## Step 6 — Automatic MQTT injection from the HA broker integration

When the add-on declares `services: [mqtt:need]`, the HA Supervisor ensures
a compatible MQTT broker is available before starting the add-on and makes
the broker credentials available via the bashio helper library.

Rather than hardcoding broker credentials in the YAML config files, the
add-on reads them directly from the Supervisor at startup and makes them
available to all services as environment variables. These environment
variables override any `mqtt:` values in the YAML config files, so users
who run the container outside HA can still use plain YAML config.

### 5.1 — config.yaml: declare the MQTT service dependency

Add to both `mimirheim/config.yaml` and `mimirheim-beta/config.yaml`:

```yaml
services:
  - mqtt:need
```

This tells the Supervisor that this add-on requires MQTT. The Supervisor
will refuse to start the add-on if no MQTT broker is available.

### 5.2 — cont-init.d script: read credentials and publish to s6 environment

In s6-overlay v3, every service run script that starts with
`#!/usr/bin/with-contenv sh` inherits all variables written to
`/var/run/s6/container_environment/`. A `cont-init.d` script runs once
before any s6 service is started, making it the correct hook for writing
these variables.

Create `container/etc/cont-init.d/01-mqtt-env.sh`:

```sh
#!/bin/sh
# Inject MQTT broker credentials from the HA Supervisor into the s6
# container environment so all services inherit them automatically.
#
# This script only runs when SUPERVISOR_TOKEN is set, which is true only
# when the container is started by the HA Supervisor. When running as a
# plain Docker container the variable is absent and this script exits
# immediately, leaving the YAML config files as the sole source of truth.

if [ -z "${SUPERVISOR_TOKEN}" ]; then
    exit 0
fi

mkdir -p /var/run/s6/container_environment

printf '%s' "$(bashio::services mqtt 'host')"     > /var/run/s6/container_environment/MQTT_HOST
printf '%s' "$(bashio::services mqtt 'port')"     > /var/run/s6/container_environment/MQTT_PORT
printf '%s' "$(bashio::services mqtt 'username')" > /var/run/s6/container_environment/MQTT_USERNAME
printf '%s' "$(bashio::services mqtt 'password')" > /var/run/s6/container_environment/MQTT_PASSWORD
printf '%s' "$(bashio::services mqtt 'ssl')"      > /var/run/s6/container_environment/MQTT_SSL
```

Install and make executable in the Dockerfile Stage 2:

```dockerfile
COPY container/etc/cont-init.d/ /etc/cont-init.d/
RUN chmod +x /etc/cont-init.d/01-mqtt-env.sh
```

### 5.3 — s6 run scripts: use with-contenv

Change every s6 service run script shebang from:

```sh
#!/bin/sh
```

To:

```sh
#!/usr/bin/with-contenv sh
```

`with-contenv` sources all variables from `/var/run/s6/container_environment/`
before executing the script. When running as plain Docker (no Supervisor),
that directory is empty (no MQTT vars were written), so the behaviour is
identical to `#!/bin/sh`.

### 5.4 — Python: env var override applied before Pydantic validation

Both `mimirheim` and all helpers use a `MqttConfig` Pydantic model loaded
from the YAML file. The override is applied after parsing the YAML and
before calling `model_validate`, so Pydantic validates the final merged
values (including env-sourced ones).

Add a shared utility function to `mimirheim_helpers/common/helper_common/config.py`:

```python
import os

def apply_mqtt_env_overrides(raw: dict) -> dict:
    """Override the mqtt: section from environment variables if present.

    When running as a HA add-on the Supervisor injects MQTT broker credentials
    as environment variables (set by the cont-init.d/01-mqtt-env.sh script).
    These take precedence over whatever appears in the YAML config file so
    users do not need to copy broker credentials into their config.

    When the environment variables are absent (plain Docker, no Supervisor)
    this function is a no-op and the YAML values are used as-is.

    Args:
        raw: The raw dict parsed from the YAML config file. Modified in-place
            and returned.

    Returns:
        The same dict with any MQTT env var overrides applied.
    """
    overrides: dict = {}
    if host := os.environ.get("MQTT_HOST"):
        overrides["host"] = host
    if port := os.environ.get("MQTT_PORT"):
        overrides["port"] = int(port)
    if username := os.environ.get("MQTT_USERNAME"):
        overrides["username"] = username
    if password := os.environ.get("MQTT_PASSWORD"):
        overrides["password"] = password
    # MQTT_SSL is 'true'/'false' from bashio.
    if ssl := os.environ.get("MQTT_SSL"):
        overrides["tls_allow_insecure"] = ssl.lower() != "true"
    if overrides:
        raw.setdefault("mqtt", {})
        raw["mqtt"].update(overrides)
    return raw
```

Call this function in `mimirheim/__main__.py`'s `_load_config` before
`MimirheimConfig.model_validate(raw)`, and in every helper's equivalent
config-loading path. Because all helpers use `helper_common`, a single
import covers the entire helper fleet.

### 5.5 — client_id in YAML config remains required

The Supervisor does not provide an MQTT client ID; each service must supply
its own unique identifier. Users still include `mqtt.client_id` in their
YAML config files. The env override does not touch `client_id`.

---

## Step 6 — Per-service enable/disable and config-editor ingress

### 6.1 — cont-init.d: read options and publish service enable flags

Create `container/etc/cont-init.d/00-options-env.sh`. This script runs
before the MQTT env script (alphabetical ordering; `00` before `01`) and
before any s6 service starts. It reads `/data/options.json` (HA's
persistent data volume) via bashio and writes each enable flag to the
s6 container environment directory.

When `SUPERVISOR_TOKEN` is absent (plain Docker), `/data/options.json`
does not exist and the script exits immediately. All `ENABLE_*` variables
remain unset, which the run scripts interpret as "not managed by HA —
use config-file presence as the gate" (same behaviour as today).

```sh
#!/bin/sh
# Read per-service enable flags from the HA add-on options and write them
# to the s6 container environment so all service run scripts can check them.
#
# Guards: only runs under the HA Supervisor (SUPERVISOR_TOKEN present).
# Also writes CONFIG_EDITOR_ALLOWED_IP so the config editor can restrict
# access to the HA ingress proxy IP.

if [ -z "${SUPERVISOR_TOKEN}" ]; then
    exit 0
fi

mkdir -p /var/run/s6/container_environment

# Create the dump and report directories under /share so they are
# immediately accessible via the Samba add-on without user intervention.
mkdir -p /share/mimirheim/dumps
mkdir -p /share/mimirheim/reports

for SERVICE in nordpool pv_fetcher pv_ml_learner baseload_ha baseload_ha_db \
               baseload_static scheduler reporter config_editor; do
    KEY="enable_${SERVICE}"
    # bashio::config.true returns exit 0 when the option is true.
    if bashio::config.true "${KEY}"; then
        printf 'true'  > "/var/run/s6/container_environment/ENABLE_$(echo ${SERVICE} | tr '[:lower:]' '[:upper:]')"
    else
        printf 'false' > "/var/run/s6/container_environment/ENABLE_$(echo ${SERVICE} | tr '[:lower:]' '[:upper:]')"
    fi
done

# Write the default gateway IP (the HA Supervisor host) for the config
# editor's IP allowlist. The gateway is always the Supervisor when running
# as a HA add-on.
GATEWAY=$(ip route show default | awk '/default/ { print $3 }')
if [ -n "${GATEWAY}" ]; then
    printf '%s' "${GATEWAY}" > /var/run/s6/container_environment/CONFIG_EDITOR_ALLOWED_IP
fi
```

Install alongside the MQTT script:

```dockerfile
COPY container/etc/cont-init.d/ /etc/cont-init.d/
RUN chmod +x /etc/cont-init.d/00-options-env.sh /etc/cont-init.d/01-mqtt-env.sh
```

### 6.2 — s6 run scripts: check the enable flag

Change every helper service run script (not mimirheim itself) to check its
env var at startup. mimirheim is the core solver and has no toggle — it
always starts.

Example for nordpool (pattern applies to all helpers):

```sh
#!/usr/bin/with-contenv sh
exec 2>&1
# ENABLE_NORDPOOL is written by cont-init.d/00-options-env.sh when running
# as a HA add-on. When unset (plain Docker), default to true so that the
# existing config-file-presence gate remains the only control.
if [ "${ENABLE_NORDPOOL:-true}" != "true" ]; then
    echo "nordpool: disabled via add-on options"
    exec sleep infinity
fi
CONFIG=/config/nordpool.yaml
if [ ! -f "$CONFIG" ]; then
    echo "nordpool: $CONFIG not found — sleeping"
    exec sleep infinity
fi
exec /app/.venv/bin/python -m nordpool_prices --config "$CONFIG"
```

`exec sleep infinity` keeps the process alive in a state s6 does not
consider a crash, so no restart loop is triggered. The service is
effectively a permanent no-op for the lifetime of the container.

The default of `true` when the env var is unset preserves the
existing plain-Docker behaviour exactly: the only gate is config file
presence.

### 6.3 — Config-editor: ingress and IP allowlist

With `ingress: true` in `config.yaml`, the HA frontend proxies all
browser traffic through the Supervisor before forwarding to port 8099
inside the container. This means:

- HA session authentication is enforced at the proxy level — no
  unauthenticated browser can reach the config editor.
- The config editor sees every request arriving from the Supervisor's
  IP (the container's default gateway), not from the browser's IP.

To add defence in depth, the config editor should accept connections
only from that gateway IP when running as a HA add-on. The
`CONFIG_EDITOR_ALLOWED_IP` env var written by `00-options-env.sh`
provides this IP.

Required code changes in `config_editor/`:

1. **`config.py`** — add an optional field:
   ```python
   allowed_ip: str | None = Field(
       default=None,
       description="If set, only accept HTTP connections from this IP address."
   )
   ```
   In `load_config()`, after `model_validate`, override `allowed_ip` from
   the `CONFIG_EDITOR_ALLOWED_IP` env var if present.

2. **`server.py`** — in `_Handler.handle()` (or early in `do_GET`/`do_POST`),
   check `self.client_address[0]` against `allowed_ip`. Reject with 403 if
   the IP does not match and `allowed_ip` is set.

When `CONFIG_EDITOR_ALLOWED_IP` is unset (plain Docker), `allowed_ip`
remains `None` and no IP check is performed — the existing behaviour is
preserved.

---

## Step 7 — Shared image, dual-channel tag strategy

Both the stable and beta add-ons point to **the same container image name**
(`ghcr.io/simonetti/mimirheim`) at **different tags**:

| Add-on        | `version` in config.yaml | Image tag pulled          |
|---------------|--------------------------|---------------------------|
| `mimirheim`   | `"0.4.2"` (release)      | `:0.4.2` → becomes latest |
| `mimirheim-beta` | `"edge"`              | `:edge`                   |

The `version` field in `config.yaml` is literally the image tag the Supervisor
will pull. Setting `version: "edge"` permanently pins the beta add-on to the
`:edge` tag.

For the stable add-on, the `version` field must be updated as part of the
release process (see Step 7).

---

## Step 8 — GitHub Actions CI/CD

Two workflows live inside `hassio-repository/.github/workflows/` and are
deployed to `simonetti/hassio-addons` by a third workflow that lives in
`hioo`'s own `.github/workflows/`.

### `hioo/.github/workflows/deploy-hassio.yaml` — subtree push to `hassio-addons`

This workflow runs in the `hioo` repository. On every push to `main` and on
every `v*` tag it pushes the contents of `hassio-repository/` to the root of
`simonetti/hassio-addons`. Once that push lands, the `builder.yaml` workflow
inside `hassio-addons` then detects the changed files and triggers the
add-on image builds.

```yaml
name: Deploy HA add-on repository

on:
  push:
    branches:
      - main
    tags:
      - "v*"
    paths:
      - "hassio-repository/**"

permissions:
  contents: read

jobs:
  deploy:
    name: Push hassio-repository/ to simonetti/hassio-addons
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          persist-credentials: false

      - name: Deploy subdirectory to hassio-addons
        uses: peaceiris/actions-gh-pages@v4
        with:
          personal_token: ${{ secrets.HASSIO_ADDONS_DEPLOY_KEY }}
          external_repository: simonetti/hassio-addons
          publish_branch: main
          publish_dir: ./hassio-repository
          # Preserve .github/workflows inside hassio-addons so its own
          # builder.yaml triggers correctly after the push.
          keep_files: false
          force_orphan: false
```

This gives a clean, auditable push on every change to `hassio-repository/`,
without needing submodules or manual syncing.

### `hassio-repository/.github/workflows/builder.yaml` — dispatcher

Detects which add-on directories have changed and dispatches to `build-app.yaml`.

```yaml
name: Builder

env:
  MONITORED_FILES: "config.yaml Dockerfile rootfs"

on:
  pull_request:
    branches:
      - main
  push:
    branches:
      - main
    tags:
      - "v*"

permissions:
  contents: read

jobs:
  init:
    name: Initialize builds
    runs-on: ubuntu-latest
    outputs:
      changed: ${{ steps.filter.outputs.changed }}
      changed_apps: ${{ steps.filter.outputs.changed_apps }}
    steps:
      - uses: actions/checkout@v4

      - name: Get changed files
        id: changed_files
        uses: tj-actions/changed-files@v47

      - name: Find app directories
        id: apps
        uses: home-assistant/actions/helpers/find-addons@master

      - name: Filter changed apps
        id: filter
        env:
          APPS: ${{ steps.apps.outputs.addons }}
          CHANGED_FILES: ${{ steps.changed_files.outputs.all_changed_files }}
        run: |
          changed_apps=()
          # If the workflow file itself changed, rebuild all apps.
          if [[ "${CHANGED_FILES}" =~ \.github/workflows/(builder|build-app)\.yaml ]]; then
            changed_apps=(${APPS})
          else
            for app in ${APPS}; do
              for file in ${MONITORED_FILES}; do
                if [[ "${CHANGED_FILES}" =~ ${app}/${file} ]]; then
                  changed_apps+=("${app}")
                  break
                fi
              done
            done
          fi
          if [[ ${#changed_apps[@]} -gt 0 ]]; then
            echo "changed=true" >> "$GITHUB_OUTPUT"
            echo "changed_apps=$(jq -nc '$ARGS.positional' --args "${changed_apps[@]}")" >> "$GITHUB_OUTPUT"
          else
            echo "changed=false" >> "$GITHUB_OUTPUT"
          fi

  build-app:
    name: Build ${{ matrix.app }}
    needs: init
    if: needs.init.outputs.changed == 'true'
    permissions:
      contents: read
      id-token: write
      packages: write
    strategy:
      fail-fast: false
      matrix:
        app: ${{ fromJSON(needs.init.outputs.changed_apps) }}
    uses: ./.github/workflows/build-app.yaml
    with:
      app: ${{ matrix.app }}
      publish: ${{ github.event_name == 'push' }}
    secrets: inherit
```

### `hassio-repository/.github/workflows/build-app.yaml` — reusable per-app builder

Builds each add-on using the HA composite actions, which handle per-arch
builds and multi-arch manifest publishing.

```yaml
name: Build app

on:
  workflow_call:
    inputs:
      app:
        required: true
        type: string
      publish:
        required: true
        type: boolean

jobs:
  prepare:
    name: Prepare
    runs-on: ubuntu-latest
    outputs:
      architectures: ${{ steps.info.outputs.architectures }}
      build_matrix: ${{ steps.matrix.outputs.matrix }}
      image_name: ${{ steps.normalize.outputs.image_name }}
      name: ${{ steps.normalize.outputs.name }}
      description: ${{ steps.normalize.outputs.description }}
      registry_prefix: ${{ steps.normalize.outputs.registry_prefix }}
      version: ${{ steps.normalize.outputs.version }}
    steps:
      - uses: actions/checkout@v4

      - name: Get app information
        id: info
        uses: home-assistant/actions/helpers/info@master
        with:
          path: "./${{ inputs.app }}"

      - name: Normalize app information
        id: normalize
        run: |
          image=${{ steps.info.outputs.image }}
          echo "image_name=${image##*/}" >> "$GITHUB_OUTPUT"
          echo "registry_prefix=${image%/*}" >> "$GITHUB_OUTPUT"
          echo "version=${{ steps.info.outputs.version }}" >> "$GITHUB_OUTPUT"
          echo "name=${{ steps.info.outputs.name }}" >> "$GITHUB_OUTPUT"
          echo "description=${{ steps.info.outputs.description }}" >> "$GITHUB_OUTPUT"

      - name: Prepare build matrix
        id: matrix
        uses: home-assistant/builder/actions/prepare-multi-arch-matrix@2026.03.2
        with:
          architectures: ${{ steps.info.outputs.architectures }}
          image-name: ${{ steps.normalize.outputs.image_name }}
          registry-prefix: ${{ steps.normalize.outputs.registry_prefix }}

  build:
    name: Build ${{ matrix.arch }} image
    needs: prepare
    runs-on: ${{ matrix.os }}
    permissions:
      contents: read
      id-token: write
      packages: write
    strategy:
      fail-fast: false
      matrix: ${{ fromJSON(needs.prepare.outputs.build_matrix) }}
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false

      - name: Build image
        uses: home-assistant/builder/actions/build-image@2026.03.2
        with:
          arch: ${{ matrix.arch }}
          container-registry-password: ${{ secrets.GITHUB_TOKEN }}
          context: "./${{ inputs.app }}"
          image: ${{ matrix.image }}
          image-tags: |
            ${{ needs.prepare.outputs.version }}
            latest
          labels: |
            io.hass.type=addon
            io.hass.name=${{ needs.prepare.outputs.name }}
            io.hass.description=${{ needs.prepare.outputs.description }}
          push: ${{ inputs.publish }}
          version: ${{ needs.prepare.outputs.version }}

  manifest:
    name: Publish multi-arch manifest
    needs: [prepare, build]
    if: inputs.publish
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      packages: write
    steps:
      - name: Publish multi-arch manifest
        uses: home-assistant/builder/actions/publish-multi-arch-manifest@2026.03.2
        with:
          architectures: ${{ needs.prepare.outputs.architectures }}
          container-registry-password: ${{ secrets.GITHUB_TOKEN }}
          image-name: ${{ needs.prepare.outputs.image_name }}
          image-tags: |
            ${{ needs.prepare.outputs.version }}
            latest
          registry-prefix: ${{ needs.prepare.outputs.registry_prefix }}
```

**Notes on the beta add-on build:**

The `build-app.yaml` always tags images with `version` (from `config.yaml`) and
`latest`. For the beta add-on whose `config.yaml` has `version: "edge"`, the
image is tagged `:edge` and `:latest` (on the beta image namespace). However,
since both add-ons share the same image (`ghcr.io/simonetti/mimirheim`), the
`:latest` tag would be overwritten by whichever runs last.

To avoid this collision, the stable and beta add-ons must publish to **different
image names** or the `image-tags` must be differentiated. The cleanest solution:

- Stable add-on `config.yaml`: `image: ghcr.io/simonetti/mimirheim`
  — publishes `:0.4.2` and `:latest`
- Beta add-on `config.yaml`: `image: ghcr.io/simonetti/mimirheim`
  with `version: "edge"` — publishes `:edge` (no `latest` override in beta)

To prevent the beta build from also tagging `:latest`, override the `image-tags`
in `build-app.yaml` based on whether the version equals `"edge"`:

```yaml
# In the build and manifest steps, conditionally omit the 'latest' tag:
image-tags: |
  ${{ needs.prepare.outputs.version }}
  ${{ needs.prepare.outputs.version != 'edge' && 'latest' || '' }}
```

---

## Step 9 — Version management

The `version` field in the stable add-on's `config.yaml` must stay in sync with
the Git tag and the published image. The release process:

1. Decide on a new version string, e.g. `0.4.2`.
2. Update `mimirheim/config.yaml`: set `version: "0.4.2"`.
3. Commit: `git commit -m "Release 0.4.2"`.
4. Tag: `git tag v0.4.2 && git push origin v0.4.2`.
5. The `builder.yaml` workflow triggers on the `v*` tag (via the `push: tags`
   trigger), sets `publish: true`, and the image is built and pushed as
   `:0.4.2` + `:latest`.

To automate the version bump in `config.yaml` as part of the CI pipeline (so
humans only need to push the tag):

```yaml
# In builder.yaml, add a pre-step that updates config.yaml on tag push:
- name: Update stable version from tag
  if: startsWith(github.ref, 'refs/tags/v')
  run: |
    VERSION=${GITHUB_REF#refs/tags/v}
    sed -i "s/^version:.*/version: \"${VERSION}\"/" mimirheim/config.yaml
```

This approach keeps `config.yaml` up to date without manual edits. The
updated file is used during the build but not committed back (the image tag
serves as the source of truth).

The beta add-on's `version: "edge"` never changes.

---

## Step 10 — Translations and user-facing text

Each add-on directory must contain `translations/en.yaml` for the HA UI to
display human-readable option labels. For the minimal options set (just
`timezone`):

```yaml
# translations/en.yaml
configuration:
  timezone:
    name: Timezone
    description: >-
      IANA timezone identifier (e.g. Europe/Amsterdam). Passed to all services
      as the TZ environment variable.
```

---

## Step 11 — Documentation files

### `DOCS.md`

Full user-facing documentation embedded in the HA add-on UI. Should cover:

- Prerequisites (MQTT broker, Home Assistant MQTT integration)
- Installation: adding the repository URL to HA Supervisor
- Configuration: placing `mimirheim.yaml` and helper YAMLs in the add-on
  config directory
- Available services and their config files
- Config editor web UI (port 8099)
- Upgrade procedure

### `CHANGELOG.md`

Conventional changelog with `## [version] — date` sections. Required for
display in the HA add-on store.

---

## Step 12 — Icon and logo

HA add-ons display an icon (256×256 PNG) and a logo (250×100 PNG) in the store.

- `icon.png` — square icon, 256×256
- `logo.png` — rectangular banner, 250×100

Both must be placed in the add-on directory (`mimirheim/`, `mimirheim-beta/`).

---

## Step 13 — Migration checklist

The following tasks must be completed in order. Each task has a clear
acceptance criterion.

| # | Task | Acceptance criterion |
|---|------|---------------------|
| 1 | Create empty `simonetti/hassio-addons` GitHub repository | Repository exists; CI will populate it |
| 2 | Add `HASSIO_ADDONS_DEPLOY_KEY` secret to `hioo` repository settings | Push from hioo CI to hassio-addons succeeds |
| 3 | Create `hassio-repository/mimirheim/config.yaml` | Validates via `ha addon check` or equivalent |
| 4 | Create `hassio-repository/mimirheim-beta/config.yaml` | `stage: experimental`, `version: "edge"` |
| 5 | Create `hassio-repository/mimirheim/Dockerfile` adapted from `container/Dockerfile` | Image builds locally for amd64: `docker buildx build --platform linux/amd64 .` |
| 6 | Verify s6 run scripts read `/config/` — no changes needed | `addon_config` already mounts to `/config` inside the container |
| 7 | Add `services: [mqtt:need]`, `ingress: true`, `ingress_port: 8099`, and all `enable_*` options to both `config.yaml` files | Config validates; ingress panel appears in HA UI |
| 8 | Create `container/etc/cont-init.d/00-options-env.sh` (service enable flags + gateway IP) | `ENABLE_NORDPOOL` etc. and `CONFIG_EDITOR_ALLOWED_IP` present in container environment |
| 9 | Create `container/etc/cont-init.d/01-mqtt-env.sh` (MQTT credentials) | MQTT env vars present in all services when running as HA add-on; no-op in plain Docker |
| 10 | Change all helper s6 run script shebangs to `#!/usr/bin/with-contenv sh`; add enable-flag check | Disabled helpers sleep; enabled helpers start normally; plain-Docker behaviour unchanged |
| 11 | Add `apply_mqtt_env_overrides()` to `helper_common/config.py`; call it in mimirheim and all helper loaders | MQTT env vars override YAML values; plain Docker behaviour unchanged |
| 12 | Add `allowed_ip` field + IP-check to config-editor (`config.py`, `server.py`) | Under HA, config editor only accepts connections from the gateway IP; under plain Docker no restriction |
| 13 | Create `hassio-repository/.github/workflows/builder.yaml` + `build-app.yaml` | Workflows present in hassio-addons after first deploy |
| 14 | Create `hioo/.github/workflows/deploy-hassio.yaml` | Push to `hioo` main triggers deploy to `hassio-addons` |
| 15 | Push to `hioo` main | `hassio-repository/` content appears at root of `simonetti/hassio-addons` |
| 16 | Push first release tag `v0.1.0` | Image `ghcr.io/simonetti/mimirheim:0.1.0` and `:latest` appear in GHCR |
| 17 | Push to `main` (beta) | Image `ghcr.io/simonetti/mimirheim:edge` appears in GHCR |
| 18 | Add `https://github.com/simonetti/hassio-addons` to HA Supervisor | Both add-ons visible in HA Supervisor store |
| 19 | Install stable add-on; enable nordpool only; place `nordpool.yaml` and `mimirheim.yaml` | mimirheim and nordpool start; other helpers sleep; config editor accessible via HA sidebar |
| 20 | Install beta add-on alongside stable | Both run independently without config collision |

---

## Step 14 — Open questions and decisions before implementation

The following require a decision before implementation begins:

1. **GitHub organization**: Confirm the org name for the new repository
   (`simonetti` or another org/user account).

2. **Image name**: Confirm `ghcr.io/simonetti/mimirheim`. The image must be
   published to the same org that owns the repository, so HA Supervisor can
   verify provenance.

3. **Source of the Dockerfile**: The add-on Dockerfile can either be a
   self-contained copy in `hassio-repository/mimirheim/Dockerfile` (duplicated
   from `hioo/container/Dockerfile`), or the add-on can pull a pre-built wheel
   from PyPI and use a simpler Dockerfile. Recommended: copy and adapt the full
   Dockerfile to avoid a PyPI publish dependency.

4. **Config directory convention**: Confirm whether users are expected to manage
   `mimirheim.yaml` and helper YAMLs by placing files in the HA add-on config
   directory (accessible via the File Editor add-on or Samba share), or whether
   a full in-UI config form is preferred. The plan above assumes file-based
   config as the primary interface, with `options` limited to `timezone`.

5. **Helper services in add-on**: All 10 s6 services (nordpool, pv-fetcher,
   baseload-ha-db, etc.) are included in the same add-on image, matching the
   current container. This is intentional — users configure only the services
   they need. Confirm this is still the desired packaging strategy (single
   fat add-on vs multiple slim add-ons).

6. **Version synchronisation with `hioo`**: Is the `version` in `config.yaml`
   intended to track the mimirheim Python package version? If so, add a step
   to the release process that reads `pyproject.toml` version and writes it
   into `config.yaml` automatically.

---

## Summary of files to create

| File | Notes |
|------|-------|
| `hassio-repository/repository.yaml` | Repository identity |
| `hassio-repository/README.md` | User-facing repo README |
| `hassio-repository/mimirheim/config.yaml` | Stable add-on manifest |
| `hassio-repository/mimirheim/Dockerfile` | Adapted from `container/Dockerfile` |
| `hassio-repository/mimirheim/translations/en.yaml` | Option labels |
| `hassio-repository/mimirheim/CHANGELOG.md` | Release log |
| `hassio-repository/mimirheim/DOCS.md` | User documentation |
| `hassio-repository/mimirheim/icon.png` | 256×256 icon |
| `hassio-repository/mimirheim/logo.png` | 250×100 logo |
| `hassio-repository/mimirheim-beta/config.yaml` | Beta add-on manifest |
| `hassio-repository/mimirheim-beta/Dockerfile` | Same as stable or symlink |
| `hassio-repository/mimirheim-beta/translations/en.yaml` | |
| `hassio-repository/mimirheim-beta/CHANGELOG.md` | |
| `hassio-repository/mimirheim-beta/DOCS.md` | |
| `hassio-repository/mimirheim-beta/icon.png` | Same or visually distinct |
| `hassio-repository/mimirheim-beta/logo.png` | |
| `hassio-repository/.github/workflows/builder.yaml` | Change-detection dispatcher (lives in hioo, deployed to hassio-addons) |
| `hassio-repository/.github/workflows/build-app.yaml` | Reusable build workflow (lives in hioo, deployed to hassio-addons) |
| `hioo/.github/workflows/deploy-hassio.yaml` | Subtree push: deploys `hassio-repository/` to `simonetti/hassio-addons` on push/tag |
| `container/etc/cont-init.d/00-options-env.sh` | Reads service enable flags + gateway IP from HA options; writes to s6 env dir |
| `container/etc/cont-init.d/01-mqtt-env.sh` | Reads MQTT from Supervisor via bashio; writes to s6 env dir |
| `mimirheim_helpers/common/helper_common/config.py` | Add `apply_mqtt_env_overrides()` |
| `mimirheim/__main__.py` | Call `apply_mqtt_env_overrides(raw)` before `model_validate` |
| Each helper's `__main__.py` | Call `apply_mqtt_env_overrides(raw)` before `model_validate` |
| `mimirheim_helpers/config_editor/config_editor/config.py` | Add `allowed_ip` field; read from `CONFIG_EDITOR_ALLOWED_IP` env var |
| `mimirheim_helpers/config_editor/config_editor/server.py` | Add IP check in request handler |

All helper s6 service run scripts require two changes: shebang to
`#!/usr/bin/with-contenv sh` and an enable-flag check at the top. The
mimirheim service run script needs only the shebang change (no enable flag).

`addon_config` already mounts to `/config` inside the container, so all
existing config-path references in run scripts remain correct.
