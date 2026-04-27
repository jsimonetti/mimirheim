"""Configuration schema for mimirheim.

This module defines all Pydantic models that represent the mimirheim YAML
configuration file. It is the single source of truth for field names, types,
constraints, and defaults.

It does not import from mimirheim.core or mimirheim.io. Config flows downward:
IO loads config, devices receive config models as constructor arguments.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

import helper_common.topics as _topics

# ---------------------------------------------------------------------------
# Infrastructure: MQTT connection and required global settings
# ---------------------------------------------------------------------------

class MqttConfig(BaseModel):
    """MQTT broker connection parameters.

    Attributes:
        host: Broker hostname or IP address.
        port: Broker port. Default is 1883 (unencrypted); use 8883 with TLS.
        client_id: MQTT client identifier. Must be unique on the broker.
        topic_prefix: Prefix applied to all mimirheim topics. Default is 'mimir'.
        username: Optional broker username. Omit for anonymous access.
        password: Optional broker password. Only meaningful when username is set.
        tls: Enable TLS for the broker connection. Set to True when the broker
            listens on an encrypted port (typically 8883). When False, a
            plaintext connection is made regardless of the port number.
        tls_allow_insecure: When True and tls is also True, skip broker
            certificate verification. Useful for self-signed certificates on
            private networks. Has no effect when tls is False. Do not use
            against a broker reachable from an untrusted network.
    """

    model_config = ConfigDict(extra="forbid")

    host: str = Field(description="MQTT broker hostname or IP address.", json_schema_extra={"ui_label": "Broker host", "ui_group": "basic"})
    port: int = Field(default=1883, description="MQTT broker port.", json_schema_extra={"ui_label": "Broker port", "ui_group": "advanced"})
    client_id: str | None = Field(default=None, description="MQTT client identifier. Defaults to 'mimir' when not set.", json_schema_extra={"ui_label": "Client ID", "ui_group": "basic"})
    topic_prefix: str = Field(default="mimir", description="Topic prefix for all mimirheim topics.", json_schema_extra={"ui_label": "Topic prefix", "ui_group": "advanced"})
    username: str | None = Field(default=None, description="Broker username. Omit for anonymous access.", json_schema_extra={"ui_label": "Username", "ui_group": "advanced"})
    password: str | None = Field(default=None, description="Broker password.", json_schema_extra={"ui_label": "Password", "ui_group": "advanced"})
    tls: bool = Field(
        default=False,
        description="Enable TLS for the broker connection. Set to true when the broker listens on an encrypted port (typically 8883).",
        json_schema_extra={"ui_label": "Enable TLS", "ui_group": "advanced"},
    )
    tls_allow_insecure: bool = Field(
        default=False,
        description="Skip broker certificate verification when TLS is enabled. Useful for self-signed certificates on private networks. Has no effect when tls is false.",
        json_schema_extra={"ui_label": "Allow insecure TLS", "ui_group": "advanced"},
    )

class GridConfig(BaseModel):
    """Configuration for the grid connection.

    There is exactly one grid connection per mimirheim instance (a single grid:
    section, not a named map). It defines the physical limits of what can be
    imported from or exported to the public grid.

    Attributes:
        import_limit_kw: Maximum power that can be drawn from the grid, in kW.
            Determined by the DNO connection agreement or main fuse rating.
        export_limit_kw: Maximum power that can be fed into the grid, in kW.
            May be zero if the connection agreement forbids export.
    """

    model_config = ConfigDict(extra="forbid")

    import_limit_kw: float = Field(ge=0, description="Maximum grid import power in kW.", json_schema_extra={"ui_label": "Import limit (kW)", "ui_group": "basic"})
    export_limit_kw: float = Field(ge=0, description="Maximum grid export power in kW.", json_schema_extra={"ui_label": "Export limit (kW)", "ui_group": "basic"})

# ---------------------------------------------------------------------------
# Strategy, objective, and solver configuration
# ---------------------------------------------------------------------------

class BalancedWeightsConfig(BaseModel):
    """Objective weights used when strategy is 'balanced'.

    Controls the relative importance of cost minimisation versus grid
    independence. Only the ratio matters — weights are normalised before use.

    Attributes:
        cost_weight: Weight applied to the revenue / import-cost objective term.
        self_sufficiency_weight: Weight applied to the grid import penalty term.
    """

    model_config = ConfigDict(extra="forbid")

    cost_weight: float = Field(ge=0, default=1.0, description="Weight on cost/revenue terms.", json_schema_extra={"ui_label": "Cost weight", "ui_group": "advanced"})
    self_sufficiency_weight: float = Field(
        ge=0, default=1.0, description="Weight on grid import penalty terms.", json_schema_extra={"ui_label": "Self-sufficiency weight", "ui_group": "advanced"}
    )

class ObjectivesConfig(BaseModel):
    """Objective function parameters.

    The active strategy is read from the MQTT topic mimir/input/strategy at
    runtime, not from this config section. This section contains only the
    parameters that tune objective behaviour — specifically the weights used
    when the balanced strategy is active.

    Attributes:
        balanced_weights: Weights for the 'balanced' strategy. If None, both
            cost and self-sufficiency weights default to 1.0.
        min_dispatch_gain_eur: Minimum cost benefit in EUR required to dispatch
            storage devices. When the projected saving over the naive baseline
            (base load covered by grid, storage idle) is below this value,
            mimirheim publishes an idle schedule instead. Set to 0.0 (the default)
            to disable the check. Applies only to ``minimize_cost`` and
            ``balanced`` strategies. Has no effect when an EV with an active
            charge deadline is connected, or when a deferrable load has an
            active scheduling window.
        exchange_shaping_weight: Weight for the optional secondary exchange-
            minimisation term ``lambda * sum_t(import_t + export_t)``. When
            set to a small positive value (e.g. 1e-4), this term breaks solver
            indifference among solutions with equivalent primary cost by
            favouring lower total exchange volume. Must be orders of magnitude
            smaller than typical prices to avoid distorting the economic
            objective. Default 0.0 disables the term completely.
    """

    model_config = ConfigDict(extra="forbid")

    balanced_weights: BalancedWeightsConfig | None = Field(
        default=None,
        description="Objective weights for the balanced strategy. Null = equal weighting.",
        json_schema_extra={"ui_label": "Balanced strategy weights", "ui_group": "advanced"},
    )
    min_dispatch_gain_eur: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Minimum benefit in EUR over the naive baseline required for mimirheim to dispatch "
            "storage. Below this threshold an idle schedule is published instead. "
            "0.0 (default) disables the check. Applies to minimize_cost and balanced only."
        ),
        json_schema_extra={"ui_label": "Minimum dispatch gain (\u20ac)", "ui_group": "advanced"},
    )
    exchange_shaping_weight: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Weight for the optional secondary exchange-minimisation term "
            "lambda * sum_t(import_t + export_t). 0.0 (default) disables the term. "
            "Use a value orders of magnitude smaller than typical energy prices "
            "(e.g. 1e-4) to break solver indifference without distorting the "
            "primary economic objective."
        ),
        json_schema_extra={"ui_label": "Exchange shaping weight", "ui_group": "advanced"},
    )

class ConstraintsConfig(BaseModel):
    """Hard constraints on grid power, applied independently of strategy.

    These are enforced as inviolable solver constraints, not as penalty terms.
    They apply across the full planning horizon.

    Attributes:
        max_import_kw: Hard cap on grid import at any time step, in kW.
            Null means no cap beyond the physical grid limit.
        max_export_kw: Hard cap on grid export at any time step, in kW.
            Set to 0.0 to enforce zero export across the horizon.
    """

    model_config = ConfigDict(extra="forbid")

    max_import_kw: float | None = Field(default=None, description="Hard cap on grid import in kW.", json_schema_extra={"ui_label": "Max import cap (kW)", "ui_group": "advanced"})
    max_export_kw: float | None = Field(default=None, description="Hard cap on grid export in kW.", json_schema_extra={"ui_label": "Max export cap (kW)", "ui_group": "advanced"})

class SolverConfig(BaseModel):
    """Solver tuning parameters for the CBC MILP backend.

    Controls resource limits and performance characteristics of the solver.
    These settings do not affect solution correctness — only how quickly the
    solver finds and proves the optimal schedule.

    Attributes:
        max_horizon_steps: Hard cap on the number of 15-minute steps passed to
            the solver per cycle. At 15-minute resolution: 96 = 24 h, 192 = 48 h,
            288 = 72 h. When the available forecast horizon exceeds this cap, the
            bundle is trimmed to this length before building the model. A longer
            horizon improves schedule quality but increases MILP complexity
            exponentially for binary-variable devices such as thermal heat pumps.
            Minimum 96 (24 h). Default 288 (72 h).
        threads: Number of parallel threads CBC may use during branch-and-bound.
            -1 means use all available CPU cores. On a dedicated home server that
            is otherwise idle between solve cycles, -1 is the recommended setting.
            Default -1.
    """

    model_config = ConfigDict(extra="forbid")

    max_horizon_steps: int = Field(
        default=288,
        ge=96,
        description=(
            "Hard cap on the solve horizon in 15-minute steps. "
            "96 = 24 h, 192 = 48 h, 288 = 72 h. "
            "Bundles longer than this are trimmed before building the model. "
            "Minimum 96 (24 h) to ensure the schedule covers at least one full day."
        ),
        json_schema_extra={"ui_label": "Max horizon steps", "ui_group": "advanced"},
    )
    threads: int = Field(
        default=-1,
        ge=-1,
        description=(
            "CBC solver threads. -1 = use all available CPU cores. "
            "1 = single-threaded."
        ),
        json_schema_extra={"ui_label": "Solver threads", "ui_group": "advanced"},
    )

class ReadinessConfig(BaseModel):
    """Tuning parameters for forecast coverage checks before a solve.

    mimirheim does not require 24 hours of forecast data to solve. Instead, it
    solves over whatever horizon all forecasts jointly cover. These settings
    control when that coverage is deemed sufficient to solve, when to warn,
    and what constitutes a gap in the data.

    Attributes:
        min_horizon_hours: Minimum hours of joint forecast coverage required
            to attempt a solve at all. If the available horizon falls below
            this threshold, mimirheim will not solve and will log a blocking message.
            Default 1 hour: any non-trivial forecast window allows solving.
        warn_below_hours: If the available horizon is below this value (but
            still above min_horizon_hours), mimirheim logs a warning on every solve
            but still solves. Default 8 hours. For example, day-ahead Nordpool
            prices published at 13:00 CET give about 11 hours until midnight,
            which is above this threshold. If forecast.solar data only covers
            the next 4 hours due to a missed refresh, the warning will fire.
        max_gap_hours: Any gap between consecutive data points within the
            computed horizon that exceeds this value triggers a warning. The
            gap is filled by interpolation regardless; the warning signals that
            the data source may have failed a refresh cycle. Default 2 hours.
    """

    model_config = ConfigDict(extra="forbid")

    min_horizon_hours: float = Field(
        ge=0,
        default=1.0,
        description="Minimum forecast coverage in hours to attempt a solve.",
        json_schema_extra={"ui_label": "Minimum horizon (h)", "ui_group": "advanced"},
    )
    warn_below_hours: float = Field(
        ge=0,
        default=8.0,
        description="Log a warning when available horizon is below this value in hours.",
        json_schema_extra={"ui_label": "Warn below (h)", "ui_group": "advanced"},
    )
    max_gap_hours: float = Field(
        ge=0,
        default=2.0,
        description="Log a warning when any gap between consecutive forecast points exceeds this value in hours.",
        json_schema_extra={"ui_label": "Max gap (h)", "ui_group": "advanced"},
    )

class ControlConfig(BaseModel):
    """Parameters for the mode-arbitration and enforcer-selection engine.

    These values govern how ``assign_control_authority`` selects the single
    closed-loop enforcer device for each time step. All fields are optional;
    the defaults are conservative values suitable for a typical residential
    installation.

    Attributes:
        exchange_epsilon_kw: Any grid exchange below this threshold (kW) is
            treated as near-zero for the purpose of enforcer activation.
            Steps where both import and export are below this value trigger
            enforcer selection; steps above it clear all enforcer flags.
            Default 0.05 kW (50 W).
        headroom_margin_kw: Minimum absorption headroom a device must have
            to be eligible as enforcer. Devices with less headroom than this
            value at their current operating point are excluded from
            candidacy. Prevents selecting a nearly-full battery as enforcer.
            Default 0.10 kW (100 W).
        switch_delta: A challenger device must exceed the current enforcer's
            score by at least this amount before a switch is permitted.
            Prevents rapid oscillation when two devices have nearly identical
            scores. Default 0.05.
        min_enforcer_dwell_steps: Once a device is selected as enforcer, it
            remains enforcer for at least this many consecutive steps, unless
            it becomes ineligible (e.g. EV unplugs, headroom drops below
            margin). Default 2 steps (30 minutes at 15-minute resolution).
    """

    model_config = ConfigDict(extra="forbid")

    exchange_epsilon_kw: float = Field(
        default=0.05,
        ge=0.0,
        description=(
            "Grid exchange below this value in kW is treated as near-zero. "
            "Enforcer activation applies only to steps below this threshold."
        ),
        json_schema_extra={"ui_label": "Exchange epsilon (kW)", "ui_group": "advanced"},
    )
    headroom_margin_kw: float = Field(
        default=0.10,
        ge=0.0,
        description=(
            "Minimum absorption headroom in kW for a device to be eligible as enforcer. "
            "Devices below this threshold at their current operating point are excluded."
        ),
        json_schema_extra={"ui_label": "Headroom margin (kW)", "ui_group": "advanced"},
    )
    switch_delta: float = Field(
        default=0.05,
        ge=0.0,
        description=(
            "Challenger must exceed current enforcer score by this amount to trigger a switch. "
            "Prevents oscillation when two devices score similarly."
        ),
        json_schema_extra={"ui_label": "Switch delta", "ui_group": "advanced"},
    )
    min_enforcer_dwell_steps: int = Field(
        default=2,
        ge=1,
        description=(
            "Minimum consecutive steps a device remains enforcer once selected. "
            "Device becomes ineligible immediately if it loses availability or headroom."
        ),
        json_schema_extra={"ui_label": "Min enforcer dwell steps", "ui_group": "advanced"},
    )

# ---------------------------------------------------------------------------
# I/O topic overrides and integrations
# ---------------------------------------------------------------------------

class InputsConfig(BaseModel):
    """MQTT input topic configuration.

    These are the topics on which mimirheim receives data from external tools and
    home automation integrations. All topics are retained by publishers so
    mimirheim receives the latest value immediately on (re)connect.

    Attributes:
        prices: Topic on which per-step import and export price data is
            published. Defaults to ``{mqtt.topic_prefix}/input/prices`` when
            not set. Set explicitly when the topic does not match the default
            pattern, for example when multiple mimirheim instances share one broker
            but publish prices to a shared topic.
    """

    model_config = ConfigDict(extra="forbid")

    prices: str | None = Field(
        default=None,
        description=(
            "Topic for per-step price data. Defaults to "
            "'{mqtt.topic_prefix}/input/prices' when not set."
        ),
        json_schema_extra={"ui_label": "Prices topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/input/prices"},
    )

class OutputsConfig(BaseModel):
    """MQTT output topic configuration.

    Attributes:
        schedule: Topic for the full horizon schedule JSON.
        current: Topic for the current-step strategy summary.
        last_solve: Topic for the retained solve status message.
        availability: Topic for birth and last-will messages. mimirheim publishes
            ``"online"`` here (retained, qos=1) on every successful broker
            connection and registers ``"offline"`` as the MQTT last-will so the
            broker publishes it automatically if the client disconnects without
            a clean shutdown.
    """

    model_config = ConfigDict(extra="forbid")

    schedule: str | None = Field(
        default=None,
        description=(
            "Topic for the full horizon schedule. "
            "Defaults to '{mqtt.topic_prefix}/strategy/schedule'."
        ),
        json_schema_extra={"ui_label": "Schedule topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/strategy/schedule"},
    )
    current: str | None = Field(
        default=None,
        description=(
            "Topic for the current-step strategy summary. "
            "Defaults to '{mqtt.topic_prefix}/strategy/current'."
        ),
        json_schema_extra={"ui_label": "Current topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/strategy/current"},
    )
    last_solve: str | None = Field(
        default=None,
        description=(
            "Topic for the retained solve-status message. "
            "Defaults to '{mqtt.topic_prefix}/status/last_solve'."
        ),
        json_schema_extra={"ui_label": "Last solve topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/status/last_solve"},
    )
    availability: str | None = Field(
        default=None,
        description=(
            "Topic for birth ('online') and last-will ('offline') messages. "
            "Defaults to '{mqtt.topic_prefix}/status/availability'."
        ),
        json_schema_extra={"ui_label": "Availability topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/status/availability"},
    )

class HomeAssistantConfig(BaseModel):
    """Configuration for Home Assistant MQTT discovery.

    When enabled, mimirheim publishes MQTT discovery payloads to the configured
    discovery prefix on every broker connection. Home Assistant picks these up
    automatically and creates entities for the schedule outputs and per-device
    setpoints without any manual YAML configuration in HA.

    See: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery

    Attributes:
        enabled: Whether to publish discovery payloads. Defaults to False so
            that existing mimirheim deployments without HA are not affected.
        discovery_prefix: The MQTT topic prefix used by Home Assistant for
            discovery. Defaults to ``"homeassistant"``. Change only if your HA
            instance uses a non-default discovery prefix.
        device_name: Human-readable name for the mimirheim device in HA. All entities
            published by this mimirheim instance are grouped under this device in the
            HA device registry.
        device_id: Unique identifier for the device in the HA device registry.
            If omitted, defaults to the MQTT ``client_id``. Must be stable
            across restarts; changing it creates a new device in HA.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable HA MQTT discovery. Default: false.", json_schema_extra={"ui_label": "Enable HA discovery", "ui_group": "advanced"})
    discovery_prefix: str = Field(
        default="homeassistant",
        description="Topic prefix used by HA for discovery. Default: 'homeassistant'.",
        json_schema_extra={"ui_label": "Discovery prefix", "ui_group": "advanced"},
    )
    device_name: str = Field(
        default="mimir",
        description="Human-readable device name shown in HA.",
        json_schema_extra={"ui_label": "HA device name", "ui_group": "advanced"},
    )
    device_id: str | None = Field(
        default=None,
        description="Stable device identifier for the HA device registry. Defaults to mqtt.client_id.",
        json_schema_extra={"ui_label": "HA device ID", "ui_group": "advanced"},
    )

