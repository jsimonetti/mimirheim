"""ReadinessState — per-topic freshness tracking and SolveBundle assembly.

This module tracks the latest validated input for each expected MQTT topic and
assembles a complete ``SolveBundle`` when all required inputs are present and
sufficiently covered.

``ReadinessState`` is the bridge between the MQTT IO layer (which receives raw
messages and calls ``update()``) and the solver core (which calls ``snapshot()``
to get a validated bundle). The IO layer and the solver core never communicate
directly; they share only the ``ReadinessState`` instance.

Readiness model:

    **Live sensor readings** (battery SOC, EV SOC, plug state):
        Presence-only check. A reading blocks the solve only if it has never
        been received. Once a value is stored it remains valid indefinitely;
        the most recently published retained message is always authoritative.
        This relies on MQTT retain: mimirheim reads the stored value on (re)connect
        so there is no startup gap, and the broker-side retained message is the
        single source of truth for current hardware state.

    **Forecast series** (prices, PV generation, base load):
        Coverage-based freshness. The message receipt time is irrelevant
        (day-ahead prices published hours ago are still valid). Instead,
        ``ReadinessState`` checks whether the stored timestamped data series
        collectively cover at least ``readiness.min_horizon_hours`` of future
        steps from the current solve start. If they do not, the solve is
        blocked. On short but non-zero coverage a warning is logged.

The trigger topic ``{prefix}/input/trigger`` drives solves. When a message
arrives on the trigger topic and readiness is met, a ``SolveBundle`` is placed
on ``solve_queue``. Data topic messages update stored values but never directly
trigger a solve. This decouples data ingestion from solve scheduling.

Thread safety:
    All public methods acquire ``_lock`` before touching shared state. The lock
    is held only long enough to update or read ``_entries`` — never during a
    solve. This matches the concurrency model described in
    ``IMPLEMENTATION_DETAILS §9``.

This module imports from ``mimirheim.core`` and ``mimirheim.config`` but never from
``mimirheim.io``.
"""

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import (
    BatteryInputs,
    CombiHeatPumpInputs,
    DeferrableWindow,
    EvInputs,
    HybridInverterInputs,
    PowerForecastStep,
    PriceStep,
    SolveBundle,
    SpaceHeatingInputs,
    ThermalBoilerInputs,
)
from mimirheim.core.forecast import (
    compute_horizon_steps,
    find_gaps,
    floor_to_15min,
    resample_power,
    resample_prices,
)

logger = logging.getLogger("mimirheim.readiness")


