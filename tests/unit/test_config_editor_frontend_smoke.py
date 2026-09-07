"""Frontend smoke tests for the Jedison-based config editor (plan 69).

Start a real ConfigEditorServer on a random local port (same pattern as
test_config_editor_crud_generic.py's live_server fixture) and confirm the
served frontend is wired up correctly: index.html references the vendored
Jedison build, the custom topic-placeholder editor, and app.js; every
referenced static asset is actually served; and, when Node.js happens to be
available on the machine running pytest, every served JS file at least
parses as syntactically valid JavaScript.

What these tests do not cover:
- Actual JS execution, DOM rendering, or any Jedison behaviour.
- Browser-side interaction, CSS layout, light/dark mode, phone width.
- Whether the CSP header actually permits Jedison to render (see plan 69's
  manual verification checklist -- that requires a real browser).

No JS test framework is added here (plan 69 Decision 9): `node --check`
parses a file without executing it and ships as a zero-dependency flag
built into Node itself, so this stays optional (skipped) wherever Node.js
is not installed, rather than becoming a new required dependency of this
Python project's test suite.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import pytest

from config_editor.server import ConfigEditorServer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_server(tmp_path: Path):
    """Start a real ConfigEditorServer on a random OS-assigned port.

    Yields the base URL (e.g. 'http://127.0.0.1:54321'). Server is shut down
    after the test.
    """
    server = ConfigEditorServer(config_dir=tmp_path, port=0)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _get(url: str) -> tuple[int, bytes, dict[str, str]]:
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read(), dict(resp.headers)


# ---------------------------------------------------------------------------
# index.html wiring
# ---------------------------------------------------------------------------


def test_index_references_jedison_and_app_scripts(live_server: str) -> None:
    """index.html must load the vendored Jedison build, the custom editor, and app.js."""
    status, body, _ = _get(live_server + "/")
    assert status == 200
    html = body.decode()
    assert "static/vendor/jedison.1.21.0.umd.js" in html
    assert "static/vendor/bootstrap.5.3.3.bundle.min.js" in html
    assert "static/topic-placeholder-editor.js" in html
    assert "static/app.js" in html


@pytest.mark.parametrize(
    "path",
    [
        "static/vendor/jedison.1.21.0.umd.js",
        "static/vendor/bootstrap.5.3.3.min.css",
        "static/vendor/bootstrap.5.3.3.bundle.min.js",
        "static/topic-placeholder-editor.js",
        "static/app.js",
        "static/style.css",
    ],
)
def test_every_referenced_static_asset_is_served(live_server: str, path: str) -> None:
    """Every script/stylesheet index.html references actually resolves to a 200 with a body."""
    status, body, headers = _get(f"{live_server}/{path}")
    assert status == 200
    assert len(body) > 0


# ---------------------------------------------------------------------------
# Syntactic loadability (Node.js optional -- see module docstring)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not available to syntax-check served JS")
@pytest.mark.parametrize("path", ["static/topic-placeholder-editor.js", "static/app.js"])
def test_served_js_is_syntactically_valid(live_server: str, path: str, tmp_path: Path) -> None:
    """`node --check` parses the served file without executing it (Decision 9: no JS test framework)."""
    _, body, _ = _get(f"{live_server}/{path}")
    js_file = tmp_path / (path.rsplit("/", 1)[-1])
    js_file.write_bytes(body)
    result = subprocess.run(
        ["node", "--check", str(js_file)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
