"""Regression test for the startup lazy-import optimization.

web_app.py transitively imports transcriber / downloader / drive_upload /
_google_auth, which each used to eagerly import a heavy third-party package
(faster_whisper, yt_dlp, googleapiclient, google_auth_oauthlib) at module
load time even though those packages are only needed once the user actually
runs a transcription / download / upload. Importing them lazily (inside the
functions that use them) keeps `import web_app` fast; this test guards
against a regression sneaking a top-level import back in.

Also builds the Blocks graph via create_ui() and checks again: a prior
regression had the YouTube auth status Textbox call youtube_api.
auth_status_summary() synchronously as its initial `value=`, which pulls in
the whole google stack (and does a silent network token refresh) while the
UI graph is still being built — undoing the point of backgrounding the auth
probe. The real check now runs via an app.load() handler, which registers
during create_ui() but only executes on an actual page load, so it must not
appear in sys.modules just from building the graph.

Runs both checks in a subprocess (rather than in-process) so this process's
own sys.modules — already polluted by other tests importing web_app — can't
hide a regression.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

REPO_ROOT = Path(__file__).parent.parent

_HEAVY_MODULES = ("faster_whisper", "yt_dlp", "googleapiclient", "google_auth_oauthlib")

_CHECK_SCRIPT = f"""
import sys
import web_app

app = web_app.create_ui()

leaked = [name for name in {_HEAVY_MODULES!r} if name in sys.modules]
print(",".join(leaked))
"""

_MATPLOTLIB_BACKEND_CHECK_SCRIPT = """
import sys
import web_app
import matplotlib

print(matplotlib.get_backend())
print(",".join(name for name in sys.modules if name.startswith("PyQt5")) or "-")
"""


def test_creating_ui_does_not_load_heavy_deps():
    """`import web_app` and `create_ui()` must not pull in faster_whisper /
    yt_dlp / googleapiclient / google_auth_oauthlib — those only load once
    the corresponding feature (transcribe / download / Drive upload / OAuth)
    actually runs, not just from building the UI graph."""
    if importlib.util.find_spec("gradio") is None:
        pytest.skip("gradio not installed")

    result = subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPT],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"`import web_app` / create_ui() failed in subprocess:\n{result.stdout}\n{result.stderr}"
    )
    leaked = result.stdout.strip()
    assert leaked == "", f"heavy module(s) loaded by import web_app + create_ui(): {leaked}"


def test_web_app_forces_headless_matplotlib_backend():
    """The browser UI must not load Qt's native DLLs into the Whisper process."""
    if importlib.util.find_spec("gradio") is None:
        pytest.skip("gradio not installed")
    if importlib.util.find_spec("matplotlib") is None:
        pytest.skip("matplotlib not installed")

    result = subprocess.run(
        [sys.executable, "-c", _MATPLOTLIB_BACKEND_CHECK_SCRIPT],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Matplotlib backend check failed in subprocess:\n{result.stdout}\n{result.stderr}"
    )
    backend, pyqt_modules = result.stdout.strip().splitlines()
    assert backend.lower() == "agg"
    assert pyqt_modules == "-"