class DebugConfig(BaseModel):
    """Debug and diagnostic configuration.

    Attributes:
        enabled: When True, sets the root logger to DEBUG level at startup.
            This causes the application to emit verbose log output and also
            enables solve dump file writing when dump_dir is set.
        dump_dir: Directory to write solve dump files (input + output JSON).
            Dumps are written after every successful solve when enabled is True
            and dump_dir is not None. Null disables file writing entirely.
        max_dumps: Maximum number of dump file pairs to retain. Oldest files are
            removed when the limit is exceeded. 0 means unlimited.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable DEBUG logging and solve dumps.", json_schema_extra={"ui_label": "Enable debug dumps", "ui_group": "advanced"})
    dump_dir: Path | None = Field(
        default=None,
        description="Directory for solve dumps. Null = disabled.",
        json_schema_extra={"ui_label": "Dump directory", "ui_group": "advanced"},
    )
    max_dumps: int = Field(ge=0, default=50, description="Maximum retained dump pairs.", json_schema_extra={"ui_label": "Max debug dumps", "ui_group": "advanced"})

class ReportingConfig(BaseModel):
    """Configuration for the standalone mimirheim-reporter daemon.

    This section is independent of ``debug``. Setting ``debug.enabled = false``
    does not suppress reporting; the two systems serve different purposes.

    ``debug`` controls log verbosity and ad-hoc developer dumps. ``reporting``
    controls the production-grade report archive consumed by mimirheim-reporter.

    Attributes:
        enabled: When True, mimirheim writes a dump file pair (input + output JSON)
            after every successful solve and publishes a small JSON notification
            to ``notify_topic``. Requires ``dump_dir`` to be set.
        dump_dir: Directory to write solve dump files. The same directory should
            be mounted into the mimirheim-reporter container as a read-only volume.
            Null disables dump writing entirely. Required when ``enabled`` is True.
        max_dumps: Maximum number of dump file pairs to retain. Oldest pairs are
            removed when the limit is exceeded. 0 means unlimited. Default 200.
        notify_topic: MQTT topic to which mimirheim publishes a small JSON pointer
            after each dump is written. The payload is at most ~200 bytes and is
            published QoS 0, not retained. mimirheim-reporter subscribes to this topic
            to learn about new dumps in real time; it does not poll the filesystem.
            Default ``mimir/status/dump_available``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Enable production dump writing and MQTT notification.",
        json_schema_extra={"ui_label": "Enable reporting", "ui_group": "advanced"},
    )
    dump_dir: Path | None = Field(
        default=None,
        description="Directory for solve dumps shared with mimirheim-reporter. Null = disabled.",
        json_schema_extra={"ui_label": "Report dump directory", "ui_group": "advanced"},
    )
    max_dumps: int = Field(
        default=200,
        ge=0,
        description="Maximum retained dump pairs. 0 = unlimited.",
        json_schema_extra={"ui_label": "Max reports", "ui_group": "advanced"},
    )
    notify_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for dump-available notifications. "
            "Defaults to '{mqtt.topic_prefix}/status/dump_available' when not set."
        ),
        json_schema_extra={"ui_label": "Notify topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/status/dump_available"},
    )

    @model_validator(mode="after")
    def _dump_dir_required_when_enabled(self) -> "ReportingConfig":
        """Enforce that dump_dir is set when reporting is enabled.

        A reporting section with ``enabled = true`` but no ``dump_dir`` would
        silently fail to write any dumps. Catch this misconfiguration at startup.
        """
        if self.enabled and self.dump_dir is None:
            raise ValueError(
                "reporting.dump_dir is required when reporting.enabled is True"
            )
        return self

# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

class SocTopicConfig(BaseModel):
    """MQTT topic configuration for a state-of-charge sensor reading.

    Attributes:
        topic: MQTT topic that publishes the SOC value.
        unit: Unit of the published value. Use ``'kwh'`` if the inverter or BMS
            supports it — this is preferred because it avoids the conversion error
            that accumulates when ``capacity_kwh`` in config drifts from the cell's
            actual usable capacity as the battery ages. Use ``'percent'`` when the
            hardware only reports a 0–100 percentage; mimirheim then converts to kWh
            using the configured capacity. Always set this field explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic publishing the SOC value. "
            "Defaults to the device-specific path derived from mqtt.topic_prefix "
            "when not set."
        ),
        json_schema_extra={"ui_label": "SOC MQTT topic", "ui_group": "advanced"},
    )
    unit: Literal["kwh", "percent"] = Field(
        default="percent",
        description=(
            "Unit of the published value. 'percent' (default) when the sensor publishes "
            "a 0\u2013100 percentage; mimirheim converts to kWh using the device capacity. "
            "'kwh' when the sensor publishes an absolute energy value. "
            "Most residential inverters report SOC as a percentage. "
            "Prefer 'kwh' if your inverter supports it: the conversion from percent "
            "introduces a small systematic error if capacity_kwh in config drifts from "
            "the cell's actual usable capacity as the battery ages. Always set this "
            "field explicitly — do not rely on the default."
        ),
        json_schema_extra={"ui_label": "SOC unit", "ui_group": "advanced"},
    )

class EfficiencySegment(BaseModel):
    """One segment of a piecewise-linear efficiency curve.

    A battery or EV charger does not have a single fixed efficiency — it
    varies with the power level. Representing this as a list of segments
    keeps the solver model fully linear while approximating the real curve.

    Each segment covers a power range from 0 up to power_max_kw. To model
    the full charge direction, list segments in order; the solver fills the
    lowest-efficiency segment first when the curve is concave.

    Attributes:
        power_max_kw: Maximum power that can flow through this segment, in kW.
            The sum of all segment power_max_kw values is the maximum power for
            the direction (charge or discharge). There is no separate cap field.
        efficiency: Round-trip efficiency fraction for power in this segment.
            Must be strictly greater than zero and at most 1.0. A value of 0.95
            means 5% of the energy is lost as heat.
    """

    model_config = ConfigDict(extra="forbid")

    power_max_kw: float = Field(gt=0, description="Maximum power through this segment in kW.", json_schema_extra={"ui_label": "Max power (kW)", "ui_group": "basic"})
    efficiency: float = Field(gt=0, le=1.0, description="Round-trip efficiency fraction [0, 1].", json_schema_extra={"ui_label": "Efficiency", "ui_group": "basic"})

class EfficiencyBreakpoint(BaseModel):
    """A single point on a piecewise-linear battery efficiency curve.

    Used with the SOS2 efficiency model (see ``BatteryConfig.charge_efficiency_curve``).
    The solver interpolates efficiency linearly between adjacent breakpoints using
    a SOS2 (Special Ordered Set type 2) constraint, which enforces that the operating
    point lies on exactly one linear segment of the curve at any time step.

    Attributes:
        power_kw: AC power at this breakpoint, in kW. The first breakpoint must be
            at 0.0 kW (idle). Subsequent breakpoints must be strictly increasing.
        efficiency: Round-trip efficiency fraction at this power level. Must be
            strictly greater than 0 and at most 1.0.
    """

    model_config = ConfigDict(extra="forbid")

    power_kw: float = Field(ge=0.0, description="AC power in kW at this breakpoint.", json_schema_extra={"ui_label": "Power (kW)", "ui_group": "basic"})
    efficiency: float = Field(gt=0.0, le=1.0, description="Round-trip efficiency fraction.", json_schema_extra={"ui_label": "Efficiency", "ui_group": "basic"})

class BatteryInputsConfig(BaseModel):
    """MQTT input topic configuration for a battery device.

    Attributes:
        soc: Topic and unit for the battery state-of-charge reading.
    """

    model_config = ConfigDict(extra="forbid")

    soc: SocTopicConfig = Field(
        default_factory=SocTopicConfig,
        description="Battery state-of-charge MQTT topic configuration. Defaults to derived topic with percent unit.",
        json_schema_extra={"ui_label": "SOC topic config", "ui_group": "advanced"},
    )

class BatteryCapabilitiesConfig(BaseModel):
    """Hardware capability flags for a battery inverter.

    These fields describe what the physical hardware can do. They are used to
    configure how mimirheim communicates the schedule to the device, not to change
    the solver model.

    Attributes:
        staged_power: True if the hardware only accepts discrete power
            setpoints (e.g. 0%, 25%, 50%, 100%) rather than any continuous
            value. When True, mimirheim will round schedule setpoints to the
            nearest supported stage before publishing. Default False
            (continuous control assumed).
        zero_exchange: True if the battery inverter supports a closed-loop
            zero-exchange firmware mode triggered by a boolean register. When
            True, the inverter uses its own current transformers to continuously
            hold grid exchange near zero until the flag is cleared. mimirheim decides
            *whether* to assert this mode per 15-minute step; the inverter
            performs the real-time enforcement autonomously. Default False.
    """

    model_config = ConfigDict(extra="forbid")

    staged_power: bool = Field(
        default=False,
        description="Hardware accepts only discrete power stages, not continuous values.",
        json_schema_extra={"ui_label": "Staged power control", "ui_group": "advanced"},
    )
    zero_exchange: bool = Field(
        default=False,
        description=(
            "Battery inverter supports a closed-loop zero-exchange firmware mode."
            " When True, the inverter autonomously holds grid exchange near zero"
            " using local CT measurements."
        ),
        json_schema_extra={"ui_label": "Zero-exchange mode", "ui_group": "advanced"},
    )

class BatteryOutputsConfig(BaseModel):
    """MQTT output topic configuration for a battery device.

    Each field is optional. A ``None`` value means mimirheim will not publish that
    particular output. The corresponding capability flag in
    ``BatteryCapabilitiesConfig`` must also be True for the topic to be active.

    Attributes:
        exchange_mode: Topic to publish the closed-loop exchange mode flag
            (``"true"`` or ``"false"``). Published only when
            ``capabilities.zero_exchange`` is True. Always set: defaults to
            ``"{mqtt.topic_prefix}/output/battery/{name}/exchange_mode"`` when
            not provided explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    exchange_mode: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the closed-loop zero-exchange mode boolean flag. "
            "Defaults to '{mqtt.topic_prefix}/output/battery/{name}/exchange_mode' "
            "when not set."
        ),
        json_schema_extra={"ui_label": "Exchange mode topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/output/battery/{name}/exchange_mode"},
    )

