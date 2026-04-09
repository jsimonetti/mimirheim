"""Unit tests for mimirheim/io/input_parser.py.

All tests must fail before the implementation exists (TDD).
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from mimirheim.io.input_parser import (
    parse_battery_inputs,
    parse_datetime,
    parse_power_forecast,
    parse_price_steps,
    parse_strategy,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def test_parses_battery_soc_payload() -> None:
    """Plain numeric string parses to a float."""
    assert parse_battery_inputs(b"5.2") == pytest.approx(5.2)


def test_parses_battery_soc_payload_as_string() -> None:
    """String-encoded float parses correctly."""
    assert parse_battery_inputs("62.5") == pytest.approx(62.5)


def test_parse_price_steps_valid() -> None:
    """A JSON array containing one valid PriceStep dict parses successfully."""
    payload = json.dumps([{
        "ts": _now_iso(),
        "import_eur_per_kwh": 0.22,
        "export_eur_per_kwh": 0.08,
        "confidence": 1.0,
    }])
    result = parse_price_steps(payload)
    assert len(result) == 1
    assert result[0].import_eur_per_kwh == pytest.approx(0.22)
    assert result[0].export_eur_per_kwh == pytest.approx(0.08)


def test_parse_price_steps_rejects_non_array() -> None:
    """A JSON object (not an array) raises ValueError."""
    payload = json.dumps({"ts": _now_iso(), "import_eur_per_kwh": 0.22})
    with pytest.raises(ValueError):
        parse_price_steps(payload)


def test_parse_price_steps_rejects_empty_array() -> None:
    """An empty JSON array raises ValueError."""
    with pytest.raises(ValueError):
        parse_price_steps(json.dumps([]))


def test_parse_power_forecast_valid() -> None:
    """A JSON array containing one valid PowerForecastStep dict parses successfully."""
    payload = json.dumps([{"ts": _now_iso(), "kw": 3.2, "confidence": 0.9}])
    result = parse_power_forecast(payload)
    assert len(result) == 1
    assert result[0].kw == pytest.approx(3.2)
    assert result[0].confidence == pytest.approx(0.9)


def test_parse_power_forecast_rejects_empty_array() -> None:
    """An empty JSON array raises ValueError."""
    with pytest.raises(ValueError):
        parse_power_forecast(json.dumps([]))


def test_parse_power_forecast_rejects_non_array() -> None:
    """A JSON object (not an array) raises ValueError."""
    payload = json.dumps({"ts": _now_iso(), "kw": 2.5})
    with pytest.raises(ValueError):
        parse_power_forecast(payload)


def test_parses_strategy_minimize_cost() -> None:
    """Plain text 'minimize_cost' parses to the string 'minimize_cost'."""
    assert parse_strategy(b"minimize_cost") == "minimize_cost"


def test_parses_strategy_balanced() -> None:
    """Plain text 'balanced' parses to the string 'balanced'."""
    assert parse_strategy("balanced") == "balanced"


def test_parses_strategy_json_object() -> None:
    """JSON object with 'strategy' key is accepted (as published by HA MQTT select)."""
    assert parse_strategy(b'{"strategy": "minimize_cost"}') == "minimize_cost"


def test_parses_strategy_json_object_str() -> None:
    """JSON object payload as a string is also accepted."""
    assert parse_strategy('{"strategy": "balanced"}') == "balanced"


def test_rejects_unknown_strategy() -> None:
    """Unknown strategy name raises ValueError."""
    with pytest.raises(ValueError, match="go_wild"):
        parse_strategy("go_wild")


def test_rejects_unknown_strategy_in_json_object() -> None:
    """Unknown strategy name inside a JSON object raises ValueError."""
    with pytest.raises(ValueError, match="go_wild"):
        parse_strategy('{"strategy": "go_wild"}')


def test_rejects_malformed_json() -> None:
    """Non-numeric bytes raise ValueError, not an unhandled exception."""
    with pytest.raises(ValueError):
        parse_battery_inputs(b"not a number")


def test_rejects_battery_extra_field() -> None:
    """Non-numeric payload (JSON object) raises ValueError."""
    with pytest.raises(ValueError):
        parse_battery_inputs(json.dumps({"soc_kwh": 5.2}))


# ---------------------------------------------------------------------------
# parse_datetime
# ---------------------------------------------------------------------------


def test_parse_datetime_with_utc_offset() -> None:
    """ISO 8601 string with '+00:00' offset parses to a UTC-aware datetime."""
    from datetime import timezone
    result = parse_datetime("2026-03-30T14:00:00+00:00")
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 0
    assert result.year == 2026
    assert result.hour == 14


def test_parse_datetime_naive_is_treated_as_utc() -> None:
    """A timezone-naive payload (HA MQTT datetime default format) is assumed UTC.

    HA's MQTT datetime entity publishes with strftime('%Y-%m-%dT%H:%M:%S'),
    which strips the timezone marker. The value HA encodes is always UTC, so
    treating naive payloads as UTC is correct.
    """
    from datetime import timezone
    result = parse_datetime("2026-03-30T13:00:00")
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 0
    assert result.hour == 13


def test_parse_datetime_naive_is_comparable_to_utc_aware() -> None:
    """A naive payload, after parse, can be subtracted from a UTC-aware datetime.

    This exercises the bug where naive datetimes caused TypeError in
    _datetime_to_step when compared against solve_time_utc.
    """
    from datetime import timezone
    solve_time = datetime(2026, 3, 30, 13, 15, 0, tzinfo=timezone.utc)
    window_start = parse_datetime("2026-03-30T13:00:00")
    delta = (solve_time - window_start).total_seconds()
    assert delta == pytest.approx(900.0)  # 15 minutes


def test_parse_datetime_non_utc_offset_preserved() -> None:
    """A payload with a non-UTC offset is left as-is (not forcibly converted)."""
    result = parse_datetime("2026-03-30T15:00:00+02:00")
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 7200


def test_parse_datetime_quoted_string() -> None:
    """JSON-quoted datetime strings (with surrounding quotes) are accepted."""
    result = parse_datetime('"2026-03-30T14:00:00+00:00"')
    assert result.hour == 14


def test_parse_datetime_rejects_invalid() -> None:
    """A non-datetime string raises ValueError."""
    with pytest.raises(ValueError):
        parse_datetime("not-a-date")
