"""Every helper's entry point must log under its own package name.

``logging.getLogger(__name__)`` at module scope in a ``__main__.py`` resolves
to the logger named ``"__main__"`` when the module is run as ``python -m
<pkg>``, which is how every helper is started -- see
``container/etc/s6-overlay/s6-rc.d/*/run``.

That matters because ``MqttDaemon.__init__`` goes to some trouble to derive the
package name for its own logger, with a nine-line comment explaining exactly
this hazard. A helper whose entry point used ``__name__`` therefore logged
under two names at once: ``nordpool`` for everything the base class emitted and
``__main__`` for its own config-load and cycle failures. Filtering or
level-setting by helper name silently missed half the output.

Two angles, because no single one covers it:

- A behavioural test, for the helpers that log during config loading. It runs
  the entry point through ``runpy`` with ``run_name="__main__"`` against a
  config path that does not exist, which reaches the failure log and exits
  before any MQTT connection is attempted.
- A source-level rule covering every entry point, including the ones that
  report a missing config by printing to stderr and so emit no log record to
  inspect.
"""
from __future__ import annotations

import ast
import logging
import runpy
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Entry points that log a config-load failure through the logging module.
# (module path used with `python -m`, expected logger name)
_LOGGING_ENTRY_POINTS = [
    ("nordpool", "nordpool"),
    ("zonneplan_prices", "zonneplan_prices"),
    ("baseload_ha", "baseload_ha"),
    ("baseload_ha_db", "baseload_ha_db"),
    ("baseload_static", "baseload_static"),
]

# Every helper entry point, for the source-level rule.
_ENTRY_POINT_SOURCES = [
    "mimirheim_helpers/prices/nordpool/nordpool/__main__.py",
    "mimirheim_helpers/prices/zonneplan/zonneplan_prices/__main__.py",
    "mimirheim_helpers/baseload/homeassistant/baseload_ha/__main__.py",
    "mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/__main__.py",
    "mimirheim_helpers/baseload/static/baseload_static/__main__.py",
    "mimirheim_helpers/pv/forecast.solar/pv_fetcher/__main__.py",
    "mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/__main__.py",
    "mimirheim_helpers/reporter/reporter/__main__.py",
    "mimirheim_helpers/scheduler/scheduler/__main__.py",
    "mimirheim_helpers/config_editor/config_editor/__main__.py",
]


class _CapturingHandler(logging.Handler):
    """Records the logger name of every record it sees."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.names: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.names.append(record.name)


def _run_entry_point(module: str, config_path: Path) -> list[str]:
    """Run ``module`` as ``__main__`` and return the logger names it emitted."""
    handler = _CapturingHandler()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    saved_argv = sys.argv
    sys.argv = [module, "--config", str(config_path)]
    try:
        with pytest.raises(SystemExit):
            runpy.run_module(module, run_name="__main__")
    finally:
        sys.argv = saved_argv
        root.removeHandler(handler)
        root.setLevel(previous_level)
    return handler.names


class TestRuntimeLoggerName:
    @pytest.mark.parametrize(
        ("module", "expected"),
        _LOGGING_ENTRY_POINTS,
        ids=[m for m, _ in _LOGGING_ENTRY_POINTS],
    )
    def test_does_not_log_as_dunder_main(
        self, module: str, expected: str, tmp_path: Path
    ) -> None:
        names = _run_entry_point(module, tmp_path / "absent.yaml")

        assert names, f"{module} emitted no log records; the test proves nothing"
        assert "__main__" not in names, (
            f"{module} logged under '__main__'; it should use {expected!r}"
        )

    @pytest.mark.parametrize(
        ("module", "expected"),
        _LOGGING_ENTRY_POINTS,
        ids=[m for m, _ in _LOGGING_ENTRY_POINTS],
    )
    def test_logs_under_its_package_name(
        self, module: str, expected: str, tmp_path: Path
    ) -> None:
        names = _run_entry_point(module, tmp_path / "absent.yaml")

        assert any(n == expected or n.startswith(f"{expected}.") for n in names), (
            f"{module} logged under {sorted(set(names))}, none of which is {expected!r}"
        )


class TestSourceRule:
    """No entry point may derive its logger name from ``__name__``.

    Covers the entry points that report a missing config by printing to stderr,
    which the behavioural tests above cannot reach.
    """

    @pytest.mark.parametrize("relpath", _ENTRY_POINT_SOURCES, ids=lambda p: p.split("/")[-2])
    def test_getlogger_is_not_called_with_dunder_name(self, relpath: str) -> None:
        path = _REPO_ROOT / relpath
        tree = ast.parse(path.read_text(), filename=str(path))

        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getLogger"
            and any(
                isinstance(arg, ast.Name) and arg.id == "__name__"
                for arg in node.args
            )
        ]

        assert not offenders, (
            f"{relpath} calls logging.getLogger(__name__) at line(s) {offenders}. "
            "Under `python -m <pkg>` that resolves to the logger named "
            "'__main__'. Name the package explicitly."
        )


class TestThirdPartyLoggersAreQuiet:
    """Helpers that use httpx must not let it log a line per request at INFO."""

    @pytest.mark.parametrize(
        "relpath",
        [
            "mimirheim_helpers/baseload/homeassistant/baseload_ha/__main__.py",
            "mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/__main__.py",
        ],
        ids=["baseload_ha", "pv_ml_learner"],
    )
    def test_httpx_is_set_to_warning(self, relpath: str) -> None:
        source = (_REPO_ROOT / relpath).read_text()

        assert 'logging.getLogger("httpx").setLevel(logging.WARNING)' in source, (
            f"{relpath} uses httpx but does not quiet it. httpx logs every "
            "request at INFO, which for pv_ml_learner would also put the "
            "Meteoserver API key in the log."
        )

    def test_every_httpx_user_is_covered_by_the_test_above(self) -> None:
        """Guard against a third httpx helper appearing without being quieted."""
        users = {
            path.parts[-2]
            for path in _REPO_ROOT.glob("mimirheim_helpers/**/*.py")
            if "/tests/" not in str(path) and "import httpx" in path.read_text()
        }
        assert users == {"baseload_ha", "pv_ml_learner"}, (
            f"httpx importers changed to {sorted(users)}; update the "
            "parametrised test above."
        )