class BatteryConfig(BaseModel):
    """Configuration for a DC-coupled residential battery.

    Two efficiency models are supported:

    **Stacked-segment model** (``charge_segments``): Each segment is an independent LP
    variable bounded by a flat efficiency. Use this when the efficiency curve is
    approximated by a small number of steps. This was the original model.

    **SOS2 piecewise-linear model** (``charge_efficiency_curve``): Uses a SOS2
    constraint to interpolate efficiency linearly between breakpoints. Use this when
    you have manufacturer-measured efficiency at several power levels and need accurate
    continuous interpolation.

    Exactly one of ``charge_segments`` or ``charge_efficiency_curve`` must be
    provided for each direction. Providing both, or neither, raises a
    ``ValidationError``.

    Attributes:
        capacity_kwh: Usable battery capacity in kWh.
        min_soc_kwh: Minimum state of charge to maintain, in kWh.
        charge_segments: Stacked-segment efficiency model for charging. At least
            one segment required. Mutually exclusive with charge_efficiency_curve.
        discharge_segments: Stacked-segment efficiency model for discharging.
        charge_efficiency_curve: SOS2 piecewise-linear efficiency curve for charging.
            At least two breakpoints required. First breakpoint must be at 0 kW.
            Mutually exclusive with charge_segments.
        discharge_efficiency_curve: SOS2 piecewise-linear efficiency curve for
            discharging. Same constraints as charge_efficiency_curve.
        wear_cost_eur_per_kwh: Cost per kWh of energy throughput in EUR.
        optimal_lower_soc_kwh: Preferred minimum SOC. See plan 21 notes.
        soc_low_penalty_eur_per_kwh_h: Penalty rate for negative SOC deviation.
        reduce_charge_above_soc_kwh: SOC threshold above which charge is derated.
        reduce_charge_min_kw: Minimum charge power at capacity_kwh when derated.
        reduce_discharge_below_soc_kwh: SOC threshold below which discharge is derated.
        reduce_discharge_min_kw: Minimum discharge power at min_soc_kwh when derated.
        capabilities: Hardware capability flags.
        inputs: MQTT input topics for live battery state readings.
        outputs: MQTT output topics for battery control signals (e.g. zero-export
            mode flag). All fields default to None (no publishing).
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this battery used in MQTT topics and automations. For example: 'home_battery' or 'garage_battery'."})

    capacity_kwh: float = Field(gt=0, description="Usable capacity in kWh.", json_schema_extra={"ui_label": "Capacity (kWh)", "ui_group": "basic"})
    min_soc_kwh: float = Field(ge=0, default=0.0, description="Minimum SOC in kWh.", json_schema_extra={"ui_label": "Minimum SOC (kWh)", "ui_group": "basic"})
    charge_segments: list[EfficiencySegment] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Stacked-segment efficiency model for charging. Mutually exclusive with "
            "charge_efficiency_curve. Exactly one must be provided."
        ),
        json_schema_extra={"ui_label": "Charge segments", "ui_group": "basic"},
    )
    discharge_segments: list[EfficiencySegment] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Stacked-segment efficiency model for discharging. Mutually exclusive with "
            "discharge_efficiency_curve. Exactly one must be provided."
        ),
        json_schema_extra={"ui_label": "Discharge segments", "ui_group": "basic"},
    )
    charge_efficiency_curve: list[EfficiencyBreakpoint] | None = Field(
        default=None,
        min_length=2,
        description=(
            "SOS2 piecewise-linear efficiency curve for charging. First breakpoint "
            "must be at power_kw=0.0. Mutually exclusive with charge_segments."
        ),
        json_schema_extra={"ui_label": "Charge efficiency curve", "ui_group": "advanced"},
    )
    discharge_efficiency_curve: list[EfficiencyBreakpoint] | None = Field(
        default=None,
        min_length=2,
        description=(
            "SOS2 piecewise-linear efficiency curve for discharging. First breakpoint "
            "must be at power_kw=0.0. Mutually exclusive with discharge_segments."
        ),
        json_schema_extra={"ui_label": "Discharge efficiency curve", "ui_group": "advanced"},
    )
    wear_cost_eur_per_kwh: float = Field(
        ge=0,
        default=0.0,
        description=(
            "Battery degradation cost per kWh of energy throughput (charge + discharge), "
            "in EUR. The solver subtracts this cost from every kWh cycled, so it will not "
            "dispatch the battery for a price spread smaller than this value. "
            "Set to 0.0 (default) to optimise purely on energy prices without modelling "
            "wear. To estimate a representative value: divide the battery replacement cost "
            "by the expected lifetime throughput. "
            "Example: a 10 kWh battery costing \u20ac3,000 with 3,000 expected full cycles "
            "has \u20ac3,000 / (3,000 \u00d7 10 kWh) = \u20ac0.10/kWh of throughput. "
            "Typical residential LFP values fall in the \u20ac0.03\u2013\u20ac0.12/kWh range."
        ),
        json_schema_extra={"ui_label": "Wear cost (\u20ac/kWh)", "ui_group": "basic"},
    )
    optimal_lower_soc_kwh: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Preferred minimum state of charge in kWh. When the SOC falls below "
            "this level, the solver accrues a penalty proportional to the deficit. "
            "Acts as a soft lower bound: the solver may still dispatch below this "
            "level when the price spread justifies it. Must be >= min_soc_kwh and "
            "<= capacity_kwh."
        ),
        json_schema_extra={"ui_label": "Preferred minimum SOC (kWh)", "ui_group": "advanced"},
    )
    soc_low_penalty_eur_per_kwh_h: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Penalty rate for SOC below optimal_lower_soc_kwh, in EUR per kWh of "
            "deficit per hour. Set to 0.0 (default) to disable. A value of 0.10 "
            "adds a 0.10 EUR × kWh-deficit × hours cost per step, discouraging "
            "dispatch below the optimal level for any price spread smaller than "
            "this rate."
        ),
        json_schema_extra={"ui_label": "Low SOC penalty (\u20ac/kWh\u00b7h)", "ui_group": "advanced"},
    )
    reduce_charge_above_soc_kwh: float | None = Field(
        default=None,
        description=(
            "SOC level in kWh above which max charge power begins to decrease "
            "linearly to reduce_charge_min_kw at capacity_kwh. Must be strictly "
            "between min_soc_kwh and capacity_kwh. Must be set together with "
            "reduce_charge_min_kw."
        ),
        json_schema_extra={"ui_label": "Derate charge above SOC (kWh)", "ui_group": "advanced"},
    )
    reduce_charge_min_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Max charge power in kW at full capacity (capacity_kwh). The charge "
            "power limit decreases linearly from max_charge_kw at "
            "reduce_charge_above_soc_kwh to this value at capacity_kwh. Must be "
            "strictly greater than 0 and strictly less than the sum of charge "
            "segment power_max_kw values. Must be set together with "
            "reduce_charge_above_soc_kwh."
        ),
        json_schema_extra={"ui_label": "Derated charge minimum (kW)", "ui_group": "advanced"},
    )
    reduce_discharge_below_soc_kwh: float | None = Field(
        default=None,
        description=(
            "SOC level in kWh below which max discharge power begins to decrease "
            "linearly to reduce_discharge_min_kw at min_soc_kwh. Must be strictly "
            "between min_soc_kwh and capacity_kwh. Must be set together with "
            "reduce_discharge_min_kw."
        ),
        json_schema_extra={"ui_label": "Derate discharge below SOC (kWh)", "ui_group": "advanced"},
    )
    reduce_discharge_min_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Max discharge power in kW at minimum SOC (min_soc_kwh). The discharge "
            "power limit decreases linearly from max_discharge_kw at "
            "reduce_discharge_below_soc_kwh to this value at min_soc_kwh. Must be "
            "strictly greater than 0 and strictly less than the sum of discharge "
            "segment power_max_kw values. Must be set together with "
            "reduce_discharge_below_soc_kwh."
        ),
        json_schema_extra={"ui_label": "Derated discharge minimum (kW)", "ui_group": "advanced"},
    )
    capabilities: BatteryCapabilitiesConfig = Field(
        default_factory=BatteryCapabilitiesConfig,
        description="Hardware capability flags.",
        json_schema_extra={"ui_label": "Hardware capabilities", "ui_group": "advanced"},
    )
    inputs: BatteryInputsConfig | None = Field(
        default_factory=BatteryInputsConfig,
        description="MQTT input topic configuration for battery state readings. Defaults to an empty model so topics are derived with percent unit. Set to null to opt out of MQTT inputs entirely.",
        json_schema_extra={"ui_label": "Input topics", "ui_group": "advanced"},
    )
    outputs: BatteryOutputsConfig = Field(
        default_factory=BatteryOutputsConfig,
        description="MQTT output topic configuration for battery control signals.",
        json_schema_extra={"ui_label": "Output topics", "ui_group": "advanced"},
    )
    min_charge_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum charge power in kW when the inverter is actively charging. "
            "When set, the solver enforces: total_charge >= min_charge_kw × mode[t]. "
            "The inverter either charges at this power or above, or stays idle (0 kW). "
            "Default None = no floor applied."
        ),
        json_schema_extra={"ui_label": "Minimum charge power (kW)", "ui_group": "advanced"},
    )
    min_discharge_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum discharge power in kW when the inverter is actively discharging. "
            "When set, the solver enforces: total_discharge >= min_discharge_kw × (1 − mode[t]). "
            "The floor is only active in the discharging direction (mode[t]=0); "
            "charging steps see a right-hand side of zero and are unaffected. "
            "Default None = no floor applied."
        ),
        json_schema_extra={"ui_label": "Minimum discharge power (kW)", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_efficiency_models(self) -> "BatteryConfig":
        """Validate that exactly one efficiency model is provided for each direction."""
        for direction, segments, curve in (
            ("charge", self.charge_segments, self.charge_efficiency_curve),
            ("discharge", self.discharge_segments, self.discharge_efficiency_curve),
        ):
            both = segments is not None and curve is not None
            neither = segments is None and curve is None
            if both:
                raise ValueError(
                    f"{direction}_segments and {direction}_efficiency_curve are mutually "
                    "exclusive. Provide exactly one."
                )
            if neither:
                raise ValueError(
                    f"Either {direction}_segments or {direction}_efficiency_curve must be "
                    "provided."
                )
            if curve is not None:
                if curve[0].power_kw != 0.0:
                    raise ValueError(
                        f"The first breakpoint in {direction}_efficiency_curve must have "
                        f"power_kw=0.0, got {curve[0].power_kw}."
                    )
                for i in range(1, len(curve)):
                    if curve[i].power_kw <= curve[i - 1].power_kw:
                        raise ValueError(
                            f"{direction}_efficiency_curve breakpoints must have strictly "
                            f"increasing power_kw values. "
                            f"Breakpoint {i} ({curve[i].power_kw}) <= breakpoint {i - 1} "
                            f"({curve[i - 1].power_kw})."
                        )
        return self

    @model_validator(mode="after")
    def _validate_soc_levels(self) -> "BatteryConfig":
        # A value of 0.0 means the soft lower bound is not configured; skip checks.
        if self.optimal_lower_soc_kwh == 0.0:
            return self
        if self.optimal_lower_soc_kwh < self.min_soc_kwh:
            raise ValueError(
                f"optimal_lower_soc_kwh ({self.optimal_lower_soc_kwh}) must be "
                f">= min_soc_kwh ({self.min_soc_kwh})"
            )
        if self.optimal_lower_soc_kwh > self.capacity_kwh:
            raise ValueError(
                f"optimal_lower_soc_kwh ({self.optimal_lower_soc_kwh}) must be "
                f"<= capacity_kwh ({self.capacity_kwh})"
            )
        return self

    @model_validator(mode="after")
    def _validate_derating(self) -> "BatteryConfig":
        """Validate that derating field pairs are set together and within valid ranges."""
        # Compute max power for each direction from whichever model is configured.
        # At this point _validate_efficiency_models has already run, so exactly one
        # of segments or curve is not None for each direction.
        if self.charge_segments is not None:
            max_charge_kw = sum(s.power_max_kw for s in self.charge_segments)
        else:
            max_charge_kw = (
                self.charge_efficiency_curve[-1].power_kw
                if self.charge_efficiency_curve
                else 0.0
            )
        if self.discharge_segments is not None:
            max_discharge_kw = sum(s.power_max_kw for s in self.discharge_segments)
        else:
            max_discharge_kw = (
                self.discharge_efficiency_curve[-1].power_kw
                if self.discharge_efficiency_curve
                else 0.0
            )

        # Charge derating: both fields must be set together (or neither).
        charge_set = (
            self.reduce_charge_above_soc_kwh is not None,
            self.reduce_charge_min_kw is not None,
        )
        if charge_set[0] != charge_set[1]:
            raise ValueError(
                "reduce_charge_above_soc_kwh and reduce_charge_min_kw must be "
                "set together or both left as None."
            )
        if self.reduce_charge_above_soc_kwh is not None:
            if not (self.min_soc_kwh < self.reduce_charge_above_soc_kwh < self.capacity_kwh):
                raise ValueError(
                    f"reduce_charge_above_soc_kwh ({self.reduce_charge_above_soc_kwh}) "
                    f"must be strictly between min_soc_kwh ({self.min_soc_kwh}) and "
                    f"capacity_kwh ({self.capacity_kwh})."
                )
            if not (0.0 < self.reduce_charge_min_kw < max_charge_kw):
                raise ValueError(
                    f"reduce_charge_min_kw ({self.reduce_charge_min_kw}) must be "
                    f"strictly between 0 and max_charge_kw ({max_charge_kw})."
                )

        # Discharge derating: both fields must be set together (or neither).
        discharge_set = (
            self.reduce_discharge_below_soc_kwh is not None,
            self.reduce_discharge_min_kw is not None,
        )
        if discharge_set[0] != discharge_set[1]:
            raise ValueError(
                "reduce_discharge_below_soc_kwh and reduce_discharge_min_kw must be "
                "set together or both left as None."
            )
        if self.reduce_discharge_below_soc_kwh is not None:
            if not (self.min_soc_kwh < self.reduce_discharge_below_soc_kwh < self.capacity_kwh):
                raise ValueError(
                    f"reduce_discharge_below_soc_kwh ({self.reduce_discharge_below_soc_kwh}) "
                    f"must be strictly between min_soc_kwh ({self.min_soc_kwh}) and "
                    f"capacity_kwh ({self.capacity_kwh})."
                )
            if not (0.0 < self.reduce_discharge_min_kw < max_discharge_kw):
                raise ValueError(
                    f"reduce_discharge_min_kw ({self.reduce_discharge_min_kw}) must be "
                    f"strictly between 0 and max_discharge_kw ({max_discharge_kw})."
                )

        return self

# ---------------------------------------------------------------------------
# EV charger
# ---------------------------------------------------------------------------

class EvInputsConfig(BaseModel):
    """MQTT input topic configuration for an EV charger device.

    Attributes:
        soc: Topic and unit for the vehicle battery state-of-charge reading.
        plugged_in_topic: MQTT topic publishing a boolean plug state.
            mimirheim subscribes to this to determine whether the vehicle is
            available for charging. Expected payloads: 'true'/'false',
            'on'/'off', '1'/'0', or a JSON boolean.
    """

    model_config = ConfigDict(extra="forbid")

    soc: SocTopicConfig = Field(
        default_factory=SocTopicConfig,
        description="Vehicle SOC MQTT topic configuration. Defaults to derived topic with percent unit.",
        json_schema_extra={"ui_label": "SOC topic config", "ui_group": "advanced"},
    )
    plugged_in_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the EV plug state. "
            "Defaults to '{mqtt.topic_prefix}/input/ev/{name}/plugged_in' when not set."
        ),
        json_schema_extra={"ui_label": "Plug state topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/input/ev/{name}/plugged_in"},
    )

class EvCapabilitiesConfig(BaseModel):
    """Hardware capability flags for an EV charger.

    Attributes:
        staged_power: True if the hardware only accepts discrete power
            setpoints rather than continuous values.
        zero_exchange: True if the EVSE supports a closed-loop zero-exchange
            firmware mode. When True, the charger firmware uses local current
            transformers to continuously hold grid exchange near zero until the
            flag is cleared. Requires ``v2h: True``; the charger must be capable
            of bidirectional power to regulate both import and export. A
            model validator on ``EvCapabilitiesConfig`` rejects
            ``zero_exchange: True`` when ``v2h: False``. Default False.
        v2h: True if the hardware supports vehicle-to-home discharge (bidirectional
            power flow). Required when ``zero_exchange`` is True. Default False.
        loadbalance: True if the EVSE firmware supports autonomous charge-only
            excess-PV following. When asserted, the charger measures net grid
            current and clamps charge power to available PV surplus. mimirheim does
            not send a numeric setpoint when this mode is asserted; the EVSE
            self-regulates. Orthogonal to ``zero_exchange``. Default False.
    """

    model_config = ConfigDict(extra="forbid")

    staged_power: bool = Field(
        default=False,
        description="Hardware accepts only discrete power stages, not continuous values.",
        json_schema_extra={"ui_label": "Staged power control", "ui_group": "advanced"},
    )
    zero_exchange: bool = Field(
        default=False,
        description=(
            "EVSE supports a closed-loop zero-exchange firmware mode. Requires v2h=True."
            " The charger autonomously holds grid exchange near zero using local CT"
            " measurements."
        ),
        json_schema_extra={"ui_label": "Zero-exchange mode", "ui_group": "advanced"},
    )
    v2h: bool = Field(
        default=False,
        description=(
            "Hardware supports vehicle-to-home discharge (bidirectional power flow)."
            " Required when zero_exchange=True."
        ),
        json_schema_extra={"ui_label": "Vehicle-to-home (V2H)", "ui_group": "advanced"},
    )
    loadbalance: bool = Field(
        default=False,
        description=(
            "EVSE firmware supports autonomous charge-only excess-PV following mode."
            " When asserted, the EVSE self-regulates to available PV surplus."
        ),
        json_schema_extra={"ui_label": "Load balance mode", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_zero_exchange_requires_v2h(self) -> "EvCapabilitiesConfig":
        """Reject zero_exchange=True when v2h=False.

        Regulating grid exchange in both directions requires the ability to
        both charge and discharge. A charge-only EVSE cannot prevent export;
        it can only reduce import. Setting zero_exchange without v2h declares
        a physically impossible capability.
        """
        if self.zero_exchange and not self.v2h:
            raise ValueError(
                "EvCapabilitiesConfig: zero_exchange=True requires v2h=True. "
                "A charge-only EVSE cannot regulate grid export. "
                "Set v2h=True if the hardware supports bidirectional power flow."
            )
        return self

class EvOutputsConfig(BaseModel):
    """MQTT output topic configuration for an EV charger.

    Each field is optional. A ``None`` value means mimirheim will not publish that
    particular output. Capabilities must also be enabled (see
    ``EvCapabilitiesConfig``) for the corresponding topic to be published.

    Attributes:
        exchange_mode: Topic to publish the closed-loop exchange mode flag
            (``"true"`` or ``"false"``). Published only when
            ``capabilities.zero_exchange`` is True. Always set: defaults to
            ``"{mqtt.topic_prefix}/output/ev/{name}/exchange_mode"`` when not
            provided explicitly.
        loadbalance_cmd: Topic to publish the load-balance mode enable flag
            (``"true"`` or ``"false"``). Published only when
            ``capabilities.loadbalance`` is True. Always set: defaults to
            ``"{mqtt.topic_prefix}/output/ev/{name}/loadbalance"`` when not
            provided explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    exchange_mode: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the closed-loop zero-exchange mode boolean flag. "
            "Defaults to '{mqtt.topic_prefix}/output/ev/{name}/exchange_mode' "
            "when not set."
        ),
        json_schema_extra={"ui_label": "Exchange mode topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/output/ev/{name}/exchange_mode"},
    )
    loadbalance_cmd: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the load-balance mode enable boolean flag. "
            "Defaults to '{mqtt.topic_prefix}/output/ev/{name}/loadbalance' "
            "when not set."
        ),
        json_schema_extra={"ui_label": "Load balance topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/output/ev/{name}/loadbalance"},
    )

