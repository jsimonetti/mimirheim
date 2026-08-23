# syntax=docker/dockerfile:1
#
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
#     -v /path/to/your/configs:/config \
#     -e TZ=Europe/Amsterdam \
#     mimirheim
#
# /config must be writable. The config editor rewrites the YAML files in place,
# and zonneplan persists its refreshed OAuth token to
# /config/zonneplan_token.json by default. Both write atomically — create a
# temporary file in the directory, then rename it over the target — so write
# permission is needed on the directory itself, not only on the files.

# Changing this requires updating the SHA-256 digests in the final stage,
# which are published per release as .sha256 files alongside the tarballs.
ARG S6_OVERLAY_VERSION=3.2.3.2

# ---------------------------------------------------------------------------
# Pinned external images.
#
# Every image reference here carries an explicit digest. A bare tag such as
# `uv:latest` or `base:stable` is mutable: when upstream republishes it the
# cache key of the layer that copies from it changes, which invalidates
# everything below — including the ~650 MB dependency venv — even though
# nothing in this repository changed. Dependabot's `docker` ecosystem updates
# these digests, so bumps become reviewable commits rather than silent drift.
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uvbin
FROM ghcr.io/hassio-addons/base:stable@sha256:47ed45a406ec6a43666e00f1b6152406478e0816a2e353c743b3e3c77c248b37 AS bashio

# ---------------------------------------------------------------------------
# Stage 1: deps-only venv — rebuilt only when pyproject.toml or uv.lock change.
# Third-party packages are large; keeping them in a separate stage means a
# source-only change never invalidates this cached layer.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134 AS deps-builder

WORKDIR /build

COPY pyproject.toml uv.lock ./

# --no-dev matters: `uv sync` installs the `dev` dependency-group by default,
# which put pytest, ruff, rich, typer and amqtt (an MQTT *broker*) into the
# production image. Extras are unaffected, so --all-extras still installs every
# helper's runtime dependencies.
#
# cbcbox ships several prebuilt CBC distributions side by side. The debug
# builds are reachable only via CBCBOX_BUILD=debug and account for ~350 MB on
# both amd64 and arm64. cbcbox raises a clear RuntimeError if a debug build is
# requested and absent, so removing them fails loudly rather than silently.
# cbc_dist must stay: it is the fallback when the CPU has no AVX2 support.
#
# The interpreter is named explicitly and must match the python tag in the FROM
# lines. This used to read `python${PYTHON_VERSION}`, but PYTHON_VERSION was a
# global ARG declared before the first FROM, and Docker only exposes those to
# FROM instructions — inside a stage it expanded to the empty string, so the
# flag silently resolved to whatever `python` happened to be. Naming the
# interpreter means a base image bump that forgets this line fails the build
# instead of quietly building against a different interpreter.
RUN --mount=from=uvbin,source=/uv,target=/usr/local/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv --python python3.12 && \
    UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --all-extras --no-dev --frozen --no-install-project && \
    rm -rf /app/.venv/lib/python*/site-packages/cbcbox/cbc_dist_debug*

# ---------------------------------------------------------------------------
# Stage 2: wheel builder — rebuilt on every source change, but produces only
# a small wheel (~2 MB). No venv needed here; uv build is self-contained.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134 AS wheel-builder

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY mimirheim/ ./mimirheim/
COPY mimirheim_helpers/ ./mimirheim_helpers/

RUN --mount=from=uvbin,source=/uv,target=/usr/local/bin/uv \
    uv build --wheel --out-dir /app/wheels
    
# ---------------------------------------------------------------------------
# Stage 3: final image
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134

ARG S6_OVERLAY_VERSION
ARG TARGETARCH

# The noarch tarball is pinned by content: --checksum makes the layer's cache
# key deterministic and turns a tampered or truncated download into a build
# failure rather than a running container.
ADD --checksum=sha256:5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12 \
    https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz \
    /tmp/s6-overlay-noarch.tar.xz

# s6-overlay uses different architecture names than Docker's TARGETARCH.
# Map TARGETARCH (amd64, arm64) to the s6-overlay tarball name and its
# published SHA-256. ADD --checksum cannot be used here because the URL varies
# per architecture, so the digest is verified explicitly after download.
# Both sets of digests come from the .sha256 files attached to the
# v${S6_OVERLAY_VERSION} release and must be updated together with it.
RUN apt-get update && \
    apt-get install -y --no-install-recommends jq curl xz-utils wget && \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz && \
    case "${TARGETARCH}" in \
        amd64) S6_ARCH="x86_64";  S6_SHA256="e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408" ;; \
        arm64) S6_ARCH="aarch64"; S6_SHA256="b17f17a82e7a515c682a91edaf2ffdabb73f891981b6c1fd712115693a2f8b4c" ;; \
        arm)   S6_ARCH="armhf";   S6_SHA256="64fc0a4d8917658e6da5f5878e2e410e92b60faf3d39a9bef1452dabe031dd40" ;; \
        *)     echo "Unsupported TARGETARCH: ${TARGETARCH}"; exit 1 ;; \
    esac && \
    wget -q -O /tmp/s6-overlay-arch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" && \
    echo "${S6_SHA256}  /tmp/s6-overlay-arch.tar.xz" | sha256sum -c - && \
    tar -C / -Jxpf /tmp/s6-overlay-arch.tar.xz && \
    apt-get remove -y xz-utils wget && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* /tmp/s6-overlay-*.tar.xz

# Create a non-root user for all services.
RUN groupadd -g 1000 app && \
    useradd -u 1000 -g app -d /app -s /sbin/nologin app

# Copy the deps-only venv first. It is by far the largest layer, so it is
# placed above everything that changes more often than the dependency set:
# a bashio or uv bump must not force this layer to be materialised again.
# No --chown, because nothing runs as `app` and the services only read the
# venv; chowning it would rewrite ownership across the whole tree for nothing.
COPY --from=deps-builder /app/.venv /app/.venv

COPY --from=bashio /usr/lib/bashio /usr/lib/bashio
RUN ln -s /usr/lib/bashio/bashio /usr/bin/bashio

# Copy example configurations so users can bootstrap their /config mount.
# All helper examples live in mimirheim_helpers/examples/; the mimirheim core example
# comes from the mimirheim package.
COPY --chown=app:app mimirheim/config/example.yaml /app/examples/mimirheim.yaml
COPY --chown=app:app mimirheim_helpers/examples/ /app/examples/

# Copy s6-overlay service definitions. The run scripts and the hassio-env
# oneshot are mode 100755 in git, so COPY preserves the executable bit and no
# chmod layer is needed. A new service must be committed executable
# (git update-index --chmod=+x) or s6 will refuse to start it.
COPY --chown=root:root container/etc/ /etc/

# Install the project wheel into the existing venv — small layer (~2 MB), and the
# only layer a source-only commit invalidates, which is why it comes last.
# --no-deps ensures uv does not re-resolve or re-download anything. uv is
# bind-mounted rather than copied: it is a build-time tool, and copying it would
# both add a layer above the venv and ship ~35 MB of unused binary.
COPY --from=wheel-builder /app/wheels /app/wheels
RUN --mount=from=uvbin,source=/uv,target=/usr/local/bin/uv \
    uv pip install --python /app/.venv --no-deps /app/wheels/*.whl && \
    rm -rf /app/wheels

# /config is the mount point for user-supplied YAML configuration files.
VOLUME ["/config"]

# Expose the config editor port. Map it with -p 8099:8099 when not using host networking.
EXPOSE 8099

# s6-overlay init is the entrypoint; CMD is unused.
ENTRYPOINT ["/init"]
