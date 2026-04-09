"""CycleResult — return value for HelperDaemon._run_cycle().

A single dataclass that replaces the previous ``datetime | None`` return type.
``suppress_until`` preserves the rate-limit suppression semantics. The
remaining fields carry per-cycle execution metadata published to stats_topic.

What this module does not do:
    - It does not import from any specific helper tool.
    - It does not perform any MQTT operations.
    - It does not interact with the HelperDaemon machinery directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class CycleResult:
    """Return value for HelperDaemon._run_cycle().

    Attributes:
        success: True when the cycle completed without raising an exception.
            The daemon sets this to False in the except branch and still
            publishes stats so operators can see the failure timestamp and
            duration. Helpers do not set this field directly.
        suppress_until: When set, the daemon discards all trigger messages
            until this UTC datetime has passed. Used by helpers that contact
            external APIs with rate-limit responses (e.g. forecast.solar
            returning HTTP 429).
        horizon_hours: Number of hours of data produced by this cycle.
            For example, a nordpool run that publishes today and tomorrow's
            prices sets this to 48. None if the helper does not track this.
        exit_code: Reserved for future use. Always None in this initial
            implementation. Individual helpers may populate this in a
            subsequent plan.
        exit_message: Reserved for future use. Always None in this initial
            implementation.
    """

    success: bool = True
    suppress_until: datetime | None = None
    horizon_hours: float | None = None
    exit_code: int | None = None
    exit_message: str | None = None