class EvConfig(BaseModel):
    """Configuration for an EV charger with optional vehicle-to-home (V2H) discharge.

    Attributes:
        capacity_kwh: Total usable battery capacity of the connected vehicle, in kWh.
        min_soc_kwh: Minimum SOC the solver will discharge to, in kWh.
        charge_segments: Piecewise efficiency segments for the charge direction.
        discharge_segments: Segments for V2H discharge. Leave empty if the hardware
            does not support discharging back to the home.
        wear_cost_eur_per_kwh: Degradation cost per kWh throughput.
        capabilities: Hardware capability flags. See EvCapabilitiesConfig.
        outputs: MQTT output topic configuration. See EvOutputsConfig.
        inputs: MQTT input topics for live EV state readings.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this EV charger used in MQTT topics and automations. For example: 'ev_charger' or 'garage_evse'."})

    capacity_kwh: float = Field(gt=0, description="Vehicle battery capacity in kWh.", json_schema_extra={"ui_label": "Vehicle capacity (kWh)", "ui_group": "basic"})
    min_soc_kwh: float = Field(ge=0, default=0.0, description="Minimum SOC in kWh.", json_schema_extra={"ui_label": "Minimum SOC (kWh)", "ui_group": "basic"})
    charge_segments: list[EfficiencySegment] = Field(
        min_length=1, description="Piecewise efficiency segments for charging.",
        json_schema_extra={"ui_label": "Charge segments", "ui_group": "basic"},
    )
    discharge_segments: list[EfficiencySegment] = Field(
        default_factory=list,
        description="Piecewise efficiency segments for V2H discharge. Empty = no V2H.",
        json_schema_extra={"ui_label": "Discharge segments (V2H)", "ui_group": "basic"},
    )
    wear_cost_eur_per_kwh: float = Field(
        ge=0,
        default=0.0,
        description=(
            "Vehicle battery degradation cost per kWh of energy throughput, in EUR. "
            "The solver subtracts this cost from every kWh charged or discharged (V2H), "
            "so it will not cycle the vehicle battery for a price spread smaller than "
            "this value. Set to 0.0 (default) to optimise purely on energy prices. "
            "To estimate: divide the expected battery replacement cost by the expected "
            "lifetime throughput. "
            "Example: a 60 kWh vehicle battery with a \u20ac8,000 pack and 1,500 full "
            "cycles gives \u20ac8,000 / (1,500 \u00d7 60 kWh) = \u20ac0.089/kWh."
        ),
        json_schema_extra={"ui_label": "Wear cost (\u20ac/kWh)", "ui_group": "basic"},
    )
    capabilities: EvCapabilitiesConfig = Field(
        default_factory=EvCapabilitiesConfig,
        description="Hardware capability flags.",
        json_schema_extra={"ui_label": "Hardware capabilities", "ui_group": "advanced"},
    )
    min_charge_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum charge power in kW when the EVSE is actively charging. "
            "IEC 61851 mandates ≥ 6 A per phase; single-phase 230 V = 1.38 kW, "
            "three-phase = 4.14 kW. When set and V2H is configured "
            "(discharge_segments non-empty), the solver enforces: "
            "total_charge >= min_charge_kw × mode[t]. "
            "Silently ignored for charge-only EVs (no mode variable). "
            "Default None = no floor applied."
        ),
        json_schema_extra={"ui_label": "Minimum EVSE charge power (kW)", "ui_group": "advanced"},
    )
    min_discharge_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum V2H discharge power in kW when the EVSE is actively discharging. "
            "When set and discharge_segments is non-empty, the solver enforces: "
            "total_discharge >= min_discharge_kw × (1 − mode[t]). "
            "Silently ignored when discharge_segments is empty. "
            "Default None = no floor applied."
        ),
        json_schema_extra={"ui_label": "Minimum V2H discharge power (kW)", "ui_group": "advanced"},
    )
    outputs: EvOutputsConfig = Field(
        default_factory=EvOutputsConfig,
        description="MQTT output topic configuration.",
        json_schema_extra={"ui_label": "Output topics", "ui_group": "advanced"},
    )
    inputs: EvInputsConfig | None = Field(
        default_factory=EvInputsConfig,
        description="MQTT input topic configuration for EV state readings. Defaults to an empty model so topics are derived with percent unit. Set to null to opt out of MQTT inputs entirely.",
        json_schema_extra={"ui_label": "Input topics", "ui_group": "advanced"},
    )

# ---------------------------------------------------------------------------
# PV array
# ---------------------------------------------------------------------------

class PvCapabilitiesConfig(BaseModel):
    """Hardware capability flags for a PV array.

    These flags declare what control outputs the inverter accepts. Enabling a
    capability causes mimirheim to publish a corresponding output topic each solve
    cycle. Capabilities that are False are never published, even if an output
    topic is configured.

    PV arrays support four mutually exclusive (or partially exclusive) control modes:

    1. **Fixed** (no capabilities, no ``production_stages``): mimirheim treats the
       per-step forecast as a constant. No output topics are published. All other
       dispatchable devices schedule around the fixed PV output.

    2. **Continuous power limit** (``power_limit: true``): mimirheim adds a
       continuous decision variable per step, bounded by the forecast. The solver
       may curtail to any value in ``[0, forecast_kw]``. The chosen setpoint is
       published as a kW value each cycle.

    3. **On/off** (``on_off: true``): mimirheim adds a binary per step. The array
       either produces the full forecast or is switched off completely. Mutually
       exclusive with ``power_limit``. Both may be combined: ``pv_kw[t]`` is then
       bounded by ``forecast[t] * pv_on[t]``.

    4. **Staged** (``production_stages`` list in ``PvConfig``): the inverter only
       accepts specific discrete power levels. mimirheim adds one binary variable
       per step per stage; exactly one stage is active at a time. The chosen stage
       register value is published as the ``power_limit_kw`` setpoint. This mode
       is configured entirely via ``PvConfig.production_stages`` — there is no
       corresponding capability flag here. It is mutually exclusive with
       ``power_limit`` and ``on_off``.

    5. **Zero-export** (``zero_export: true``): orthogonal to all modes above.
       When selected by the arbitration engine as the closed-loop enforcer for a
       step, mimirheim publishes ``true`` to the inverter's zero-export register.
       The inverter firmware then uses local CT measurements to prevent grid export
       in real time. mimirheim decides *whether* the mode should be active per
       step; the inverter performs the real-time enforcement autonomously.

    Attributes:
        power_limit: True if the inverter accepts a continuous production limit
            setpoint in kW. Publishing 0.0 turns the array off completely;
            any positive value caps production at that level. Default False.
        zero_export: True if the inverter has a boolean zero-export mode
            register. Default False.
        on_off: True if the inverter supports discrete on/off control only, with
            no intermediate power levels. Mutually exclusive with ``power_limit``.
            Default False.
    """

    model_config = ConfigDict(extra="forbid")

    power_limit: bool = Field(
        default=False,
        description="Inverter accepts a continuous production limit setpoint in kW.",
        json_schema_extra={"ui_label": "Power limit control", "ui_group": "advanced"},
    )
    zero_export: bool = Field(
        default=False,
        description=(
            "Inverter has a boolean zero-export mode register. When True, the inverter"
            " autonomously prevents grid export using local CT measurements."
        ),
        json_schema_extra={"ui_label": "Zero-export mode", "ui_group": "advanced"},
    )
    on_off: bool = Field(
        default=False,
        description=(
            "Inverter supports discrete on/off control. When True, mimirheim treats PV "
            "as a binary decision variable: the array either produces the full "
            "forecast or is switched off. Mutually exclusive with power_limit."
        ),
        json_schema_extra={"ui_label": "On/off control", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_mutually_exclusive_modes(self) -> "PvCapabilitiesConfig":
        """Reject configurations that enable both power_limit and on_off simultaneously.

        The two modes correspond to incompatible hardware interfaces:

        - ``power_limit``: the inverter accepts a continuous kW setpoint register.
          Setting the register to 0.0 is equivalent to switching off. There is no
          separate on/off register.
        - ``on_off``: the inverter accepts a binary on/off register only. No
          intermediate power levels are possible.

        No real inverter drives both registers at the same time. Enabling both
        would create a solver model that does not correspond to any physical
        hardware, and would add an unnecessary binary variable per step and a
        Big-M constraint for no practical benefit.

        For discrete-stage inverters (e.g. Enphase ACB with 16 stages including
        0 and 100%), use ``production_stages`` in ``PvConfig`` instead.
        """
        if self.power_limit and self.on_off:
            raise ValueError(
                "PV capabilities 'power_limit' and 'on_off' are mutually exclusive. "
                "A continuous inverter accepts a power limit setpoint (use 'power_limit'). "
                "A binary on/off inverter supports only on or off (use 'on_off'). "
                "For discrete-stage inverters use 'production_stages' in PvConfig."
            )
        return self

class PvOutputsConfig(BaseModel):
    """MQTT output topic names for PV array control commands.

    All fields are optional. A topic is only published if the corresponding
    capability flag is True and the topic is not None.

    Attributes:
        power_limit_kw: Topic to which the production limit setpoint in kW is
            published, retained. Publishing 0.0 turns the array off. Always
            set: defaults to
            ``"{mqtt.topic_prefix}/output/pv/{name}/power_limit_kw"`` when not
            provided explicitly.
        zero_export_mode: Topic to which the zero-export mode command is
            published, retained. Payload is ``"true"`` or ``"false"``. Always
            set: defaults to
            ``"{mqtt.topic_prefix}/output/pv/{name}/zero_export_mode"`` when
            not provided explicitly.
        on_off_mode: Topic to which the on/off command is published, retained.
            Payload ``"true"`` means producing, ``"false"`` means curtailed.
            Always set: defaults to
            ``"{mqtt.topic_prefix}/output/pv/{name}/on_off_mode"`` when not
            provided explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    power_limit_kw: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the production limit setpoint in kW. "
            "Defaults to '{mqtt.topic_prefix}/output/pv/{name}/power_limit_kw' "
            "when not set."
        ),
        json_schema_extra={"ui_label": "Power limit topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/output/pv/{name}/power_limit_kw"},
    )
    zero_export_mode: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the zero-export mode boolean command. "
            "Defaults to '{mqtt.topic_prefix}/output/pv/{name}/zero_export_mode' "
            "when not set."
        ),
        json_schema_extra={"ui_label": "Zero-export mode topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/output/pv/{name}/zero_export_mode"},
    )
    on_off_mode: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the on/off command. Published once per solve cycle. "
            "Payload \"true\" means the inverter should be ON (producing); "
            "payload \"false\" means the inverter should be OFF (curtailed by mimirheim). "
            "Only relevant when capabilities.on_off is True. "
            "Defaults to '{mqtt.topic_prefix}/output/pv/{name}/on_off_mode' "
            "when not set."
        ),
        json_schema_extra={"ui_label": "On/off mode topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/output/pv/{name}/on_off_mode"},
    )
    is_curtailed: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the curtailment status boolean. Published once per solve cycle. "
            "Payload \"true\" means mimirheim is actively limiting PV output below the "
            "forecast; \"false\" means the inverter is free to produce at full capacity. "
            "Valid for staged, power_limit, and on_off modes. None for fixed-mode arrays. "
            "Defaults to '{mqtt.topic_prefix}/output/pv/{name}/is_curtailed' when not set."
        ),
        json_schema_extra={"ui_label": "Is curtailed topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/output/pv/{name}/is_curtailed"},
    )

