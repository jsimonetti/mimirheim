"""Joins KNMI observations and PV actuals into a unified training dataset.

This module provides the ``build_training_rows`` function, which performs an
inner join between KNMI hourly observations and PV production actuals, applies
exclusion rules (night, negative actuals), and computes calendar features for
use in XGBoost training.

It does not read from the database, fetch data from external sources, or write
files.  Callers must supply already-fetched ``KnmiRow`` and ``PvActualRow``
lists.  Meteoserver data is not used at all during training.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from pv_ml_learner.storage import KnmiRow, PvActualRow

logger = logging.getLogger(__name__)

# Hours with KNMI global radiation at or below this threshold are treated as
# night-time and excluded from training.  A model trained on night-time zeros
# would learn a trivial pattern that dominates daytime predictions.  The
# threshold matches Critical Concern 5 in the plan.
_NIGHT_GHI_THRESHOLD_WM2 = 5.0


@dataclass
class TrainingRow:
    """One hour of joined, filtered training data.

    Attributes:
        hour_utc: Unix timestamp (seconds) of the start of the hour in UTC.
        ghi_wm2: Global horizontal irradiance from KNMI observation (W/m²).
        wind_ms: Wind speed in m/s from KNMI FH.  ``None`` when the station did
            not report this variable for the hour.
        temp_c: Air temperature in °C from KNMI T.  ``None`` when the station
            did not report this variable for the hour.
        rain_mm: Precipitation in mm from KNMI RH.  Always ``0.0`` or positive;
            trace amounts (-1 in raw KNMI data) are resolved to 0.0 before this
            point.
        hour_of_day: UTC hour component, 0–23.
        month: UTC month component, 1–12.
        week_nr: ISO week number, 1–53.
        quarter: Calendar quarter, 1–4.
        kwh_actual: Measured PV production for the hour in kWh.
    """

    hour_utc: int
    ghi_wm2: float
    wind_ms: float | None
    temp_c: float | None
    rain_mm: float
    hour_of_day: int
    month: int
    week_nr: int
    quarter: int
    kwh_actual: float


def _iso_week(t: time.struct_time) -> int:
    """Return the ISO week number for a ``time.struct_time``.

    ``time.struct_time`` exposes ``tm_yday`` and ``tm_wday`` but not the ISO
    week number directly.  ``datetime.date.isocalendar`` is cleaner but
    importing ``datetime`` here keeps the module dependency-light; the
    calculation below is the standard ISO-8601 formula.
    """
    import datetime

    d = datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
    return d.isocalendar()[1]


def build_training_rows(
    knmi_rows: list[KnmiRow],
    pv_rows: list[PvActualRow],
) -> list[TrainingRow]:
    """Join KNMI observations with PV actuals and apply exclusion rules.

    Performs an inner join on ``hour_utc``.  Only hours present in both inputs
    pass through.  The result is filtered to remove night-time hours and hours
    with negative PV actuals.

    Hours absent from ``pv_rows`` because the ``ha_actuals`` layer excluded
    them (e.g. due to limiting sensor activity) simply do not appear in the
    join and therefore do not produce a training row.  The dataset builder does
    not need to know about limiting sensors; the exclusion is already applied
    upstream.

    Args:
        knmi_rows: KNMI hourly observations.  Must include at minimum
            ``hour_utc`` and ``ghi_wm2``.
        pv_rows: Per-hour PV production actuals in kWh.  Must have been
            pre-filtered to exclude hours with inverter throttling active.

    Returns:
        A list of ``TrainingRow`` instances in ascending ``hour_utc`` order,
        with night hours and negative actuals removed.
    """
    pv_by_hour: dict[int, float] = {r.hour_utc: r.kwh for r in pv_rows}

    result: list[TrainingRow] = []
    skipped_night = 0

    for knmi in sorted(knmi_rows, key=lambda r: r.hour_utc):
        kwh = pv_by_hour.get(knmi.hour_utc)
        if kwh is None:
            # No PV actual for this hour — inner join excludes it.
            continue

        if knmi.ghi_wm2 <= _NIGHT_GHI_THRESHOLD_WM2:
            # Night-time hour: exclude to avoid teaching the model a trivial
            # zero pattern that would dominate predictions around sunrise/sunset.
            skipped_night += 1
            continue

        if kwh < 0.0:
            # Negative actuals result from cumulative rollover or meter resets
            # in HA long_term_statistics.  Including them would corrupt the
            # regression target.
            continue

        t = time.gmtime(knmi.hour_utc)
        result.append(
            TrainingRow(
                hour_utc=knmi.hour_utc,
                ghi_wm2=knmi.ghi_wm2,
                wind_ms=knmi.wind_ms,
                temp_c=knmi.temp_c,
                rain_mm=knmi.rain_mm,
                hour_of_day=t.tm_hour,
                month=t.tm_mon,
                week_nr=_iso_week(t),
                quarter=(t.tm_mon - 1) // 3 + 1,
                kwh_actual=kwh,
            )
        )

    if skipped_night:
        logger.debug("Excluded %d night-time hours (GHI <= 5 W/m²).", skipped_night)

    # Log a warning if wind or temperature is consistently absent, so operators
    # know early that the configured KNMI station may not report these variables.
    if result:
        sample = result[:100]
        if all(r.wind_ms is None for r in sample):
            logger.warning(
                "wind_ms is None for the first %d training rows.  The KNMI "
                "station may not report wind speed; this column will be absent "
                "from the feature matrix.",
                len(sample),
            )
        if all(r.temp_c is None for r in sample):
            logger.warning(
                "temp_c is None for the first %d training rows.  The KNMI "
                "station may not report temperature; this column will be absent "
                "from the feature matrix.",
                len(sample),
            )

    return result
