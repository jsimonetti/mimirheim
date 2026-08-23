"""Standard five-field cron expressions, translated for APScheduler.

The scheduler's configuration documents its schedule keys as standard cron
expressions. APScheduler parses almost all of that syntax already, with one
incompatibility in the day-of-week field:

- Standard cron numbers Sunday as 0 and accepts 7 as a second spelling of
  Sunday.
- APScheduler numbers Monday as 0, has no day 7, and orders its weekday names
  Monday first.

Every numeric day-of-week is therefore one day out, and both ``7`` and
Sunday-first ranges such as ``sun-thu`` are rejected outright. This module
translates the day-of-week field into explicit APScheduler weekday names, which
are unambiguous in both systems, and leaves the other four fields untouched.

What this module does not do:
- It does not validate the minute, hour, day-of-month or month fields. Those
  are already compatible, so they are passed to APScheduler as written and any
  error in them keeps APScheduler's own wording.
- It does not emulate standard cron's OR between day-of-month and day-of-week.
  See ``to_apscheduler_crontab`` for why that combination is refused instead.
"""

from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

# Standard cron weekday order: Sunday is 0. Index 7 is the second spelling of
# Sunday that standard cron allows and APScheduler does not. Both systems use
# the same weekday names, which is why the translation emits names rather than
# renumbered digits: the result is unambiguous and stays readable in an error.
_CRON_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Output order, so a translated expression reads Monday first like the rest of
# APScheduler. The trigger itself is indifferent to the order.
_OUTPUT_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_FIELD_COUNT = 5
_DAY_OF_MONTH_FIELD = 2
_DAY_OF_WEEK_FIELD = 4

_MAX_CRON_DAY = 7


def _parse_day(token: str) -> int:
    """Return the standard cron index for a single day-of-week token.

    Args:
        token: A day number (``"0"`` to ``"7"``) or a weekday name
            (``"sun"`` to ``"sat"``), in any case.

    Returns:
        The standard cron day index: 0 for Sunday through 6 for Saturday, and
        7 for the alternative spelling of Sunday. Folding 7 back to 0 is left
        until after ranges have been expanded, because ``6-7`` means Saturday
        to Sunday and would otherwise look like a range running backwards.

    Raises:
        ValueError: If the token is neither a valid day number nor a
            recognised weekday name.
    """
    lowered = token.strip().lower()
    if lowered.isdigit():
        value = int(lowered)
        if value > _MAX_CRON_DAY:
            raise ValueError(
                f"day-of-week value {token!r} is out of range; "
                f"standard cron allows 0 to 7, where 0 and 7 are both Sunday"
            )
        return value
    if lowered in _CRON_DAY_NAMES:
        return _CRON_DAY_NAMES.index(lowered)
    raise ValueError(
        f"unrecognised day-of-week value {token!r}; expected 0 to 7 or one of "
        f"{', '.join(_CRON_DAY_NAMES[:7])}"
    )


def _parse_step(term: str) -> tuple[str, int]:
    """Split a ``base/step`` term into its base and step.

    Args:
        term: One comma-separated term of a day-of-week field.

    Returns:
        The base part and the step. The step is 1 when the term has none.

    Raises:
        ValueError: If the term has more than one ``/``, or a step that is not
            a positive integer.
    """
    if "/" not in term:
        return term, 1
    base, _, step_text = term.partition("/")
    if "/" in step_text:
        raise ValueError(f"day-of-week term {term!r} has more than one step")
    if not step_text.isdigit() or int(step_text) < 1:
        raise ValueError(f"day-of-week term {term!r} has an invalid step {step_text!r}")
    return base, int(step_text)


