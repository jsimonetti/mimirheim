# Mimirheim Add-on Repository

Home Assistant add-on repository for [mimirheim](https://github.com/jsimonetti/mimirheim) —
a MILP energy optimiser that schedules home battery, PV, and EV charging against
dynamic electricity prices.

## Add-ons

| Add-on | Description |
|--------|-------------|
| [Mimirheim](mimirheim/) | Stable release channel |
| [Mimirheim (Beta)](mimirheim-beta/) | Edge channel — rebuilt on every commit to `main` |

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on store**.
2. Click the menu (⋮) in the top right and choose **Repositories**.
3. Add the URL:
   ```
   https://github.com/jsimonetti/hassio-apps
   ```
4. Install **Mimirheim** from the store.

## Configuration

After installation, place your `mimirheim.yaml` (and any helper YAML files you
want to enable) in the add-on configuration directory. This directory is
accessible via the **File editor** add-on or over **Samba** at
`\\<ha-host>\addon_configs\<slug>`.

See [DOCS.md](mimirheim/DOCS.md) for full configuration instructions.
