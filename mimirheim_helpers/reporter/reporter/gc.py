"""Garbage collection for old mimirheim-reporter HTML reports.

Deletes the oldest ``*_report.html`` files from the output directory when
the number of retained reports exceeds the configured maximum, and removes
the corresponding entries from ``inventory.js``.

What this module does not do:
- It does not delete dump JSON files from the source directory.
- It does not subscribe to MQTT.
- It does not render HTML files.
"""
from __future__ import annotations

import logging
from pathlib import Path

from reporter import inventory as inv

logger = logging.getLogger(__name__)


def collect(output_dir: Path, max_reports: int) -> None:
    """Delete the oldest reports beyond the retention limit.

    Reads the current ``inventory.js`` to determine which reports exist and
    their order (newest-first). If the count exceeds ``max_reports``, removes
    the oldest entries first: deletes the HTML file, then removes the entry
    from the inventory.

    A no-op when ``max_reports == 0`` (unlimited) or when the count is within
    the limit.

    Args:
        output_dir: Directory containing ``*_report.html`` files and
            ``inventory.js``.
        max_reports: Maximum number of report files to retain. 0 means
            unlimited.
    """
    if max_reports == 0:
        return

    # Inventory is sorted newest-first; to delete oldest, work from the tail.
    entries = inv._read_inventory(output_dir)
    if len(entries) <= max_reports:
        return

    to_remove = entries[max_reports:]  # oldest entries, beyond the limit
    n_removed = 0
    for entry in to_remove:
        report_file = entry.get("file")
        ts = entry.get("ts")
        if report_file:
            html_path = output_dir / report_file
            if html_path.exists():
                html_path.unlink()
        if ts:
            inv.remove(output_dir, ts)
        n_removed += 1

    n_retained = len(entries) - n_removed
    logger.info("GC: removed %d report(s); %d retained.", n_removed, n_retained)
