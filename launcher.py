#!/usr/bin/env python3
"""Clip Extractor launcher - entry point for .exe build."""

import argparse
import subprocess
import sys
import webbrowser
import threading
from pathlib import Path

# Ensure working directory is the exe's directory
import os
if getattr(sys, "frozen", False):
    os.chdir(Path(sys.executable).parent)


SETTINGS_FILE = Path(__file__).parent / "default_settings.json"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 7860
SERVER_URL = f"http://localhost:{SERVER_PORT}"
PROCESS_EXIT_TIMEOUT = 5.0


def _is_clip_extractor_page_available(*, url=SERVER_URL, timeout=0.75):
    """Return whether ``url`` is an already-running Clip Extractor page."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if getattr(response, "status", None) != 200:
                return False
            page = response.read(512 * 1024)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    return b"Clip Extractor" in page


def _find_listening_process(port=SERVER_PORT):
    """Return the process listening on ``port``, if it can be identified."""
    import psutil

    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.Error:
        return None

    for connection in connections:
        local_address = connection.laddr
        if (
            connection.status == psutil.CONN_LISTEN
            and connection.pid
            and local_address
            and local_address.port == port
        ):
            try:
                return psutil.Process(connection.pid)
            except psutil.NoSuchProcess:
                return None
    return None


def _is_owned_clip_extractor_process(process):
    """Verify that ``process`` was launched from this Clip Extractor copy."""
    import psutil

    try:
        if getattr(sys, "frozen", False):
            return Path(process.exe()).resolve() == Path(sys.executable).resolve()

        working_directory = Path(process.cwd()).resolve()
        command_line = process.cmdline()
    except (psutil.Error, OSError):
        return False

    expected_launcher = Path(__file__).resolve()
    for argument in command_line[1:]:
        candidate = Path(argument)
        if candidate.name.lower() != expected_launcher.name.lower():
            continue
        if not candidate.is_absolute():
            candidate = working_directory / candidate
        try:
            if candidate.resolve() == expected_launcher:
                return True
        except OSError:
            return False
    return False


def _wait_for_port_release(*, timeout=PROCESS_EXIT_TIMEOUT):
    """Wait until the fixed UI port no longer accepts connections."""
    import socket
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=0.2):
                pass
        except OSError:
            return True
        time.sleep(0.1)
    return False


def stop_existing_instance_if_running(*, attempts=1, retry_interval=0.15):
    """Stop an existing Clip Extractor so this launch uses the fixed port."""
    import psutil
    import time

    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        if _is_clip_extractor_page_available():
            process = _find_listening_process()
            if process is None:
                if _wait_for_port_release(timeout=0.5):
                    return True
                print(
                    f"ERROR: 起動中のClip Extractorを特定できませんでした。"
                    f"{SERVER_PORT}番ポートを使用しているプロセスを終了してください。"
                )
                return False
            if process.pid == os.getpid():
                print("ERROR: 起動中プロセスの判定に失敗しました。")
                return False
            if not _is_owned_clip_extractor_process(process):
                print(
                    "ERROR: 7860番ポートのプロセスがこのClip Extractorか"
                    "安全確認できないため、終了しませんでした。"
                )
                return False

            print(
                f"[INFO] 起動中のClip Extractor (PID {process.pid}) を"
                "終了して再起動します。"
            )
            try:
                process.kill()
                process.wait(timeout=PROCESS_EXIT_TIMEOUT)
            except psutil.NoSuchProcess:
                pass
            except (psutil.Error, OSError) as exc:
                print(f"ERROR: 起動中のClip Extractorを終了できませんでした: {exc}")
                return False

            if not _wait_for_port_release():
                print(f"ERROR: {SERVER_PORT}番ポートが解放されませんでした。")
                return False
            print(f"[OK] {SERVER_PORT}番ポートを解放しました。")
            return True
        if attempt + 1 < attempts:
            time.sleep(retry_interval)
    return True


def _find_windows_default_browser_executable():
    """Find the executable registered for HTTPS URLs on Windows."""
    if sys.platform != "win32":
        return None

    import re
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations"
            r"\UrlAssociations\https\UserChoice",
        ) as choice_key:
            prog_id = winreg.QueryValueEx(choice_key, "ProgId")[0]
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT,
            rf"{prog_id}\shell\open\command",
        ) as command_key:
            command = winreg.QueryValue(command_key, None)
    except OSError:
        return None

    match = re.match(r'\s*"([^"]+\.exe)"|\s*([^\s]+\.exe)', command, re.I)
    if not match:
        return None
    executable = Path(os.path.expandvars(match.group(1) or match.group(2)))
    return executable if executable.is_file() else None


def open_app_page(*, url=SERVER_URL, platform=None):
    """Open the UI visibly, preferring a new browser window on Windows."""
    target_platform = platform or sys.platform
    if target_platform == "win32":
        browser = _find_windows_default_browser_executable()
        if browser is not None:
            browser_name = browser.name.lower()
            if browser_name == "firefox.exe":
                args = [str(browser), "-new-window", url]
            elif browser_name in {
                "brave.exe",
                "chrome.exe",
                "chromium.exe",
                "msedge.exe",
                "opera.exe",
                "vivaldi.exe",
            }:
                args = [str(browser), "--new-window", url]
            else:
                args = [str(browser), url]
            try:
                subprocess.Popen(args, close_fds=True)
            except OSError:
                pass
            else:
                return True
    return bool(webbrowser.open(url, new=2, autoraise=True))


def _is_port_bind_error(exc):
    """Return whether an OSError reports that the fixed UI port is occupied."""
    message = str(exc).lower()
    return (
        getattr(exc, "winerror", None) == 10048
        or getattr(exc, "errno", None) in {98, 10048}
        or "cannot find empty port" in message
        or "address already in use" in message
        or "attempting to bind" in message
    )


def _launch_with_port_reuse(app, launch_kwargs):
    """Launch Gradio, replacing a simultaneous Clip Extractor instance."""
    for launch_attempt in range(2):
        try:
            app.launch(**launch_kwargs)
        except OSError as exc:
            if not _is_port_bind_error(exc):
                raise
            if launch_attempt == 0 and stop_existing_instance_if_running(
                attempts=20,
                retry_interval=0.15,
            ):
                continue
            print(
                f"ERROR: {SERVER_PORT}番ポートを別のアプリが使用しています。"
                "そのアプリを終了してから再起動してください。"
            )
            return 1
        return 0
    return 1


def open_browser():
    """Open the browser as soon as the server is accepting connections.

    Polls the port instead of a fixed sleep so we open immediately on a
    fast machine and still wait out a slow model/dependency load on a
    slower one. Falls back to opening anyway after 30s so a firewall or
    other probe failure never leaves the user without a browser tab.
    """
    import time

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _is_clip_extractor_page_available(timeout=0.25):
            break
        time.sleep(0.25)
    open_app_page()


def launch_obs_if_requested(argv=None, settings_path=None):
    """Apply saved OBS launch settings or the combined-shortcut override."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--with-obs", action="store_true")
    args, _unknown = parser.parse_known_args(argv)

    from obs_launcher import launch_obs_from_settings

    result = launch_obs_from_settings(
        settings_path or SETTINGS_FILE,
        force=args.with_obs,
    )
    if result is None:
        return None
    level = "OK" if result.ok else "WARN"
    print(f"[{level}] {result.message}")
    return result


