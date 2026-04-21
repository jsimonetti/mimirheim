"""Contract test: every optional DeviceSetpoint field must be accounted for in the reporter.

This test imports ``DeviceSetpoint`` from ``mimirheim.core.bundle``, enumerates
its optional fields (those that are not ``kw`` or ``type``), and checks that
each one appears in one of two acknowledged sets:

- ``_REPORTER_DISPLAYED_FIELDS``: fields that the reporter renders as a column
  in the schedule data table (``_render_helpers._build_data_table``).
- ``_REPORTER_EXPLICITLY_EXCLUDED_FIELDS``: fields the reporter intentionally
  does not display, with a documented reason.

When a new optional field is added to ``DeviceSetpoint``, this test will fail
with a clear message. The developer must then either:

1. Add a reporter column for the field in
   ``mimirheim_helpers/reporter/reporter/_render_helpers.py`` and add the field
   name to ``_REPORTER_DISPLAYED_FIELDS`` here, or
2. Add the field to ``_REPORTER_EXPLICITLY_EXCLUDED_FIELDS`` here with a
   comment explaining why the reporter does not need to show it.

This test does not verify that the reporter *correctly* renders each field —
that is covered by the unit tests in
``mimirheim_helpers/reporter/tests/unit/test_render_helpers_pv.py`` and the
ZEX/LB coverage in ``_build_data_table``. Its sole purpose is to ensure no
field is silently overlooked.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from mimirheim.core.bundle import DeviceSetpoint


# Fields that the reporter renders as visible columns in the schedule table.
# Update this set whenever you add a new column to _build_data_table.
#
# PV devices:
#   power_limit_kw     -> {name}<br>lim kW  (present when capability enabled)
#   on_off_active      -> {name}<br>on/off  (present when capability enabled)
#   zero_exchange_active -> {name}<br>ZEX   (present when zero_export enabled)
#
# Battery and EV devices:
#   zero_exchange_active -> {name}<br>ZEX  (in device_meta loop)
#
# EV devices only:
#   loadbalance_active -> {name}<br>LB
_REPORTER_DISPLAYED_FIELDS: frozenset[str] = frozenset(
    {
        "power_limit_kw",
        "zero_exchange_active",
        "on_off_active",
        "loadbalance_active",
        "pv_is_curtailed",
    }
)

# Fields that the reporter intentionally does not display.
# Each entry must have a comment explaining why.
_REPORTER_EXPLICITLY_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {
        # None currently. Add entries here when a DeviceSetpoint field exists
        # for purely internal or MQTT-routing purposes that an operator does
        # not need to see in the schedule table.
    }
)


def test_all_optional_setpoint_fields_are_accounted_for() -> None:
    """Every optional field in DeviceSetpoint must appear in one of the two sets.

    Failure means a field was added to DeviceSetpoint without updating the
    reporter. Fix by either adding a display column to
    ``_render_helpers._build_data_table`` and adding the field to
    ``_REPORTER_DISPLAYED_FIELDS``, or adding it to
    ``_REPORTER_EXPLICITLY_EXCLUDED_FIELDS`` with a documented reason.
    """
    # Collect all fields defined in DeviceSetpoint except the two that every
    # device has: kw (the primary setpoint) and type (used for routing).
    optional_fields = {
        name
        for name in DeviceSetpoint.model_fields
        if name not in ("kw", "type")
    }

    accounted = _REPORTER_DISPLAYED_FIELDS | _REPORTER_EXPLICITLY_EXCLUDED_FIELDS
    unaccounted = optional_fields - accounted

    assert not unaccounted, (
        f"These DeviceSetpoint optional fields are not accounted for in the reporter:\n"
        f"  {sorted(unaccounted)}\n\n"
        f"For each field, either:\n"
        f"  1. Add a reporter column in _render_helpers._build_data_table and\n"
        f"     add the field name to _REPORTER_DISPLAYED_FIELDS in this file, or\n"
        f"  2. Add the field to _REPORTER_EXPLICITLY_EXCLUDED_FIELDS with a\n"
        f"     comment explaining why the reporter does not need to display it."
    )


def test_no_phantom_entries_in_displayed_fields() -> None:
    """Every name in _REPORTER_DISPLAYED_FIELDS must be an actual DeviceSetpoint field.

    This guards against stale entries left behind when a field is renamed or
    removed from DeviceSetpoint.
    """
    all_fields = set(DeviceSetpoint.model_fields)
    phantom = _REPORTER_DISPLAYED_FIELDS - all_fields

    assert not phantom, (
        f"These names appear in _REPORTER_DISPLAYED_FIELDS but are not fields\n"
        f"in DeviceSetpoint: {sorted(phantom)}. Remove stale entries."
    )
