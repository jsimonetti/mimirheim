# Mimirheim

Mimirheim is a MILP (mixed-integer linear programme) energy optimiser that
schedules a home battery, PV system, and EV charging against dynamic
electricity prices (e.g. Nordpool day-ahead). It runs as a persistent
background process, solving the optimisation problem periodically and
publishing the resulting schedule to your MQTT broker.

## Prerequisites

- A working MQTT broker integrated with Home Assistant (e.g. Mosquitto
  broker add-on). Mimirheim uses MQTT for all input and output.
- At minimum, a `mimirheim.yaml` configuration file describing your
  physical setup (battery, grid connection, tariffs).

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on store**.
2. Click the menu (⋮) and choose **Repositories**.
3. Add:
   ```
   https://github.com/jsimonetti/hassio-apps
   ```
4. Install **Mimirheim** from the store.

## Configuration files

All configuration files are placed in the add-on config directory,
accessible via the **File editor** add-on or Samba share
`\\<ha-host>\addon_configs\local_mimirheim`.

| File | Required | Purpose |
|------|----------|---------|
| `mimirheim.yaml` | Yes | Core solver configuration |
| `nordpool.yaml` | Optional | Nordpool price fetcher |
| `pv-fetcher.yaml` | Optional | forecast.solar PV forecasts |
| `pv-ml-learner.yaml` | Optional | ML-based PV forecast learner |
| `baseload-ha.yaml` | Optional | Baseload from HA REST API |
| `baseload-ha-db.yaml` | Optional | Baseload from HA SQLite database |
| `baseload-static.yaml` | Optional | Static baseload profile |
| `scheduler.yaml` | Optional | Cron-based MQTT scheduler |
| `reporter.yaml` | Optional | HTML report generator |
| `config-editor.yaml` | Optional | Web-based config editor |

Example configuration files are bundled in the image at `/app/examples/`.

## MQTT credentials

When MQTT integration is active in Home Assistant, the add-on reads the
broker credentials automatically from the Supervisor. You do not need to
include `mqtt.host`, `mqtt.port`, `mqtt.username`, or `mqtt.password` in
your YAML files. You must still set `mqtt.client_id` to a unique identifier
for each service.

## Enabling helpers

Each optional service (Nordpool, PV Fetcher, etc.) must be enabled
individually in the add-on options panel. Once enabled, place the
corresponding YAML file in the config directory and restart the add-on.

## Data directories

| Path | Content |
|------|---------|
| `/config/` | Your YAML configuration files |
| `/homeassistant/` | HA config directory (read-only; used by baseload-ha-db) |
| `/share/mimirheim/dumps/` | Solver debug dumps (viewable via Samba) |
| `/share/mimirheim/reports/` | HTML energy reports (viewable via Samba) |
| `/data/` | Trained ML model files (private to the add-on) |

## Config editor

When the **Config Editor** service is enabled, a web-based editor is
accessible via the HA sidebar (panel icon appears after restart). Changes
saved through the editor take effect on the next solver run.

## Upgrade procedure

Stable releases are version-tagged images. Upgrades follow the standard
HA add-on update flow: a notification appears in the store when a new
version is available, and clicking **Update** pulls the new image.
