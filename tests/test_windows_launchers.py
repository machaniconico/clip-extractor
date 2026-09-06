"""Static checks for the Windows launch and shortcut wiring."""

from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_default_batch_forwards_launcher_arguments():
    source = (ROOT / "Clip Extractor.bat").read_text(encoding="utf-8")

    assert "%PYTHON_CMD% launcher.py %*" in source


def test_default_batch_updates_ytdlp_before_launch():
    source = (ROOT / "Clip Extractor.bat").read_text(encoding="utf-8")
    lines = source.splitlines()
    upgrade_lines = [
        index
        for index, line in enumerate(lines)
        if "pip install --upgrade" in line and '"yt-dlp[default]"' in line
    ]

    assert upgrade_lines
    assert any(
        lines.index(":found_python") < index < lines.index("%PYTHON_CMD% launcher.py %*")
        for index in upgrade_lines
    )


def test_combined_batch_requests_obs_launch():
    source = (ROOT / "Clip Extractor with OBS.bat").read_text(encoding="ascii")

    assert 'call "%~dp0Clip Extractor.bat" --with-obs' in source


def test_setup_creates_only_the_default_desktop_shortcut():
    source = (ROOT / "setup.bat").read_text(encoding="utf-8")

    assert source.count("$ws.CreateShortcut(") == 1
    assert "Clip Extractor.lnk" in source
    assert "Clip Extractor + OBS.lnk" not in source
    assert ".Arguments = '--with-obs'" not in source


def test_setup_updates_ytdlp_after_installing_requirements():
    source = (ROOT / "setup.bat").read_text(encoding="utf-8")
    upgrade_command = 'pip install --upgrade "yt-dlp[default]"'

    assert upgrade_command in source
    assert source.index(upgrade_command) > source.index("pip install -r requirements.txt")