class PvConfig(BaseModel):
    """Configuration for a PV array.

    Four control modes are available. See ``PvCapabilitiesConfig`` for the full
    description of each mode and their mutual exclusivity rules.

    **Fixed** (no capabilities, no ``production_stages``): the per-step forecast
    is treated as a constant. No output topics are published.

    **Continuous power limit** (``capabilities.power_limit: true``): a continuous
    decision variable per step bounded by the forecast. The solver may curtail to
    any value in ``[0, forecast_kw]``.

    **On/off** (``capabilities.on_off: true``): a binary per step. The array
    either produces the full forecast or is switched off. May be combined with
    ``power_limit``.

    **Staged** (``production_stages``): the inverter only accepts discrete power
    register values. The solver selects exactly one stage per step. This is the
    only mode that is configured here rather than in ``capabilities``, because the
    stage values are the configuration — the presence of the list is the declaration
    that staged mode is active. Mutually exclusive with ``power_limit`` and ``on_off``.

    Attributes:
        max_power_kw: Array peak output in kW. Used to clip unreasonably large
            forecast values that may indicate sensor errors.
        topic_forecast: MQTT topic from which the per-step power forecast is read.
        production_stages: Discrete power levels the inverter accepts, in kW. When
            provided, the solver selects exactly one stage per time step. The first
            value must be ``0.0`` (the off state). Values must be strictly increasing.
            ``max_power_kw`` must be at least as large as the final (largest) value.
            Mutually exclusive with ``capabilities.power_limit`` and ``capabilities.on_off``.
        capabilities: Hardware capability flags. Controls which output topics
            are published. Default: all capabilities disabled.
        outputs: MQTT output topic names for production limit and zero-export
            mode commands.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this PV array used in MQTT topics and automations. For example: 'roof_pv' or 'south_array'."})

    max_power_kw: float = Field(gt=0, description="Array peak output in kW.", json_schema_extra={"ui_label": "Peak power (kW)", "ui_group": "basic"})
    topic_forecast: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the per-step PV power forecast in kW. "
            "Defaults to '{mqtt.topic_prefix}/input/pv/{name}/forecast' when not set."
        ),
        json_schema_extra={"ui_label": "Forecast topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/input/pv/{name}/forecast"},
    )
    production_stages: list[float] | None = Field(
        default=None,
        description=(
            "Discrete power levels the inverter accepts, in ascending order, starting "
            "with 0.0. When set, the solver selects exactly one stage per step. "
            "Mutually exclusive with capabilities.power_limit and capabilities.on_off."
        ),
        json_schema_extra={"ui_label": "Production stages (kW)", "ui_group": "advanced"},
    )
    capabilities: PvCapabilitiesConfig = Field(
        default_factory=PvCapabilitiesConfig,
        description="Hardware capability flags.",
        json_schema_extra={"ui_label": "Hardware capabilities", "ui_group": "advanced"},
    )
    outputs: PvOutputsConfig = Field(
        default_factory=PvOutputsConfig,
        description="MQTT output topic names for PV control commands.",
        json_schema_extra={"ui_label": "Output topics", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_production_stages(self) -> "PvConfig":
        """Validate production_stages consistency rules when present."""
        stages = self.production_stages
        if stages is None:
            return self

        # Rule 1: first stage must be 0.0 (the off state).
        if stages[0] != 0.0:
            raise ValueError(
                "production_stages must start with 0.0 (the off state). "
                f"Got first stage: {stages[0]}."
            )

        # Rule 2: all values must be non-negative and strictly increasing.
        for i in range(1, len(stages)):
            if stages[i] <= stages[i - 1]:
                raise ValueError(
                    "production_stages must be strictly increasing. "
                    f"Stage {i} ({stages[i]}) is not greater than stage {i - 1} ({stages[i - 1]})."
                )

        # Rule 3: max_power_kw must be at least the largest stage.
        last_stage = stages[-1]
        if self.max_power_kw < last_stage:
            raise ValueError(
                f"max_power_kw ({self.max_power_kw}) must be >= the largest stage "
                f"({last_stage})."
            )

        # Rule 4: mutually exclusive with continuous power_limit.
        if self.capabilities.power_limit:
            raise ValueError(
                "production_stages and capabilities.power_limit are mutually exclusive. "
                "Use production_stages for discrete-level inverters and power_limit for "
                "continuous-setpoint inverters."
            )

        # Rule 5: mutually exclusive with on_off.
        if self.capabilities.on_off:
            raise ValueError(
                "production_stages and capabilities.on_off are mutually exclusive. "
                "For a two-level inverter, use production_stages: [0.0, <rated_kw>] "
                "instead of on_off."
            )

        return self

# ---------------------------------------------------------------------------
# Hybrid inverter (integrated PV + battery DC bus)
# ---------------------------------------------------------------------------

class HybridInverterCapabilitiesConfig(BaseModel):
    """Hardware capability flags for a hybrid inverter.

    Attributes:
        zero_exchange: True if the inverter supports a closed-loop zero-exchange
            firmware mode triggered by a boolean register. When True, the
            inverter uses its own current transformers to continuously hold grid
            exchange near zero until the flag is cleared. mimirheim decides
            *whether* to assert this mode per 15-minute step; the inverter
            performs the real-time enforcement autonomously. Default False.
    """

    model_config = ConfigDict(extra="forbid")

    zero_exchange: bool = Field(
        default=False,
        description=(
            "Inverter supports a closed-loop zero-exchange firmware mode. "
            "When True, the inverter autonomously holds grid exchange near zero "
            "using local CT measurements."
        ),
        json_schema_extra={"ui_label": "Zero-exchange mode", "ui_group": "advanced"},
    )


class HybridInverterOutputsConfig(BaseModel):
    """MQTT output topic configuration for a hybrid inverter.

    Attributes:
        exchange_mode: Topic to publish the closed-loop zero-exchange mode flag
            (``"true"`` or ``"false"``). Published only when
            ``capabilities.zero_exchange`` is True. Defaults to None (not
            published).
    """

    model_config = ConfigDict(extra="forbid")

    exchange_mode: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the closed-loop zero-exchange mode boolean flag. "
            "Defaults to '{mqtt.topic_prefix}/output/hybrid/{name}/exchange_mode' "
            "when not set."
        ),
        json_schema_extra={
            "ui_label": "Exchange mode topic",
            "ui_group": "advanced",
            "ui_placeholder": "{mqtt.topic_prefix}/output/hybrid/{name}/exchange_mode",
        },
    )


class HybridInverterConfig(BaseModel):
    """Configuration for a DC-coupled hybrid inverter.

    A hybrid inverter integrates a PV MPPT input, a battery DC bus, and an AC
    grid connection into a single unit. Use this device class when PV and
    battery share a DC bus inside the inverter. Use separate ``PvConfig`` and
    ``BatteryConfig`` for AC-coupled systems where each device has its own
    dedicated inverter.

    The key difference from AC-coupled systems is the DC bus power balance:
    PV can charge the battery directly without going through AC conversion, and
    the inverter applies a single conversion efficiency for both import and
    export directions.

    Attributes:
        capacity_kwh: Usable battery capacity in kWh.
        min_soc_kwh: Minimum battery SOC the solver may dispatch to, in kWh.
        max_charge_kw: Maximum DC charge power delivered to the battery cells,
            in kW. This is the DC bus side limit; the AC import limit is
            derived as ``max_charge_kw / inverter_efficiency``.
        max_discharge_kw: Maximum DC discharge power drawn from the battery
            cells, in kW.
        battery_charge_efficiency: Fraction of DC bus power that reaches the
            battery cells during charging. Accounts for BMS and cell losses.
            Must be in (0, 1].
        battery_discharge_efficiency: Fraction of battery cell energy that
            appears on the DC bus during discharge. Must be in (0, 1].
        inverter_efficiency: Round-trip AC-to-DC (and DC-to-AC) conversion
            efficiency of the shared inverter stage. Applied symmetrically to
            both import and export directions. Must be in (0, 1].
        max_pv_kw: Peak PV power at the MPPT input in kW. Used to clip
            unreasonably large forecast values that may indicate sensor errors.
        wear_cost_eur_per_kwh: Battery degradation cost per kWh of DC
            throughput (charge + discharge). In EUR.
        topic_pv_forecast: MQTT topic publishing the per-step PV DC power
            forecast in kW for this inverter's MPPT input.
        inputs: Optional MQTT input topic configuration for live battery
            state readings (SOC). When None, the solver uses the initial SOC
            from the previous step.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this hybrid inverter used in MQTT topics and automations. For example: 'hybrid_inv' or 'solis_hybrid'."})

    capacity_kwh: float = Field(gt=0, description="Usable battery capacity in kWh.", json_schema_extra={"ui_label": "Battery capacity (kWh)", "ui_group": "basic"})
    min_soc_kwh: float = Field(ge=0, default=0.0, description="Minimum SOC in kWh.", json_schema_extra={"ui_label": "Minimum SOC (kWh)", "ui_group": "basic"})
    max_charge_kw: float = Field(gt=0, description="Maximum DC charge power to battery cells in kW.", json_schema_extra={"ui_label": "Max charge power (kW)", "ui_group": "basic"})
    max_discharge_kw: float = Field(
        gt=0, description="Maximum DC discharge power from battery cells in kW.",
        json_schema_extra={"ui_label": "Max discharge power (kW)", "ui_group": "basic"},
    )
    battery_charge_efficiency: float = Field(
        gt=0,
        le=1.0,
        default=0.95,
        description="Efficiency of battery charge process (DC bus to cell storage).",
        json_schema_extra={"ui_label": "Battery charge efficiency", "ui_group": "advanced"},
    )
    battery_discharge_efficiency: float = Field(
        gt=0,
        le=1.0,
        default=0.95,
        description="Efficiency of battery discharge process (cell to DC bus).",
        json_schema_extra={"ui_label": "Battery discharge efficiency", "ui_group": "advanced"},
    )
    inverter_efficiency: float = Field(
        gt=0,
        le=1.0,
        default=0.97,
        description="AC-to-DC and DC-to-AC inverter conversion efficiency.",
        json_schema_extra={"ui_label": "Inverter efficiency", "ui_group": "advanced"},
    )
    max_pv_kw: float = Field(
        gt=0, description="Peak PV power at MPPT input in kW. Used to clip forecasts.",
        json_schema_extra={"ui_label": "PV peak power (kW)", "ui_group": "basic"},
    )
    wear_cost_eur_per_kwh: float = Field(
        ge=0,
        default=0.0,
        description=(
            "Battery degradation cost per kWh of DC throughput (charge + discharge), "
            "in EUR. Applied to the DC-side energy flow, before inverter conversion losses. "
            "Set to 0.0 (default) to optimise purely on energy prices. "
            "To estimate: divide the battery replacement cost by the expected lifetime "
            "DC throughput. "
            "Example: a 10 kWh battery costing \u20ac3,000 with 3,000 full cycles "
            "gives \u20ac3,000 / (3,000 \u00d7 10 kWh) = \u20ac0.10/kWh."
        ),
        json_schema_extra={"ui_label": "Wear cost (\u20ac/kWh)", "ui_group": "basic"},
    )
    topic_pv_forecast: str | None = Field(
        default=None,
        description=(
            "MQTT topic for per-step PV DC power forecast in kW. "
            "Defaults to '{mqtt.topic_prefix}/input/hybrid/{name}/pv_forecast' when not set."
        ),
        json_schema_extra={"ui_label": "PV forecast topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/input/hybrid/{name}/pv_forecast"},
    )
    inputs: BatteryInputsConfig | None = Field(
        default_factory=BatteryInputsConfig,
        description="MQTT input topic configuration for live battery SOC readings. Defaults to an empty model so topics are derived with percent unit. Set to null to opt out of MQTT inputs entirely.",
        json_schema_extra={"ui_label": "Input topics", "ui_group": "advanced"},
    )
    optimal_lower_soc_kwh: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Preferred minimum state of charge in kWh. When the SOC falls below "
            "this level, the solver accrues a penalty proportional to the deficit. "
            "Acts as a soft lower bound: the solver may still dispatch below this "
            "level when the price spread justifies it. Must be >= min_soc_kwh and "
            "<= capacity_kwh."
        ),
        json_schema_extra={"ui_label": "Preferred minimum SOC (kWh)", "ui_group": "advanced"},
    )
    soc_low_penalty_eur_per_kwh_h: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Penalty rate for SOC below optimal_lower_soc_kwh, in EUR per kWh of "
            "deficit per hour. Set to 0.0 (default) to disable."
        ),
        json_schema_extra={"ui_label": "Low SOC penalty (\u20ac/kWh\u00b7h)", "ui_group": "advanced"},
    )
    reduce_charge_above_soc_kwh: float | None = Field(
        default=None,
        description=(
            "SOC level in kWh above which max charge power begins to decrease "
            "linearly to reduce_charge_min_kw at capacity_kwh. Must be strictly "
            "between min_soc_kwh and capacity_kwh. Must be set together with "
            "reduce_charge_min_kw."
        ),
        json_schema_extra={"ui_label": "Derate charge above SOC (kWh)", "ui_group": "advanced"},
    )
    reduce_charge_min_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Max charge power in kW at full capacity (capacity_kwh). The charge "
            "power limit decreases linearly from max_charge_kw at "
            "reduce_charge_above_soc_kwh to this value at capacity_kwh. Must be "
            "set together with reduce_charge_above_soc_kwh."
        ),
        json_schema_extra={"ui_label": "Derated charge minimum (kW)", "ui_group": "advanced"},
    )
    reduce_discharge_below_soc_kwh: float | None = Field(
        default=None,
        description=(
            "SOC level in kWh below which max discharge power begins to decrease "
            "linearly to reduce_discharge_min_kw at min_soc_kwh. Must be strictly "
            "between min_soc_kwh and capacity_kwh. Must be set together with "
            "reduce_discharge_min_kw."
        ),
        json_schema_extra={"ui_label": "Derate discharge below SOC (kWh)", "ui_group": "advanced"},
    )
    reduce_discharge_min_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Max discharge power in kW at minimum SOC (min_soc_kwh). The discharge "
            "power limit decreases linearly from max_discharge_kw at "
            "reduce_discharge_below_soc_kwh to this value at min_soc_kwh. Must be "
            "set together with reduce_discharge_below_soc_kwh."
        ),
        json_schema_extra={"ui_label": "Derated discharge minimum (kW)", "ui_group": "advanced"},
    )
    min_charge_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum DC charge power in kW when the inverter is actively charging. "
            "The inverter either charges at this power or above, or stays idle. "
            "Default None = no floor applied."
        ),
        json_schema_extra={"ui_label": "Minimum charge power (kW)", "ui_group": "advanced"},
    )
    min_discharge_kw: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum DC discharge power in kW when the inverter is actively discharging. "
            "Default None = no floor applied."
        ),
        json_schema_extra={"ui_label": "Minimum discharge power (kW)", "ui_group": "advanced"},
    )
    capabilities: HybridInverterCapabilitiesConfig = Field(
        default_factory=HybridInverterCapabilitiesConfig,
        description="Hardware capability flags.",
        json_schema_extra={"ui_label": "Hardware capabilities", "ui_group": "advanced"},
    )
    outputs: HybridInverterOutputsConfig = Field(
        default_factory=HybridInverterOutputsConfig,
        description="MQTT output topic configuration for hybrid inverter control signals.",
        json_schema_extra={"ui_label": "Output topics", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_soc_levels(self) -> "HybridInverterConfig":
        if self.optimal_lower_soc_kwh == 0.0:
            return self
        if self.optimal_lower_soc_kwh < self.min_soc_kwh:
            raise ValueError(
                f"optimal_lower_soc_kwh ({self.optimal_lower_soc_kwh}) must be "
                f">= min_soc_kwh ({self.min_soc_kwh})"
            )
        if self.optimal_lower_soc_kwh > self.capacity_kwh:
            raise ValueError(
                f"optimal_lower_soc_kwh ({self.optimal_lower_soc_kwh}) must be "
                f"<= capacity_kwh ({self.capacity_kwh})"
            )
        return self

    @model_validator(mode="after")
    def _validate_derating(self) -> "HybridInverterConfig":
        """Validate that derating field pairs are set together and within valid ranges."""
        charge_set = (
            self.reduce_charge_above_soc_kwh is not None,
            self.reduce_charge_min_kw is not None,
        )
        if charge_set[0] != charge_set[1]:
            raise ValueError(
                "reduce_charge_above_soc_kwh and reduce_charge_min_kw must be "
                "set together or both left as None."
            )
        if self.reduce_charge_above_soc_kwh is not None:
            if not (self.min_soc_kwh < self.reduce_charge_above_soc_kwh < self.capacity_kwh):
                raise ValueError(
                    f"reduce_charge_above_soc_kwh ({self.reduce_charge_above_soc_kwh}) "
                    f"must be strictly between min_soc_kwh ({self.min_soc_kwh}) and "
                    f"capacity_kwh ({self.capacity_kwh})."
                )

        discharge_set = (
            self.reduce_discharge_below_soc_kwh is not None,
            self.reduce_discharge_min_kw is not None,
        )
        if discharge_set[0] != discharge_set[1]:
            raise ValueError(
                "reduce_discharge_below_soc_kwh and reduce_discharge_min_kw must be "
                "set together or both left as None."
            )
        if self.reduce_discharge_below_soc_kwh is not None:
            if not (self.min_soc_kwh < self.reduce_discharge_below_soc_kwh < self.capacity_kwh):
                raise ValueError(
                    f"reduce_discharge_below_soc_kwh ({self.reduce_discharge_below_soc_kwh}) "
                    f"must be strictly between min_soc_kwh ({self.min_soc_kwh}) and "
                    f"capacity_kwh ({self.capacity_kwh})."
                )
        return self

# ---------------------------------------------------------------------------
# Loads
# ---------------------------------------------------------------------------

class DeferrableLoadConfig(BaseModel):
    """Configuration for a deferrable load (e.g. washing machine, dishwasher).

    A deferrable load runs through a fixed power profile — an ordered sequence
    of per-step power levels — within a window supplied at runtime via MQTT.
    The solver chooses the optimal start time within the window to minimise
    total cost. The run takes exactly ``len(power_profile)`` consecutive steps.

    Using a per-step profile rather than a flat ``power_kw`` allows irregular
    consumption patterns to be represented accurately. A washing machine, for
    example, draws high power during the heating phase, low power during the
    wash cycle, and high power again during the spin cycle.

    Once the load physically starts, the external automation publishes the
    actual start datetime to ``topic_committed_start_time`` (retained). From that point
    mimirheim treats the remaining profile steps as fixed draws — no binary variable
    is needed and the window topics are ignored.

    Attributes:
        power_profile: Per-step power draw, in kW. Must be non-empty. Each
            entry is the power drawn during one time step of the run cycle,
            in the order the steps occur. The number of entries determines
            the run duration: ``len(power_profile)`` steps.
            Example: ``[2.0, 0.8, 0.8, 2.5]`` models a 4-step cycle that
            starts and ends at high power with low power in the middle.
        topic_window_earliest: MQTT topic publishing the earliest permitted
            start datetime (ISO 8601 UTC).
        topic_window_latest: MQTT topic publishing the latest permitted end
            datetime. The load must finish by this time.
        topic_committed_start_time: Optional MQTT topic to which the external automation
            publishes the actual start datetime (ISO 8601 UTC, retained) when
            the load physically begins. When this topic is absent or not
            configured, mimirheim never enters the "running" fixed-draw state for
            this load.
        topic_recommended_start_time: Optional MQTT topic to which mimirheim publishes
            the solver-recommended start datetime (ISO 8601 UTC, retained)
            after each solve cycle. An automation can subscribe to this topic
            to program the appliance at the optimal time. The value is the
            UTC datetime of the first nonzero-setpoint step in the schedule
            for this load. When the load is in running or committed state (no
            binary optimisation occurred), nothing is published.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this deferrable load used in MQTT topics and automations. For example: 'washing_machine' or 'dishwasher'."})

    power_profile: list[float] = Field(
        min_length=1,
        description=(
            "Per-step power draw in kW, one entry per step of the run cycle. "
            "All values must be positive. The array length defines the run duration."
        ),
        json_schema_extra={"ui_label": "Power profile (kW per step)", "ui_group": "basic"},
    )
    topic_window_earliest: str | None = Field(
        default=None,
        description=(
            "MQTT topic for earliest start datetime. "
            "Defaults to '{mqtt.topic_prefix}/input/deferrable/{name}/window_earliest' when not set."
        ),
        json_schema_extra={"ui_label": "Earliest start topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/input/deferrable/{name}/window_earliest"},
    )
    topic_window_latest: str | None = Field(
        default=None,
        description=(
            "MQTT topic for latest end datetime. "
            "Defaults to '{mqtt.topic_prefix}/input/deferrable/{name}/window_latest' when not set."
        ),
        json_schema_extra={"ui_label": "Latest end topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/input/deferrable/{name}/window_latest"},
    )
    topic_committed_start_time: str | None = Field(
        default=None,
        description=(
            "Optional MQTT topic for the actual start datetime published by the automation "
            "when the load physically begins. Retained. When present and current, mimirheim "
            "treats the load as a fixed draw for the remaining profile steps."
        ),
        json_schema_extra={"ui_label": "Committed start time topic", "ui_group": "advanced"},
    )
    topic_recommended_start_time: str | None = Field(
        default=None,
        description=(
            "Optional MQTT topic to which mimirheim publishes the solver-recommended start "
            "datetime (ISO 8601 UTC, retained) after each solve. The value is the UTC "
            "datetime of the first nonzero-setpoint step for this load. Only published "
            "when the load is in binary scheduling state (not running/committed)."
        ),
        json_schema_extra={"ui_label": "Recommended start time topic", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_power_profile(self) -> "DeferrableLoadConfig":
        """All power levels in the profile must be strictly positive."""
        for i, p in enumerate(self.power_profile):
            if p <= 0.0:
                raise ValueError(
                    f"power_profile[{i}] must be > 0, got {p}."
                )
        return self

class StaticLoadConfig(BaseModel):
    """Configuration for a static (inflexible) load.

    Static loads represent household consumption that cannot be shifted or
    controlled — lighting, fridge, TV, cooking, etc. Their power draw is
    provided as a per-step forecast from MQTT. The solver treats these values
    as fixed parameters in the power balance.

    Attributes:
        topic_forecast: MQTT topic from which the per-step base load forecast
            is read, in kW.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this static load used in MQTT topics and automations. For example: 'base_load' or 'house_loads'."})

    topic_forecast: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the per-step base load forecast in kW. "
            "Defaults to '{mqtt.topic_prefix}/input/baseload/{name}/forecast' when not set."
        ),
        json_schema_extra={"ui_label": "Forecast topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/input/baseload/{name}/forecast"},
    )

