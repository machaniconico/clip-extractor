"""Static checks for the Windows launch and shortcut wiring."""

from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_default_batch_forwards_launcher_arguments():
    source = (ROOT / "Clip Extractor.bat").read_text(encoding="utf-8")

    assert "%PYTHON_CMD% launcher.py %*" in source


def test_combined_batch_requests_obs_launch():
    source = (ROOT / "Clip Extractor with OBS.bat").read_text(encoding="ascii")

    assert 'call "%~dp0Clip Extractor.bat" --with-obs' in source


def test_setup_creates_opt_in_obs_desktop_shortcut():
    source = (ROOT / "setup.bat").read_text(encoding="utf-8")

    assert "Clip Extractor + OBS.lnk" in source
    assert ".Arguments = '--with-obs'" in source
