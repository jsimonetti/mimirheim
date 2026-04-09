"""mimirheim-reporter package.

This package contains the rendering library (``render.py``, ``_render_helpers.py``) and the
event-driven reporting daemon (``daemon.py``) for mimirheim solve-dump analysis.

Sub-modules:
    render     — Build interactive HTML reports from mimirheim JSON dump pairs.
    config     — ``ReporterConfig`` Pydantic model.
    daemon     — ``ReporterDaemon`` HelperDaemon subclass.
    inventory  — ``inventory.js`` management functions.
    gc         — Garbage collection for old HTML reports.
"""