# ---------------------------------------------------------------------------
# Thermal devices
# ---------------------------------------------------------------------------

class BuildingThermalInputsConfig(BaseModel):
    """MQTT input topic configuration for the building thermal model.

    Both topics are required when the BTM is active. They provide the initial
    condition (current indoor temperature) and the per-step driving parameter
    (outdoor temperature forecast) for the building heat balance equation.

    Attributes:
        topic_current_indoor_temp_c: MQTT topic publishing the current mean
            indoor temperature in degrees Celsius, retained. Published by a
            thermostat, climate entity, or temperature sensor in the home
            automation system.
        topic_outdoor_temp_forecast_c: MQTT topic publishing the per-step
            outdoor temperature forecast as a JSON array of floats, retained.
            One value per 15-minute step, covering at least as many steps as
            the mimirheim horizon. Typically published by a weather integration
            (e.g. Open-Meteo, Met.no) via the home automation system.
    """

    model_config = ConfigDict(extra="forbid")

    topic_current_indoor_temp_c: str | None = Field(
        default=None,
        description=(
            "MQTT topic for current indoor temperature in °C, retained. "
            "Defaults to a path derived from mqtt.topic_prefix and device name."
        ),
        json_schema_extra={"ui_label": "Indoor temperature topic", "ui_group": "advanced"},
    )
    topic_outdoor_temp_forecast_c: str | None = Field(
        default=None,
        description=(
            "MQTT topic for per-step outdoor temperature forecast, "
            "JSON array of floats, retained. "
            "Defaults to a path derived from mqtt.topic_prefix and device name."
        ),
        json_schema_extra={"ui_label": "Outdoor temperature forecast topic", "ui_group": "advanced"},
    )

