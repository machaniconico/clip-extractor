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
            with socket.create_connection(("127.0.0.1", 7860), timeout=0.25):
                break
        except OSError:
            time.sleep(0.25)
    webbrowser.open("http://localhost:7860")


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
    print("ブラウザで http://localhost:7860 が開きます")
    print("終了するにはこのウィンドウを閉じてください")
    print()

    from web_app import create_ui, LAUNCH_THEME_KWARGS, safe_launch_kwargs

    app = create_ui()
    app.queue()
    app.launch(**safe_launch_kwargs(
        server_name="0.0.0.0",
        server_port=7860,
        ssr_mode=False,
        inbrowser=False,  # we handle browser open ourselves
        **LAUNCH_THEME_KWARGS,
    ))


if __name__ == "__main__":
    main()
