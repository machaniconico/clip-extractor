#!/usr/bin/env python3
"""Clip Extractor launcher - entry point for .exe build."""

import sys
import webbrowser
import threading
from pathlib import Path

# Ensure working directory is the exe's directory
import os
if getattr(sys, "frozen", False):
    os.chdir(Path(sys.executable).parent)


def open_browser():
    """Open browser after a short delay."""
    import time
    time.sleep(2)
    webbrowser.open("http://localhost:8080")


def main():
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
    print("ブラウザで http://localhost:8080 が開きます")
    print("終了するにはこのウィンドウを閉じてください")
    print()

    from web_app import create_ui

    app = create_ui()
    app.queue()
    app.launch(
        server_name="0.0.0.0",
        server_port=8080,
        ssr_mode=False,
        inbrowser=False,  # we handle browser open ourselves
    )


if __name__ == "__main__":
    main()