def _expand_term(term: str) -> set[int]:
    """Expand one comma-separated day-of-week term to standard cron indices.

    Handles ``*``, a single day, a ``first-last`` range, and any of those with
    a ``/step`` suffix.

    Args:
        term: One term of a day-of-week field, e.g. ``"1-5"`` or ``"*/2"``.

    Returns:
        The set of standard cron day indices the term covers. Indices are in
        the 0 to 7 space, so 7 may be present and means Sunday.

    Raises:
        ValueError: If the term is empty, malformed, or specifies a range whose
            first day comes after its last.
    """
    base, step = _parse_step(term)
    base = base.strip()
    if not base:
        raise ValueError(f"empty day-of-week term in {term!r}")

    if base == "*":
        first, last = 0, 6
    elif "-" in base.lstrip("-"):
        first_text, _, last_text = base.partition("-")
        first, last = _parse_day(first_text), _parse_day(last_text)
        if first > last:
            raise ValueError(
                f"day-of-week range {base!r} runs backwards; standard cron "
                f"ranges go from the earlier day to the later one, counting "
                f"from Sunday"
            )
    else:
        first = last = _parse_day(base)

    return set(range(first, last + 1, step))


def _translate_day_of_week(field: str) -> str:
    """Translate a standard cron day-of-week field to APScheduler weekday names.

    Args:
        field: The fifth field of a standard cron expression.

    Returns:
        A comma-separated list of APScheduler weekday names, or ``"*"`` when
        the field places no restriction on the weekday.

    Raises:
        ValueError: If any term of the field is malformed.
    """
    if field.strip() == "*":
        return "*"

    days: set[int] = set()
    for term in field.split(","):
        days |= _expand_term(term)

    # Fold the alternative spelling of Sunday in only now that every range has
    # been expanded over the full 0 to 7 space.
    names = {_CRON_DAY_NAMES[day] for day in days}
    return ",".join(name for name in _OUTPUT_ORDER if name in names)


def to_apscheduler_crontab(expr: str) -> str:
    """Translate a standard five-field cron expression for APScheduler.

    Only the day-of-week field is rewritten. The other four fields are
    compatible as written and are passed through unchanged, so errors in them
    surface later with APScheduler's own wording rather than a second,
    divergent copy of the same message.

    An expression that restricts both day-of-month and day-of-week is refused.
    Standard cron runs such a job when *either* field matches, while APScheduler
    requires *both*. Emulating the OR would mean registering two triggers per
    schedule entry; refusing is honest, and the same result is available by
    writing two schedule entries.

    Args:
        expr: A standard five-field cron expression, e.g. ``"0 14 * * 1-5"``.

    Returns:
        An equivalent expression for ``CronTrigger.from_crontab``. Expressions
        that do not have five fields are returned unchanged, so that
        APScheduler reports the field count itself.

    Raises:
        ValueError: If the day-of-week field is malformed, or if both day
            fields are restricted.
    """
    fields = expr.split()
    if len(fields) != _FIELD_COUNT:
        return expr

    day_of_week = fields[_DAY_OF_WEEK_FIELD]
    if day_of_week.strip() == "*":
        return expr

    if fields[_DAY_OF_MONTH_FIELD].strip() != "*":
        raise ValueError(
            "day-of-month and day-of-week are both restricted. Standard cron "
            "fires when either matches, which cannot be expressed as one "
            "APScheduler trigger. Write two schedule entries instead, one "
            "restricting each field."
        )

    fields[_DAY_OF_WEEK_FIELD] = _translate_day_of_week(day_of_week)
    return " ".join(fields)


def build_trigger(expr: str, timezone: str = "UTC") -> CronTrigger:
    """Build an APScheduler trigger from a standard five-field cron expression.

    This is the single place the scheduler turns a configured cron expression
    into a trigger, so that validation at startup and job registration at run
    time can never interpret the same expression differently.

    Args:
        expr: A standard five-field cron expression.
        timezone: The timezone the expression is evaluated in. The scheduler
            uses UTC throughout.

    Returns:
        The configured ``CronTrigger``.

    Raises:
        ValueError: If the expression is not a valid standard cron expression.
    """
    return CronTrigger.from_crontab(to_apscheduler_crontab(expr), timezone=timezone)
