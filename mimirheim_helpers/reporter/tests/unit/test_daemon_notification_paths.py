"""Unit tests for the paths ReporterDaemon derives from a notification payload.

``_on_notification`` builds the report filename from the payload's ``ts`` field
and reads the two dump files at the payload's ``input_path`` and
``output_path``. All three values arrive over MQTT, so none of them can be
trusted to stay inside the configured directories.

mimirheim publishes ``ts`` as an ISO 8601 timestamp and the two paths as
absolute paths inside ``reporting.dump_dir`` (see ``mimirheim/__main__.py``).
Anything else is a malformed or hostile notification.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from reporter.daemon import ReporterDaemon, _iso_to_safe_ts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daemon(output_dir: Path, dump_dir: Path) -> ReporterDaemon:
    """Return a ReporterDaemon that does no MQTT work."""
    cfg = MagicMock()
    cfg.reporting.output_dir = output_dir
    cfg.reporting.dump_dir = dump_dir
    cfg.reporting.max_reports = 10
    cfg.reporting.notify_topic = "mimir/status/dump_available"
    daemon = object.__new__(ReporterDaemon)
    daemon._reporter_config = cfg.reporting
    return daemon


def _message(ts: str, input_path: Path, output_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        topic="mimir/status/dump_available",
        payload=json.dumps(
            {
                "ts": ts,
                "input_path": str(input_path),
                "output_path": str(output_path),
            }
        ).encode(),
    )


@pytest.fixture
def dirs(tmp_path: Path) -> SimpleNamespace:
    """Three sibling directories: reports, dumps, and somewhere off-limits."""
    output_dir = tmp_path / "reports"
    dump_dir = tmp_path / "dumps"
    outside = tmp_path / "outside"
    for d in (output_dir, dump_dir, outside):
        d.mkdir()
    return SimpleNamespace(output=output_dir, dumps=dump_dir, outside=outside)


@pytest.fixture
def dump_pair(dirs: SimpleNamespace, fixture_dump_pair: tuple[Path, Path]) -> tuple[Path, Path]:
    """Copy the committed fixture pair into the dump directory."""
    src_in, src_out = fixture_dump_pair
    dst_in = dirs.dumps / "2026-04-03T15-30-00Z_input.json"
    dst_out = dirs.dumps / "2026-04-03T15-30-00Z_output.json"
    dst_in.write_bytes(src_in.read_bytes())
    dst_out.write_bytes(src_out.read_bytes())
    return dst_in, dst_out


# ---------------------------------------------------------------------------
# _iso_to_safe_ts
# ---------------------------------------------------------------------------


class TestIsoToSafeTs:
    """The function claimed to return "a filesystem-safe string". It did not.

    Its whole body was a colon substitution, so every separator and every
    traversal segment passed through untouched. No test exercised it: replacing
    the substitution with a plain return left all 92 reporter tests passing.
    """

    def test_replaces_colons_in_the_time_part(self) -> None:
        assert _iso_to_safe_ts("2026-04-02T14:00:00Z") == "2026-04-02T14-00-00Z"

    def test_leaves_the_date_part_alone(self) -> None:
        assert _iso_to_safe_ts("2026-04-02T00:00:00Z").startswith("2026-04-02T")

    def test_handles_a_timestamp_with_no_time_part(self) -> None:
        assert _iso_to_safe_ts("2026-04-02") == "2026-04-02"

    def test_accepts_a_numeric_utc_offset(self) -> None:
        assert _iso_to_safe_ts("2026-04-02T14:00:00+00:00") == "2026-04-02T14-00-00+00-00"

    @pytest.mark.parametrize(
        "ts",
        [
            "../escaped/PWNED",
            "../../../../tmp/pwned",
            "a/b",
            "a\\b",
            "/etc/passwd",
            "..",
            "2026-04-02T14:00:00Z/../../x",
            "2026-04-02T14:00:00Z\x00.html",
        ],
        ids=[
            "parent-segment",
            "deep-traversal",
            "forward-slash",
            "backslash",
            "absolute",
            "bare-dotdot",
            "traversal-after-valid-prefix",
            "embedded-nul",
        ],
    )
    def test_rejects_anything_that_is_not_a_bare_timestamp(self, ts: str) -> None:
        with pytest.raises(ValueError):
            _iso_to_safe_ts(ts)


# ---------------------------------------------------------------------------
# _on_notification: the report path
# ---------------------------------------------------------------------------


class TestReportPathContainment:
    def test_traversal_in_ts_writes_nothing_outside_output_dir(
        self,
        dirs: SimpleNamespace,
        dump_pair: tuple[Path, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The proven exploit: ts = "../escaped/PWNED" wrote PWNED_report.html."""
        daemon = _make_daemon(dirs.output, dirs.dumps)
        inp, out = dump_pair

        with caplog.at_level(logging.WARNING):
            daemon._on_notification(_message("../outside/PWNED", inp, out))

        assert list(dirs.outside.iterdir()) == []
        assert not any(p.name.endswith("_report.html") for p in dirs.output.iterdir())

    def test_traversal_in_ts_is_logged(
        self,
        dirs: SimpleNamespace,
        dump_pair: tuple[Path, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        daemon = _make_daemon(dirs.output, dirs.dumps)
        inp, out = dump_pair

        with caplog.at_level(logging.WARNING):
            daemon._on_notification(_message("../outside/PWNED", inp, out))

        assert "timestamp" in caplog.text.lower()

    def test_a_valid_notification_still_renders(
        self, dirs: SimpleNamespace, dump_pair: tuple[Path, Path]
    ) -> None:
        """Regression guard: the rejection must not break the normal path."""
        daemon = _make_daemon(dirs.output, dirs.dumps)
        inp, out = dump_pair

        daemon._on_notification(_message("2026-04-03T15:30:00Z", inp, out))

        assert (dirs.output / "2026-04-03T15-30-00Z_report.html").exists()


# ---------------------------------------------------------------------------
# _on_notification: the dump paths
# ---------------------------------------------------------------------------


class TestDumpPathContainment:
    def test_dump_path_outside_dump_dir_is_refused(
        self,
        dirs: SimpleNamespace,
        dump_pair: tuple[Path, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A payload naming a file elsewhere on disk must not be read.

        Otherwise anyone who can publish to the broker can have the reporter
        read an arbitrary file and embed it in a report, which the config
        editor then serves over HTTP.
        """
        daemon = _make_daemon(dirs.output, dirs.dumps)
        _inp, out = dump_pair
        secret = dirs.outside / "secret_input.json"
        secret.write_text(json.dumps({"stolen": True}))

        with caplog.at_level(logging.WARNING):
            daemon._on_notification(_message("2026-04-03T15:30:00Z", secret, out))

        assert not (dirs.output / "2026-04-03T15-30-00Z_report.html").exists()
        assert "dump" in caplog.text.lower()

    def test_output_dump_path_outside_dump_dir_is_refused(
        self,
        dirs: SimpleNamespace,
        dump_pair: tuple[Path, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        daemon = _make_daemon(dirs.output, dirs.dumps)
        inp, _out = dump_pair
        secret = dirs.outside / "secret_output.json"
        secret.write_text(json.dumps({"stolen": True}))

        with caplog.at_level(logging.WARNING):
            daemon._on_notification(_message("2026-04-03T15:30:00Z", inp, secret))

        assert not (dirs.output / "2026-04-03T15-30-00Z_report.html").exists()

    def test_traversal_within_the_dump_path_is_refused(
        self,
        dirs: SimpleNamespace,
        dump_pair: tuple[Path, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A relative escape from inside dump_dir must not resolve out of it."""
        daemon = _make_daemon(dirs.output, dirs.dumps)
        inp, out = dump_pair
        # Valid dump content, so the only thing that can stop the render is the
        # containment check. With garbage content the test would pass whether
        # or not the guard exists, because build_report_html would raise.
        planted = dirs.outside / "planted_input.json"
        planted.write_bytes(inp.read_bytes())
        escaping = dirs.dumps / ".." / "outside" / "planted_input.json"
        assert escaping.exists(), "the escaping path must resolve to a real file"

        with caplog.at_level(logging.WARNING):
            daemon._on_notification(_message("2026-04-03T15:30:00Z", escaping, out))

        assert not (dirs.output / "2026-04-03T15-30-00Z_report.html").exists()
        assert "outside" in caplog.text
