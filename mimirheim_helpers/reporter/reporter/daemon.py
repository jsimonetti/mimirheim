"""ReporterDaemon — event-driven HTML report generation for mimirheim.

Subscribes to the mimirheim dump-available notification topic and renders an HTML
report for each new solve dump pair. Maintains ``inventory.js`` for use by
``index.html`` and garbage-collects old reports.

The daemon is event-driven rather than trigger-driven. It does not poll the
filesystem for new dumps; instead it reacts to MQTT notifications that mimirheim
publishes after each successful solve. Missed notifications are recovered via
a filesystem catch-up scan on startup.

Architecture:
    ReporterDaemon subclasses MqttDaemon, which provides MQTT connection
    management and signal handling. The daemon only subscribes to the
    dump-available notify_topic.

    The core logic lives in ``_on_notification``, registered as the on_message
    handler for the notify_topic subscription.

What this module does not do:
- It does not write dump JSON files. mimirheim writes those.
- It does not serve files over HTTP. A separate static file server is required.
- It does not subscribe to ``homeassistant/status`` beyond what the base class
  handles automatically.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from helper_common.daemon import MqttDaemon

from reporter import gc, inventory
from reporter.chart_publisher import build_chart_payload, build_summary_payload
from reporter.config import ChartPublishingConfig, ReporterConfig
from reporter.render import build_report_html

logger = logging.getLogger(__name__)

# Names of static files copied into output_dir on first startup.
_INDEX_FILENAME = "index.html"
_INDEX_CSS_FILENAME = "index.css"
_PLOTLY_JS_FILENAME = "plotly.min.js"


class ReporterDaemon(MqttDaemon):
    """Event-driven HTML report generator for mimirheim solve dumps.

    Subclasses ``MqttDaemon`` for MQTT lifecycle management. Subscribes
    exclusively to the dump-available notification topic.

    On every dump-available notification:
    1. Parse the JSON payload; validate ``ts``, ``input_path``,
       ``output_path`` keys.
    2. Verify both source files exist.
    3. Skip silently if the corresponding report HTML already exists.
    4. Render the HTML report via ``render.build_report_html``.
    5. Write the HTML file.
    7. Run garbage collection via ``gc.collect()``.

    On startup:
    1. Copy ``index.html`` into output_dir if it does not already exist.
    2. Catch-up scan: process all dump pairs that have no corresponding report.
    3. ``inventory.rebuild_from_disk()`` to ensure ``inventory.js`` is complete.
    4. ``gc.collect()`` once to enforce the retention limit.

    """

    def __init__(self, config: ReporterConfig) -> None:
        """Initialise the reporter daemon.

        Args:
            config: Validated reporter configuration.
        """
        super().__init__(config)
        self._reporter_config = config.reporting
        self._chart_config = config.chart_publishing
        self._discovery_config = config.ha_discovery

    # ------------------------------------------------------------------
    # Public entry point (extends HelperDaemon.run)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run startup tasks then delegate to the base class run loop.

        Startup tasks (called before entering the MQTT loop):
        - Ensure output_dir exists.
        - Copy index.html from package static/ into output_dir if absent.
        - Catch-up: render any dump pairs that have no report yet.
        - Rebuild inventory.js from disk.
        - Run GC once.
        """
        cfg = self._reporter_config
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        self._install_index_html()
        self._catch_up()
        inventory.rebuild_from_disk(cfg.output_dir, cfg.dump_dir)
        gc.collect(cfg.output_dir, cfg.max_reports)
        super().run()

    # ------------------------------------------------------------------
    # MqttDaemon overrides
    # ------------------------------------------------------------------

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Subscribe to the dump-available notify_topic on connect.

        When HA discovery is enabled, also subscribes to
        ``homeassistant/status`` and publishes the chart discovery payload.

        Args:
            client: The connected paho MQTT client.
            userdata: Unused.
            flags: Unused.
            reason_code: MQTT connect reason code.
            properties: Unused.
        """
        super()._on_connect(client, userdata, flags, reason_code, properties)
        if reason_code != 0:
            return
        notify_topic = self._reporter_config.notify_topic
        client.subscribe(notify_topic, qos=0)
        logger.info("Subscribed to dump-available notifications on %r.", notify_topic)
        disc = self._discovery_config
        if disc is not None and disc.enabled:
            client.subscribe("homeassistant/status", qos=0)
            self._publish_chart_discovery(client)

    def _on_message(
        self,
        client: mqtt.Client,
        userdata: Any,
        message: Any,
    ) -> None:
        """Route dump-available notifications and HA birth messages."""
        if message.topic == self._reporter_config.notify_topic:
            self._on_notification(message)
        elif (
            message.topic == "homeassistant/status"
            and message.payload == b"online"
        ):
            disc = self._discovery_config
            if disc is not None and disc.enabled:
                self._publish_chart_discovery(client)

    # ------------------------------------------------------------------
    # Notification handler
    # ------------------------------------------------------------------

    def _on_notification(self, message: Any) -> None:
        """Handle one dump-available notification.

        Parses the JSON payload, validates it, checks the report does not
        already exist, renders the HTML, updates the inventory, and runs GC.

        On any error (bad payload, missing files, render failure), logs a
        warning and returns without raising. Crashing the paho callback thread
        on a bad payload would stop the daemon entirely.

        Args:
            message: Received MQTT message with JSON payload.
        """
        cfg = self._reporter_config

        # Parse and validate the payload.
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Could not parse notification payload: %s", exc)
            return

        ts = payload.get("ts")
        input_path_str = payload.get("input_path")
        output_path_str = payload.get("output_path")

        if not ts or not input_path_str or not output_path_str:
            logger.warning(
                "Notification payload is missing required keys "
                "(ts, input_path, output_path): %r",
                payload,
            )
            return

        input_path = Path(input_path_str)
        output_dump_path = Path(output_path_str)

        # Determine the safe_ts (hyphens in time part, no colons) for the HTML filename.
        safe_ts = _iso_to_safe_ts(ts)
        report_filename = f"{safe_ts}_report.html"
        report_path = cfg.output_dir / report_filename

        if not input_path.exists():
            logger.warning("Input dump file does not exist: %s", input_path)
            return
        if not output_dump_path.exists():
            logger.warning("Output dump file does not exist: %s", output_dump_path)
            return

        # Read the dump files here so that both the HTML render and the chart
        # publish step can use the same parsed data without reading twice.
        try:
            inp = json.loads(input_path.read_text())
            out = json.loads(output_dump_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read dump pair for %s: %s", ts, exc)
            return

        if report_path.exists():
            # Report already rendered (e.g. from a previous run or catch-up).
            # Still update the inventory in case inventory.js was incomplete —
            # for example when the daemon restarted after dump files were deleted
            # and stub entries were written by rebuild_from_disk.
            inventory.update(cfg.output_dir, ts, report_filename, inp, out)
        else:
            self._render_and_save(ts, report_filename, report_path, inp, out)

        self._publish_chart_data(inp, out)

    def _render_and_save(
        self,
        ts: str,
        report_filename: str,
        report_path: Path,
        inp: dict[str, Any],
        out: dict[str, Any],
    ) -> None:
        """Render a dump pair and write the HTML report.

        Separated from ``_on_notification`` so that catch-up can call it with
        the same error-handling contract: log and continue on failure, never raise.

        The dump dicts are accepted pre-parsed so the caller can also use them
        for the chart publish step without reading the files twice.

        Args:
            ts: ISO 8601 timestamp string for the inventory entry.
            report_filename: Filename of the output HTML report.
            report_path: Full path to the output HTML report.
            inp: Parsed SolveBundle JSON.
            out: Parsed SolveResult JSON.
        """
        cfg = self._reporter_config

        try:
            html_content = build_report_html(inp, out)
            report_path.write_text(html_content, encoding="utf-8")
        except Exception:
            logger.exception("Failed to render report for %s.", ts)
            return

        inventory.update(cfg.output_dir, ts, report_filename, inp, out)
        gc.collect(cfg.output_dir, cfg.max_reports)
        self._install_index_html()
        logger.info("Rendered report: %s", report_path)

    # ------------------------------------------------------------------
    # Chart and discovery helpers
    # ------------------------------------------------------------------

    def _publish_chart_data(self, inp: dict[str, Any], out: dict[str, Any]) -> None:
        """Publish chart and summary payloads to configured MQTT topics.

        Skips publication if the serialised chart payload exceeds
        ``max_payload_bytes``. Summary payloads are always published when a
        topic is configured because they are compact scalar objects.

        Args:
            inp: Parsed SolveBundle JSON.
            out: Parsed SolveResult JSON.
        """
        cfg = self._chart_config
        if cfg.chart_topic is not None:
            payload = json.dumps(build_chart_payload(inp, out))
            if cfg.max_payload_bytes > 0 and len(payload.encode()) > cfg.max_payload_bytes:
                logger.warning(
                    "Chart payload (%d bytes) exceeds max_payload_bytes=%d; skipping.",
                    len(payload.encode()),
                    cfg.max_payload_bytes,
                )
            else:
                self._client.publish(cfg.chart_topic, payload, qos=1, retain=True)

        if cfg.summary_topic is not None:
            payload = json.dumps(build_summary_payload(inp, out))
            self._client.publish(cfg.summary_topic, payload, qos=1, retain=True)

    def _publish_chart_discovery(self, client: Any) -> None:
        """Publish a single HA device JSON discovery payload for reporter sensors.

        Publishes to ``{discovery_prefix}/device/{device_id}/config`` with a
        ``components`` map containing one sensor per configured topic:

        - ``{device_id}_chart_series``: state from ``chart_topic``; all series
          data available via ``json_attributes_topic``.
        - ``{device_id}_summary``: state from ``summary_topic``; all scalar
          fields available via ``json_attributes_topic``.

        This method is a no-op if discovery is None or disabled, or if neither
        chart_topic nor summary_topic is configured.

        Args:
            client: The connected paho MQTT client.
        """
        disc = self._discovery_config
        if disc is None or not disc.enabled:
            return
        chart_cfg = self._chart_config
        if chart_cfg.chart_topic is None and chart_cfg.summary_topic is None:
            return

        mqtt_cfg = self._config.mqtt
        device_id = disc.device_id or mqtt_cfg.client_id
        disc_prefix = disc.discovery_prefix

        components: dict[str, dict[str, Any]] = {}

        if chart_cfg.chart_topic is not None:
            uid = f"{device_id}_chart_series"
            components[uid] = {
                "platform": "sensor",
                "unique_id": uid,
                "name": "mimirheim Chart Series",
                "state_topic": chart_cfg.chart_topic,
                "value_template": "{{ value_json.solve_time_utc }}",
                "json_attributes_topic": chart_cfg.chart_topic,
                "entity_category": "diagnostic",
            }

        if chart_cfg.summary_topic is not None:
            uid = f"{device_id}_summary"
            components[uid] = {
                "platform": "sensor",
                "unique_id": uid,
                "name": "mimirheim Summary",
                "state_topic": chart_cfg.summary_topic,
                "value_template": "{{ value_json.solve_time_utc }}",
                "json_attributes_topic": chart_cfg.summary_topic,
                "entity_category": "diagnostic",
            }

        payload = {
            "device": {
                "identifiers": [device_id],
                "name": disc.device_name,
                "manufacturer": "Mimirheim",
            },
            "origin": {"name": "mimirheim-reporter"},
            "availability": [],
            "components": components,
        }
        topic = f"{disc_prefix}/device/{device_id}/config"
        client.publish(topic, json.dumps(payload), qos=1, retain=True)
        logger.info(
            "Published HA reporter discovery to %s with %d component(s).",
            topic,
            len(components),
        )

    # ------------------------------------------------------------------
    # Startup helpers
    # ------------------------------------------------------------------

    def _install_index_html(self) -> None:
        """Copy static web assets into output_dir if they are not already present.

        Installs three files:
        - ``index.html``: the report index page (from the package static/ directory).
        - ``index.css``: the stylesheet used by index.html (from static/).
        - ``plotly.min.js``: the Plotly JS library used by every report HTML file
          (sourced from the plotly Python package's own package_data).

        None of the files are overwritten if they already exist, so operators can
        customise index.html without it being reset on restart.
        """
        cfg = self._reporter_config
        static_dir = Path(__file__).parent / "static"

        for filename in (_INDEX_FILENAME, _INDEX_CSS_FILENAME):
            dest = cfg.output_dir / filename
            if dest.exists():
                continue
            src = static_dir / filename
            if not src.exists():
                logger.warning("Package static/%s not found; skipping install.", filename)
                continue
            dest.write_bytes(src.read_bytes())
            logger.info("Installed %s into %s.", filename, cfg.output_dir)

        plotly_dest = cfg.output_dir / _PLOTLY_JS_FILENAME
        if not plotly_dest.exists():
            self._install_plotly_js(plotly_dest)

    def _install_plotly_js(self, dest: Path) -> None:
        """Copy plotly.min.js from the plotly package data into output_dir.

        The plotly Python package ships its own minified JS bundle inside
        ``plotly/package_data/plotly.min.js``.  We copy that file rather than
        downloading it from the network, so the reporter has no external
        dependencies at runtime.

        Args:
            dest: Destination path to write plotly.min.js to.
        """
        try:
            import plotly as _plotly  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "plotly package is not installed; cannot install plotly.min.js."
            )
            return

        src = Path(_plotly.__file__).parent / "package_data" / _PLOTLY_JS_FILENAME
        if not src.exists():
            logger.warning(
                "plotly package_data/plotly.min.js not found at %s; skipping.", src
            )
            return

        dest.write_bytes(src.read_bytes())
        logger.info("Installed plotly.min.js into %s.", dest.parent)

    def _catch_up(self) -> None:
        """Render any dump pairs in dump_dir that have no corresponding report.

        Scans dump_dir for all ``*_input.json`` / ``*_output.json`` pairs,
        sorted newest-first. For each pair without a matching
        ``output_dir/{safe_ts}_report.html``, calls ``_render_and_save``.

        Newest-first ordering means the most recent solves are processed first,
        which is useful if the daemon starts with a large backlog and the operator
        prefers recent reports to appear quickly.
        """
        cfg = self._reporter_config
        input_files = sorted(
            cfg.dump_dir.glob("*_input.json"),
            reverse=True,  # newest-first
        )
        caught_up = 0
        for input_path in input_files:
            ts_file = input_path.name[: -len("_input.json")]
            output_dump_path = cfg.dump_dir / f"{ts_file}_output.json"
            if not output_dump_path.exists():
                continue
            report_path = cfg.output_dir / f"{ts_file}_report.html"
            if report_path.exists():
                continue
            if "T" in ts_file:
                date_part, time_part = ts_file.split("T", 1)
                ts = date_part + "T" + time_part.replace("-", ":", 2)
            else:
                ts = ts_file
            try:
                inp = json.loads(input_path.read_text())
                out = json.loads(output_dump_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not read dump pair for %s: %s", ts, exc)
                continue
            report_filename = f"{ts_file}_report.html"
            self._render_and_save(ts, report_filename, report_path, inp, out)
            caught_up += 1

        if caught_up:
            logger.info("Catch-up complete: rendered %d new report(s).", caught_up)
        else:
            logger.info("Catch-up: no new dump pairs to process.")


def _iso_to_safe_ts(ts: str) -> str:
    """Convert an ISO 8601 timestamp to a filesystem-safe string.

    Replaces colons in the time portion with hyphens so the string can be
    used as a filename component on systems that disallow colons.

    For example: ``"2026-04-02T14:00:00Z"`` becomes ``"2026-04-02T14-00-00Z"``.

    Args:
        ts: ISO 8601 timestamp string.

    Returns:
        A filesystem-safe version of the timestamp.
    """
    if "T" in ts:
        date_part, time_part = ts.split("T", 1)
        return date_part + "T" + time_part.replace(":", "-")
    return ts.replace(":", "-")
