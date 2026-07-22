"""Locate and optionally launch OBS Studio without blocking app startup.

The combined Windows launcher uses this module before starting Gradio.  All
failures are converted to :class:`ObsLaunchResult` so an OBS installation
problem never prevents Clip Extractor itself from opening.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Mapping


OBS_EXECUTABLE_ENV = "OBS_EXECUTABLE"
_WINDOWS_PROCESS_NAMES = ("obs64.exe", "obs32.exe")


@dataclass(frozen=True)
class ObsLaunchResult:
    """Outcome of an optional OBS launch attempt."""

    status: str
    ok: bool
    message: str
    executable: Path | None = None


@dataclass(frozen=True)
class ObsLaunchPreferences:
    """Persisted preferences controlling launch-on-startup behavior."""

    enabled: bool = False
    executable_path: str = ""


def load_obs_launch_preferences(settings_path: str | Path) -> ObsLaunchPreferences:
    """Read OBS launch preferences, defaulting safely for missing/bad JSON."""
    try:
        data = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ObsLaunchPreferences()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return ObsLaunchPreferences()

    raw_path = data.get("obs_executable_path", "")
    executable_path = raw_path.strip() if isinstance(raw_path, str) else ""
    return ObsLaunchPreferences(
        enabled=data.get("obs_launch_on_startup") is True,
        executable_path=executable_path,
    )


def _clean_path(value: str) -> Path:
    """Expand a user-supplied executable path while preserving relative paths."""
    return Path(os.path.expandvars(os.path.expanduser(value.strip().strip('"'))))


def _windows_install_candidates(env: Mapping[str, str]) -> list[Path]:
    """Return common OBS Studio install locations in deterministic order."""
    candidates: list[Path] = []
    program_files = env.get("ProgramFiles")
    if program_files:
        candidates.append(
            Path(program_files) / "obs-studio" / "bin" / "64bit" / "obs64.exe"
        )

    program_w6432 = env.get("ProgramW6432")
    if program_w6432:
        candidate = (
            Path(program_w6432) / "obs-studio" / "bin" / "64bit" / "obs64.exe"
        )
        if candidate not in candidates:
            candidates.append(candidate)

    program_files_x86 = env.get("ProgramFiles(x86)")
    if program_files_x86:
        base = Path(program_files_x86)
        candidates.extend(
            [
                base / "obs-studio" / "bin" / "64bit" / "obs64.exe",
                base / "obs-studio" / "bin" / "32bit" / "obs32.exe",
                base
                / "Steam"
                / "steamapps"
                / "common"
                / "OBS Studio"
                / "bin"
                / "64bit"
                / "obs64.exe",
            ]
        )

    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            Path(local_app_data)
            / "Programs"
            / "obs-studio"
            / "bin"
            / "64bit"
            / "obs64.exe"
        )
    return candidates


def find_obs_executable(
    *, platform: str | None = None, env: Mapping[str, str] | None = None
) -> Path | None:
    """Find OBS via an explicit override, PATH, then common install folders."""
    current_platform = platform or sys.platform
    current_env = os.environ if env is None else env

    explicit = current_env.get(OBS_EXECUTABLE_ENV, "").strip()
    if explicit:
        path = _clean_path(explicit)
        if path.is_file():
            return path
        # An explicit override is authoritative; do not silently launch a
        # different OBS installation when this path is mistyped or stale.
        return None

    command_names = (
        ("obs64.exe", "obs32.exe", "obs.exe")
        if current_platform == "win32"
        else ("obs",)
    )
    for name in command_names:
        found = shutil.which(name)
        if found:
            path = Path(found)
            if path.is_file():
                return path

    if current_platform == "win32":
        candidates = _windows_install_candidates(current_env)
    elif current_platform == "darwin":
        candidates = [Path("/Applications/OBS.app/Contents/MacOS/OBS")]
    else:
        candidates = []

    return next((path for path in candidates if path.is_file()), None)


def is_obs_running(*, platform: str | None = None) -> bool:
    """Return whether OBS is already running, failing safely if probing fails."""
    current_platform = platform or sys.platform
    try:
        if current_platform == "win32":
            for process_name in _WINDOWS_PROCESS_NAMES:
                result = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/NH"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                if process_name.casefold() in (result.stdout or "").casefold():
                    return True
            return False

        pgrep = shutil.which("pgrep")
        if not pgrep:
            return False
        process_name = "OBS" if current_platform == "darwin" else "obs"
        result = subprocess.run(
            [pgrep, "-x", process_name],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def launch_obs(executable: str | Path | None = None) -> ObsLaunchResult:
    """Start OBS once and return a non-throwing, user-readable result."""
    if is_obs_running():
        return ObsLaunchResult(
            status="already_running",
            ok=True,
            message="OBS Studio は既に起動しています。二重起動をスキップしました。",
        )

    path = (
        _clean_path(str(executable))
        if executable is not None
        else find_obs_executable()
    )
    if path is None or not path.is_file():
        return ObsLaunchResult(
            status="not_found",
            ok=False,
            message=(
                "OBS Studio が見つかりません。Settings の「OBS実行ファイルのパス」を"
                f"確認するか、環境変数 {OBS_EXECUTABLE_ENV} に obs64.exe の"
                "フルパスを設定してください。"
            ),
            executable=path,
        )

    try:
        # OBS resolves some runtime assets relative to bin/64bit, so mirror its
        # normal Windows shortcut and use the executable directory as cwd.
        subprocess.Popen([str(path)], cwd=str(path.parent))
    except (OSError, subprocess.SubprocessError) as exc:
        return ObsLaunchResult(
            status="error",
            ok=False,
            message=f"OBS Studio の起動に失敗しました: {exc}",
            executable=path,
        )

    return ObsLaunchResult(
        status="started",
        ok=True,
        message=f"OBS Studio を起動しました: {path}",
        executable=path,
    )


def launch_obs_from_settings(
    settings_path: str | Path, *, force: bool = False
) -> ObsLaunchResult | None:
    """Apply saved launch preferences, optionally forcing a combined launch.

    ``None`` means automatic launch is disabled. Unexpected failures are
    converted to a result so Clip Extractor startup always proceeds.
    """
    try:
        preferences = load_obs_launch_preferences(settings_path)
        if not force and not preferences.enabled:
            return None
        return launch_obs(preferences.executable_path or None)
    except Exception as exc:  # final fail-open boundary for app startup
        return ObsLaunchResult(
            status="error",
            ok=False,
            message=f"OBS Studio の起動確認に失敗しました: {exc}",
        )