class BuildingThermalConfig(BaseModel):
    """Static parameters for the building thermal model.

    These parameters describe the thermal behaviour of the building zone being
    controlled. They must be calibrated or estimated from the building's
    construction, insulation, and historical heating data.

    The building is modelled as a single lumped thermal mass (a single-node
    model). This is a first-order approximation suitable for well-mixed spaces
    such as open-plan living areas or underfloor-heated buildings where the
    temperature is reasonably uniform.

    The dynamics equation for each 15-minute step t is:

        T_indoor[t] = alpha * T_prev
                    + (dt / C) * P_heat[t]
                    + beta_outdoor * T_outdoor[t]

    where:
        alpha        = 1 - dt * L / C
        beta_outdoor = dt * L / C
        T_prev       = current_indoor_temp_c for t=0; T_indoor[t-1] for t>0
        P_heat[t]    = thermal power the HP delivers at step t (kW)

    All terms are linear in the solver variables.

    Attributes:
        thermal_capacity_kwh_per_k: Effective thermal mass of the building in
            kWh per degree Kelvin. Represents how much energy is stored or
            released when the indoor temperature changes by 1 °C. Typical
            values range from 3–5 kWh/K for a small well-insulated apartment
            to 15–40 kWh/K for a large passive house with concrete floors.
            Can be estimated from the time the building takes to cool by 1 °C
            when the HP is off in calm weather.
        heat_loss_coeff_kw_per_k: Building heat loss coefficient in kW per
            degree Kelvin of indoor–outdoor temperature difference. At a 15 °C
            delta, a coefficient of 0.5 kW/K means 7.5 kW of heat is needed
            to maintain temperature. Typical range: 0.05 kW/K (very well
            insulated) to 1.5 kW/K (draughty older building).
        comfort_min_c: Minimum acceptable indoor temperature in degrees
            Celsius. The solver will not allow T_indoor to drop below this
            value at any step. Default 19.0 °C.
        comfort_max_c: Maximum acceptable indoor temperature in degrees
            Celsius. Pre-heating is bounded by this ceiling. Default 24.0 °C.
        inputs: MQTT input topic configuration. None is allowed in unit tests
            where live data is injected directly via SpaceHeatingInputs or
            CombiHeatPumpInputs.
    """

    model_config = ConfigDict(extra="forbid")

    thermal_capacity_kwh_per_k: float = Field(
        gt=0,
        description="Building thermal mass in kWh/K.",
        json_schema_extra={"ui_label": "Thermal capacity (kWh/K)", "ui_group": "basic"},
    )
    heat_loss_coeff_kw_per_k: float = Field(
        gt=0,
        description="Building heat loss coefficient in kW/K.",
        json_schema_extra={"ui_label": "Heat loss coefficient (kW/K)", "ui_group": "basic"},
    )
    comfort_min_c: float = Field(
        default=19.0,
        description="Minimum acceptable indoor temperature in °C.",
        json_schema_extra={"ui_label": "Minimum comfort (°C)", "ui_group": "advanced"},
    )
    comfort_max_c: float = Field(
        default=24.0,
        description="Maximum acceptable indoor temperature in °C.",
        json_schema_extra={"ui_label": "Maximum comfort (°C)", "ui_group": "advanced"},
    )
    inputs: BuildingThermalInputsConfig | None = Field(
        default_factory=BuildingThermalInputsConfig,
        description="MQTT input topics. Defaults to an empty model so topics are derived. Set to null to opt out of MQTT inputs entirely.",
        json_schema_extra={"ui_label": "Input topics", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_comfort_range(self) -> "BuildingThermalConfig":
        """Validate that comfort_min_c is strictly less than comfort_max_c."""
        if self.comfort_min_c >= self.comfort_max_c:
            raise ValueError(
                f"comfort_min_c ({self.comfort_min_c}) must be strictly less than "
                f"comfort_max_c ({self.comfort_max_c})."
            )
        return self

class ThermalBoilerInputsConfig(BaseModel):
    """MQTT input topic configuration for a thermal boiler device.

    Attributes:
        topic_current_temp: MQTT topic publishing the current water temperature
            in degrees Celsius. The payload must be a plain numeric string or
            a JSON number. The topic must be retained so mimirheim receives the
            most recent value on reconnect.
    """

    model_config = ConfigDict(extra="forbid")

    topic_current_temp: str | None = Field(
        default=None,
        description=(
            "MQTT topic publishing the current water temperature in °C, retained. "
            "Defaults to '{mqtt.topic_prefix}/input/thermal_boiler/{name}/temp_c' when not set."
        ),
        json_schema_extra={"ui_label": "Current temperature topic", "ui_group": "advanced", "ui_placeholder": "{mqtt.topic_prefix}/input/thermal_boiler/{name}/temp_c"},
    )

class ThermalBoilerConfig(BaseModel):
    """Configuration for a thermal boiler: electric immersion heater or heat pump DHW.

    A resistive immersion heater and a heat pump DHW boiler share the same MILP
    model. The only difference is the coefficient on the electrical input: for a
    resistive element ``cop=1.0`` (1 kWh electric = 1 kWh thermal); for a heat pump
    ``cop >= 2.0`` (1 kWh electric produces 2+ kWh of heat). Use ``min_run_steps``
    to prevent heat pump compressor short-cycling; set it to 0 for resistive elements.

    Attributes:
        volume_liters: Water volume of the tank in litres. Used to compute thermal
            capacity (kWh/K). At 15-minute resolution, 200 L rises roughly 1.3°C
            per kWh of thermal input.
        elec_power_kw: Rated electrical power of the heating element or heat pump
            compressor, in kW. This is the constant draw when the device is active.
        cop: Coefficient of performance. ``cop=1.0`` for resistive elements;
            ``cop=3.0`` means 1 kWh electric produces 3 kWh of heat. Must be
            strictly positive.
        setpoint_c: Target hot water temperature in degrees Celsius. The solver
            will not heat the tank above this temperature.
        min_temp_c: Minimum allowable water temperature in degrees Celsius. The
            solver heats the tank to avoid falling below this level. For legionella
            prevention, 45°C is the recommended lower bound.
        cooling_rate_k_per_hour: Rate at which the tank temperature falls in
            K/hour when the heater is off. Combines standby heat loss through tank
            insulation and expected hot water draws from the household.
        min_run_steps: Minimum number of consecutive 15-minute steps the heater
            must remain on once started. Use 0 or 1 for resistive elements (free
            cycling). Use 4 (one hour) for heat pump compressors to avoid
            short-cycling that accelerates compressor wear.
        wear_cost_eur_per_kwh: Optional cycling cost per kWh of electrical
            consumption, in EUR. Add a small value for heat pump compressors to
            further discourage unnecessary cycling beyond the minimum run
            constraint. Set to 0.0 (default) for resistive elements.
        inputs: Optional MQTT input topic configuration for live water temperature
            readings. When None, the initial temperature must be supplied via
            ``ThermalBoilerInputs.current_temp_c`` in the solve bundle.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this thermal boiler used in MQTT topics and automations. For example: 'hot_water' or 'dhw_tank'."})

    volume_liters: float = Field(gt=0, description="Water volume of the tank in litres.", json_schema_extra={"ui_label": "Tank volume (L)", "ui_group": "basic"})
    elec_power_kw: float = Field(
        gt=0, description="Rated electrical power of the heating element in kW.",
        json_schema_extra={"ui_label": "Electrical power (kW)", "ui_group": "basic"},
    )
    cop: float = Field(
        gt=0,
        default=1.0,
        description="Coefficient of performance. 1.0 = resistive; 2+ = heat pump.",
        json_schema_extra={"ui_label": "Coefficient of performance (COP)", "ui_group": "advanced"},
    )
    setpoint_c: float = Field(description="Target hot water temperature in °C.", json_schema_extra={"ui_label": "Target temperature (°C)", "ui_group": "basic"})
    min_temp_c: float = Field(
        default=40.0, description="Minimum allowable water temperature in °C.",
        json_schema_extra={"ui_label": "Minimum temperature (°C)", "ui_group": "advanced"},
    )
    cooling_rate_k_per_hour: float = Field(
        ge=0,
        description="Tank temperature decay rate in K/hour when the heater is off.",
        json_schema_extra={"ui_label": "Cooling rate (K/h)", "ui_group": "basic"},
    )
    min_run_steps: int = Field(
        ge=0,
        default=0,
        description="Minimum consecutive active steps once started. 0 = free cycling.",
        json_schema_extra={"ui_label": "Minimum run steps", "ui_group": "advanced"},
    )
    wear_cost_eur_per_kwh: float = Field(
        ge=0,
        default=0.0,
        description=(
            "Cycling cost per kWh of electrical consumption, in EUR. "
            "Adds a small penalty to each kWh consumed, discouraging unnecessary "
            "cycling beyond what the minimum run constraint already enforces. "
            "Meaningful only for heat pump compressors where short-cycling causes "
            "wear; set to 0.0 (default) for resistive immersion elements."
        ),
        json_schema_extra={"ui_label": "Wear cost (\u20ac/kWh)", "ui_group": "basic"},
    )
    inputs: ThermalBoilerInputsConfig | None = Field(
        default_factory=ThermalBoilerInputsConfig,
        description="MQTT input topic configuration for live water temperature readings. Defaults to an empty model so topics are derived. Set to null to opt out of MQTT temperature input entirely.",
        json_schema_extra={"ui_label": "Input topics", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_temp_range(self) -> "ThermalBoilerConfig":
        """Validate that min_temp_c is strictly below setpoint_c."""
        if self.min_temp_c >= self.setpoint_c:
            raise ValueError(
                f"min_temp_c ({self.min_temp_c}) must be strictly less than "
                f"setpoint_c ({self.setpoint_c})."
            )
        return self

class HeatingStage(BaseModel):
    """A single operating point on a heat pump's electrical-to-thermal power curve.

    Used in the SOS2 power-stage model for space heating heat pumps. Each stage
    corresponds to a compressor operating mode (off, minimum, half, or full power).

    The first stage in any list of HeatingStage objects must have ``elec_kw=0.0``
    and ``cop=0.0``. This zero-power sentinel allows the SOS2 weight variables to
    represent the heat pump being completely off — without it, the convex combination
    constraint would force some power flow at every step.

    Attributes:
        elec_kw: Electrical power consumed at this operating point, in kW. Must be
            0.0 for the first (off) stage. Subsequent stages must have strictly
            increasing values.
        cop: Coefficient of performance at this operating point. 1 kW of electricity
            produces ``cop`` kW of heat. Must be 0.0 for the zero-power sentinel stage.
    """

    model_config = ConfigDict(extra="forbid")

    elec_kw: float = Field(ge=0.0, description="Electrical power at this stage in kW.", json_schema_extra={"ui_label": "Electrical power (kW)", "ui_group": "basic"})
    cop: float = Field(ge=0.0, description="COP at this stage.", json_schema_extra={"ui_label": "COP", "ui_group": "basic"})

class SpaceHeatingInputsConfig(BaseModel):
    """MQTT input topic configuration for a space heating heat pump.

    Attributes:
        topic_heat_needed_kwh: MQTT topic publishing the total thermal energy
            in kWh that must be produced this horizon. Computed externally
            (typically by the home automation system from degree-days data)
            and published retained so mimirheim receives it on reconnect.
            The external system is responsible for subtracting heat already
            produced today before publishing. Set to 0.0 when no heating is
            needed.
        topic_heat_produced_today_kwh: Optional informational topic reporting
            accumulated thermal output produced today. Not read by the solver;
            kept here so the MQTT topic structure is documented alongside the
            required input.
    """

    model_config = ConfigDict(extra="forbid")

    topic_heat_needed_kwh: str | None = Field(
        default=None,
        description=(
            "MQTT topic publishing remaining heat needed this horizon in kWh, retained. "
            "Defaults to a path derived from mqtt.topic_prefix and device name."
        ),
        json_schema_extra={"ui_label": "Heat needed topic", "ui_group": "advanced"},
    )
    topic_heat_produced_today_kwh: str | None = Field(
        default=None,
        description=(
            "Optional informational topic for accumulated heat produced today in kWh."
        ),
        json_schema_extra={"ui_label": "Heat produced today topic", "ui_group": "advanced"},
    )

class SpaceHeatingConfig(BaseModel):
    """Configuration for a space heating heat pump.

    Two control modes are supported. Exactly one must be configured:

    **On/off mode**: Provide ``elec_power_kw`` and ``cop``. The solver uses a
    single binary variable per step — the HP either runs at full rated power
    or is completely off. Suitable for fixed-speed compressors.

    **Power-stage (SOS2) mode**: Provide ``stages`` — a list of ``HeatingStage``
    objects. Stage 0 must be the zero-power off sentinel. The solver uses SOS2
    weight variables per step to model partial-load operation between any two
    adjacent operating points. Suitable for inverter-driven (variable-speed)
    compressors.

    Attributes:
        elec_power_kw: Rated electrical power for on/off mode, in kW. Mutually
            exclusive with ``stages``.
        cop: Coefficient of performance for on/off mode. Mutually exclusive
            with ``stages``.
        stages: List of operating points for SOS2 power-stage mode. Must contain
            at least 2 entries. Stage 0 must have ``elec_kw=0.0, cop=0.0``.
            Mutually exclusive with ``elec_power_kw`` and ``cop``.
        min_run_steps: Minimum consecutive 15-minute steps the HP must run
            once started. Use 4 (one hour) for most heat pump compressors.
            Set to 0 or 1 to disable the minimum run constraint.
        wear_cost_eur_per_kwh: Cycling cost per kWh of electrical consumption
            in EUR. Discourages unnecessary cycling on top of the minimum run
            constraint.
        inputs: Optional MQTT input topic configuration. Required for live
            operation; may be None in unit tests.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this space heating heat pump used in MQTT topics and automations. For example: 'space_hp' or 'underfloor_hp'."})

    elec_power_kw: float | None = Field(
        default=None, gt=0, description="Rated electrical power for on/off mode in kW.",
        json_schema_extra={"ui_label": "Electrical power (kW)", "ui_group": "basic"},
    )
    cop: float | None = Field(
        default=None, gt=0, description="COP for on/off mode.",
        json_schema_extra={"ui_label": "COP (on/off mode)", "ui_group": "basic"},
    )
    stages: list[HeatingStage] | None = Field(
        default=None,
        min_length=2,
        description="Operating points for SOS2 power-stage mode.",
        json_schema_extra={"ui_label": "Operating stages", "ui_group": "basic"},
    )
    min_run_steps: int = Field(
        ge=0, default=4, description="Minimum consecutive active steps once started.",
        json_schema_extra={"ui_label": "Minimum run steps", "ui_group": "advanced"},
    )
    wear_cost_eur_per_kwh: float = Field(
        ge=0,
        default=0.0,
        description=(
            "Cycling cost per kWh of electrical consumption, in EUR. "
            "Adds a small penalty to each kWh consumed, discouraging unnecessary "
            "cycling beyond what the minimum run constraint already enforces. "
            "Set to 0.0 (default) for minimal cycling cost modelling."
        ),
        json_schema_extra={"ui_label": "Wear cost (\u20ac/kWh)", "ui_group": "basic"},
    )
    inputs: SpaceHeatingInputsConfig | None = Field(
        default_factory=SpaceHeatingInputsConfig,
        description="MQTT input topic configuration. Defaults to an empty model so topics are derived. Set to null to opt out of MQTT inputs entirely.",
        json_schema_extra={"ui_label": "Input topics", "ui_group": "advanced"},
    )
    building_thermal: BuildingThermalConfig | None = Field(
        default=None,
        description=(
            "Optional building thermal model. When set, the solver tracks indoor "
            "temperature as a per-step state variable and enforces a comfort band "
            "instead of the degree-days total-heat lower bound."
        ),
        json_schema_extra={"ui_label": "Building thermal model", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_mode(self) -> "SpaceHeatingConfig":
        """Validate that exactly one control mode (on/off or staged) is configured."""
        on_off = self.elec_power_kw is not None or self.cop is not None
        staged = self.stages is not None
        if on_off and staged:
            raise ValueError(
                "Provide either (elec_power_kw + cop) for on/off mode or stages "
                "for power-stage mode, not both."
            )
        if not on_off and not staged:
            raise ValueError("Provide either (elec_power_kw + cop) or stages.")
        if on_off and (self.elec_power_kw is None or self.cop is None):
            raise ValueError("On/off mode requires both elec_power_kw and cop.")
        if staged:
            powers = [s.elec_kw for s in self.stages]
            if powers[0] != 0.0:
                raise ValueError(
                    f"First stage must have elec_kw=0.0 (off sentinel), got {powers[0]}."
                )
            if len(powers) != len(set(powers)):
                raise ValueError(
                    "Stage elec_kw values must be strictly increasing (duplicates not allowed)."
                )
        return self

class CombiHeatPumpInputsConfig(BaseModel):
    """MQTT input topics for live combined DHW and space heating heat pump state.

    Attributes:
        topic_current_temp: MQTT topic publishing the current DHW water temperature
            in degrees Celsius, retained. Used to initialise the tank temperature
            model at each solve cycle.
        topic_heat_needed_kwh: MQTT topic publishing the total space heating thermal
            energy in kWh required this horizon, retained. Computed externally
            from degree-days data by the home automation system.
    """

    model_config = ConfigDict(extra="forbid")

    topic_current_temp: str | None = Field(
        default=None,
        description=(
            "MQTT topic for DHW water temperature in °C, retained. "
            "Defaults to a path derived from mqtt.topic_prefix and device name."
        ),
        json_schema_extra={"ui_label": "DHW temperature topic", "ui_group": "advanced"},
    )
    topic_heat_needed_kwh: str | None = Field(
        default=None,
        description=(
            "MQTT topic for space heating demand in kWh this horizon, retained. "
            "Defaults to a path derived from mqtt.topic_prefix and device name."
        ),
        json_schema_extra={"ui_label": "Heat needed topic", "ui_group": "advanced"},
    )

class CombiHeatPumpConfig(BaseModel):
    """Configuration for a combined DHW and space heating heat pump.

    A combi heat pump has a single compressor that can operate in two mutually
    exclusive modes each time step: DHW mode heats the hot water storage tank;
    SH mode delivers heat to the space heating circuit (e.g. underfloor heating
    or radiators). The device cannot run in both modes simultaneously.

    DHW mode typically has a lower COP than SH mode because heating water to a
    high setpoint (55 °C or above) requires a larger temperature lift than
    heating a low-temperature floor circuit (35 °C). The separate ``cop_dhw``
    and ``cop_sh`` parameters capture this asymmetry and influence which mode
    the solver prefers at each price step.

    The tank model (temperature dynamics, hard bounds) is identical to
    ``ThermalBoilerDevice`` (plan 25). The space heating model (degree-days
    demand, minimum run) is identical to ``SpaceHeatingDevice`` in on/off mode
    (plan 26). The unique constraint in this device is the mutual exclusion
    between the two modes.

    Attributes:
        elec_power_kw: Rated electrical power in kW. Applied at this level in
            both DHW and SH modes (same compressor power for both).
        cop_dhw: COP in DHW (domestic hot water) mode. Lower than cop_sh
            because of the higher temperature lift to the tank setpoint.
        cop_sh: COP in space heating mode.
        volume_liters: DHW tank water volume in litres.
        setpoint_c: Target DHW water temperature in degrees Celsius. The
            solver will not heat the tank above this temperature.
        min_temp_c: Minimum allowable DHW water temperature in degrees Celsius.
            The solver enforces this as a hard constraint. Must be strictly
            less than setpoint_c.
        cooling_rate_k_per_hour: DHW tank cooling rate in K/h, combining
            insulation losses and expected hot water draws.
        min_run_steps: Minimum consecutive 15-minute steps the HP must run
            once started, across both modes combined. A mode switch (e.g.
            DHW→SH) within a running block counts as continuous operation.
        wear_cost_eur_per_kwh: Cycling cost per kWh of electrical consumption
            in EUR. Discourages unnecessary cycling.
        inputs: MQTT input topic configuration. Required for live operation.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"ui_instance_name_description": "A short identifier for this combi heat pump used in MQTT topics and automations. For example: 'combi_hp' or 'main_hp'."})

    elec_power_kw: float = Field(gt=0, description="Rated electrical power in kW.", json_schema_extra={"ui_label": "Electrical power (kW)", "ui_group": "basic"})
    cop_dhw: float = Field(gt=0, description="COP in DHW mode.", json_schema_extra={"ui_label": "COP (DHW mode)", "ui_group": "basic"})
    cop_sh: float = Field(gt=0, description="COP in space heating mode.", json_schema_extra={"ui_label": "COP (space heating mode)", "ui_group": "basic"})
    volume_liters: float = Field(gt=0, description="DHW tank volume in litres.", json_schema_extra={"ui_label": "DHW tank volume (L)", "ui_group": "basic"})
    setpoint_c: float = Field(description="DHW target temperature in °C.", json_schema_extra={"ui_label": "DHW target temperature (°C)", "ui_group": "basic"})
    min_temp_c: float = Field(default=40.0, description="DHW minimum temperature in °C.", json_schema_extra={"ui_label": "DHW minimum temperature (°C)", "ui_group": "advanced"})
    cooling_rate_k_per_hour: float = Field(ge=0, description="Tank cooling rate in K/h.", json_schema_extra={"ui_label": "Cooling rate (K/h)", "ui_group": "basic"})
    min_run_steps: int = Field(ge=0, default=4, description="Minimum consecutive active steps.", json_schema_extra={"ui_label": "Minimum run steps", "ui_group": "advanced"})
    wear_cost_eur_per_kwh: float = Field(
        ge=0,
        default=0.0,
        description=(
            "Cycling cost per kWh of electrical consumption, in EUR. "
            "Adds a small penalty to each kWh consumed, discouraging unnecessary "
            "cycling beyond what the minimum run constraint already enforces. "
            "Set to 0.0 (default) for minimal cycling cost modelling."
        ),
        json_schema_extra={"ui_label": "Wear cost (\u20ac/kWh)", "ui_group": "basic"},
    )
    inputs: CombiHeatPumpInputsConfig | None = Field(
        default_factory=CombiHeatPumpInputsConfig,
        description="MQTT input topic configuration. Defaults to an empty model so topics are derived. Set to null to opt out of MQTT inputs entirely.",
        json_schema_extra={"ui_label": "Input topics", "ui_group": "advanced"},
    )
    building_thermal: BuildingThermalConfig | None = Field(
        default=None,
        description=(
            "Optional building thermal model for the SH mode. When set, the solver "
            "tracks indoor temperature as a per-step state variable and enforces a "
            "comfort band instead of the degree-days total-heat lower bound for SH. "
            "DHW mode is unaffected."
        ),
        json_schema_extra={"ui_label": "Building thermal model", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _validate_temp_range(self) -> "CombiHeatPumpConfig":
        """Validate that min_temp_c is strictly less than setpoint_c."""
        if self.min_temp_c >= self.setpoint_c:
            raise ValueError(
                f"min_temp_c ({self.min_temp_c}) must be strictly less than "
                f"setpoint_c ({self.setpoint_c})."
            )
        return self

# ---------------------------------------------------------------------------
# Root configuration model
# ---------------------------------------------------------------------------

class MimirheimConfig(BaseModel):
    """Root configuration model for mimirheim.

    All named device sections (batteries, pv_arrays, ev_chargers,
    deferrable_loads, static_loads) are keyed by a user-chosen device name.
    That name becomes the key in MQTT output payloads and must be unique
    across all device sections — enforced by the device_names_unique validator.

    Attributes:
        batteries: Named battery devices.
        pv_arrays: Named PV array devices.
        ev_chargers: Named EV charger devices.
        deferrable_loads: Named deferrable load devices.
        static_loads: Named static load devices.
        grid: Grid connection parameters. Exactly one per mimirheim instance.
        objectives: Objective function parameters.
        constraints: Hard constraints on grid power.
        solver: Solver tuning parameters (horizon cap, thread count).
        readiness: Forecast coverage thresholds. Controls when mimirheim is willing
            to solve and when it emits warnings about short horizons or gaps.
        mqtt: MQTT broker connection parameters.
        outputs: MQTT output topic names.
        inputs: MQTT input topic overrides. All fields default to the standard
            derived topic when not set.
        debug: Debug and diagnostic settings.
        reporting: Reporting daemon settings. Independent of ``debug``; controls
            production dump writing and MQTT notification for mimirheim-reporter.
    """

    model_config = ConfigDict(extra="forbid")

    batteries: dict[str, BatteryConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "Batteries", "ui_group": "basic"})
    pv_arrays: dict[str, PvConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "PV arrays", "ui_group": "basic"})
    ev_chargers: dict[str, EvConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "EV chargers", "ui_group": "basic"})
    deferrable_loads: dict[str, DeferrableLoadConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "Deferrable loads", "ui_group": "advanced"})
    static_loads: dict[str, StaticLoadConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "Static loads", "ui_group": "basic"})
    hybrid_inverters: dict[str, HybridInverterConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "Hybrid inverters", "ui_group": "basic"})
    thermal_boilers: dict[str, ThermalBoilerConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "Thermal boilers", "ui_group": "advanced"})
    space_heating_hps: dict[str, SpaceHeatingConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "Space heating heat pumps", "ui_group": "advanced"})
    combi_heat_pumps: dict[str, CombiHeatPumpConfig] = Field(default_factory=dict, json_schema_extra={"ui_label": "Combi heat pumps", "ui_group": "advanced"})
    grid: GridConfig = Field(json_schema_extra={"ui_label": "Grid connection", "ui_group": "basic"})
    objectives: ObjectivesConfig = Field(default_factory=ObjectivesConfig, json_schema_extra={"ui_label": "Objectives", "ui_group": "advanced"})
    constraints: ConstraintsConfig = Field(default_factory=ConstraintsConfig, json_schema_extra={"ui_label": "Constraints", "ui_group": "advanced"})
    solver: SolverConfig = Field(default_factory=SolverConfig, json_schema_extra={"ui_label": "Solver", "ui_group": "advanced"})
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig, json_schema_extra={"ui_label": "Readiness", "ui_group": "advanced"})
    mqtt: MqttConfig = Field(json_schema_extra={"ui_label": "MQTT", "ui_group": "basic"})
    outputs: OutputsConfig = Field(default_factory=OutputsConfig, json_schema_extra={"ui_label": "Output topics", "ui_group": "advanced"})
    inputs: InputsConfig = Field(default_factory=InputsConfig, json_schema_extra={"ui_label": "Input topics", "ui_group": "advanced"})
    homeassistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig, json_schema_extra={"ui_label": "Home Assistant", "ui_group": "advanced"})
    debug: DebugConfig = Field(default_factory=DebugConfig, json_schema_extra={"ui_label": "Debug", "ui_group": "advanced"})
    control: ControlConfig = Field(default_factory=ControlConfig, json_schema_extra={"ui_label": "Control", "ui_group": "advanced"})
    reporting: ReportingConfig = Field(default_factory=ReportingConfig, json_schema_extra={"ui_label": "Reporting", "ui_group": "advanced"})

    @model_validator(mode="after")
    def device_names_unique(self) -> "MimirheimConfig":
        """Ensure no device name appears in more than one section.

        Device names are used as keys in MQTT output payloads. A duplicate
        name would cause one device's output to silently overwrite another's,
        violating the output schema.
        """
        all_names: list[str] = [
            *self.batteries,
            *self.pv_arrays,
            *self.ev_chargers,
            *self.deferrable_loads,
            *self.static_loads,
            *self.hybrid_inverters,
            *self.thermal_boilers,
            *self.space_heating_hps,
            *self.combi_heat_pumps,
        ]
        seen: set[str] = set()
        duplicates: set[str] = set()
        for name in all_names:
            if name in seen:
                duplicates.add(name)
            seen.add(name)
        if duplicates:
            raise ValueError(
                f"Device names must be unique across all sections. "
                f"Duplicate names found: {sorted(duplicates)}"
            )
        return self

    @model_validator(mode="after")
    def _set_client_id_default(self) -> "MimirheimConfig":
        """Set the default MQTT client identifier when not explicitly configured."""
        if not self.mqtt.client_id:
            self.mqtt.client_id = "mimir"
        return self

    @model_validator(mode="after")
    def _derive_global_topics(self) -> "MimirheimConfig":
        """Fill in global topic fields that were not explicitly set.

        Any global topic field left as None in the YAML is derived from
        mqtt.topic_prefix using the standard naming convention. Explicit values
        are preserved unchanged. After this validator, no global topic field is
        None; downstream code can read these fields without a fallback.

        See IMPLEMENTATION_DETAILS §14 for the full naming convention.
        """
        p = self.mqtt.topic_prefix

        if self.outputs.schedule is None:
            self.outputs.schedule = _topics.schedule_topic(p)
        if self.outputs.current is None:
            self.outputs.current = _topics.current_topic(p)
        if self.outputs.last_solve is None:
            self.outputs.last_solve = _topics.last_solve_topic(p)
        if self.outputs.availability is None:
            self.outputs.availability = _topics.availability_topic(p)

        if self.inputs.prices is None:
            self.inputs.prices = _topics.prices_topic(p)

        if self.reporting.notify_topic is None:
            self.reporting.notify_topic = _topics.dump_available_topic(p)

        return self

    @model_validator(mode="after")
    def _derive_device_topics(self) -> "MimirheimConfig":
        """Fill in per-device topic fields that were not explicitly set.

        Topic strings are derived from mqtt.topic_prefix and the device name
        using the convention documented in IMPLEMENTATION_DETAILS §14.

        After this validator, no topic field on any configured device is None.
        Output topics are derived regardless of whether the related capability
        is enabled; they are simply never published for disabled capabilities.
        """
        p = self.mqtt.topic_prefix

        for name, cfg in self.batteries.items():
            if cfg.inputs is not None and cfg.inputs.soc.topic is None:
                cfg.inputs.soc.topic = _topics.battery_soc_topic(p, name)
            if cfg.outputs.exchange_mode is None:
                cfg.outputs.exchange_mode = _topics.battery_exchange_mode_topic(p, name)

        for name, cfg in self.ev_chargers.items():
            if cfg.inputs is not None:
                if cfg.inputs.soc.topic is None:
                    cfg.inputs.soc.topic = _topics.ev_soc_topic(p, name)
                if cfg.inputs.plugged_in_topic is None:
                    cfg.inputs.plugged_in_topic = _topics.ev_plugged_in_topic(p, name)
            if cfg.outputs.exchange_mode is None:
                cfg.outputs.exchange_mode = _topics.ev_exchange_mode_topic(p, name)
            if cfg.outputs.loadbalance_cmd is None:
                cfg.outputs.loadbalance_cmd = _topics.ev_loadbalance_topic(p, name)

        for name, cfg in self.pv_arrays.items():
            if cfg.topic_forecast is None:
                cfg.topic_forecast = _topics.pv_forecast_topic(p, name)
            if cfg.outputs.power_limit_kw is None:
                cfg.outputs.power_limit_kw = _topics.pv_power_limit_topic(p, name)
            if cfg.outputs.zero_export_mode is None:
                cfg.outputs.zero_export_mode = _topics.pv_zero_export_topic(p, name)
            if cfg.outputs.on_off_mode is None:
                cfg.outputs.on_off_mode = _topics.pv_on_off_topic(p, name)
            if cfg.outputs.is_curtailed is None:
                cfg.outputs.is_curtailed = _topics.pv_is_curtailed_topic(p, name)

        for name, cfg in self.static_loads.items():
            if cfg.topic_forecast is None:
                cfg.topic_forecast = _topics.baseload_forecast_topic(p, name)

        for name, cfg in self.hybrid_inverters.items():
            if cfg.inputs is not None and cfg.inputs.soc.topic is None:
                cfg.inputs.soc.topic = _topics.hybrid_soc_topic(p, name)
            if cfg.topic_pv_forecast is None:
                cfg.topic_pv_forecast = _topics.hybrid_pv_forecast_topic(p, name)

        for name, cfg in self.deferrable_loads.items():
            if cfg.topic_window_earliest is None:
                cfg.topic_window_earliest = _topics.deferrable_window_earliest_topic(p, name)
            if cfg.topic_window_latest is None:
                cfg.topic_window_latest = _topics.deferrable_window_latest_topic(p, name)
            if cfg.topic_committed_start_time is None:
                cfg.topic_committed_start_time = _topics.deferrable_committed_start_topic(p, name)
            if cfg.topic_recommended_start_time is None:
                cfg.topic_recommended_start_time = _topics.deferrable_recommended_start_topic(p, name)

        for name, cfg in self.thermal_boilers.items():
            if cfg.inputs is not None and cfg.inputs.topic_current_temp is None:
                cfg.inputs.topic_current_temp = _topics.thermal_boiler_temp_topic(p, name)

        for name, cfg in self.space_heating_hps.items():
            if cfg.inputs is not None:
                if cfg.inputs.topic_heat_needed_kwh is None:
                    cfg.inputs.topic_heat_needed_kwh = _topics.space_heating_heat_needed_topic(p, name)
                if cfg.inputs.topic_heat_produced_today_kwh is None:
                    cfg.inputs.topic_heat_produced_today_kwh = _topics.space_heating_heat_produced_topic(p, name)
            if cfg.building_thermal is not None and cfg.building_thermal.inputs is not None:
                btm = cfg.building_thermal.inputs
                if btm.topic_current_indoor_temp_c is None:
                    btm.topic_current_indoor_temp_c = _topics.space_heating_btm_indoor_topic(p, name)
                if btm.topic_outdoor_temp_forecast_c is None:
                    btm.topic_outdoor_temp_forecast_c = _topics.space_heating_btm_outdoor_topic(p, name)

        for name, cfg in self.combi_heat_pumps.items():
            if cfg.inputs is not None:
                if cfg.inputs.topic_current_temp is None:
                    cfg.inputs.topic_current_temp = _topics.combi_hp_temp_topic(p, name)
                if cfg.inputs.topic_heat_needed_kwh is None:
                    cfg.inputs.topic_heat_needed_kwh = _topics.combi_hp_heat_needed_topic(p, name)
            if cfg.building_thermal is not None and cfg.building_thermal.inputs is not None:
                btm = cfg.building_thermal.inputs
                if btm.topic_current_indoor_temp_c is None:
                    btm.topic_current_indoor_temp_c = _topics.combi_hp_btm_indoor_topic(p, name)
                if btm.topic_outdoor_temp_forecast_c is None:
                    btm.topic_outdoor_temp_forecast_c = _topics.combi_hp_btm_outdoor_topic(p, name)

        return self

def load_config(path: str) -> MimirheimConfig:
    """Load and validate a mimirheim YAML configuration file.

    Args:
        path: Filesystem path to the YAML configuration file.

    Returns:
        Validated MimirheimConfig instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        yaml.YAMLError: If the file is not valid YAML.
        pydantic.ValidationError: If the config does not conform to the schema.
    """
    with open(path) as f:
        raw = yaml.safe_load(f)
    return MimirheimConfig.model_validate(raw)
