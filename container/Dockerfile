# Mimirheim — all-in-one image
#
# Runs the mimirheim solver and all input tools under s6-overlay. Each service
# reads its configuration from a bind-mounted /config directory. A service
# whose config file is absent sleeps harmlessly until the container is
# restarted with the file in place.
#
# Expected config files (only mimirheim.yaml is strictly required):
#   /config/mimirheim.yaml           — main solver (required)
#   /config/scheduler.yaml      — cron MQTT scheduler
#   /config/nordpool.yaml       — Nordpool day-ahead price fetcher
#   /config/zonneplan.yaml      — Zonneplan dynamic price fetcher
#   /config/pv-fetcher.yaml     — forecast.solar PV forecast fetcher
#   /config/pv-ml-learner.yaml  — ML-based PV forecast learner
#   /config/baseload-ha.yaml    — Home Assistant baseload fetcher (REST API)
#   /config/baseload-ha-db.yaml  — Home Assistant baseload fetcher (database)
#   /config/baseload-static.yaml — static baseload profile publisher
#   /config/reporter.yaml        — mimirheim-reporter HTML report generator
#   /config/config-editor.yaml   — config editor web UI (port 8099)
#
# Example configs are baked into the image at /app/examples/:
#   /app/examples/mimirheim.yaml
#   /app/examples/scheduler.yaml
#   /app/examples/nordpool.yaml
#   /app/examples/zonneplan.yaml
#   /app/examples/pv-fetcher.yaml
#   /app/examples/pv-ml-learner.yaml
#   /app/examples/baseload-ha.yaml
#   /app/examples/baseload-ha-db.yaml
#   /app/examples/baseload-static.yaml
#   /app/examples/reporter.yaml
#   /app/examples/config-editor.yaml
#
# Build:
#   docker build -t mimirheim -f container/Dockerfile .
#
# Run:
#   docker run -d \
#     -v /path/to/your/configs:/config:ro \
#     -e TZ=Europe/Amsterdam \
#     mimirheim

ARG S6_OVERLAY_VERSION=3.2.2.0
ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1: deps-only venv — rebuilt only when pyproject.toml or uv.lock change.
# Third-party packages are large; keeping them in a separate stage means a
# source-only change never invalidates this cached layer.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS deps-builder

RUN apt-get update && apt-get install -y --no-install-recommends binutils
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

COPY pyproject.toml uv.lock ./

RUN uv venv /app/.venv --python python${PYTHON_VERSION} && \
    UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --all-extras --frozen --no-install-project && \
    /app/.venv/bin/python -m compileall -q -b /app/.venv

# ---------------------------------------------------------------------------
# Stage 2: wheel builder — rebuilt on every source change, but produces only
# a small wheel (~2 MB). No venv needed here; uv build is self-contained.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS wheel-builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY mimirheim/ ./mimirheim/
COPY mimirheim_helpers/ ./mimirheim_helpers/

RUN uv build --wheel --out-dir /app/wheels
    
# ---------------------------------------------------------------------------
# Stage 3: final image
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG S6_OVERLAY_VERSION
ARG TARGETARCH

# s6-overlay uses different architecture names than Docker's TARGETARCH.
# Map TARGETARCH (amd64, arm64) to the s6-overlay tarball name.
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/s6-overlay-noarch.tar.xz

RUN apt-get update && \
    apt-get install -y --no-install-recommends jq curl xz-utils wget && \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz && \
    case "${TARGETARCH}" in \
        amd64) S6_ARCH="x86_64" ;; \
        arm64) S6_ARCH="aarch64" ;; \
        arm)   S6_ARCH="armhf" ;; \
        *)     echo "Unsupported TARGETARCH: ${TARGETARCH}"; exit 1 ;; \
    esac && \
    wget -q -O /tmp/s6-overlay-arch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" && \
    tar -C / -Jxpf /tmp/s6-overlay-arch.tar.xz && \
    apt-get remove -y xz-utils wget && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* /tmp/s6-overlay-*.tar.xz

# Create a non-root user for all services.
RUN groupadd -g 1000 app && \
    useradd -u 1000 -g app -d /app -s /sbin/nologin app

COPY --from=ghcr.io/hassio-addons/base:stable /usr/lib/bashio /usr/lib/bashio
RUN ln -s /usr/lib/bashio/bashio /usr/bin/bashio

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy the deps-only venv — large layer (~700 MB), but cached between source-only changes.
COPY --from=deps-builder --chown=app:app /app/.venv /app/.venv

# Install the project wheel into the existing venv — small layer (~2 MB), rebuilt on
# every source change. --no-deps ensures uv does not re-resolve or re-download anything.
COPY --from=wheel-builder /app/wheels /app/wheels
RUN uv pip install --python /app/.venv --no-deps /app/wheels/*.whl && \
    /app/.venv/bin/python -m compileall -q -b /app/.venv && \
    rm -rf /app/wheels


# Copy example configurations so users can bootstrap their /config mount.
# All helper examples live in mimirheim_helpers/examples/; the mimirheim core example
# comes from the mimirheim package.
COPY --chown=app:app mimirheim/config/example.yaml /app/examples/mimirheim.yaml
COPY --chown=app:app mimirheim_helpers/examples/ /app/examples/

# Copy s6-overlay service definitions.
COPY --chown=root:root container/etc/ /etc/

# Make all run scripts and the hassio-env oneshot executable.
RUN chmod +x \
    /etc/s6-overlay/s6-rc.d/mimirheim/run \
    /etc/s6-overlay/s6-rc.d/nordpool/run \
    /etc/s6-overlay/s6-rc.d/zonneplan/run \
    /etc/s6-overlay/s6-rc.d/pv-fetcher/run \
    /etc/s6-overlay/s6-rc.d/pv-ml-learner/run \
    /etc/s6-overlay/s6-rc.d/baseload-ha/run \
    /etc/s6-overlay/s6-rc.d/baseload-ha-db/run \
    /etc/s6-overlay/s6-rc.d/baseload-static/run \
    /etc/s6-overlay/s6-rc.d/scheduler/run \
    /etc/s6-overlay/s6-rc.d/reporter/run \
    /etc/s6-overlay/s6-rc.d/config-editor/run \
    /etc/s6-overlay/s6-rc.d/hassio-env/hassio-env.sh \
    /etc/s6-overlay/s6-rc.d/hassio-env/up

# /config is the mount point for user-supplied YAML configuration files.
VOLUME ["/config"]

# Expose the config editor port. Map it with -p 8099:8099 when not using host networking.
EXPOSE 8099

# s6-overlay init is the entrypoint; CMD is unused.
ENTRYPOINT ["/init"]