class ReadinessState:
    """Tracks per-topic freshness and assembles ``SolveBundle`` when ready.

    One instance lives for the lifetime of the mimirheim process. The MQTT
    ``on_message`` callback calls ``update()`` on every incoming message; the
    trigger-topic handler calls ``snapshot()`` to obtain a ready bundle.

    Forecast topics use coverage-based readiness: the stored
    ``list[PriceStep]`` or ``list[PowerForecastStep]`` must collectively cover
    at least ``config.readiness.min_horizon_hours`` of future steps. Live
    sensor topics use presence-only readiness: the solving is blocked only if
    no value has ever been received on the topic.

    Attributes:
        _lock: Threading lock protecting all instance state.
        _entries: Maps MQTT topic → (validated_input, received_at).
        _sensor_topics: Set of sensor topics that must have been received at
            least once before a solve is permitted.
        _forecast_topics: Set of topics that carry timestamped forecast series.
        _prices_topic: Topic string for horizon prices.
        _pv_topics: Ordered list of PV forecast topic strings.
        _load_topics: Ordered list of static-load forecast topic strings.
        _current_strategy: The currently active strategy string.
        _strategy_topic: MQTT topic for strategy updates.
        _config: Static system configuration.
        _min_horizon_steps: Minimum steps (from config) to attempt a solve.
        _warn_below_steps: Steps threshold below which a warning is logged.
        _max_gap_hours: Gap threshold for internal forecast gap warnings.
    """

    def __init__(self, config: MimirheimConfig) -> None:
        """Initialise the readiness tracker.

        Derives all topic sets, staleness windows, and coverage thresholds
        from the device configuration.

        Args:
            config: Static system configuration loaded at startup.
        """
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[Any, datetime]] = {}
        self._current_strategy: str = "minimize_cost"
        self._config = config

        prefix = config.mqtt.topic_prefix
        self._strategy_topic = f"{prefix}/input/strategy"
        self._prices_topic = config.inputs.prices or f"{prefix}/input/prices"

        # Convert hours → 15-min step counts.
        self._min_horizon_steps = max(1, int(config.readiness.min_horizon_hours * 4))
        self._warn_below_steps = int(config.readiness.warn_below_hours * 4)
        self._max_gap_hours = config.readiness.max_gap_hours

        # Sensor topics: presence-only check. These must have been received at
        # least once before a solve is permitted. No staleness window is applied;
        # the broker's retained message is the authoritative current value.
        self._sensor_topics: set[str] = set()
        for bat_cfg in config.batteries.values():
            if bat_cfg.inputs is not None:
                self._sensor_topics.add(bat_cfg.inputs.soc.topic)

        for ev_cfg in config.ev_chargers.values():
            if ev_cfg.inputs is not None:
                self._sensor_topics.add(ev_cfg.inputs.soc.topic)
                self._sensor_topics.add(ev_cfg.inputs.plugged_in_topic)

        # Hybrid inverter SOC (plan 24): presence-only sensor check.
        # The PV forecast topic is also presence-gated; it is resampled in snapshot().
        for hi_cfg in config.hybrid_inverters.values():
            if hi_cfg.inputs is not None:
                self._sensor_topics.add(hi_cfg.inputs.soc.topic)
            # PV forecast is always required whenever the device is in the config.
            self._sensor_topics.add(hi_cfg.topic_pv_forecast)

        # Thermal boiler current temperature (plan 25): presence-only sensor check.
        for tb_cfg in config.thermal_boilers.values():
            if tb_cfg.inputs is not None:
                self._sensor_topics.add(tb_cfg.inputs.topic_current_temp)

        # Space heating demand (plan 26): presence-only sensor check.
        # When BTM is also configured, the indoor temp and outdoor forecast
        # topics are also required.
        for sh_cfg in config.space_heating_hps.values():
            if sh_cfg.inputs is not None:
                self._sensor_topics.add(sh_cfg.inputs.topic_heat_needed_kwh)
            if (
                sh_cfg.building_thermal is not None
                and sh_cfg.building_thermal.inputs is not None
            ):
                self._sensor_topics.add(sh_cfg.building_thermal.inputs.topic_current_indoor_temp_c)
                self._sensor_topics.add(sh_cfg.building_thermal.inputs.topic_outdoor_temp_forecast_c)

        # Combi HP temp + SH demand (plan 27): presence-only sensor checks.
        # When BTM is also configured, the indoor temp and outdoor forecast
        # topics are also required.
        for chp_cfg in config.combi_heat_pumps.values():
            if chp_cfg.inputs is not None:
                self._sensor_topics.add(chp_cfg.inputs.topic_current_temp)
                self._sensor_topics.add(chp_cfg.inputs.topic_heat_needed_kwh)
            if (
                chp_cfg.building_thermal is not None
                and chp_cfg.building_thermal.inputs is not None
            ):
                self._sensor_topics.add(chp_cfg.building_thermal.inputs.topic_current_indoor_temp_c)
                self._sensor_topics.add(
                    chp_cfg.building_thermal.inputs.topic_outdoor_temp_forecast_c
                )

        # Forecast topics: coverage-based readiness.
        self._pv_topics: list[str] = [cfg.topic_forecast for cfg in config.pv_arrays.values()]
        self._load_topics: list[str] = [cfg.topic_forecast for cfg in config.static_loads.values()]
        self._forecast_topics: set[str] = (
            {self._prices_topic} | set(self._pv_topics) | set(self._load_topics)
        )

        # Deferrable load topics: window endpoints and optional start_time.
        # None of these block readiness — all are optional inputs.
        # _start_time_topics maps start_time topic → device name so snapshot()
        # can populate deferrable_start_times without iterating config again.
        self._start_time_topics: dict[str, str] = {}  # topic → device name
        for name, dl_cfg in config.deferrable_loads.items():
            if dl_cfg.topic_committed_start_time is not None:
                self._start_time_topics[dl_cfg.topic_committed_start_time] = name

    def update(self, topic: str, validated_input: Any) -> None:
        """Record a fresh validated input for the given topic. Thread-safe.

        For the strategy topic, ``validated_input`` is a string (the strategy
        name) and it is stored separately rather than in ``_entries``.

        For all other topics, ``validated_input`` is a type-specific object:

        - Prices topic → ``list[PriceStep]``
        - Battery SOC topic → ``float`` (kWh)
        - EV SOC topic → ``float`` (kWh)
        - EV plug state topic → ``bool``
        - PV forecast topic → ``list[PowerForecastStep]``
        - Static load forecast topic → ``list[PowerForecastStep]``
        - Deferrable window earliest topic → ``datetime``
        - Deferrable window latest topic → ``datetime``
        - Deferrable start_time topic → ``datetime``

        The ``received_at`` timestamp is recorded at the time ``update()`` is
        called. For forecast topics this is used only for diagnostics; readiness
        is determined by the data timestamps, not the receive time.

        Args:
            topic: The MQTT topic that the message arrived on.
            validated_input: The already-validated input value.
        """
        with self._lock:
            if topic == self._strategy_topic:
                self._current_strategy = validated_input
            else:
                self._entries[topic] = (validated_input, datetime.now(UTC))

    def is_ready(self) -> bool:
        """Return True if all required inputs are present and forecast coverage is sufficient.

        Two checks are performed:

        1. **Sensor presence**: every battery SOC, EV SOC, and plug state topic
           must have been received at least once since startup. The age of the
           reading is not checked; the most recently retained value is always
           used.
        2. **Forecast coverage**: the stored price, PV, and load series must
           jointly cover at least ``config.readiness.min_horizon_hours`` of
           future 15-minute steps from the current solve start.

        Returns:
            True only if both conditions are met.
        """
        with self._lock:
            return self._is_ready_locked()

    def not_ready_reason(self) -> str:
        """Return a human-readable explanation of why readiness is not met.

        Intended for diagnostic logging and status topic publishing when
        ``is_ready()`` returns False. If the state happens to be ready at the
        time of the call, returns an empty string.

        Returns:
            A string describing the first blocking condition found, or an
            empty string if the state is ready.
        """
        with self._lock:
            missing = [t for t in self._sensor_topics if self._entries.get(t) is None]
            if missing:
                return (
                    f"Waiting for first message on {len(missing)} sensor topic(s): "
                    + ", ".join(sorted(missing))
                )
            now = datetime.now(UTC)
            available = self._compute_horizon_steps(now)
            if available < self._min_horizon_steps:
                solve_start = floor_to_15min(now)
                empty_topics = [
                    t
                    for t in sorted(self._forecast_topics)
                    if self._entries.get(t) is None
                    or compute_horizon_steps(
                        solve_start, self._entries[t][0]
                    ) == 0
                ]
                detail = (
                    f" Empty or expired forecast topic(s): {', '.join(empty_topics)}."
                    if empty_topics
                    else ""
                )
                return (
                    f"Forecast horizon too short: {available} steps available "
                    f"({available / 4:.1f} h), need {self._min_horizon_steps} "
                    f"({self._min_horizon_steps / 4:.1f} h).{detail}"
                )
            return ""

    def snapshot(self) -> SolveBundle:
        """Assemble and return a SolveBundle from the current state.

        Must be called only when ``is_ready()`` is True. The caller is
        responsible for checking readiness before calling this method.

        Computes the available horizon (number of 15-minute steps) from the
        joint coverage of all forecast series, resamples each series to that
        grid, and assembles the ``SolveBundle``. Gaps in intermediate steps
        are filled by interpolation (power) or step-function (prices) with a
        warning logged when a gap exceeds ``max_gap_hours``.

        Returns:
            A ``SolveBundle`` with flat resampled arrays for the available
            horizon.

        Raises:
            RuntimeError: If called when ``is_ready()`` is False.
        """
        with self._lock:
            if not self._is_ready_locked():
                raise RuntimeError(
                    "ReadinessState.snapshot() called when not ready. "
                    "Call is_ready() before snapshot()."
                )

            now = datetime.now(UTC)
            solve_start = floor_to_15min(now)
            n_steps = self._compute_horizon_steps(now)

            if n_steps < self._warn_below_steps:
                logger.warning(
                    "Short forecast horizon: %d steps (%.1f hours). "
                    "Warn threshold is %d steps (%.1f hours). "
                    "Solve will proceed with reduced horizon.",
                    n_steps,
                    n_steps / 4,
                    self._warn_below_steps,
                    self._warn_below_steps / 4,
                )

            # Check and warn about internal gaps in each forecast series.
            horizon_end = solve_start + n_steps * timedelta(minutes=15)
            self._check_gaps(solve_start, horizon_end)

            # Resample prices.
            price_steps: list[PriceStep] = self._entries[self._prices_topic][0]
            horizon_prices, horizon_export_prices, horizon_confidence = resample_prices(
                price_steps, solve_start, n_steps
            )

            # Resample PV forecast: sum all arrays.
            pv_forecast = [0.0] * n_steps
            for topic in self._pv_topics:
                entry = self._entries.get(topic)
                if entry is not None:
                    pv_steps: list[PowerForecastStep] = entry[0]
                    for t, v in enumerate(resample_power(pv_steps, solve_start, n_steps)):
                        pv_forecast[t] += v

            # Resample base load forecast: sum all static loads.
            base_load_forecast = [0.0] * n_steps
            for topic in self._load_topics:
                entry = self._entries.get(topic)
                if entry is not None:
                    load_steps: list[PowerForecastStep] = entry[0]
                    for t, v in enumerate(resample_power(load_steps, solve_start, n_steps)):
                        base_load_forecast[t] += v

            # Battery inputs: assemble BatteryInputs from stored float.
            battery_inputs: dict[str, BatteryInputs] = {}
            for name, cfg in self._config.batteries.items():
                if cfg.inputs is not None:
                    entry = self._entries.get(cfg.inputs.soc.topic)
                    if entry is not None:
                        soc_kwh, _ = entry
                        battery_inputs[name] = BatteryInputs(soc_kwh=soc_kwh)

            # EV inputs: combine SOC float + plug bool into EvInputs.
            ev_inputs: dict[str, EvInputs] = {}
            for name, cfg in self._config.ev_chargers.items():
                if cfg.inputs is not None:
                    soc_entry = self._entries.get(cfg.inputs.soc.topic)
                    plug_entry = self._entries.get(cfg.inputs.plugged_in_topic)
                    if soc_entry is not None and plug_entry is not None:
                        soc_kwh, _ = soc_entry
                        available, _ = plug_entry
                        ev_inputs[name] = EvInputs(
                            soc_kwh=soc_kwh,
                            available=available,
                        )

            # Deferrable windows: include only loads that have both endpoints.
            deferrable_windows: dict[str, DeferrableWindow] = {}
            for name, cfg in self._config.deferrable_loads.items():
                earliest_entry = self._entries.get(cfg.topic_window_earliest)
                latest_entry = self._entries.get(cfg.topic_window_latest)
                if earliest_entry is not None and latest_entry is not None:
                    deferrable_windows[name] = DeferrableWindow(
                        earliest=earliest_entry[0],
                        latest=latest_entry[0],
                    )

            # Deferrable start times: present when the automation has published
            # the actual start datetime for a running load.
            deferrable_start_times: dict[str, datetime] = {}
            for topic, name in self._start_time_topics.items():
                entry = self._entries.get(topic)
                if entry is not None:
                    deferrable_start_times[name] = entry[0]

            # Hybrid inverter inputs (plan 24): SOC + per-device PV forecast.
            hybrid_inverter_inputs: dict[str, HybridInverterInputs] = {}
            for name, cfg in self._config.hybrid_inverters.items():
                pv_entry = self._entries.get(cfg.topic_pv_forecast)
                if pv_entry is None:
                    continue
                pv_steps: list[PowerForecastStep] = pv_entry[0]
                pv_forecast_kw = resample_power(pv_steps, solve_start, n_steps)

                soc_kwh = 0.0
                if cfg.inputs is not None:
                    soc_entry = self._entries.get(cfg.inputs.soc.topic)
                    if soc_entry is not None:
                        soc_kwh = soc_entry[0]
                        if cfg.inputs.soc.unit == "percent":
                            soc_kwh = soc_kwh * cfg.capacity_kwh / 100.0

                hybrid_inverter_inputs[name] = HybridInverterInputs(
                    soc_kwh=soc_kwh,
                    pv_forecast_kw=pv_forecast_kw,
                )

            # Thermal boiler inputs (plan 25): current tank temperature.
            thermal_boiler_inputs: dict[str, ThermalBoilerInputs] = {}
            for name, cfg in self._config.thermal_boilers.items():
                if cfg.inputs is None:
                    continue
                temp_entry = self._entries.get(cfg.inputs.topic_current_temp)
                if temp_entry is None:
                    continue
                thermal_boiler_inputs[name] = ThermalBoilerInputs(
                    current_temp_c=temp_entry[0],
                )

            # Space heating inputs (plan 26): demand + optional BTM fields.
            space_heating_inputs: dict[str, SpaceHeatingInputs] = {}
            for name, cfg in self._config.space_heating_hps.items():
                if cfg.inputs is None:
                    continue
                demand_entry = self._entries.get(cfg.inputs.topic_heat_needed_kwh)
                if demand_entry is None:
                    continue
                heat_needed_kwh: float = demand_entry[0]

                current_indoor_temp_c: float | None = None
                outdoor_temp_forecast_c: list[float] | None = None
                if (
                    cfg.building_thermal is not None
                    and cfg.building_thermal.inputs is not None
                ):
                    btm_in = cfg.building_thermal.inputs
                    indoor_entry = self._entries.get(btm_in.topic_current_indoor_temp_c)
                    outdoor_entry = self._entries.get(btm_in.topic_outdoor_temp_forecast_c)
                    if indoor_entry is not None:
                        current_indoor_temp_c = indoor_entry[0]
                    if outdoor_entry is not None:
                        outdoor_temp_forecast_c = outdoor_entry[0]

                space_heating_inputs[name] = SpaceHeatingInputs(
                    heat_needed_kwh=heat_needed_kwh,
                    current_indoor_temp_c=current_indoor_temp_c,
                    outdoor_temp_forecast_c=outdoor_temp_forecast_c,
                )

            # Combi HP inputs (plan 27): DHW temp + SH demand + optional BTM fields.
            combi_hp_inputs: dict[str, CombiHeatPumpInputs] = {}
            for name, cfg in self._config.combi_heat_pumps.items():
                if cfg.inputs is None:
                    continue
                temp_entry = self._entries.get(cfg.inputs.topic_current_temp)
                sh_demand_entry = self._entries.get(cfg.inputs.topic_heat_needed_kwh)
                if temp_entry is None or sh_demand_entry is None:
                    continue

                current_indoor_temp_c_chp: float | None = None
                outdoor_temp_forecast_c_chp: list[float] | None = None
                if (
                    cfg.building_thermal is not None
                    and cfg.building_thermal.inputs is not None
                ):
                    btm_in = cfg.building_thermal.inputs
                    indoor_entry = self._entries.get(btm_in.topic_current_indoor_temp_c)
                    outdoor_entry = self._entries.get(btm_in.topic_outdoor_temp_forecast_c)
                    if indoor_entry is not None:
                        current_indoor_temp_c_chp = indoor_entry[0]
                    if outdoor_entry is not None:
                        outdoor_temp_forecast_c_chp = outdoor_entry[0]

                combi_hp_inputs[name] = CombiHeatPumpInputs(
                    current_temp_c=temp_entry[0],
                    heat_needed_kwh=sh_demand_entry[0],
                    current_indoor_temp_c=current_indoor_temp_c_chp,
                    outdoor_temp_forecast_c=outdoor_temp_forecast_c_chp,
                )

            return SolveBundle(
                strategy=self._current_strategy,
                solve_time_utc=solve_start,
                triggered_at_utc=now,
                horizon_prices=horizon_prices,
                horizon_export_prices=horizon_export_prices,
                horizon_confidence=horizon_confidence,
                pv_forecast=pv_forecast,
                base_load_forecast=base_load_forecast,
                battery_inputs=battery_inputs,
                ev_inputs=ev_inputs,
                deferrable_windows=deferrable_windows,
                deferrable_start_times=deferrable_start_times,
                hybrid_inverter_inputs=hybrid_inverter_inputs,
                thermal_boiler_inputs=thermal_boiler_inputs,
                space_heating_inputs=space_heating_inputs,
                combi_hp_inputs=combi_hp_inputs,
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_ready_locked(self) -> bool:
        """Check readiness without acquiring the lock.

        Must only be called when ``_lock`` is already held. Checks both
        sensor presence and forecast coverage.

        Returns:
            True if all sensor topics have been received at least once and
            forecast coverage meets the minimum horizon threshold.
        """
        now = datetime.now(UTC)

        # Sensor topics: presence only — block if never received.
        for topic in self._sensor_topics:
            if self._entries.get(topic) is None:
                return False

        # Forecast topics: must cover at least min_horizon_steps.
        return self._compute_horizon_steps(now) >= self._min_horizon_steps

    def _compute_horizon_steps(self, now: datetime) -> int:
        """Return the number of jointly covered 15-minute steps from solve_start.

        Must only be called when ``_lock`` is already held.

        Args:
            now: Current UTC time. Used to compute solve_start.

        Returns:
            Number of available 15-minute steps. Zero if any forecast topic
            has no data or no future data.
        """
        solve_start = floor_to_15min(now)
        series: list[list[Any]] = []

        for topic in self._forecast_topics:
            entry = self._entries.get(topic)
            if entry is None:
                return 0
            validated_input, _ = entry
            series.append(validated_input)

        return compute_horizon_steps(solve_start, *series)

    def _check_gaps(self, solve_start: datetime, horizon_end: datetime) -> None:
        """Log warnings for any gaps exceeding max_gap_hours in forecast series.

        Must only be called when ``_lock`` is already held.

        Args:
            solve_start: Start of the horizon window.
            horizon_end: End of the horizon window.
        """
        for topic in self._forecast_topics:
            entry = self._entries.get(topic)
            if entry is None:
                continue
            validated_input, _ = entry
            gaps = find_gaps(validated_input, solve_start, horizon_end, self._max_gap_hours)
            for gap_start, gap_end in gaps:
                gap_hours = (gap_end - gap_start).total_seconds() / 3600
                logger.warning(
                    "Gap of %.1f hours in forecast topic %r from %s to %s. "
                    "The gap is filled by interpolation.",
                    gap_hours,
                    topic,
                    gap_start.isoformat(),
                    gap_end.isoformat(),
                )

