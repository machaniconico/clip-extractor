#!/usr/bin/env python3
"""Clip Extractor launcher - entry point for .exe build."""

import argparse
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


def open_existing_instance_if_running(*, attempts=1, retry_interval=0.15):
    """Open the current UI and return True instead of starting a duplicate."""
    import time

    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        if _is_clip_extractor_page_available():
            print("[OK] Clip Extractor は既に起動しています。既存の画面を開きます。")
            webbrowser.open(SERVER_URL)
            return True
        if attempt + 1 < attempts:
            time.sleep(retry_interval)
    return False


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
    """Launch Gradio, reusing a simultaneous winner when the port is taken."""
    try:
        app.launch(**launch_kwargs)
    except OSError as exc:
        if not _is_port_bind_error(exc):
            raise
        if open_existing_instance_if_running(attempts=20, retry_interval=0.15):
            return 0
        print(
            f"ERROR: {SERVER_PORT}番ポートを別のアプリが使用しています。"
            "そのアプリを終了してから再起動してください。"
        )
        return 1
    return 0


def open_browser():
    """Open the browser as soon as the server is accepting connections.

    Polls the port instead of a fixed sleep so we open immediately on a
    fast machine and still wait out a slow model/dependency load on a
    slower one. Falls back to opening anyway after 30s so a firewall or
    other probe failure never leaves the user without a browser tab.
    """
    import socket
    import time

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=0.25):
                break
        except OSError:
            time.sleep(0.25)
    webbrowser.open(SERVER_URL)


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
    if open_existing_instance_if_running():
        return 0

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
        server_name="0.0.0.0",
        server_port=SERVER_PORT,
        ssr_mode=False,
        inbrowser=False,  # we handle browser open ourselves
        **LAUNCH_THEME_KWARGS,
    )
    return _launch_with_port_reuse(app, launch_kwargs)


if __name__ == "__main__":
    main()
