# Mimirheim — Home Assistant add-on image
#
# Adapted from container/Dockerfile for use as a HA add-on.
# Key differences from the standalone container image:
#
#   - Base image is the HA base-python image (Alpine), which ships with
#     s6-overlay v3, bashio, and tempio pre-installed.
#   - All apt-get calls are replaced with apk.
#   - The s6-overlay download block is removed (already in the base image).
#   - The non-root app user is removed (HA add-ons run as root by default).
#   - VOLUME is removed (HA manages mounts via config.yaml map entries).
#   - cont-init.d scripts are copied to inject MQTT credentials and service
#     enable flags from the Supervisor before any s6 service starts.
#   - Required OCI labels and HA build-arg labels are added.
#
# Build (HA builder action handles this automatically in CI):
#   docker buildx build --platform linux/amd64 \
#     --build-arg BUILD_FROM=ghcr.io/home-assistant/base-python:3.12-alpine3.21 \
#     -f hassio-repository/mimirheim/Dockerfile .

ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.12-alpine3.21
ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1: install Python dependencies into an isolated venv
# ---------------------------------------------------------------------------
FROM ${BUILD_FROM} AS builder

# Install uv — fast dependency installer.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Step 1: manifests only — resolved before source is present.
# This layer only rebuilds when pyproject.toml or uv.lock change, not on
# source edits, keeping third-party dependency installation cached separately.
COPY pyproject.toml uv.lock ./

RUN uv venv /app/.venv --python python${PYTHON_VERSION} && \
    UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --no-install-project --all-extras --frozen

# Step 2: source files — install the project wheel (no deps, already done).
COPY mimirheim/ ./mimirheim/
COPY mimirheim_helpers/ ./mimirheim_helpers/

RUN UV_PROJECT_ENVIRONMENT=/app/.venv uv sync --all-extras --frozen --no-editable

# ---------------------------------------------------------------------------
# Stage 2: final HA add-on image
# ---------------------------------------------------------------------------
FROM ${BUILD_FROM}

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
    org.opencontainers.image.source="https://github.com/jsimonetti/mimirheim" \
    org.opencontainers.image.version="${BUILD_VERSION}"

# Copy the venv from the builder stage.
COPY --from=builder /app/.venv /app/.venv

# Copy example configurations so users can bootstrap their /config mount.
COPY mimirheim/config/example.yaml /app/examples/mimirheim.yaml
COPY mimirheim_helpers/examples/ /app/examples/

# Copy s6-overlay service definitions.
COPY container/etc/ /etc/

# Copy cont-init.d scripts that inject Supervisor credentials.
COPY container/etc/cont-init.d/ /etc/cont-init.d/

# Remove the stray notification-fd file left from an earlier iteration;
# mimirheim does not implement the s6 readiness protocol.
RUN rm -f /etc/s6-overlay/s6-rc.d/mimirheim/notification-fd

# Make all run scripts and cont-init.d scripts executable.
RUN chmod +x \
    /etc/s6-overlay/s6-rc.d/mimirheim/run \
    /etc/s6-overlay/s6-rc.d/nordpool/run \
    /etc/s6-overlay/s6-rc.d/pv-fetcher/run \
    /etc/s6-overlay/s6-rc.d/pv-ml-learner/run \
    /etc/s6-overlay/s6-rc.d/baseload-ha/run \
    /etc/s6-overlay/s6-rc.d/baseload-ha-db/run \
    /etc/s6-overlay/s6-rc.d/baseload-static/run \
    /etc/s6-overlay/s6-rc.d/scheduler/run \
    /etc/s6-overlay/s6-rc.d/reporter/run \
    /etc/s6-overlay/s6-rc.d/config-editor/run \
    /etc/cont-init.d/00-options-env.sh \
    /etc/cont-init.d/01-mqtt-env.sh

# Expose the config editor port.
EXPOSE 8099

# s6-overlay is provided by the HA base image.
ENTRYPOINT ["/init"]
