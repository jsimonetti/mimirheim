"""Unit tests for the two entry points that had no coverage at all.

``reporter/__main__.py`` and ``config_editor/__main__.py`` sat at 0%. Both are
small, and both are pure wiring -- parse arguments, load configuration, build
the thing, run it -- which is exactly the code where a mistake means the
service does not start and no test notices.

Neither test lets the real thing run: the daemon and the server are replaced,
and the assertions are about how they were constructed and torn down.
"""
from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import config_editor.__main__ as ce_main
import reporter.__main__ as reporter_main


def _argv(module: str, config: Path) -> list[str]:
    return [module, "--config", str(config)]


# ---------------------------------------------------------------------------
# reporter
# ---------------------------------------------------------------------------


class TestReporterMain:
    def test_loads_the_config_and_runs_the_daemon(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "reporter.yaml"
        config_path.write_text("")
        loaded = object()
        monkeypatch.setattr(sys, "argv", _argv("reporter", config_path))

        with (
            patch.object(reporter_main, "load_config", return_value=loaded) as load,
            patch.object(reporter_main, "ReporterDaemon") as daemon_cls,
        ):
            reporter_main.main()

        load.assert_called_once_with(str(config_path))
        daemon_cls.assert_called_once_with(loaded)
        daemon_cls.return_value.run.assert_called_once()

    def test_config_is_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["reporter"])

        with pytest.raises(SystemExit) as exc:
            reporter_main.main()

        assert exc.value.code == 2  # argparse usage error

    def test_logging_is_configured_before_the_daemon_is_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A daemon that logs during construction must not lose those records."""
        config_path = tmp_path / "reporter.yaml"
        config_path.write_text("")
        monkeypatch.setattr(sys, "argv", _argv("reporter", config_path))
        order: list[str] = []

        with (
            patch.object(
                reporter_main.logging,
                "basicConfig",
                side_effect=lambda **_kw: order.append("basicConfig"),
            ),
            patch.object(
                reporter_main, "load_config", side_effect=lambda _p: order.append("load")
            ),
            patch.object(
                reporter_main,
                "ReporterDaemon",
                side_effect=lambda _c: order.append("daemon") or MagicMock(),
            ),
        ):
            reporter_main.main()

        assert order.index("basicConfig") < order.index("daemon")


# ---------------------------------------------------------------------------
# config editor
# ---------------------------------------------------------------------------


class TestConfigEditorMain:
    def _run_main_until_shutdown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Any
    ) -> MagicMock:
        """Run ce_main.main() and return the mock server it built.

        main() blocks on a stop event until a signal arrives, so the recorded
        SIGTERM handler is invoked from this thread once it is installed.
        """
        config_path = tmp_path / "config-editor.yaml"
        config_path.write_text("")
        monkeypatch.setattr(sys, "argv", _argv("config_editor", config_path))
        server = MagicMock()
        handlers: dict[int, Any] = {}
        installed = threading.Event()

        def _record(signum: int, handler: Any) -> None:
            handlers[signum] = handler
            if len(handlers) == 2:
                installed.set()

        finished = threading.Event()

        def _run() -> None:
            try:
                with pytest.raises(SystemExit):
                    ce_main.main()
            finally:
                finished.set()

        with (
            patch.object(ce_main, "load_config", return_value=cfg),
            patch.object(
                ce_main, "ConfigEditorServer", return_value=server
            ) as server_cls,
            patch.object(ce_main.signal, "signal", _record),
        ):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            assert installed.wait(timeout=5.0), "main() never installed its handlers"
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            assert finished.wait(timeout=5.0), "main() did not exit after SIGTERM"
            thread.join(timeout=5.0)
            self.construction = server_cls.call_args
        return server

    def test_builds_the_server_from_the_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Port, config_dir and allowed_ip must all reach the server.

        allowed_ip especially: it is the only access control this server has,
        and dropping it on the way through would be silent.
        """
        cfg = MagicMock(
            port=8099,
            config_dir=Path("/config"),
            log_level="INFO",
            allowed_ip="10.0.0.1",
        )

        self._run_main_until_shutdown(tmp_path, monkeypatch, cfg)

        assert self.construction.kwargs == {
            "config_dir": Path("/config"),
            "port": 8099,
            "allowed_ip": "10.0.0.1",
        }

    def test_shuts_the_server_down_on_sigterm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = MagicMock(port=0, config_dir=Path("/config"), log_level="INFO", allowed_ip=None)

        server = self._run_main_until_shutdown(tmp_path, monkeypatch, cfg)

        server.shutdown.assert_called_once()

    def test_serves_in_a_background_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """serve_forever must not run on the main thread, or shutdown deadlocks."""
        cfg = MagicMock(port=0, config_dir=Path("/config"), log_level="INFO", allowed_ip=None)

        server = self._run_main_until_shutdown(tmp_path, monkeypatch, cfg)

        server.serve_forever.assert_called_once()

    def test_an_unknown_log_level_falls_back_to_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """getattr(logging, name, INFO) is the fallback; prove it is reached."""
        cfg = MagicMock(port=0, config_dir=Path("/config"), log_level="NONSENSE", allowed_ip=None)
        captured: dict[str, Any] = {}

        with patch.object(
            ce_main.logging,
            "basicConfig",
            side_effect=lambda **kw: captured.update(kw),
        ):
            self._run_main_until_shutdown(tmp_path, monkeypatch, cfg)

        assert captured["level"] == logging.INFO

    def test_a_configured_log_level_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = MagicMock(port=0, config_dir=Path("/config"), log_level="debug", allowed_ip=None)
        captured: dict[str, Any] = {}

        with patch.object(
            ce_main.logging,
            "basicConfig",
            side_effect=lambda **kw: captured.update(kw),
        ):
            self._run_main_until_shutdown(tmp_path, monkeypatch, cfg)

        assert captured["level"] == logging.DEBUG
