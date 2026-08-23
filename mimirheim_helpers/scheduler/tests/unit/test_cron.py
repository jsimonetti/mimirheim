"""Unit tests for scheduler.cron.

The scheduler documents its schedule keys as standard five-field cron
expressions, but it runs them through APScheduler, which numbers the
day-of-week field from Monday rather than Sunday. These tests pin the
translation that reconciles the two.

Tests verify:
- Numeric day-of-week values follow standard cron, where 0 and 7 are Sunday.
- Ranges, lists, steps and weekday names all resolve to the correct weekdays.
- Expressions with no day-of-week restriction are passed through untouched.
- The other four fields are never rewritten.
- Restricting day-of-month and day-of-week together is rejected, because
  standard cron ORs those two fields and APScheduler ANDs them.
- Malformed day-of-week fields are rejected with the offending value named.
"""

from datetime import UTC, datetime, timedelta

import pytest

from scheduler.cron import build_trigger, to_apscheduler_crontab

# A Monday, so that a two-week window starting here covers every weekday twice.
_START = datetime(2026, 8, 17, tzinfo=UTC)
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _weekdays(expr: str) -> list[str]:
    """Return the distinct weekdays ``expr`` fires on over two weeks.

    Args:
        expr: A standard five-field cron expression.

    Returns:
        Weekday abbreviations in Monday-first order.
    """
    trigger = build_trigger(expr)
    end = _START + timedelta(days=14)
    seen: set[str] = set()
    previous = _START - timedelta(seconds=1)
    while True:
        nxt = trigger.get_next_fire_time(previous, previous + timedelta(seconds=1))
        if nxt is None or nxt >= end:
            break
        seen.add(_DAYS[nxt.weekday()])
        previous = nxt
    return sorted(seen, key=_DAYS.index)


# ---------------------------------------------------------------------------
# Numeric day-of-week follows standard cron
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dow", "expected"),
    [
        ("0", ["Sun"]),
        ("1", ["Mon"]),
        ("2", ["Tue"]),
        ("3", ["Wed"]),
        ("4", ["Thu"]),
        ("5", ["Fri"]),
        ("6", ["Sat"]),
        ("7", ["Sun"]),
    ],
)
def test_single_numeric_day_matches_standard_cron(dow: str, expected: list[str]) -> None:
    """Each numeric day-of-week value means the day standard cron says it does."""
    assert _weekdays(f"0 14 * * {dow}") == expected


def test_zero_and_seven_are_both_sunday() -> None:
    """Standard cron accepts 0 and 7 for Sunday; APScheduler alone rejects 7."""
    assert _weekdays("0 14 * * 0") == _weekdays("0 14 * * 7") == ["Sun"]


# ---------------------------------------------------------------------------
# Ranges, lists and steps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dow", "expected"),
    [
        ("1-5", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("0-6", _DAYS),
        ("1-7", _DAYS),
        ("6-7", ["Sat", "Sun"]),
        ("0,6", ["Sat", "Sun"]),
        ("6,0", ["Sat", "Sun"]),
        ("1,3,5", ["Mon", "Wed", "Fri"]),
        ("*/2", ["Tue", "Thu", "Sat", "Sun"]),
        ("1-5/2", ["Mon", "Wed", "Fri"]),
        ("0-4/2", ["Tue", "Thu", "Sun"]),
        ("1-3,6", ["Mon", "Tue", "Wed", "Sat"]),
    ],
)
def test_ranges_lists_and_steps(dow: str, expected: list[str]) -> None:
    """Ranges, lists and steps expand to the weekdays standard cron specifies."""
    assert _weekdays(f"0 14 * * {dow}") == expected


# ---------------------------------------------------------------------------
# Weekday names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dow", "expected"),
    [
        ("sun", ["Sun"]),
        ("SUN", ["Sun"]),
        ("mon-fri", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("MON-FRI", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
        ("sat,sun", ["Sat", "Sun"]),
        ("mon,3", ["Mon", "Wed"]),
    ],
)
def test_weekday_names(dow: str, expected: list[str]) -> None:
    """Weekday names resolve the same way numbers do, in either case."""
    assert _weekdays(f"0 14 * * {dow}") == expected


def test_sunday_first_range_by_name() -> None:
    """``sun-thu`` is a forward range in standard cron, where Sunday is first.

    APScheduler rejects it outright, because it orders Sunday last.
    """
    assert _weekdays("0 14 * * sun-thu") == ["Mon", "Tue", "Wed", "Thu", "Sun"]


# ---------------------------------------------------------------------------
# Pass-through
# ---------------------------------------------------------------------------


def test_unrestricted_day_of_week_is_untouched() -> None:
    """An expression with ``*`` for day-of-week is returned verbatim."""
    assert to_apscheduler_crontab("*/15 * * * *") == "*/15 * * * *"


@pytest.mark.parametrize(
    "expr",
    [
        "*/15 * * * *",
        "0 14 * * *",
        "5 0,12 * * *",
        "0 3,6,9,12,15,18 * * *",
        "0 0 1 1 *",
        "0 0 1-7 * *",
        "0 0 * jan-mar *",
    ],
)
def test_other_fields_are_never_rewritten(expr: str) -> None:
    """Only the day-of-week field is translated; the first four are left alone."""
    translated = to_apscheduler_crontab(expr)
    assert translated.split()[:4] == expr.split()[:4]


def test_translation_only_replaces_the_last_field() -> None:
    """The translated expression differs from the original in day-of-week only."""
    assert to_apscheduler_crontab("0 14 * * 1-5") == "0 14 * * mon,tue,wed,thu,fri"


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_day_of_month_and_day_of_week_together_is_rejected() -> None:
    """Standard cron ORs the two day fields; APScheduler ANDs them.

    Rather than silently applying the wrong one, the combination is refused.
    """
    with pytest.raises(ValueError, match="day-of-month and day-of-week"):
        to_apscheduler_crontab("0 0 13 * fri")


def test_day_of_month_alone_is_accepted() -> None:
    """Restricting only day-of-month is unambiguous and stays allowed."""
    assert to_apscheduler_crontab("0 0 13 * *") == "0 0 13 * *"


@pytest.mark.parametrize("dow", ["8", "-1", "xyz", "5-1", "1-", "1--3", "/2", "1/"])
def test_malformed_day_of_week_is_rejected(dow: str) -> None:
    """A day-of-week field that is not standard cron raises ValueError."""
    with pytest.raises(ValueError):
        to_apscheduler_crontab(f"0 14 * * {dow}")


def test_reversed_range_names_the_offending_value() -> None:
    """A descending range is rejected, and the message quotes it."""
    with pytest.raises(ValueError, match="5-1"):
        to_apscheduler_crontab("0 14 * * 5-1")


def test_wrong_field_count_is_left_to_apscheduler() -> None:
    """Field-count errors keep APScheduler's own wording rather than a second copy."""
    with pytest.raises(ValueError, match="Wrong number of fields"):
        build_trigger("0 14 * *")


# ---------------------------------------------------------------------------
# The expressions the shipped documentation promises
# ---------------------------------------------------------------------------


def test_readme_cron_reference_table() -> None:
    """Every row of the README cron reference table means what it claims."""
    assert _weekdays("0 14 * * 1-5") == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert _weekdays("*/15 * * * *") == _DAYS
    assert _weekdays("0 14 * * *") == _DAYS


def test_example_yaml_weekly_training_slot_is_sunday() -> None:
    """``0 2 * * 0`` in example.yaml is described as the weekly overnight slot."""
    assert _weekdays("0 2 * * 0") == ["Sun"]
