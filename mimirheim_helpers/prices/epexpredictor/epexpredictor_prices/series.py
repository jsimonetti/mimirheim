"""Confidence shaping for the EpexPredictor price pipeline.

This module turns the raw (timestamp, price) pairs returned by the fetcher
into the payload mimirheim expects: the import/export formulas applied and a
confidence value attached to every step.

Confidence decays with the forecast horizon relative to fetch time, using the
same band envelope as the open-meteo PV helper's ``ConfidenceDecay``:

    0-6 h ahead:   0.90
    6-24 h ahead:  0.75
    24-48 h ahead: 0.55
    48+ h ahead:   0.35

This anchor is deliberate and applies uniformly, including to steps at or
before the API's ``knownUntil`` boundary (real EPEX auction data), unless the
operator sets ``known_until_confidence`` — see ``build_price_steps`` below.

What this module does not do:
- It does not call the EpexPredictor API.
- It does not publish to MQTT.
- It does not import from mimirheim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ConfidenceDecay:
    """Confidence values to assign per horizon band.

    Attributes:
        hours_0_to_6: Confidence for steps 0-6 hours ahead.
        hours_6_to_24: Confidence for steps 6-24 hours ahead.
        hours_24_to_48: Confidence for steps 24-48 hours ahead.
        hours_48_plus: Confidence for steps more than 48 hours ahead.
    """

    hours_0_to_6: float
    hours_6_to_24: float
    hours_24_to_48: float
    hours_48_plus: float

    def confidence_for_step(self, step_ts: datetime, fetch_time: datetime) -> float:
        """Return the confidence value for a forecast step.

        Selects the band from how many hours ahead ``step_ts`` is relative to
        ``fetch_time``. Steps in the recent past fall in the nearest band.

        Args:
            step_ts: The timestamp of the forecast step (UTC-aware).
            fetch_time: The time at which the forecast was fetched (UTC-aware).

        Returns:
            A confidence value in [0.0, 1.0].
        """
        hours_ahead = (step_ts - fetch_time).total_seconds() / 3600
        if hours_ahead < 6:
            return self.hours_0_to_6
        if hours_ahead < 24:
            return self.hours_6_to_24
        if hours_ahead < 48:
            return self.hours_24_to_48
        return self.hours_48_plus


def build_price_steps(
    raw: list[tuple[datetime, float]],
    *,
    fetch_time: datetime,
    decay: ConfidenceDecay,
    import_formula: str,
    export_formula: str,
    known_until: datetime,
    known_until_confidence: float | None = None,
) -> list[dict]:
    """Apply the import/export formulas and confidence decay to raw prices.

    For each step, confidence is ``known_until_confidence`` when it is not
    None and the step's timestamp is at or before ``known_until``; otherwise
    it falls back to ``decay.confidence_for_step()``, unconditionally. This
    is the only place the override takes effect: a step after
    ``known_until`` always uses the fetch-time band, regardless of the
    override setting.

    Args:
        raw: Sorted (UTC-aware ts, predicted EUR/kWh) pairs from fetcher.py.
        fetch_time: Reference instant for the confidence bands.
        decay: Confidence values per horizon band.
        import_formula: Python expression string, same variables as Nordpool.
        export_formula: Python expression string, same variables as Nordpool.
        known_until: The API's real/predicted boundary for this fetch, as
            returned in ``FetchResult.known_until``.
        known_until_confidence: Fixed confidence to use for every step at or
            before ``known_until``. None (default): no override, every step
            uses the decay bands, matching the original fetch-time-only
            design.

    Returns:
        List of dicts, each with ``ts`` (ISO 8601 UTC string, matching
        ``datetime.isoformat()`` on an aware UTC datetime, i.e. a "+00:00"
        suffix, not "Z" - the same format Nordpool publishes),
        ``import_eur_per_kwh``, ``export_eur_per_kwh`` (both rounded to 6
        decimals, same as Nordpool), and ``confidence``.
    """
    # Imported here rather than at module scope to avoid a circular import:
    # config.py has no reason to import from series.py, but series.py needs
    # the same formula compiler config.py already defines and validates.
    from epexpredictor_prices.config import _compile_formula

    import_fn = _compile_formula(import_formula)
    export_fn = _compile_formula(export_formula)

    steps: list[dict] = []
    for ts, price in raw:
        if known_until_confidence is not None and ts <= known_until:
            confidence = known_until_confidence
        else:
            confidence = decay.confidence_for_step(ts, fetch_time)
        steps.append({
            "ts": ts.isoformat(),
            "import_eur_per_kwh": round(import_fn(ts, price), 6),
            "export_eur_per_kwh": round(export_fn(ts, price), 6),
            "confidence": confidence,
        })
    return steps
