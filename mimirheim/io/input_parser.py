"""Input parsers for raw MQTT payloads.

This module contains pure parsing functions that take raw bytes or a string
payload from an MQTT topic and return a validated Python object. Functions in
this module have no side effects: they do not read from the network, update
state, or log. They raise ``ValueError`` on invalid JSON and
``pydantic.ValidationError`` on schema violations.

Each function corresponds to one logical MQTT input category:

- ``parse_battery_inputs``: live sensor reading (plain float).
- ``parse_ev_inputs``: EV state — plain SOC number or a JSON object that also
  carries the departure target (``target_soc_kwh``, ``window_latest``).
- ``parse_price_steps``: hourly (or arbitrary-resolution) price forecast.
- ``parse_power_forecast``: hourly (or arbitrary-resolution) power forecast
  (used for both PV generation and static load forecasts).
- ``parse_strategy``: optimisation strategy selection.

The ``mqtt_client`` module calls these functions from within the ``on_message``
callback and passes the results to ``ReadinessState.update()``.

This module imports from ``mimirheim.core.bundle`` (the shared vocabulary of input
and output models) but never from ``mimirheim.core.readiness``,
``mimirheim.core.model_builder``, or any other module that implies a solve-time
dependency.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from mimirheim.core.bundle import PowerForecastStep, PriceStep

_VALID_STRATEGIES = {"minimize_cost", "minimize_consumption", "balanced"}


def _decode(payload: bytes | str) -> str:
    """Decode bytes to str if necessary.

    Args:
        payload: Raw MQTT payload as bytes or string.

    Returns:
        The payload as a UTF-8 string.
    """
    if isinstance(payload, bytes):
        return payload.decode("utf-8")
    return payload


def _parse_json(payload: bytes | str) -> object:
    """Parse a JSON payload and raise ValueError on failure.

    Args:
        payload: Raw MQTT payload.

    Returns:
        The parsed Python object.

    Raises:
        ValueError: If the payload is not valid JSON.
    """
    try:
        return json.loads(_decode(payload))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid JSON payload: {exc}") from exc


def parse_battery_inputs(payload: bytes | str) -> float:
    """Parse a battery state-of-charge payload into a float kWh value.

    The payload must be a plain numeric string or a JSON number.

    .. code-block:: text

        5.2

    Args:
        payload: Raw MQTT payload from the battery SOC topic.

    Returns:
        State of charge as a float. Unit conversion (percent → kWh) is
        applied by the caller using the device capacity from config.

    Raises:
        ValueError: If the payload cannot be parsed as a number.
    """
    text = _decode(payload).strip()
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"Battery SOC payload is not a valid number: {text!r}") from exc


@dataclass
class ParsedEvState:
    """EV state parsed from the SOC topic payload.

    Attributes:
        soc: State of charge as published. Unit conversion (percent → kWh)
            is applied by the caller using the device capacity from config.
        target_soc_kwh: SOC the vehicle must reach by ``window_latest``, in
            kWh (never percent — it feeds the hard constraint directly).
            None when the payload carries no departure target.
        window_earliest: Optional earliest charging datetime (UTC).
        window_latest: Optional departure deadline (UTC). The target
            constraint requires both ``target_soc_kwh`` and this field.
    """

    soc: float
    target_soc_kwh: float | None = None
    window_earliest: datetime | None = None
    window_latest: datetime | None = None


def parse_ev_inputs(payload: bytes | str) -> ParsedEvState:
    """Parse an EV state payload.

    Two payload forms are accepted:

    A plain numeric string or JSON number — state of charge only:

    .. code-block:: text

        20.0

    A JSON object carrying the SOC and the optional departure target:

    .. code-block:: json

        {
            "soc": 55.0,
            "target_soc_kwh": 51.8,
            "window_latest": "2026-08-30T05:00:00Z"
        }

    ``soc`` is mandatory in the object form. ``target_soc_kwh`` is always in
    kWh, regardless of the configured SOC unit. Datetime fields follow the
    same rules as ``parse_datetime`` (naive values are interpreted as UTC).

    The plug state is published on a separate ``plugged_in_topic`` and is
    parsed independently. See the inline ``_parse_plug`` handler in
    ``mqtt_client.py``.

    Args:
        payload: Raw MQTT payload from the EV SOC topic.

    Returns:
        The parsed EV state.

    Raises:
        ValueError: If the payload is neither a number nor a valid state object.
    """
    text = _decode(payload).strip()
    try:
        return ParsedEvState(soc=float(text))
    except ValueError:
        pass

    data = _parse_json(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"EV state payload must be a number or a JSON object, got: {text!r}"
        )
    allowed = {"soc", "target_soc_kwh", "window_earliest", "window_latest"}
    unknown = set(data) - allowed
    if unknown:
        # This is a system boundary: a typo such as 'target_soc_kw' must not
        # silently drop the departure constraint.
        raise ValueError(
            f"EV state object contains unknown field(s) {sorted(unknown)!r}; "
            f"allowed fields are {sorted(allowed)!r}."
        )
    if "soc" not in data:
        raise ValueError(f"EV state object is missing the mandatory 'soc' field: {text!r}")
    try:
        soc = float(data["soc"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"EV state 'soc' is not a number: {data['soc']!r}") from exc

    target = data.get("target_soc_kwh")
    if target is not None:
        try:
            target = float(target)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"EV state 'target_soc_kwh' is not a number: {data['target_soc_kwh']!r}"
            ) from exc
        if target < 0:
            raise ValueError(f"EV state 'target_soc_kwh' must be >= 0, got {target}")

    def _optional_dt(field: str) -> datetime | None:
        raw = data.get(field)
        return parse_datetime(raw) if raw is not None else None

    return ParsedEvState(
        soc=soc,
        target_soc_kwh=target,
        window_earliest=_optional_dt("window_earliest"),
        window_latest=_optional_dt("window_latest"),
    )


def parse_price_steps(payload: bytes | str) -> list[PriceStep]:
    """Parse a timestamped electricity price forecast into a list of PriceStep objects.

    Expected payload: a JSON array of objects, each with ``ts``,
    ``import_eur_per_kwh``, ``export_eur_per_kwh``, and optionally
    ``confidence``.

    .. code-block:: json

        [
            {"ts": "2026-03-30T14:00:00+00:00", "import_eur_per_kwh": 0.22,
             "export_eur_per_kwh": 0.08, "confidence": 1.0},
            {"ts": "2026-03-30T15:00:00+00:00", "import_eur_per_kwh": 0.19,
             "export_eur_per_kwh": 0.07}
        ]

    The array does not need to be sorted by timestamp — ``ReadinessState``
    sorts on use.

    Args:
        payload: Raw MQTT payload from the prices topic.

    Returns:
        A list of validated ``PriceStep`` instances.

    Raises:
        ValueError: If the payload is not a JSON array or is empty.
        pydantic.ValidationError: If any element fails schema validation.
    """
    data = _parse_json(payload)
    if not isinstance(data, list):
        raise ValueError(
            f"Price steps payload must be a JSON array; got {type(data).__name__}."
        )
    if not data:
        raise ValueError("Price steps payload must not be empty.")
    return [PriceStep.model_validate(item) for item in data]


def parse_power_forecast(payload: bytes | str) -> list[PowerForecastStep]:
    """Parse a timestamped power forecast (PV or static load) into PowerForecastStep objects.

    Expected payload: a JSON array of objects, each with ``ts``, ``kw``,
    and optionally ``confidence``.

    .. code-block:: json

        [
            {"ts": "2026-03-30T14:00:00+00:00", "kw": 3.2, "confidence": 0.92},
            {"ts": "2026-03-30T15:00:00+00:00", "kw": 2.8, "confidence": 0.88}
        ]

    The array does not need to be sorted — ``ReadinessState`` sorts on use.

    Args:
        payload: Raw MQTT payload from a PV or static-load forecast topic.

    Returns:
        A list of validated ``PowerForecastStep`` instances.

    Raises:
        ValueError: If the payload is not a JSON array or is empty.
        pydantic.ValidationError: If any element fails schema validation.
    """
    data = _parse_json(payload)
    if not isinstance(data, list):
        raise ValueError(
            f"Power forecast payload must be a JSON array; got {type(data).__name__}."
        )
    if not data:
        raise ValueError("Power forecast payload must not be empty.")
    return [PowerForecastStep.model_validate(item) for item in data]


def parse_datetime(payload: bytes | str) -> datetime:
    """Parse an ISO 8601 UTC datetime payload.

    Used for deferrable load window endpoints and the ``topic_committed_start_time``
    topic. The payload may be a bare ISO 8601 string or a JSON-quoted string.

    Home Assistant's MQTT ``datetime`` entity publishes in
    ``%Y-%m-%dT%H:%M:%S`` format (no timezone suffix) when using the default
    payload format. The value HA stores internally is UTC, so a timezone-naive
    payload is interpreted here as UTC.  Payloads that include an explicit
    offset (e.g. ``+00:00``, ``+02:00``) are left as-is; Python's arithmetic
    handles the conversion correctly when comparing to a UTC-aware
    ``solve_time_utc``.

    Args:
        payload: Raw MQTT payload. Examples::

            2026-03-30T14:00:00+00:00
            2026-03-30T14:00:00
            "2026-03-30T14:00:00+00:00"

    Returns:
        A timezone-aware ``datetime``. Naive inputs are assumed to be UTC.

    Raises:
        ValueError: If the payload cannot be parsed as a datetime.
    """
    text = _decode(payload).strip().strip('"')
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Cannot parse datetime from payload {payload!r}: {exc}") from exc

    if dt.tzinfo is None:
        # HA MQTT datetime entities publish in UTC without a timezone marker.
        # Treat naive values as UTC so downstream arithmetic against
        # solve_time_utc (which is always timezone-aware) does not raise
        # TypeError.
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def parse_strategy(payload: bytes | str) -> str:
    """Parse the ``{prefix}/input/strategy`` payload.

    Accepts either a plain text strategy name::

        minimize_cost

    or a JSON object with a ``"strategy"`` key (as published by Home Assistant
    MQTT select entities)::

        {"strategy": "minimize_cost"}

    Valid strategy values are ``"minimize_cost"``, ``"minimize_consumption"``,
    and ``"balanced"``.

    Args:
        payload: Raw MQTT payload from the strategy topic.

    Returns:
        The strategy string.

    Raises:
        ValueError: If the payload is not a recognised strategy name.
    """
    raw = _decode(payload).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and "strategy" in parsed:
            strategy = str(parsed["strategy"])
        else:
            strategy = raw
    except json.JSONDecodeError:
        strategy = raw
    if strategy not in _VALID_STRATEGIES:
        raise ValueError(
            f"Unknown strategy {strategy!r}. "
            f"Valid values: {sorted(_VALID_STRATEGIES)}."
        )
    return strategy


def parse_hybrid_inverter_soc(payload: bytes | str) -> float:
    """Parse a hybrid inverter battery state-of-charge payload.

    The payload must be a plain numeric string or a JSON number. This function
    is used for the ``inputs.soc`` topic declared in a ``HybridInverterConfig``.
    It behaves identically to ``parse_battery_inputs``: the unit conversion
    (percent to kWh) is performed by the caller using the device capacity.

    Args:
        payload: Raw MQTT payload from the hybrid inverter SOC topic.

    Returns:
        State of charge as a float in the unit declared by the topic config.

    Raises:
        ValueError: If the payload cannot be parsed as a number.
    """
    text = _decode(payload).strip()
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(
            f"Hybrid inverter SOC payload is not a valid number: {text!r}"
        ) from exc


def parse_thermal_boiler_temp(payload: bytes | str) -> float:
    """Parse a thermal boiler water temperature payload.

    The payload must be a plain numeric string or a JSON number representing
    the current water temperature in degrees Celsius. Used for the
    ``inputs.topic_current_temp`` topic declared in a ``ThermalBoilerConfig``.

    Args:
        payload: Raw MQTT payload from the boiler temperature sensor topic.

    Returns:
        Current water temperature as a float in degrees Celsius.

    Raises:
        ValueError: If the payload cannot be parsed as a number.
    """
    text = _decode(payload).strip()
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(
            f"Thermal boiler temperature payload is not a valid number: {text!r}"
        ) from exc


def parse_space_heating_demand(payload: bytes | str) -> float:
    """Parse a space heating demand payload.

    The payload must be a plain numeric string or a JSON number representing
    the total thermal energy in kWh that the heat pump must produce this
    horizon. Published retained to the topic declared in
    ``SpaceHeatingInputsConfig.topic_heat_needed_kwh``.

    Use 0.0 to indicate that no heating is needed for this horizon. The space
    heating device will pin all on variables to zero when it receives 0.0.

    Args:
        payload: Raw MQTT payload from the heat demand topic.

    Returns:
        Heat demand as a non-negative float in kWh.

    Raises:
        ValueError: If the payload cannot be parsed as a non-negative number.
    """
    text = _decode(payload).strip()
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(
            f"Space heating demand payload is not a valid number: {text!r}"
        ) from exc
    if value < 0.0:
        raise ValueError(
            f"Space heating demand must be non-negative, got {value}."
        )
    return value


def parse_combi_hp_temp(payload: bytes | str) -> float:
    """Parse a combined heat pump DHW tank temperature payload.

    The payload must be a plain numeric string or a JSON number representing
    the current DHW water temperature in degrees Celsius. Used for the
    ``inputs.topic_current_temp`` topic declared in a ``CombiHeatPumpConfig``.

    Args:
        payload: Raw MQTT payload from the DHW temperature sensor topic.

    Returns:
        Current DHW water temperature as a float in degrees Celsius.

    Raises:
        ValueError: If the payload cannot be parsed as a number.
    """
    text = _decode(payload).strip()
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(
            f"Combi heat pump DHW temperature payload is not a valid number: {text!r}"
        ) from exc


def parse_combi_hp_sh_demand(payload: bytes | str) -> float:
    """Parse a combined heat pump space heating demand payload.

    The payload must be a plain numeric string or a JSON number representing
    the total space heating thermal energy in kWh required this horizon.
    Used for the ``inputs.topic_heat_needed_kwh`` topic declared in a
    ``CombiHeatPumpConfig``.

    Args:
        payload: Raw MQTT payload from the space heating demand topic.

    Returns:
        Space heating demand as a non-negative float in kWh.

    Raises:
        ValueError: If the payload cannot be parsed as a non-negative number.
    """
    text = _decode(payload).strip()
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(
            f"Combi HP space heating demand payload is not a valid number: {text!r}"
        ) from exc
    if value < 0.0:
        raise ValueError(
            f"Combi HP space heating demand must be non-negative, got {value}."
        )
    return value


def parse_current_indoor_temp(payload: bytes | str) -> float:
    """Parse a current indoor temperature payload.

    The payload is either a plain numeric string or a JSON object with a
    ``temp_c`` or ``value`` key. Both forms are accepted to allow integration
    with common HA temperature sensor formats.

    Plain numeric form:
        ``"19.5"``

    JSON object form (``temp_c`` key takes precedence over ``value``):
        ``{"temp_c": 19.5}``  or  ``{"value": 19.5}``

    Used for the ``building_thermal.inputs.topic_current_indoor_temp_c`` topic.

    Args:
        payload: Raw MQTT payload from the indoor temperature sensor topic.

    Returns:
        Current indoor temperature as a float in degrees Celsius.

    Raises:
        ValueError: If the payload cannot be parsed as a number.
    """
    text = _decode(payload).strip()

    # Attempt JSON parsing first to support structured payloads.
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Indoor temperature JSON payload is malformed: {text!r}"
            ) from exc
        for key in ("temp_c", "value"):
            if key in data:
                try:
                    return float(data[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Indoor temperature JSON field '{key}' is not a number: {data[key]!r}"
                    ) from exc
        raise ValueError(
            f"Indoor temperature JSON payload has neither 'temp_c' nor 'value' key: {text!r}"
        )

    # Plain numeric form.
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(
            f"Indoor temperature payload is not a valid number: {text!r}"
        ) from exc


def parse_outdoor_temp_forecast(payload: bytes | str) -> list[float]:
    """Parse a per-step outdoor temperature forecast payload.

    The payload is a JSON array of floats, one value per 15-minute horizon
    step. Used for the ``building_thermal.inputs.topic_outdoor_temp_forecast_c``
    topic.

    Example payload:
        ``[10.5, 10.2, 9.8, 9.3, 8.9, ...]``

    Args:
        payload: Raw MQTT payload from the outdoor temperature forecast topic.

    Returns:
        List of outdoor temperature floats in degrees Celsius, one per step.

    Raises:
        ValueError: If the payload is not a JSON array of numbers.
    """
    text = _decode(payload).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Outdoor temperature forecast payload is not valid JSON: {text!r}"
        ) from exc

    if not isinstance(data, list):
        raise ValueError(
            f"Outdoor temperature forecast must be a JSON array, got {type(data).__name__}."
        )

    result: list[float] = []
    for i, item in enumerate(data):
        try:
            result.append(float(item))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Outdoor temperature forecast element {i} is not a number: {item!r}"
            ) from exc

    return result