def main(argv=None):
    if not stop_existing_instance_if_running():
        return 1

    launch_obs_if_requested(argv)

    # Check external dependencies
    import shutil

    missing_required = []
    missing_optional = []
    if not shutil.which("ffmpeg"):
        missing_required.append("FFmpeg (https://ffmpeg.org/download.html)")
    if not shutil.which("claude"):
        # Claude CLI is optional — only needed when ai_provider = "claude".
        # OpenAI / Gemini users can proceed without it.
        missing_optional.append("Claude Code CLI (npm install -g @anthropic-ai/claude-code) — Claudeモード使用時のみ必要")

    if missing_required:
        print("=" * 50)
        print("ERROR: 以下の必須ツールが見つかりません:")
        for m in missing_required:
            print(f"  - {m}")
        print("PATHに追加してから再起動してください。")
        print("=" * 50)
        print()
    if missing_optional:
        print("=" * 50)
        print("INFO: 以下の任意ツールが見つかりません (OpenAI/Gemini 使用時は不要):")
        for m in missing_optional:
            print(f"  - {m}")
        print("=" * 50)
        print()

    # Launch browser in background
    threading.Thread(target=open_browser, daemon=True).start()

    print("Clip Extractor を起動しています...")
    print(f"ブラウザで {SERVER_URL} が開きます")
    print("終了するにはこのウィンドウを閉じてください")
    print()

    from web_app import (
        LAUNCH_THEME_KWARGS,
        create_ui,
        safe_launch_kwargs,
        schedule_obs_auto_connect,
    )

    schedule_obs_auto_connect()
    app = create_ui()
    app.queue()
    launch_kwargs = safe_launch_kwargs(
        server_name=SERVER_HOST,
        server_port=SERVER_PORT,
        ssr_mode=False,
        inbrowser=False,  # we handle browser open ourselves
        **LAUNCH_THEME_KWARGS,
    )
    return _launch_with_port_reuse(app, launch_kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
