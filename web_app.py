#!/usr/bin/env python3
"""clip-extractor Web UI using Gradio."""

import logging
import os
import sys
import shutil
import socket
import subprocess
import traceback
import inspect
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

# Gradio temporarily switches Matplotlib backends around every event.  On
# Windows, Matplotlib's automatic choice can be QtAgg, which loads PyQt5's
# bundled MSVCP140.dll into the same process as CTranslate2/CUDA and can cause
# a native access violation while Whisper is loading.  This browser-based UI
# never needs a desktop plotting backend, so keep the process headless.
os.environ["MPLBACKEND"] = "Agg"

import gradio as gr


@dataclass
class ProcessResult:
    """Structured result from the processing pipeline.

    Fields line up with the Gradio outputs wired in render_btn.click:
    (log_output, highlights_output, download_output, drive_link_output,
    chapters_output, premiere_job_state). Building this dataclass instead of
    scattering raw tuples across every return statement keeps the field order in one
    place — adding/removing a field no longer requires touching every
    early-exit and error branch.

    download_path=None clears the gr.File output widget; a real Path
    value populates it with the resulting zip.
    """
    log: str = ""
    highlights: str = ""
    download_path: Path | None = None
    drive_link: str = ""
    chapters_text: str = ""
    premiere_job: dict | None = None

    def as_gradio_outputs(self) -> tuple:
        """Order matches render_btn.click(outputs=[...])."""
        return (
            self.log,
            self.highlights,
            self.download_path,
            self.drive_link,
            self.chapters_text,
            self.premiere_job,
        )


@dataclass(frozen=True)
class ObsPipelineOutcome:
    """Machine-readable result for unattended OBS processing."""

    log: str
    success: bool
    error: str = ""
    output_dir: str = ""
    clip_paths: tuple[str, ...] = ()
    shorts_paths: tuple[str, ...] = ()
    chapters_path: str = ""
    chapters_text: str = ""
    youtube_appended: bool | None = None
    skipped: bool = False

# --- File logging setup ---
# Use TEMP dir to avoid Japanese path issues with OneDrive/Desktop
LOG_DIR = Path(os.environ.get("TEMP", ".")) / "clip-extractor-logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"app_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logger = logging.getLogger("clip-extractor")
logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_fh)
logger.addHandler(_sh)
logger.info(f"Log file: {LOG_FILE}")


def get_system_fonts():
    """Get list of installed font family names from the system."""
    try:
        ps_cmd = (
            'powershell -NoProfile -Command "'
            "[System.Reflection.Assembly]::LoadWithPartialName('System.Drawing') | Out-Null; "
            "(New-Object System.Drawing.Text.InstalledFontCollection).Families | "
            "ForEach-Object { $_.Name }\""
        )
        result = subprocess.run(ps_cmd, capture_output=True, text=True, shell=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            fonts = sorted(set(result.stdout.strip().splitlines()))
            return fonts
    except Exception:
        pass
    return [
        "BIZ UDPGothic", "BIZ UDPMincho", "M PLUS Rounded 1c",
        "Meiryo", "Noto Sans JP", "Noto Serif JP", "Yu Gothic UI",
    ]


import json

FONT_CACHE_FILE = LOG_DIR / "font_cache.json"


def _write_font_cache(fonts: list) -> None:
    """Write FONT_CACHE_FILE atomically (tmp file in the same dir + os.replace)
    so a reader never sees a partially-written cache from a crash or a
    concurrent launch."""
    tmp = FONT_CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fonts, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, FONT_CACHE_FILE)


def get_system_fonts_cached():
    """Return the system font list fast, refreshing the on-disk cache
    in the background.

    get_system_fonts() shells out to PowerShell + .NET and takes 1-3s on
    every call. On a cache hit we return the cached list immediately and
    kick off a fresh get_system_fonts() in a daemon thread to update the
    cache file for next launch (this session's UI is unaffected). On a
    cache miss/corruption we fall back to the synchronous call, same as
    before this cache existed.
    """
    try:
        cached = json.loads(FONT_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(cached, list) and cached and all(isinstance(f, str) for f in cached):
            def _refresh_cache():
                try:
                    fonts = get_system_fonts()
                    if fonts:
                        _write_font_cache(fonts)
                except Exception:
                    pass

            threading.Thread(target=_refresh_cache, daemon=True).start()
            return cached
    except Exception:
        pass

    fonts = get_system_fonts()
    if fonts:
        try:
            _write_font_cache(fonts)
        except Exception:
            pass
    return fonts


from config import FontConfig

SETTINGS_FILE = Path(__file__).parent / "default_settings.json"
GEMINI_KEY_FILE = Path(__file__).parent / ".gemini_key"
OBS_PASSWORD_FILE = Path(__file__).parent / ".obs_password"

OBS_CONNECTION_DEFAULTS = {
    "obs_trigger_method": "websocket",
    "obs_host": "localhost",
    "obs_port": 4455,
    "obs_stop_event": "record",
    "obs_watch_folder": "",
    "obs_auto_process": True,
}


def _normalise_shorts_blur_strength(value) -> float:
    try:
        strength = float(value)
    except (TypeError, ValueError):
        strength = 20.0
    if strength != strength:  # NaN
        strength = 20.0
    return min(50.0, max(0.0, strength))


def _normalise_shorts_title_position(value) -> str:
    position = str(value or "top")
    return position if position in {"top", "bottom", "overlay"} else "top"


def shorts_blur_visibility(mode: str):
    """Only show blur strength when the blur background mode is selected."""
    return gr.update(visible=mode == "blur")


# Processing controls are kept in a separate profile for OBS automation.  The
# archive/Input profile remains the top-level settings profile so users can tune
# both workflows independently.  Older settings files do not have this nested
# profile; in that case OBS falls back to the existing archive values until the
# user saves an OBS profile.
OBS_PROCESSING_DEFAULTS = {
    "enable_clips": True,
    "clip_prompt": "",
    "enable_chapters": True,
    "chapter_prompt": "",
    "auto_append_youtube": False,
    "confirm_before_auto_process": True,
    "num_clips": 5,
    "min_duration": 30,
    "max_duration": 90,
    "output_mode": "combined",
    "generate_shorts": False,
    "shorts_mode": "pad",
    "shorts_blur_strength": 20,
    "shorts_crop": "center",
    "shorts_title": True,
    "shorts_title_position": "top",
    "generate_thumbnails": False,
    "audio_fusion": False,
    "audio_alpha": 0.35,
    "karaoke": False,
}


def _obs_processing_settings_from_defaults(defaults: dict | None = None) -> dict:
    """Return the OBS processing profile with legacy top-level fallbacks."""
    source = defaults if defaults is not None else load_defaults()
    saved_profile = source.get("obs_processing")
    if not isinstance(saved_profile, dict):
        saved_profile = {}
    return {
        key: saved_profile.get(key, source.get(key, fallback))
        for key, fallback in OBS_PROCESSING_DEFAULTS.items()
    }


def _normalise_obs_processing_settings(
    values: dict | None,
    defaults: dict | None = None,
) -> dict:
    """Merge live OBS controls into a complete, serialisable profile."""
    base = _obs_processing_settings_from_defaults(defaults)
    if values:
        for key in OBS_PROCESSING_DEFAULTS:
            value = values.get(key)
            if value is not None:
                base[key] = value

    base["enable_clips"] = bool(base["enable_clips"])
    base["enable_chapters"] = bool(base["enable_chapters"])
    base["auto_append_youtube"] = bool(base["auto_append_youtube"])
    base["confirm_before_auto_process"] = bool(base["confirm_before_auto_process"])
    base["generate_shorts"] = bool(base["generate_shorts"])
    base["shorts_title"] = bool(base["shorts_title"])
    base["generate_thumbnails"] = bool(base["generate_thumbnails"])
    base["audio_fusion"] = bool(base["audio_fusion"])
    base["karaoke"] = bool(base["karaoke"])
    try:
        base["num_clips"] = int(base["num_clips"])
    except (TypeError, ValueError):
        base["num_clips"] = OBS_PROCESSING_DEFAULTS["num_clips"]
    for key in ("min_duration", "max_duration"):
        try:
            base[key] = int(base[key])
        except (TypeError, ValueError):
            base[key] = OBS_PROCESSING_DEFAULTS[key]
    try:
        base["audio_alpha"] = float(base["audio_alpha"])
    except (TypeError, ValueError):
        base["audio_alpha"] = OBS_PROCESSING_DEFAULTS["audio_alpha"]
    base["shorts_blur_strength"] = _normalise_shorts_blur_strength(
        base["shorts_blur_strength"]
    )
    base["shorts_title_position"] = _normalise_shorts_title_position(
        base["shorts_title_position"]
    )
    return base


def _build_obs_processing_settings(
    enable_clips,
    clip_prompt,
    enable_chapters,
    chapter_prompt,
    auto_append_youtube,
    num_clips,
    min_duration,
    max_duration,
    output_mode,
    generate_shorts,
    shorts_mode,
    shorts_crop,
    shorts_title,
    generate_thumbnails,
    audio_fusion,
    audio_alpha,
    karaoke,
    confirm_before_auto_process=True,
    shorts_blur_strength=20,
    shorts_title_position="top",
) -> dict:
    """Build the OBS profile from the dedicated controls in the UI."""
    return _normalise_obs_processing_settings(
        {
            "enable_clips": enable_clips,
            "clip_prompt": clip_prompt,
            "enable_chapters": enable_chapters,
            "chapter_prompt": chapter_prompt,
            "auto_append_youtube": auto_append_youtube,
            "confirm_before_auto_process": confirm_before_auto_process,
            "num_clips": num_clips,
            "min_duration": min_duration,
            "max_duration": max_duration,
            "output_mode": output_mode,
            "generate_shorts": generate_shorts,
            "shorts_mode": shorts_mode,
            "shorts_blur_strength": shorts_blur_strength,
            "shorts_crop": shorts_crop,
            "shorts_title": shorts_title,
            "shorts_title_position": shorts_title_position,
            "generate_thumbnails": generate_thumbnails,
            "audio_fusion": audio_fusion,
            "audio_alpha": audio_alpha,
            "karaoke": karaoke,
        }
    )


def load_gemini_api_key(env_var: str = "GEMINI_API_KEY") -> str:
    """Return the Gemini API key.

    File-first precedence: .gemini_key > env var > empty. The file
    represents a key the user explicitly saved via the UI for this
    specific install, so it wins over a system-wide environment
    variable that may belong to a different project entirely. Env var
    is kept as a fallback so CI / fresh installs without a saved file
    still work.
    """
    if GEMINI_KEY_FILE.exists():
        try:
            saved = GEMINI_KEY_FILE.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            saved = ""
        if saved:
            return saved
    val = os.environ.get(env_var, "").strip()
    if val:
        return val
    return ""


def save_gemini_api_key(key_text: str) -> None:
    """Persist the Gemini API key to GEMINI_KEY_FILE, or delete the file
    when the textbox is cleared.

    Uses gr.Info / gr.Warning for feedback since this is wired to a
    Gradio button click. Never raises — a failed write surfaces as a
    warning toast, keeping the UI responsive.
    """
    text = (key_text or "").strip()
    try:
        if text:
            GEMINI_KEY_FILE.write_text(text, encoding="utf-8")
            gr.Info("API キーを .gemini_key に保存しました。次回起動時から自動で読み込まれます。")
        elif GEMINI_KEY_FILE.exists():
            GEMINI_KEY_FILE.unlink()
            gr.Info("API キーをクリアしました (.gemini_key を削除)。")
        else:
            gr.Warning("保存する API キーが空です。textbox にキーを入力してから押してください。")
    except Exception as exc:
        gr.Warning(f"API キーの保存に失敗しました: {exc}")


def load_obs_password() -> str:
    """Load the local OBS secret without placing it in tracked settings."""
    if not OBS_PASSWORD_FILE.exists():
        return ""
    try:
        return OBS_PASSWORD_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _obs_password_ui_copy(has_saved_password: bool) -> tuple[str, str]:
    """Describe saved-secret state without sending the secret to the browser."""
    if has_saved_password:
        return (
            "••••••••（保存済み）",
            "Passwordは保存済みです。空欄のまま再利用します。"
            "変更する場合は新しいPasswordを入力して「OBS連携 開始」を"
            "押してください。",
        )
    return (
        "Passwordを入力",
        "Passwordは未保存です。「Passwordを保存」をONにして入力後、"
        "「OBS連携 開始」を押すと保存します。"
        "チェックだけでは保存されません。",
    )


def _save_obs_password(password: str) -> None:
    """Persist the OBS secret locally, or remove it when cleared."""
    value = password or ""
    if value:
        OBS_PASSWORD_FILE.write_text(value, encoding="utf-8")
    elif OBS_PASSWORD_FILE.exists():
        OBS_PASSWORD_FILE.unlink()


def load_defaults() -> dict:
    """Load saved default settings."""
    defaults = {
        "ai_provider": "gemini", "ai_model": "gemini-2.5-flash",
        "enable_clips": True, "enable_chapters": True,
        "clip_prompt": "", "chapter_prompt": "",
        "auto_append_youtube": False,
        "num_clips": 5, "min_duration": 30, "max_duration": 90,
        "output_mode": "combined", "generate_shorts": False,
        "shorts_mode": "pad", "shorts_crop": "center",
        "shorts_blur_strength": 20,
        "shorts_title": True, "shorts_title_position": "top",
        "generate_thumbnails": False,
        "audio_fusion": False, "audio_alpha": 0.35,
        "karaoke": False,
        "whisper_model": "large-v3", "language": "ja",
        "font_name": "Noto Sans JP", "font_size": 96, "font_color": "#FFFFFF",
        "output_base_dir": "",
        "premiere_executable_path": "",
        "obs_launch_on_startup": False,
        "obs_auto_connect_on_startup": True,
        "obs_executable_path": "",
    }
    defaults.update(OBS_CONNECTION_DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update(saved)
        except Exception:
            pass
    defaults["shorts_blur_strength"] = _normalise_shorts_blur_strength(
        defaults.get("shorts_blur_strength", 20)
    )
    defaults["shorts_title_position"] = _normalise_shorts_title_position(
        defaults.get("shorts_title_position", "top")
    )
    # Secrets are loaded only inside start_obs_watch(). Returning one here can
    # expose it in Gradio's component configuration when the app is LAN-bound.
    defaults.pop("obs_password", None)
    return defaults


def _save_obs_connection_defaults(
    method: str,
    host: str,
    port: int,
    password: str,
    save_password: bool,
    stop_event: str,
    watch_folder: str,
    auto_process: bool,
    processing_settings: dict | None = None,
) -> None:
    """Persist OBS controls while keeping the password out of tracked JSON."""
    data = load_defaults()
    data.pop("obs_password", None)
    data.update(
        {
            "obs_trigger_method": method,
            "obs_host": host,
            "obs_port": port,
            "obs_stop_event": stop_event,
            "obs_watch_folder": watch_folder,
            "obs_auto_process": bool(auto_process),
        }
    )
    if processing_settings is not None:
        data["obs_processing"] = _normalise_obs_processing_settings(
            processing_settings,
            defaults=data,
        )
    SETTINGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _save_obs_password(password if save_password else "")


def save_defaults(ai_provider, ai_model,
                  enable_clips, enable_chapters, clip_prompt, chapter_prompt,
                  auto_append_youtube,
                  num_clips, output_mode, generate_shorts, shorts_mode, shorts_crop, shorts_title,
                  min_duration, max_duration,
                  whisper_model, language,
                  font_name, font_size, font_color,
                  output_base_dir,
                  generate_thumbnails=False,
                  audio_fusion=False, audio_alpha=0.35,
                  karaoke=False,
                  shorts_blur_strength=20,
                  shorts_title_position="top",
                  premiere_executable_path="",
                  obs_launch_on_startup=False,
                  obs_executable_path="",
                  obs_auto_connect_on_startup=True):
    """Save current settings as defaults."""
    loaded_defaults = load_defaults()
    saved_obs = {
        key: value
        for key, value in loaded_defaults.items()
        if key in OBS_CONNECTION_DEFAULTS
    }
    saved_obs_processing = loaded_defaults.get("obs_processing")
    data = {
        "ai_provider": ai_provider, "ai_model": ai_model,
        "enable_clips": bool(enable_clips), "enable_chapters": bool(enable_chapters),
        "clip_prompt": clip_prompt, "chapter_prompt": chapter_prompt,
        "auto_append_youtube": bool(auto_append_youtube),
        "num_clips": int(num_clips),
        "output_mode": output_mode, "generate_shorts": bool(generate_shorts),
        "shorts_mode": shorts_mode, "shorts_crop": shorts_crop,
        "shorts_blur_strength": _normalise_shorts_blur_strength(
            shorts_blur_strength
        ),
        "shorts_title": bool(shorts_title),
        "shorts_title_position": _normalise_shorts_title_position(
            shorts_title_position
        ),
        "min_duration": int(min_duration), "max_duration": int(max_duration),
        "whisper_model": whisper_model, "language": language,
        "font_name": font_name, "font_size": int(font_size),
        "font_color": font_color,
        "output_base_dir": (output_base_dir or "").strip(),
        "generate_thumbnails": bool(generate_thumbnails),
        "audio_fusion": bool(audio_fusion),
        "audio_alpha": float(audio_alpha),
        "karaoke": bool(karaoke),
        "premiere_executable_path": (premiere_executable_path or "").strip(),
        "obs_launch_on_startup": bool(obs_launch_on_startup),
        "obs_auto_connect_on_startup": bool(obs_auto_connect_on_startup),
        "obs_executable_path": (obs_executable_path or "").strip(),
    }
    data.update(saved_obs)
    if isinstance(saved_obs_processing, dict):
        data["obs_processing"] = dict(saved_obs_processing)
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "Settings saved as default!"


def save_obs_processing_defaults(
    enable_clips,
    clip_prompt,
    enable_chapters,
    chapter_prompt,
    auto_append_youtube,
    num_clips,
    min_duration,
    max_duration,
    output_mode,
    generate_shorts,
    shorts_mode,
    shorts_crop,
    shorts_title,
    generate_thumbnails,
    audio_fusion,
    audio_alpha,
    karaoke,
    auto_start_without_prompt_confirmation=False,
    shorts_blur_strength=20,
    shorts_title_position="top",
):
    """Persist the dedicated OBS processing profile without changing Input."""
    data = load_defaults()
    data.pop("obs_password", None)
    data["obs_processing"] = _build_obs_processing_settings(
        enable_clips,
        clip_prompt,
        enable_chapters,
        chapter_prompt,
        auto_append_youtube,
        num_clips,
        min_duration,
        max_duration,
        output_mode,
        generate_shorts,
        shorts_mode,
        shorts_crop,
        shorts_title,
        generate_thumbnails,
        audio_fusion,
        audio_alpha,
        karaoke,
        not bool(auto_start_without_prompt_confirmation),
        shorts_blur_strength=shorts_blur_strength,
        shorts_title_position=shorts_title_position,
    )
    SETTINGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return "OBS用の生成設定を保存しました"


def resolve_output_base(user_text: str) -> Path:
    """Resolve the effective output base dir.

    Empty / whitespace input → <repo>/output. Otherwise honour the user
    input (absolute, relative, or ~-prefixed). Called from both the UI
    event handlers and detection/render phases so the "displayed path" in Settings
    matches the path that actually gets written to.
    """
    base_text = (user_text or "").strip()
    if base_text:
        return Path(base_text).expanduser()
    return Path(__file__).resolve().parent / "output"


def _create_output_dir(base_dir: Path) -> Path:
    """Create a collision-free directory for one processing run."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for _ in range(10):
        candidate = base_dir / f"output_{timestamp}_{uuid.uuid4().hex[:8]}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("一意な出力フォルダを作成できませんでした")


def pick_folder_dialog(
    current_value: str,
    title: str = "保存先フォルダを選択",
) -> str:
    """Open the native OS folder-picker and return the selected path.

    On cancel / error, returns the current textbox value unchanged so
    Gradio's .click() doesn't blank the field. Windows uses PowerShell's
    FolderBrowserDialog (run in STA mode, which the control requires);
    other OSes fall back to tkinter.filedialog.askdirectory.
    """
    initial = resolve_output_base(current_value)
    try:
        initial.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    fallback = current_value or ""

    if os.name == "nt":
        try:
            safe_initial = str(initial).replace("'", "''")
            safe_title = str(title).replace("'", "''")
            ps_cmd = (
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
                "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.SelectedPath = '{safe_initial}';"
                f"$d.Description = '{safe_title}';"
                "$d.ShowNewFolderButton = $true;"
                "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
                "{ [Console]::Out.WriteLine($d.SelectedPath) }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Sta", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace",
            )
            picked = (result.stdout or "").strip()
            if picked:
                return picked
        except Exception as exc:
            logger.warning(f"PowerShell folder picker failed: {exc}")
    else:
        try:
            import tkinter as _tk
            from tkinter import filedialog as _fd
            _root = _tk.Tk()
            _root.withdraw()
            _root.attributes("-topmost", True)
            picked = _fd.askdirectory(
                title=title,
                initialdir=str(initial),
            )
            _root.destroy()
            if picked:
                return picked
        except Exception as exc:
            logger.warning(f"tkinter folder picker failed: {exc}")

    return fallback


def pick_obs_watch_folder_dialog(current_value: str) -> str:
    """Open the native picker for the OBS recording output directory."""
    return pick_folder_dialog(
        current_value,
        title="録画出力フォルダを選択",
    )


def open_output_folder(current_base: str) -> None:
    """Create (if missing) and open the output base dir in Explorer / Finder.

    Uses gr.Info / gr.Warning for feedback instead of a persistent status
    textbox — fire-and-forget. Never raises.
    """
    target = resolve_output_base(current_base)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        gr.Warning(f"フォルダを作成できません: {exc}")
        return
    try:
        if os.name == "nt":
            os.startfile(str(target))
        else:
            import subprocess as _sp
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            _sp.Popen([opener, str(target)])
        gr.Info(f"開きました: {target}")
    except Exception as exc:
        gr.Warning(f"フォルダは作成しましたが開けません ({target}): {exc}")


from chapters import generate_chapter_text, write_chapter_file
from downloader import download_video, get_url_source
from transcriber import transcribe, segments_to_text
from highlighter import detect_highlights
from audio_energy import fuse_audio_energy
import clipper
from clipper import extract_clips, generate_thumbnails as generate_thumbnail_candidates, get_video_info
from subtitles import (
    generate_all_karaoke_ass,
    generate_all_short_title_srts,
    generate_all_srts,
)
from premiere_xml import generate_combined_xml, generate_individual_xmls
from premiere_bridge import (
    get_bridge_status_text,
    open_plugin_installer,
    request_premiere_edit,
)
from drive_upload import upload_output_directory, is_configured as drive_is_configured
from modes import GenerationModes
import youtube_api


_MIN_REVIEW_CLIP_DURATION_SEC = 0.1


def install_premiere_plugin_ui() -> str:
    """Open the CCX installer and keep errors inside the Gradio event."""
    try:
        return open_plugin_installer()
    except Exception as exc:
        logger.exception("Premiere plugin installer failed")
        message = f"Premiere連携プラグインを開けません: {exc}"
        gr.Warning(message)
        return message


def request_premiere_edit_ui(
    premiere_job: dict | None,
    include_shorts: bool,
    executable_path: str,
) -> str:
    """Queue one rendered output for Premiere and return reader-facing status."""
    try:
        return request_premiere_edit(
            premiere_job,
            include_shorts=bool(include_shorts),
            executable_path=executable_path or "",
        )
    except Exception as exc:
        logger.exception("Premiere edit request failed")
        message = f"Premiere連携エラー: {exc}"
        gr.Warning(message)
        return message


def clear_premiere_job_state() -> None:
    """Invalidate the previous render before a new Detect or Render starts."""
    return None


def _format_highlight_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm for reviewed highlight metadata."""
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours = total_ms // 3_600_000
    total_ms %= 3_600_000
    minutes = total_ms // 60_000
    total_ms %= 60_000
    secs = total_ms // 1000
    ms = total_ms % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _coerce_float(value, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _session_video_duration(session: dict | None) -> float:
    if not isinstance(session, dict):
        return 0.0
    video_info = session.get("video_info") or {}
    return max(0.0, _coerce_float(video_info.get("duration"), 0.0))


def _clamp_review_range(start_sec, end_sec, video_duration: float) -> tuple[float, float]:
    """Clamp edited review bounds and correct inverted ranges."""
    start = max(0.0, _coerce_float(start_sec, 0.0))
    end = _coerce_float(end_sec, start + _MIN_REVIEW_CLIP_DURATION_SEC)
    duration = max(0.0, float(video_duration))

    if duration > 0:
        start = min(start, duration)
        end = min(max(0.0, end), duration)
        if end <= start:
            if start + _MIN_REVIEW_CLIP_DURATION_SEC <= duration:
                end = start + _MIN_REVIEW_CLIP_DURATION_SEC
            else:
                end = duration
                start = max(0.0, end - _MIN_REVIEW_CLIP_DURATION_SEC)
        if end <= start:
            start = 0.0
            end = duration
    else:
        end = max(0.0, end)
        if end <= start:
            end = start + _MIN_REVIEW_CLIP_DURATION_SEC

    return float(start), float(end)


def _normalize_highlight_for_review(highlight: dict, video_duration: float) -> dict:
    start, end = _clamp_review_range(
        highlight.get("start_sec", highlight.get("start", 0.0)),
        highlight.get("end_sec", highlight.get("end", 0.0)),
        video_duration,
    )
    highlight["start_sec"] = start
    highlight["end_sec"] = end
    highlight["duration"] = float(end - start)
    highlight["start"] = _format_highlight_timestamp(start)
    highlight["end"] = _format_highlight_timestamp(end)
    highlight["title"] = str(highlight.get("title") or "")
    return highlight


def _normalize_session_highlights(session: dict, *, sort: bool = False) -> dict:
    video_duration = _session_video_duration(session)
    highlights = session.get("highlights") or []
    for highlight in highlights:
        if isinstance(highlight, dict):
            _normalize_highlight_for_review(highlight, video_duration)
    if sort:
        highlights.sort(key=lambda item: float(item.get("start_sec", 0.0)))
    session["highlights"] = highlights
    return session


def _format_highlights_summary(highlights: list[dict]) -> str:
    if not highlights:
        return "No highlights detected. / ハイライトが見つかりませんでした。"

    lines: list[str] = []
    for i, h in enumerate(highlights, 1):
        title = h.get("title") or f"Clip {i}"
        start = h.get("start") or _format_highlight_timestamp(h.get("start_sec", 0.0))
        end = h.get("end") or _format_highlight_timestamp(h.get("end_sec", 0.0))
        duration = _coerce_float(h.get("duration"), 0.0)
        reason = h.get("reason") or ""
        lines.append(f"**{i}. {title}**")
        lines.append(f"   {start} → {end} ({duration:.1f}s)")
        if reason:
            lines.append(f"   {reason}")
        lines.append("")
    return "\n".join(lines)


def highlights_for_review(session: dict | None) -> list[dict]:
    """Return highlight rows for @gr.render, including video duration metadata."""
    if not isinstance(session, dict):
        return []
    _normalize_session_highlights(session)
    video_duration = _session_video_duration(session)
    rows: list[dict] = []
    for highlight in session.get("highlights") or []:
        item = dict(highlight)
        item["_video_duration"] = video_duration
        rows.append(item)
    return rows


def apply_edits_to_session(
    session: dict | None,
    idx: int,
    start_sec,
    end_sec,
    title,
) -> dict:
    """Apply one reviewed clip edit to session State.

    範囲外・逆転した値はここで補正し、後段は従来通り start_sec/end_sec/title
    だけを読む形に保ちます。
    """
    if not isinstance(session, dict):
        return {}
    highlights = session.get("highlights") or []
    if idx < 0 or idx >= len(highlights):
        return session

    highlight = highlights[idx]
    if not isinstance(highlight, dict):
        return session

    video_duration = _session_video_duration(session)
    start, end = _clamp_review_range(start_sec, end_sec, video_duration)
    highlight["start_sec"] = start
    highlight["end_sec"] = end
    highlight["duration"] = float(end - start)
    highlight["start"] = _format_highlight_timestamp(start)
    highlight["end"] = _format_highlight_timestamp(end)
    highlight["title"] = str(title or "")
    session["highlights"] = highlights
    return session


def render_preview_clip(session: dict | None, idx: int, start_sec, end_sec) -> str:
    """Render one reviewed clip preview and return its mp4 path."""
    if not isinstance(session, dict):
        return ""
    highlights = session.get("highlights") or []
    if idx < 0 or idx >= len(highlights):
        return ""

    title = highlights[idx].get("title", "") if isinstance(highlights[idx], dict) else ""
    session = apply_edits_to_session(session, idx, start_sec, end_sec, title)
    highlight = session["highlights"][idx]
    output_dir = Path(session["output_dir"])
    preview_dir = output_dir / "_preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"clip_{idx}.mp4"
    clipper.extract_clip(
        Path(session["video_path"]),
        preview_path,
        highlight["start_sec"],
        highlight["end_sec"],
    )
    return str(preview_path)


def _apply_review_edit_event(session: dict | None, idx: int, start_sec, end_sec, title):
    return apply_edits_to_session(session, idx, start_sec, end_sec, title)


def _apply_review_edit_event_session_only(
    session: dict | None,
    idx: int,
    start_sec,
    end_sec,
    title,
) -> dict:
    return apply_edits_to_session(session, idx, start_sec, end_sec, title)


def detect_phase(
    input_url: str,
    input_file,
    enable_clips: bool,
    clip_prompt: str,
    enable_chapters: bool,
    chapter_prompt: str,
    num_clips: int,
    ai_provider: str,
    ai_model: str,
    api_key: str,
    min_duration: int,
    max_duration: int,
    whisper_model: str,
    language: str,
    audio_fusion: bool,
    audio_alpha: float,
    output_base_dir: str,
    generate_shorts: bool = False,
    progress=gr.Progress(),
):
    """Detection phase: validate, resolve input, transcribe, and find highlights."""
    logs = []

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)

    try:
        input_value = str(input_url or "").strip()
        source_kind = "local" if input_file is not None else (
            get_url_source(input_value) or ("url" if input_value else "local")
        )
        if source_kind == "twitch":
            # Twitch processing is video-output-only by design; there is no Twitch
            # description target for the generated chapter text.
            enable_chapters = False
            chapter_prompt = ""
        modes = GenerationModes(
            enable_clips=bool(enable_clips),
            enable_shorts=bool(generate_shorts),
            enable_chapters=bool(enable_chapters),
            clip_prompt=clip_prompt or "",
            chapter_prompt=chapter_prompt or "",
        )
        try:
            modes.validate()
        except ValueError as mode_err:
            return {}, f"Error: {mode_err}", gr.update(visible=False)
        log(
            f"Modes: clips={modes.enable_clips}, shorts={modes.enable_shorts}, "
            f"chapters={modes.enable_chapters}"
        )
        if source_kind == "twitch":
            log("Twitch入力: タイムスタンプ生成とYouTube概要欄への追記をスキップ")

        base_dir = resolve_output_base(output_base_dir)
        output_dir = _create_output_dir(base_dir)
        log(f"Output base: {base_dir}")

        youtube_video_id: str | None = None
        if source_kind == "youtube":
            youtube_video_id = youtube_api.extract_video_id(input_value)
            if youtube_video_id:
                log(f"YouTube video id: {youtube_video_id}")

        if input_file is not None:
            original_path = Path(getattr(input_file, "name", input_file))
            log(f"Local file: {original_path.name}")
            try:
                str(original_path).encode("ascii")
                video_path = original_path
            except UnicodeEncodeError:
                safe_dir = output_dir / "_safe"
                safe_dir.mkdir(parents=True, exist_ok=True)
                safe_name = f"input{original_path.suffix}"
                video_path = safe_dir / safe_name
                shutil.copy2(original_path, video_path)
                log(f"Copied to safe path: {video_path}")
        elif input_value:
            progress(0.05, desc="Downloading video...")
            video_path = download_video(input_value, output_dir / "source")
            log(f"Downloaded: {video_path.name}")
        else:
            return (
                {"logs": logs},
                "Error: URLを入力するかファイルをアップロードしてください",
                gr.update(visible=False),
            )

        progress(0.1, desc="[Step 1/3] Analyzing video...")
        log(f"[Step 1/3] Analyzing video: {video_path}")
        video_info = get_video_info(video_path)
        log(
            f"  Resolution: {video_info['width']}x{video_info['height']}, "
            f"FPS: {video_info['fps']:.2f}, Duration: {video_info['duration']:.0f}s"
        )

        progress(0.15, desc="[Step 2/3] Transcribing audio...")
        log("[Step 2/3] Transcribing... (this may take a while)")
        segments = transcribe(video_path, whisper_model, language)
        transcript_text = segments_to_text(segments)

        transcript_path = output_dir / "transcript.txt"
        transcript_path.write_text(transcript_text, encoding="utf-8")
        log(f"  Transcription complete: {len(segments)} segments")

        progress(0.5, desc="[Step 3/3] Detecting highlights...")
        provider_name = {"claude": "Claude", "openai": "ChatGPT", "gemini": "Gemini"}.get(ai_provider, ai_provider)
        log(f"[Step 3/3] Analyzing with {provider_name}...")
        highlights = detect_highlights(
            transcript_text,
            num_clips=num_clips,
            min_duration=min_duration,
            max_duration=max_duration,
            custom_prompt=modes.active_prompt,
            ai_provider=ai_provider,
            api_key=api_key,
            ai_model=ai_model,
        )

        if audio_fusion:
            alpha = float(audio_alpha if audio_alpha is not None else 0.35)
            log(f"  Applying audio excitement fusion (alpha={alpha:.2f})")
            highlights = fuse_audio_energy(
                video_path,
                highlights,
                alpha=alpha,
                min_duration=min_duration,
                max_duration=max_duration,
            )

        session = {
            "output_dir": output_dir,
            "video_path": video_path,
            "video_info": video_info,
            "segments": segments,
            "highlights": highlights,
            "source_kind": source_kind,
            "youtube_video_id": youtube_video_id,
            "enable_clips": modes.enable_clips,
            "enable_shorts": modes.enable_shorts,
            "enable_chapters": modes.enable_chapters,
            "modes": {
                "enable_clips": modes.enable_clips,
                "enable_shorts": modes.enable_shorts,
                "enable_chapters": modes.enable_chapters,
                "clip_prompt": modes.clip_prompt,
                "chapter_prompt": modes.chapter_prompt,
                "active_prompt": modes.active_prompt,
            },
            "logs": logs,
        }
        _normalize_session_highlights(session)
        log(f"  Found {len(session['highlights'])} highlights")
        log(f"\nDetection complete. Review clips, then Render. Output: {output_dir}")

        status_md = (
            "### 検出完了 / Detection Complete\n\n"
            "開始・終了・タイトルを確認してから Render を押してください。"
            " / Review start, end, and title before rendering.\n\n"
            f"{_format_highlights_summary(session['highlights'])}"
        )
        return session, status_md, gr.update(visible=True)

    except subprocess.CalledProcessError as e:
        err_detail = f"Command failed: {e.cmd}\nReturn code: {e.returncode}"
        if e.stdout:
            err_detail += f"\nstdout: {e.stdout[:500]}"
        if e.stderr:
            err_detail += f"\nstderr: {e.stderr[:500]}"
        logger.error(err_detail)
        log(f"\nError (subprocess): {err_detail}")
        return {"logs": logs}, "\n".join(logs), gr.update(visible=False)
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Error: {e}\n{tb}")
        log(f"\nError: {e}")
        log(tb)
        return {"logs": logs}, "\n".join(logs), gr.update(visible=False)


def render_phase(
    session: dict,
    output_mode: str,
    generate_shorts: bool,
    shorts_mode: str,
    shorts_crop: str,
    shorts_title: bool,
    generate_zip: bool,
    upload_to_drive: bool,
    auto_append_youtube: bool,
    font_name: str,
    font_size: int,
    font_color: str,
    generate_thumbnails: bool,
    karaoke: bool,
    shorts_blur_strength=20,
    shorts_title_position: str = "top",
    progress=gr.Progress(),
):
    """Render phase: replay downstream output generation with edited highlights."""
    if not isinstance(session, dict) or not session.get("video_path"):
        return ProcessResult(
            log="Error: 先に Detect を実行してください / Run Detect before Render.",
        ).as_gradio_outputs()

    session.pop("_premiere_output", None)
    logs = list(session.get("logs") or [])
    premiere_output = None

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)
        session["logs"] = logs

    try:
        _normalize_session_highlights(session, sort=True)
        output_dir = Path(session["output_dir"])
        video_path = Path(session["video_path"])
        video_info = session["video_info"]
        segments = session["segments"]
        highlights = session["highlights"]
        youtube_video_id = session.get("youtube_video_id")
        source_kind = str(session.get("source_kind") or "local")
        mode_data = session.get("modes") or {}
        chapters_enabled = bool(
            mode_data.get("enable_chapters", session.get("enable_chapters", True))
        )
        if source_kind == "twitch":
            chapters_enabled = False
        modes = GenerationModes(
            enable_clips=bool(mode_data.get("enable_clips", session.get("enable_clips", True))),
            enable_shorts=bool(generate_shorts),
            enable_chapters=chapters_enabled,
            clip_prompt=mode_data.get("clip_prompt", ""),
            chapter_prompt=mode_data.get("chapter_prompt", ""),
        )
        try:
            modes.validate()
        except ValueError as mode_err:
            return ProcessResult(log=f"Error: {mode_err}").as_gradio_outputs()

        if source_kind == "twitch" and auto_append_youtube:
            log("Twitch入力ではタイムスタンプとYouTube概要欄への追記をスキップ")
            auto_append_youtube = False

        obs_render_outcome = {
            "clip_paths": [],
            "shorts_paths": [],
            "chapters_path": "",
            "chapters_text": "",
            "youtube_append_requested": bool(auto_append_youtube),
            "youtube_append_succeeded": None if not auto_append_youtube else False,
        }
        session["_obs_render_outcome"] = obs_render_outcome

        log("[Render] Applying reviewed highlight edits")

        if auto_append_youtube:
            yt_status = youtube_api.check_auth_status()
            if not yt_status["configured"]:
                return ProcessResult(
                    log="\n".join(logs + [
                        "Error: 概要欄に自動追加が有効ですが credentials.json が未設定です。"
                        "Settings タブの『YouTube API 認証』で配置手順を確認してください。"
                    ]),
                ).as_gradio_outputs()
            if not yt_status["authenticated"]:
                return ProcessResult(
                    log="\n".join(logs + [
                        "Error: YouTube 認証が切れています。Settings タブの"
                        "『YouTube API 認証』で『認証する』を押して再認証してください。"
                    ]),
                ).as_gradio_outputs()
            log(f"YouTube auth pre-check: {youtube_api.auth_status_summary()}")

        font_config = FontConfig(
            font_name=font_name,
            font_size=font_size,
            font_color=font_color,
        )
        highlights_summary = _format_highlights_summary(highlights)

        clip_paths: list[Path] = []
        srt_paths: list[Path] = []
        shorts_paths: list[Path] = []
        shorts_srt_paths: list[Path] = []
        shorts_title_srt_paths: list[Path] = []
        shorts_ass_paths: list[Path] = []
        thumbnail_paths: list[Path] = []
        xml_paths: list[Path] = []

        clips_dir = output_dir / "clips"
        shorts_dir = output_dir / "shorts"

        if modes.enable_clips:
            progress(0.6, desc="[Step 4/6] Extracting clips...")
            log("[Step 4/6] Extracting clips...")
            clip_paths = extract_clips(video_path, highlights, clips_dir)
            obs_render_outcome["clip_paths"] = [str(path) for path in clip_paths]
            log(f"  Extracted {len(clip_paths)} clips")

            progress(0.7, desc="[Step 5/6] Generating subtitles...")
            log("[Step 5/6] Generating subtitles...")
            srt_paths = generate_all_srts(segments, highlights, clips_dir)
            log(f"  Generated {len(srt_paths)} SRT files")

        if modes.enable_shorts:
            progress(0.75, desc="Generating shorts (9:16) with burned-in subtitles...")
            shorts_dir.mkdir(parents=True, exist_ok=True)
            shorts_srt_paths = generate_all_srts(
                segments,
                highlights,
                shorts_dir,
                shorts=True,
            )
            shorts_title_srt_paths = generate_all_short_title_srts(
                highlights,
                shorts_dir,
            )
            if karaoke:
                shorts_ass_paths = generate_all_karaoke_ass(
                    segments, highlights, shorts_dir, font_config,
                )
            shorts_paths = extract_clips(
                video_path, highlights, shorts_dir,
                shorts=True,
                srt_paths=shorts_srt_paths,
                karaoke=bool(karaoke),
                ass_paths=shorts_ass_paths,
                font_config=font_config,
                crop_x=shorts_crop,
                shorts_mode=shorts_mode,
                shorts_blur_strength=shorts_blur_strength,
                shorts_title=shorts_title,
                shorts_title_position=shorts_title_position,
            )
            obs_render_outcome["shorts_paths"] = [str(path) for path in shorts_paths]
            obs_render_outcome["shorts_srt_paths"] = [
                str(path) for path in shorts_srt_paths
            ]
            obs_render_outcome["shorts_title_srt_paths"] = [
                str(path) for path in shorts_title_srt_paths
            ]
            subtitle_kind = "ASS karaoke" if karaoke else "SRT"
            log(f"  Generated {len(shorts_paths)} shorts with {subtitle_kind} subtitles ({font_config.font_name} @ {font_config.font_size}pt)")
            log(
                "  Generated "
                f"{len(shorts_srt_paths) + len(shorts_title_srt_paths)} "
                "editable Short SRT files (archive + title)"
            )

        if generate_thumbnails and (modes.enable_clips or modes.enable_shorts):
            progress(0.8, desc="Generating thumbnail candidates...")
            if modes.enable_shorts:
                thumbnail_paths = generate_thumbnail_candidates(
                    video_path, highlights, shorts_dir,
                    vertical=True,
                    crop_x=shorts_crop,
                    shorts_mode=shorts_mode,
                    shorts_blur_strength=shorts_blur_strength,
                    shorts_title_position=shorts_title_position,
                    font_config=font_config,
                )
                log(f"  Generated {len(thumbnail_paths)} vertical thumbnail candidates")
            else:
                thumbnail_paths = generate_thumbnail_candidates(
                    video_path, highlights, clips_dir,
                    font_config=font_config,
                )
                log(f"  Generated {len(thumbnail_paths)} thumbnail candidates")

        if clip_paths or shorts_paths:
            progress(0.85, desc="[Step 6/6] Exporting XML...")
            log("[Step 6/6] Exporting Premiere Pro XML...")
            if output_mode == "combined":
                xml_paths.append(
                    generate_combined_xml(
                        clip_paths,
                        highlights,
                        video_info,
                        output_dir / "project.xml",
                        project_name=video_path.stem,
                        source_video_path=video_path,
                        shorts_paths=shorts_paths,
                    )
                )
                log("  Premiere Pro XML (combined mode) exported")
            else:
                xml_paths.extend(
                    generate_individual_xmls(
                        clip_paths,
                        highlights,
                        video_info,
                        clips_dir if clip_paths else shorts_dir,
                        source_video_path=video_path,
                        shorts_paths=shorts_paths,
                    )
                )
                log("  Premiere Pro XML (individual mode) exported")

            premiere_output = {
                "output_dir": str(output_dir.resolve()),
                "project_name": video_path.stem,
                "source_path": str(video_path.resolve()),
                "clip_paths": [str(path.resolve()) for path in clip_paths],
                "shorts_paths": [str(path.resolve()) for path in shorts_paths],
                "srt_paths": [str(path.resolve()) for path in srt_paths],
                "shorts_srt_paths": [
                    str(path.resolve()) for path in shorts_srt_paths
                ],
                "shorts_title_srt_paths": [
                    str(path.resolve()) for path in shorts_title_srt_paths
                ],
                "xml_paths": [str(path.resolve()) for path in xml_paths],
                "highlights": [
                    {
                        "title": str(highlight.get("title") or ""),
                        "start_sec": float(highlight["start_sec"]),
                        "end_sec": float(highlight["end_sec"]),
                    }
                    for highlight in highlights
                ],
            }
            session["_premiere_output"] = premiere_output
        elif not modes.enable_clips and not modes.enable_shorts:
            log("[Skip 4-6] Video generation disabled — chapters-only run")

        drive_link = ""
        if upload_to_drive:
            progress(0.9, desc="Uploading to Google Drive...")
            if drive_is_configured():
                log("Uploading to Google Drive...")
                result = upload_output_directory(output_dir)
                drive_link = result.get("folder_link", "")
                log(f"  Google Drive: {drive_link}")
            else:
                log("Google Drive: credentials.json が未設定のためスキップ")

        zip_path = None
        if generate_zip:
            progress(0.95, desc="Creating download archive...")
            zip_path = shutil.make_archive(str(output_dir), "zip", str(output_dir))
            log(f"  ZIP created: {zip_path}")

        chapters_text = ""
        if modes.enable_chapters:
            try:
                video_duration = float(video_info.get("duration", 0))
                chapters_text = generate_chapter_text(highlights, video_duration=video_duration)
                chapters_path = output_dir / "chapters.txt"
                write_chapter_file(highlights, chapters_path, video_duration=video_duration)
                obs_render_outcome["chapters_path"] = str(chapters_path)
                obs_render_outcome["chapters_text"] = chapters_text
                log(f"Chapters saved: {chapters_path}")
            except Exception as ch_err:
                log(f"Chapter generation failed: {ch_err}")
        else:
            log("[Skip chapters] タイムスタンプ (概要欄) 生成を無効化")

        if auto_append_youtube and modes.enable_chapters and chapters_text:
            if not youtube_video_id:
                log("[Skip auto-append] URL 入力ではないため YouTube 概要欄への自動追記はスキップ")
            elif not youtube_api.is_configured():
                log("[Skip auto-append] credentials.json 未設定のため YouTube 概要欄への自動追記はスキップ")
            else:
                progress(0.97, desc="YouTube 概要欄に自動追加中...")
                try:
                    yt_service = youtube_api.get_youtube_service()
                    youtube_api.update_video_description(
                        yt_service, youtube_video_id, chapters_text, position="prepend",
                    )
                    obs_render_outcome["youtube_append_succeeded"] = True
                    log(f"  YouTube 概要欄に自動追加: video_id={youtube_video_id}")
                except Exception as yt_err:
                    tb = traceback.format_exc()
                    logger.error(f"YouTube 概要欄更新失敗: {yt_err}\n{tb}")
                    log(f"  YouTube 概要欄更新失敗: {yt_err} (他の出力は維持)")

        log(f"\nDone! Output: {output_dir}")
        return ProcessResult(
            log="\n".join(logs),
            highlights=highlights_summary,
            download_path=zip_path,
            drive_link=drive_link,
            chapters_text=chapters_text,
            premiere_job=premiere_output,
        ).as_gradio_outputs()

    except subprocess.CalledProcessError as e:
        err_detail = f"Command failed: {e.cmd}\nReturn code: {e.returncode}"
        if e.stdout:
            err_detail += f"\nstdout: {e.stdout[:500]}"
        if e.stderr:
            err_detail += f"\nstderr: {e.stderr[:500]}"
        logger.error(err_detail)
        log(f"\nError (subprocess): {err_detail}")
        return ProcessResult(log="\n".join(logs)).as_gradio_outputs()
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Error: {e}\n{tb}")
        log(f"\nError: {e}")
        log(tb)
        return ProcessResult(log="\n".join(logs)).as_gradio_outputs()


def maybe_render_phase(
    auto_run: bool,
    session: dict,
    output_mode: str,
    generate_shorts: bool,
    shorts_mode: str,
    shorts_crop: str,
    shorts_title: bool,
    generate_zip: bool,
    upload_to_drive: bool,
    auto_append_youtube: bool,
    font_name: str,
    font_size: int,
    font_color: str,
    generate_thumbnails: bool,
    karaoke: bool,
    shorts_blur_strength=20,
    shorts_title_position: str = "top",
    progress=gr.Progress(),
):
    """Chain STEP 2 right after STEP 1 when the 'run both' checkbox is on.

    Returns no-op updates (leaving the STEP 2 output fields untouched) when the
    checkbox is off or when detection produced nothing renderable, so manual
    STEP 2 still behaves exactly as before.
    """
    if not auto_run or not isinstance(session, dict) or not session.get("video_path"):
        # A new Detect invalidates the previous render's Premiere payload even
        # when automatic STEP 2 is disabled. Keep the five visible outputs but
        # explicitly clear the hidden Premiere job state.
        return tuple(gr.update() for _ in range(5)) + (None,)
    return render_phase(
        session,
        output_mode,
        generate_shorts,
        shorts_mode,
        shorts_crop,
        shorts_title,
        generate_zip,
        upload_to_drive,
        auto_append_youtube,
        font_name,
        font_size,
        font_color,
        generate_thumbnails,
        karaoke,
        shorts_blur_strength=shorts_blur_strength,
        shorts_title_position=shorts_title_position,
        progress=progress,
    )


# ---------------------------------------------------------------------------
# OBS integration — bridge from local recording / stream lifecycle events to
# the existing detect→render pipeline. The watchers live in obs_integration.py;
# here we only manage the lifecycle, run the pipeline on a background thread,
# and surface status to the UI via a polled shared buffer (never by touching
# Gradio components from a worker thread).
# ---------------------------------------------------------------------------

class _DummyProgress:
    """No-op callable standing in for ``gr.Progress()`` outside the UI thread.

    detect_phase / render_phase call ``progress(frac, desc=...)``; this just
    swallows those calls so the auto pipeline can run headless.
    """

    def __call__(self, *args, **kwargs):
        return None


# Module-level watcher singleton + shared status buffer. Worker threads only
# ever append to _obs_status_lines (under _obs_status_lock); the UI polls via
# _obs_status_poll() on a Timer / button — no component writes from threads.
_obs_watcher = None
_obs_watcher_lock = threading.Lock()
_obs_start_lock = threading.Lock()
_obs_auto_connect_cancel = threading.Event()
_obs_auto_connect_thread: threading.Thread | None = None
_obs_auto_connect_lock = threading.Lock()
# Generation token: bumped on every start/stop so a callback created for a
# superseded watcher refuses to run the pipeline with stale settings.
_obs_generation = 0
# Auto-pipeline worker threads, tracked so stop can join finished ones and the
# lifecycle is observable (the watcher's own _spawn_worker does not see these).
_obs_pipeline_threads: list[threading.Thread] = []
_obs_status_lines: list[str] = []
_obs_status_lock = threading.Lock()
_obs_confirmation_lock = threading.Lock()
_obs_pending_confirmation: dict | None = None
_OBS_STATUS_MAX = 80
_OBS_ARCHIVE_DISCOVERY_TIMEOUT = 15 * 60
_OBS_ARCHIVE_READY_TIMEOUT = 6 * 60 * 60
_OBS_RECORDING_EVENT_TIMEOUT = 60
_OBS_ARCHIVE_POLL_INITIAL = 15
_OBS_ARCHIVE_POLL_MAX = 5 * 60
_OBS_ARCHIVE_END_LOOKBACK = timedelta(minutes=2)


def _obs_wait_for_poll(seconds: float, is_current: Callable[[], bool]) -> None:
    """Wait in short chunks so stopping OBS integration cancels promptly."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    while True:
        if not is_current():
            raise RuntimeError("OBS連携が停止されたためアーカイブ待機を中止しました")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _obs_wait_for_event(
    event: threading.Event,
    timeout: float,
    is_current: Callable[[], bool],
) -> bool:
    """Wait for an event while keeping watcher cancellation responsive."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if not is_current():
            raise RuntimeError("OBS連携が停止されたためアーカイブ待機を中止しました")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return event.is_set()
        if event.wait(timeout=min(0.25, remaining)):
            return True


def _is_retriable_youtube_api_error(exc: Exception) -> bool:
    """Return True for temporary transport, rate-limit, and server failures."""
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            return False
        if status_code in {429, 500, 502, 503, 504}:
            return True
        if status_code == 403:
            reasons = {
                str(detail.get("reason", ""))
                for detail in (getattr(exc, "error_details", None) or [])
                if isinstance(detail, dict)
            }
            content = getattr(exc, "content", b"")
            if content:
                try:
                    payload = json.loads(
                        content.decode("utf-8")
                        if isinstance(content, bytes)
                        else str(content)
                    )
                    reasons.update(
                        str(detail.get("reason", ""))
                        for detail in payload.get("error", {}).get("errors", [])
                        if isinstance(detail, dict)
                    )
                except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
                    pass
            return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return exc.__class__.__name__ in {"TransportError", "ServerNotFoundError"}


def _obs_retry_after_api_error(
    stage: str,
    exc: Exception,
    deadline: float,
    poll_delay: float,
    is_current: Callable[[], bool],
    timeout_message: str,
) -> float:
    """Back off after a temporary API failure or re-raise a permanent one."""
    if not _is_retriable_youtube_api_error(exc):
        raise exc
    if time.monotonic() >= deadline:
        raise TimeoutError(timeout_message) from exc
    _obs_append_status(
        f"YouTube API一時エラー ({stage}): {exc} — {poll_delay}秒後に再試行"
    )
    _obs_wait_for_poll(poll_delay, is_current)
    return min(poll_delay * 2, _OBS_ARCHIVE_POLL_MAX)


def _obs_append_status(msg: str) -> None:
    """Append a status line (thread-safe, capped to the last N lines)."""
    if not msg:
        return
    with _obs_status_lock:
        _obs_status_lines.append(str(msg))
        del _obs_status_lines[:-_OBS_STATUS_MAX]


def _obs_status_text() -> str:
    with _obs_status_lock:
        return "\n".join(_obs_status_lines[-_OBS_STATUS_MAX:])


def _obs_status_poll() -> str:
    """Gradio Timer/btn target: return the current shared status text."""
    return _obs_status_text()


def _obs_effective_prompt(settings: dict) -> str:
    """Return the prompt that will actually drive automatic highlight detection."""
    if bool(settings.get("enable_clips", True)):
        return str(settings.get("clip_prompt", "") or "").strip()
    if bool(settings.get("enable_chapters", True)):
        return str(settings.get("chapter_prompt", "") or "").strip()
    return ""


def _obs_apply_effective_prompt(settings: dict, prompt: str) -> None:
    """Apply a one-run prompt to the generation mode that drives detection."""
    prompt_text = str(prompt or "").strip()
    if bool(settings.get("enable_clips", True)):
        settings["clip_prompt"] = prompt_text
    elif bool(settings.get("enable_chapters", True)):
        settings["chapter_prompt"] = prompt_text


def _obs_confirmation_message(request: dict) -> str:
    message = str(request.get("message", "") or "")
    validation_error = str(request.get("validation_error", "") or "")
    if validation_error:
        message += f"\n\n**{validation_error}**"
    return message


def _obs_confirmation_poll(seen_request_token: str = "") -> tuple:
    """Return the pending confirmation panel state for the Gradio timer."""
    with _obs_confirmation_lock:
        request = _obs_pending_confirmation
        if request is None:
            return gr.update(visible=False), "", gr.update(value=""), ""
        message = _obs_confirmation_message(request)
        request_token = str(request.get("token", "") or "")
        if seen_request_token == request_token:
            prompt_update = gr.update()
        else:
            prompt_update = gr.update(value=request.get("initial_prompt", ""))
    return gr.update(visible=True), message, prompt_update, request_token


def _obs_resolve_confirmation_action(action: str, prompt: str = "") -> bool:
    """Resolve the pending confirmation with one of the three UI actions."""
    valid_actions = {"start_as_is", "start_with_prompt", "skip"}
    if action not in valid_actions:
        return False

    prompt_text = str(prompt or "").strip()
    with _obs_confirmation_lock:
        request = _obs_pending_confirmation
        if request is None or request.get("decision") is not None:
            return False
        if action == "start_with_prompt" and not prompt_text:
            request["validation_error"] = "プロンプトを入力してください。"
            return False
        request["validation_error"] = ""
        request["decision"] = action
        request["prompt"] = prompt_text if action == "start_with_prompt" else None
        request["event"].set()
    return True


def _obs_resolve_confirmation(approved: bool) -> bool:
    """Backward-compatible boolean resolver used by non-UI callers and tests."""
    action = "start_as_is" if approved else "skip"
    return _obs_resolve_confirmation_action(action)


def _obs_confirmation_button_result(action: str, prompt: str = "") -> tuple:
    resolved = _obs_resolve_confirmation_action(action, prompt)
    if resolved:
        action_labels = {
            "start_as_is": "そのまま生成を開始します",
            "start_with_prompt": "入力したプロンプトで生成を開始します",
            "skip": "今回は生成しません",
        }
        _obs_append_status(
            "自動生成の選択を受け付けました: " + action_labels[action]
        )
        return (
            gr.update(visible=False),
            "",
            gr.update(value=""),
            _obs_status_text(),
        )
    with _obs_confirmation_lock:
        request = _obs_pending_confirmation
        if request is None:
            panel_update = gr.update(visible=False)
            message = ""
        else:
            panel_update = gr.update(visible=True)
            message = _obs_confirmation_message(request)
    return panel_update, message, gr.update(), _obs_status_text()


def _obs_confirm_generation() -> tuple:
    return _obs_confirmation_button_result("start_as_is")


def _obs_confirm_generation_with_prompt(prompt: str) -> tuple:
    return _obs_confirmation_button_result("start_with_prompt", prompt)


def _obs_skip_generation() -> tuple:
    return _obs_confirmation_button_result("skip")


def _obs_cancel_pending_confirmation() -> None:
    """Wake a worker waiting for confirmation when OBS integration stops."""
    with _obs_confirmation_lock:
        request = _obs_pending_confirmation
        if request is not None and request.get("decision") is None:
            request["decision"] = "skip"
            request["event"].set()


def _obs_confirm_before_auto_process(
    settings: dict,
    source_label: str,
    is_current: Callable[[], bool],
) -> bool:
    """Wait indefinitely for the post-stream generation choice when enabled.

    The watcher runs outside a Gradio request, so confirmation is represented by
    a small shared request that the UI timer renders and the three buttons resolve.
    A watcher stop is fail-closed and skips generation. User confirmation has
    no deadline; the short event wait only keeps cancellation responsive.
    """
    if not bool(settings.get("confirm_before_auto_process", False)):
        return True

    request = {
        "event": threading.Event(),
        "token": uuid.uuid4().hex,
        "decision": None,
        "prompt": None,
        "initial_prompt": _obs_effective_prompt(settings),
        "validation_error": "",
        "message": (
            "### 配信終了後の生成\n\n"
            "生成方法を選んでください。プロンプトを変更する場合は入力欄を使います。"
            "この確認は選択するまでタイムアウトしません。\n\n"
            f"対象: `{source_label}`"
        ),
    }
    global _obs_pending_confirmation
    with _obs_confirmation_lock:
        if _obs_pending_confirmation is not None:
            _obs_append_status("別の自動生成確認が処理中のため、この処理をスキップしました")
            return False
        _obs_pending_confirmation = request
    _obs_append_status("配信終了後の生成方法の選択を待っています（タイムアウトなし）")

    while is_current():
        if request["event"].wait(timeout=0.25):
            break

    with _obs_confirmation_lock:
        if _obs_pending_confirmation is request:
            _obs_pending_confirmation = None
        decision = request.get("decision")

    if not is_current():
        return False
    if decision == "start_with_prompt":
        _obs_apply_effective_prompt(settings, request.get("prompt", ""))
        return True
    return decision == "start_as_is"


def _register_obs_worker(t: threading.Thread) -> None:
    with _obs_watcher_lock:
        # prune dead threads to avoid unbounded growth
        _obs_pipeline_threads[:] = [w for w in _obs_pipeline_threads if w.is_alive()]
        _obs_pipeline_threads.append(t)


def _unregister_obs_worker(t: threading.Thread) -> None:
    with _obs_watcher_lock:
        try:
            _obs_pipeline_threads.remove(t)
        except ValueError:
            pass


def _join_obs_workers(timeout: float = 0.1) -> None:
    """Best-effort join of tracked auto-pipeline workers.

    Uses a short timeout because this runs on the UI thread (Stop button); an
    in-flight pipeline (ffmpeg/transcribe) is a daemon thread that finishes on
    its own. The generation gate already prevents *new* stale runs.
    """
    with _obs_watcher_lock:
        workers = list(_obs_pipeline_threads)
    for w in workers:
        if w is not threading.current_thread():
            try:
                w.join(timeout=timeout)
            except Exception:
                pass


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _run_obs_detect_render(
    input_url: str,
    input_file,
    source_label: str,
    settings: dict,
) -> ObsPipelineOutcome:
    """Shared headless detect→render bridge for local and YouTube sources."""
    logs: list[str] = []

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)

    try:
        s = dict(settings)  # shallow copy; we only read
        clips_enabled = bool(s.get("enable_clips", True))
        shorts_enabled = bool(s.get("generate_shorts", False))
        chapters_enabled = bool(s.get("enable_chapters", True))
        s["enable_clips"] = clips_enabled
        s["generate_shorts"] = shorts_enabled
        s["enable_chapters"] = chapters_enabled
        if bool(s.get("auto_append_youtube", False)) and not chapters_enabled:
            # A description can only receive generated timestamp text.  Keep
            # the pipeline successful when a caller leaves auto-append on but
            # turns timestamp generation off in the OBS profile.
            log("[OBS] タイムスタンプ生成が無効のため概要欄への自動追加をスキップ")
            s["auto_append_youtube"] = False
        progress = _DummyProgress()

        log(f"[OBS] Detect 開始: {source_label}")
        detect_result = detect_phase(
            input_url,
            input_file,
            bool(s.get("enable_clips", True)),
            s.get("clip_prompt", ""),
            bool(s.get("enable_chapters", True)),
            s.get("chapter_prompt", ""),
            _coerce_int(s.get("num_clips", 5), 5),
            s.get("ai_provider", "gemini"),
            s.get("ai_model", ""),
            load_gemini_api_key(),
            _coerce_int(s.get("min_duration", 30), 30),
            _coerce_int(s.get("max_duration", 90), 90),
            s.get("whisper_model", "large-v3"),
            s.get("language", "ja"),
            bool(s.get("audio_fusion", False)),
            _coerce_float(s.get("audio_alpha", 0.35), 0.35),
            s.get("output_base_dir", ""),
            bool(s.get("generate_shorts", False)),
            progress=progress,
        )
        # detect_phase returns (session, status_md, review_panel_update)
        session = detect_result[0] if isinstance(detect_result, tuple) and detect_result else None
        detect_status = detect_result[1] if isinstance(detect_result, tuple) and len(detect_result) > 1 else ""
        if not isinstance(session, dict) or not session.get("video_path"):
            error = str(detect_status) or "Detect処理に失敗しました"
            if "Error:" not in error:
                error = f"Error: {error}"
            return ObsPipelineOutcome(
                log="\n".join(logs + [error]),
                success=False,
                error=error,
            )

        log("[OBS] Detect 完了 — Render 開始")
        render_result = render_phase(
            session,
            s.get("output_mode", "combined"),
            bool(s.get("generate_shorts", False)),
            s.get("shorts_mode", "pad"),
            s.get("shorts_crop", "center"),
            bool(s.get("shorts_title", True)),
            False,  # generate_zip — 自動処理では ZIP を作らない
            False,  # upload_to_drive — 自動処理では Drive 投稿しない
            bool(s.get("auto_append_youtube", False)),
            s.get("font_name", "Noto Sans JP"),
            _coerce_int(s.get("font_size", 96), 96),
            s.get("font_color", "#FFFFFF"),
            bool(s.get("generate_thumbnails", False)),
            bool(s.get("karaoke", False)),
            shorts_blur_strength=_normalise_shorts_blur_strength(
                s.get("shorts_blur_strength", 20)
            ),
            shorts_title_position=_normalise_shorts_title_position(
                s.get("shorts_title_position", "top")
            ),
            progress=progress,
        )
        # render_phase returns ProcessResult.as_gradio_outputs() = (log, highlights, dl, drive, chapters)
        render_log = render_result[0] if isinstance(render_result, tuple) and render_result else ""
        render_log = str(render_log)
        combined_logs = logs + [render_log]
        if "Error:" in render_log:
            return ObsPipelineOutcome(
                log="\n".join(combined_logs),
                success=False,
                error=render_log,
            )

        render_outcome = session.get("_obs_render_outcome") or {}
        clip_paths = tuple(str(path) for path in render_outcome.get("clip_paths", []))
        shorts_paths = tuple(str(path) for path in render_outcome.get("shorts_paths", []))
        chapters_path = str(render_outcome.get("chapters_path", ""))
        chapters_text = str(
            render_outcome.get("chapters_text", "")
            or (
                render_result[4]
                if isinstance(render_result, tuple) and len(render_result) > 4
                else ""
            )
        )
        output_errors: list[str] = []

        if bool(s.get("enable_clips", True)):
            clip_files = [Path(path) for path in clip_paths]
            if not clip_files or any(
                not path.is_file() or path.stat().st_size <= 0
                for path in clip_files
            ):
                output_errors.append("切り抜きファイルを確認できませんでした")

        if bool(s.get("generate_shorts", False)):
            short_files = [Path(path) for path in shorts_paths]
            if not short_files or any(
                not path.is_file() or path.stat().st_size <= 0
                for path in short_files
            ):
                output_errors.append("ショート動画ファイルを確認できませんでした")

        if bool(s.get("enable_chapters", True)):
            chapter_file = Path(chapters_path) if chapters_path else None
            if (
                not chapters_text.strip()
                or chapter_file is None
                or not chapter_file.is_file()
                or chapter_file.stat().st_size <= 0
            ):
                output_errors.append("タイムスタンプファイルの生成を確認できませんでした")

        youtube_appended = render_outcome.get("youtube_append_succeeded")
        if bool(s.get("auto_append_youtube", False)) and youtube_appended is not True:
            output_errors.append("YouTube概要欄へのタイムスタンプ反映を確認できませんでした")

        if output_errors:
            error = "Error: " + " / ".join(output_errors)
            return ObsPipelineOutcome(
                log="\n".join(combined_logs + [error]),
                success=False,
                error=error,
                output_dir=str(session.get("output_dir", "")),
                clip_paths=clip_paths,
                shorts_paths=shorts_paths,
                chapters_path=chapters_path,
                chapters_text=chapters_text,
                youtube_appended=youtube_appended,
            )

        log("[OBS] Render 完了")
        return ObsPipelineOutcome(
            log="\n".join(logs + [render_log]),
            success=True,
            output_dir=str(session.get("output_dir", "")),
            clip_paths=clip_paths,
            shorts_paths=shorts_paths,
            chapters_path=chapters_path,
            chapters_text=chapters_text,
            youtube_appended=youtube_appended,
        )
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"OBS auto pipeline error: {e}\n{tb}")
        error = f"Error: {e}"
        return ObsPipelineOutcome(
            log="\n".join(logs + [error, tb]),
            success=False,
            error=error,
        )


def _run_obs_auto_pipeline_outcome(
    video_path: str,
    settings: dict,
) -> ObsPipelineOutcome:
    """Run detect→render end-to-end on a finished local recording."""
    if not video_path:
        return ObsPipelineOutcome(
            log="Error: OBS auto: 録画パスが空です",
            success=False,
            error="録画パスが空です",
        )
    if not Path(video_path).exists():
        error = f"OBS auto: ファイルが見つかりません: {video_path}"
        return ObsPipelineOutcome(
            log=f"Error: {error}",
            success=False,
            error=error,
        )
    fake_file = type("F", (), {"name": video_path})()
    return _run_obs_detect_render("", fake_file, video_path, settings)


def run_obs_auto_pipeline(video_path: str, settings: dict) -> str:
    """Compatibility wrapper returning the unattended pipeline log."""
    return _run_obs_auto_pipeline_outcome(video_path, settings).log


def _run_obs_youtube_pipeline_outcome(
    video_url: str,
    settings: dict,
) -> ObsPipelineOutcome:
    """Download a completed stream archive, then create clips and timestamps."""
    if not youtube_api.extract_video_id(video_url):
        error = f"OBS auto: YouTubeアーカイブURLが不正です: {video_url}"
        return ObsPipelineOutcome(
            log=f"Error: {error}",
            success=False,
            error=error,
        )
    # Keep the OBS profile exactly as configured.  In particular, archive mode
    # must respect independent clip and timestamp toggles.
    return _run_obs_detect_render(video_url, None, video_url, dict(settings))


def run_obs_youtube_pipeline(video_url: str, settings: dict) -> str:
    """Compatibility wrapper returning the unattended pipeline log."""
    return _run_obs_youtube_pipeline_outcome(video_url, settings).log


def _resolve_obs_youtube_archive(
    cached_broadcast: dict | None,
    stopped_at: datetime,
    is_current: Callable[[], bool],
    exclude_video_ids: set[str] | None = None,
    started_after: datetime | None = None,
    completed_after: datetime | None = None,
    wait_for_processed: bool = True,
) -> dict:
    """Resolve the just-finished broadcast and optionally wait for download."""
    excluded_ids = set(exclude_video_ids or ())
    broadcast = dict(cached_broadcast) if cached_broadcast else None
    if broadcast and broadcast.get("video_id") in excluded_ids:
        broadcast = None
    poll_delay = _OBS_ARCHIVE_POLL_INITIAL
    discovery_deadline = time.monotonic() + _OBS_ARCHIVE_DISCOVERY_TIMEOUT

    while True:
        if not is_current():
            raise RuntimeError("OBS連携が停止されたためアーカイブ待機を中止しました")
        try:
            service = youtube_api.get_youtube_service()
            break
        except Exception as exc:
            poll_delay = _obs_retry_after_api_error(
                "YouTube接続",
                exc,
                discovery_deadline,
                poll_delay,
                is_current,
                "YouTube APIへ15分以内に接続できませんでした",
            )

    if broadcast is None:
        ended_after = stopped_at - _OBS_ARCHIVE_END_LOOKBACK
        if completed_after is not None:
            if completed_after.tzinfo is None:
                completed_after = completed_after.replace(tzinfo=timezone.utc)
            else:
                completed_after = completed_after.astimezone(timezone.utc)
            ended_after = max(ended_after, completed_after)
        while broadcast is None:
            if not is_current():
                raise RuntimeError("OBS連携が停止されたためアーカイブ待機を中止しました")
            try:
                broadcast = youtube_api.find_active_broadcast(
                    service,
                    started_before=stopped_at,
                    started_after=started_after,
                )
                if broadcast and broadcast.get("video_id") in excluded_ids:
                    broadcast = None
                if broadcast is None:
                    broadcast = youtube_api.find_recent_completed_broadcast(
                        service,
                        ended_after=ended_after,
                        exclude_video_ids=excluded_ids,
                        started_before=stopped_at,
                        started_after=started_after,
                    )
            except Exception as exc:
                poll_delay = _obs_retry_after_api_error(
                    "配信ID確認",
                    exc,
                    discovery_deadline,
                    poll_delay,
                    is_current,
                    "終了したYouTube配信を15分以内に特定できませんでした",
                )
                continue
            if broadcast is not None:
                break
            if time.monotonic() >= discovery_deadline:
                raise TimeoutError("終了したYouTube配信を15分以内に特定できませんでした")
            wait_reason = (
                "YouTube側の配信終了反映を待機中"
                if exclude_video_ids is not None
                else "開始時の終了済み配信一覧を取得できなかったため配信IDを安全に確認中"
            )
            _obs_append_status(f"{wait_reason}… 次回確認は{poll_delay}秒後")
            _obs_wait_for_poll(poll_delay, is_current)
            poll_delay = min(poll_delay * 2, _OBS_ARCHIVE_POLL_MAX)

    video_id = broadcast.get("video_id", "")
    if not video_id:
        raise RuntimeError("YouTube配信IDを取得できませんでした")

    completion_deadline = time.monotonic() + _OBS_ARCHIVE_DISCOVERY_TIMEOUT
    poll_delay = _OBS_ARCHIVE_POLL_INITIAL
    while True:
        if not is_current():
            raise RuntimeError("OBS連携が停止されたためアーカイブ待機を中止しました")
        try:
            lifecycle_status = youtube_api.get_broadcast_lifecycle_status(
                service,
                video_id,
            )
        except Exception as exc:
            poll_delay = _obs_retry_after_api_error(
                "配信終了確認",
                exc,
                completion_deadline,
                poll_delay,
                is_current,
                "YouTube側の配信終了が15分以内に確定しませんでした",
            )
            continue
        if not is_current():
            raise RuntimeError("OBS連携が停止されたためアーカイブ待機を中止しました")
        if lifecycle_status == "complete":
            break
        if lifecycle_status == "revoked":
            raise RuntimeError("YouTube配信が取り消されたためアーカイブを取得できません")
        if time.monotonic() >= completion_deadline:
            raise TimeoutError("YouTube側の配信終了が15分以内に確定しませんでした")
        _obs_append_status(
            "YouTube側の配信終了確定待ち: "
            f"status={lifecycle_status or '不明'} — 次回確認は{poll_delay}秒後"
        )
        _obs_wait_for_poll(poll_delay, is_current)
        poll_delay = min(poll_delay * 2, _OBS_ARCHIVE_POLL_MAX)

    archive = {
        **broadcast,
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }
    if not wait_for_processed:
        return archive

    ready_deadline = time.monotonic() + _OBS_ARCHIVE_READY_TIMEOUT
    poll_delay = _OBS_ARCHIVE_POLL_INITIAL
    while True:
        if not is_current():
            raise RuntimeError("OBS連携が停止されたためアーカイブ待機を中止しました")
        try:
            state = youtube_api.get_archive_processing_state(service, video_id)
        except Exception as exc:
            poll_delay = _obs_retry_after_api_error(
                "アーカイブ処理確認",
                exc,
                ready_deadline,
                poll_delay,
                is_current,
                "YouTubeアーカイブが6時間以内にダウンロード可能になりませんでした",
            )
            continue
        if not is_current():
            raise RuntimeError("OBS連携が停止されたためアーカイブ待機を中止しました")
        if state["failed"]:
            raise RuntimeError(
                "YouTubeアーカイブの処理に失敗しました "
                f"(processing={state['processing_status']}, upload={state['upload_status']})"
            )
        if state["ready"]:
            return archive
        if time.monotonic() >= ready_deadline:
            raise TimeoutError(
                "YouTubeアーカイブが6時間以内にダウンロード可能になりませんでした"
            )
        detail = (
            f"processing={state['processing_status'] or '待機中'}, "
            f"privacy={state['privacy_status'] or '不明'}"
        )
        if state["privacy_status"] == "private":
            detail += "（公開または限定公開への変更待ち）"
        _obs_append_status(
            f"YouTubeアーカイブ処理待ち: {detail} — 次回確認は{poll_delay}秒後"
        )
        _obs_wait_for_poll(poll_delay, is_current)
        poll_delay = min(poll_delay * 2, _OBS_ARCHIVE_POLL_MAX)


def _append_obs_chapters_to_archive(
    video_id: str,
    chapters_text: str,
    is_current: Callable[[], bool],
) -> None:
    """Append locally generated chapters to the matching YouTube archive."""
    chapters = (chapters_text or "").strip()
    if not chapters:
        raise RuntimeError("録画から生成したタイムスタンプが空です")

    deadline = time.monotonic() + _OBS_ARCHIVE_DISCOVERY_TIMEOUT
    poll_delay = _OBS_ARCHIVE_POLL_INITIAL
    while True:
        if not is_current():
            raise RuntimeError("OBS連携が停止されたため概要欄更新を中止しました")
        try:
            service = youtube_api.get_youtube_service()
            youtube_api.update_video_description(
                service,
                video_id,
                chapters,
                position="prepend",
            )
            return
        except Exception as exc:
            poll_delay = _obs_retry_after_api_error(
                "概要欄更新",
                exc,
                deadline,
                poll_delay,
                is_current,
                "YouTube概要欄を15分以内に更新できませんでした",
            )


def _obs_make_stream_pipeline_callbacks(
    auto_process: bool,
    settings: dict,
    generation: int | None = None,
    *,
    recording_primary: bool,
) -> tuple[
    Callable[[str], None] | None,
    Callable[[str], None] | None,
    Callable[..., None],
    Callable[[], None],
]:
    """Build callbacks for archive-only or recording-primary automation."""
    state_lock = threading.Lock()
    archive_pipeline_lock = threading.Lock()
    state = {
        "epoch": 0,
        "streams": {},
        "finishing_epochs": set(),
        "completed_epochs": set(),
        "inflight_ids": set(),
        "claimed_ids": set(),
        "processed_ids": set(),
        "recording_reservations": {},
        "seen_record_paths": set(),
    }

    def _is_current() -> bool:
        return generation is None or generation == _obs_generation

    def _spawn(target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, daemon=True)
        _register_obs_worker(t)
        t.start()

    def _recording_key(video_path: str) -> str:
        return os.path.normcase(os.path.abspath(str(video_path)))

    def _new_stream_state(
        started_at: datetime,
        *,
        observed_start: bool,
        capture_complete: bool = False,
    ) -> dict:
        capture_done = threading.Event()
        if capture_complete:
            capture_done.set()
        return {
            "broadcast": None,
            "baseline_ids": None,
            "resolved_archive": None,
            "started_at": started_at,
            "observed_start": observed_start,
            "capture_done": capture_done,
            "recording_ready": threading.Event(),
            "recording_done": threading.Event(),
            "recording_path": "",
            "recording_outcome": None,
            "confirmation_decision": None,
            "processing_settings": None,
            "source_claim": None,
        }

    def _on_recording_stopped(video_path: str) -> None:
        """Reserve the current stream before file-stability waiting can reorder it."""
        if not recording_primary or not auto_process or not _is_current():
            return
        key = _recording_key(video_path)
        with state_lock:
            epoch = state["epoch"]
            event_key = (epoch, key)
            if event_key in state["seen_record_paths"]:
                return
            if epoch in state["completed_epochs"]:
                state["seen_record_paths"].add(event_key)
                _obs_append_status(
                    "処理済み配信の重複録画停止イベントをスキップしました"
                )
                return
            stream_state = state["streams"].get(epoch)
            if stream_state is None:
                stream_state = _new_stream_state(
                    datetime.now(timezone.utc),
                    observed_start=False,
                    capture_complete=True,
                )
                state["streams"][epoch] = stream_state
            if (
                stream_state.get("source_claim") == "archive"
                or stream_state["recording_ready"].is_set()
            ):
                state["seen_record_paths"].add(event_key)
                return
            state["recording_reservations"].setdefault(key, epoch)

    def _on_recording_finished(video_path: str) -> None:
        if not recording_primary or not _is_current():
            return
        _obs_append_status(f"録画終了を検知: {video_path}")
        if not auto_process:
            _obs_append_status("自動処理が無効のため検知のみ記録しました")
            return

        key = _recording_key(video_path)
        with state_lock:
            epoch = state["recording_reservations"].get(
                key,
                state["epoch"],
            )
            event_key = (epoch, key)
            if event_key in state["seen_record_paths"]:
                _obs_append_status("同じ録画終了イベントは処理済みのためスキップしました")
                return
            state["recording_reservations"].pop(key, None)
            if epoch in state["completed_epochs"]:
                state["seen_record_paths"].add(event_key)
                _obs_append_status(
                    "処理済み配信の重複録画終了イベントをスキップしました"
                )
                return
            stream_state = state["streams"].get(epoch)
            if stream_state is None:
                stream_state = _new_stream_state(
                    datetime.now(timezone.utc),
                    observed_start=False,
                    capture_complete=True,
                )
                state["streams"][epoch] = stream_state
            if stream_state.get("source_claim") == "archive":
                state["seen_record_paths"].add(event_key)
                _obs_append_status(
                    "完成アーカイブ処理を開始済みのため、遅れて届いた録画イベントを"
                    "スキップしました"
                )
                return
            if stream_state["recording_ready"].is_set():
                state["seen_record_paths"].add(event_key)
                _obs_append_status("同じ録画終了イベントは処理済みのためスキップしました")
                return
            state["seen_record_paths"].add(event_key)
            stream_state["source_claim"] = "recording"
            stream_state["recording_path"] = video_path
            stream_state["recording_ready"].set()

        def _recording_worker() -> None:
            try:
                if not _is_current():
                    return
                local_settings = dict(settings)
                if not _obs_confirm_before_auto_process(
                    local_settings,
                    str(video_path),
                    _is_current,
                ):
                    skipped = ObsPipelineOutcome(
                        log="自動生成の確認がないため、OBS録画の処理をスキップしました",
                        success=True,
                        skipped=True,
                    )
                    with state_lock:
                        stream_state["confirmation_decision"] = "skipped"
                        stream_state["recording_outcome"] = skipped
                    _obs_append_status(skipped.log)
                    return
                with state_lock:
                    stream_state["confirmation_decision"] = "approved"
                    stream_state["processing_settings"] = dict(local_settings)
                # A local path cannot identify the matching YouTube video.
                # The stream-finish worker applies these chapters once the
                # broadcast ID is known.
                local_settings["auto_append_youtube"] = False
                _obs_append_status(f"OBS録画の自動パイプライン開始: {video_path}")
                outcome = _run_obs_auto_pipeline_outcome(video_path, local_settings)
                _obs_append_status(outcome.log)
                with state_lock:
                    stream_state["recording_outcome"] = outcome
                if outcome.success:
                    generated = []
                    if bool(local_settings.get("enable_clips", True)):
                        generated.append("切り抜き")
                    if bool(local_settings.get("enable_chapters", True)):
                        generated.append("タイムスタンプ")
                    _obs_append_status(
                        "OBS録画から" + "と".join(generated) + "を生成しました"
                    )
                else:
                    _obs_append_status(
                        "OBS録画の処理に失敗 — 完成アーカイブを保険として使用します: "
                        f"{outcome.error or outcome.log}"
                    )
            except Exception as exc:
                logger.exception("OBS recording-primary pipeline failed")
                with state_lock:
                    stream_state["recording_outcome"] = ObsPipelineOutcome(
                        log=f"Error: {exc}",
                        success=False,
                        error=str(exc),
                    )
                _obs_append_status(
                    f"OBS録画の処理に失敗 — 完成アーカイブを保険として使用します: {exc}"
                )
            finally:
                stream_state["recording_done"].set()
                _unregister_obs_worker(threading.current_thread())

        _spawn(_recording_worker)

    def _on_stream_started(proactive: bool = False) -> None:
        if not auto_process or not _is_current():
            return
        with state_lock:
            state["epoch"] += 1
            epoch = state["epoch"]
            stream_state = _new_stream_state(
                datetime.now(timezone.utc),
                observed_start=not proactive,
            )
            state["streams"][epoch] = stream_state
            for old_epoch in list(state["streams"]):
                if (
                    old_epoch != epoch
                    and old_epoch not in state["finishing_epochs"]
                ):
                    state["streams"].pop(old_epoch, None)
        _obs_append_status("配信開始を検知 — YouTube配信IDを確認中…")

        def _capture_worker() -> None:
            broadcast = None
            baseline_ids = None
            lookup_errors: list[str] = []
            try:
                service = youtube_api.get_youtube_service()
                try:
                    capture_started_after = (
                        stream_state["started_at"] - timedelta(seconds=30)
                        if stream_state.get("observed_start")
                        else None
                    )
                    broadcast = youtube_api.find_active_broadcast(
                        service,
                        started_after=capture_started_after,
                    )
                except Exception as exc:
                    lookup_errors.append(f"配信ID: {exc}")
                    logger.warning("YouTube active broadcast lookup failed: %s", exc)
                try:
                    baseline_ids = youtube_api.list_completed_broadcast_ids(
                        service,
                        completed_before=stream_state["started_at"],
                    )
                except Exception as exc:
                    lookup_errors.append(f"終了済み配信一覧: {exc}")
                    logger.warning("YouTube completed baseline lookup failed: %s", exc)

                with state_lock:
                    if (
                        state["streams"].get(epoch) is not stream_state
                        or not _is_current()
                    ):
                        return
                    stream_state["broadcast"] = broadcast
                    stream_state["baseline_ids"] = baseline_ids

                if broadcast:
                    _obs_append_status(
                        "YouTube配信IDを記録: "
                        f"{broadcast['video_id']} ({broadcast.get('title', '')})"
                    )
                else:
                    _obs_append_status(
                        "現在のYouTube配信はまだ見つかりません（終了時に再検索します）"
                    )
                if lookup_errors:
                    _obs_append_status(
                        "YouTube事前確認の一部に失敗: " + " / ".join(lookup_errors)
                    )
            except Exception as exc:
                logger.warning("YouTube stream lookup setup failed: %s", exc)
                _obs_append_status(
                    f"YouTube配信IDの事前取得に失敗（終了時に再検索）: {exc}"
                )
            finally:
                stream_state["capture_done"].set()
                _unregister_obs_worker(threading.current_thread())

        _spawn(_capture_worker)

    def _on_stream_finished() -> None:
        if not _is_current():
            return
        if recording_primary:
            _obs_append_status("配信終了を検知 — OBS録画の完了を確認します")
        else:
            _obs_append_status("配信終了を検知 — YouTubeアーカイブを待機します")
        if not auto_process:
            _obs_append_status("自動処理が無効のため検知のみ記録しました")
            return
        stopped_at = datetime.now(timezone.utc)
        with state_lock:
            epoch = state["epoch"]
            if epoch in state["completed_epochs"]:
                _obs_append_status("同じ配信終了イベントは処理済みのためスキップしました")
                return
            if epoch in state["finishing_epochs"]:
                _obs_append_status("同じ配信終了イベントは既に処理中のためスキップしました")
                return
            state["finishing_epochs"].add(epoch)
            stream_state = state["streams"].get(epoch)
            if stream_state is None:
                stream_state = _new_stream_state(
                    stopped_at,
                    observed_start=False,
                    capture_complete=True,
                )
                state["streams"][epoch] = stream_state

        def _finish_worker() -> None:
            video_id = ""
            owns_inflight = False
            owns_pipeline_slot = False
            local_outcome: ObsPipelineOutcome | None = None
            use_recording = False
            try:
                if recording_primary:
                    recording_arrived = _obs_wait_for_event(
                        stream_state["recording_ready"],
                        _OBS_RECORDING_EVENT_TIMEOUT,
                        _is_current,
                    )
                    if recording_arrived:
                        if not _obs_wait_for_event(
                            stream_state["recording_done"],
                            _OBS_ARCHIVE_READY_TIMEOUT,
                            _is_current,
                        ):
                            raise TimeoutError(
                                "OBS録画の自動処理が6時間以内に完了しませんでした"
                            )
                        with state_lock:
                            candidate_outcome = stream_state.get(
                                "recording_outcome"
                            )
                        if isinstance(candidate_outcome, ObsPipelineOutcome):
                            local_outcome = candidate_outcome
                        elif candidate_outcome is not None:
                            local_outcome = candidate_outcome
                        if getattr(local_outcome, "skipped", False):
                            with state_lock:
                                state["completed_epochs"].add(epoch)
                            _obs_append_status(
                                "確認されなかったため、この配信の自動生成をスキップしました"
                            )
                            return
                        use_recording = bool(
                            local_outcome is not None
                            and getattr(local_outcome, "success", False)
                        )
                        if not use_recording:
                            with state_lock:
                                stream_state["source_claim"] = "archive"
                            _obs_append_status(
                                "OBS録画を使用できないため、YouTube完成アーカイブへ"
                                "フォールバックします"
                            )
                    else:
                        with state_lock:
                            stream_state["source_claim"] = "archive"
                        _obs_append_status(
                            "配信終了後60秒以内に安定したOBS録画を検知できなかったため、"
                            "YouTube完成アーカイブへフォールバックします"
                        )

                with state_lock:
                    confirmation_decision = stream_state.get(
                        "confirmation_decision"
                    )
                    processing_settings = stream_state.get("processing_settings")
                if confirmation_decision == "skipped":
                    with state_lock:
                        state["completed_epochs"].add(epoch)
                    _obs_append_status(
                        "確認されなかったため、この配信の自動生成をスキップしました"
                    )
                    return
                if confirmation_decision != "approved":
                    processing_settings = dict(settings)
                    if not _obs_confirm_before_auto_process(
                        processing_settings,
                        "YouTube完成アーカイブ",
                        _is_current,
                    ):
                        if _is_current():
                            with state_lock:
                                state["completed_epochs"].add(epoch)
                            _obs_append_status(
                                "確認されなかったため、この配信の自動生成をスキップしました"
                            )
                        return
                    with state_lock:
                        stream_state["confirmation_decision"] = "approved"
                        stream_state["processing_settings"] = dict(
                            processing_settings
                        )

                effective_settings = dict(processing_settings or settings)

                while not archive_pipeline_lock.acquire(timeout=0.25):
                    if not _is_current():
                        raise RuntimeError(
                            "OBS連携が停止されたためアーカイブ処理を中止しました"
                        )
                owns_pipeline_slot = True
                if not _obs_wait_for_event(
                    stream_state["capture_done"],
                    _OBS_ARCHIVE_DISCOVERY_TIMEOUT,
                    _is_current,
                ):
                    raise TimeoutError(
                        "配信開始時のYouTube情報取得が15分以内に完了しませんでした"
                    )
                if not _is_current():
                    raise RuntimeError("OBS連携が停止されたためアーカイブ処理を中止しました")
                with state_lock:
                    cached_broadcast = (
                        dict(stream_state["broadcast"])
                        if isinstance(stream_state["broadcast"], dict)
                        else None
                    )
                    baseline_ids = (
                        set(stream_state["baseline_ids"])
                        if stream_state["baseline_ids"] is not None
                        else None
                    )
                    started_after = (
                        stream_state["started_at"] - timedelta(seconds=30)
                        if stream_state.get("observed_start")
                        else None
                    )
                    excluded_ids = set(baseline_ids or ())
                    excluded_ids.update(state["processed_ids"])
                    excluded_ids.update(state["inflight_ids"])
                    excluded_ids.update(state["claimed_ids"])
                    for other_epoch, other_stream in state["streams"].items():
                        if other_epoch == epoch:
                            continue
                        other_broadcast = other_stream.get("broadcast") or {}
                        other_archive = other_stream.get("resolved_archive") or {}
                        if other_broadcast.get("video_id"):
                            excluded_ids.add(other_broadcast["video_id"])
                        if other_archive.get("video_id"):
                            excluded_ids.add(other_archive["video_id"])
                    resolved_archive = stream_state.get("resolved_archive")
                if isinstance(resolved_archive, dict):
                    archive = dict(resolved_archive)
                else:
                    archive = _resolve_obs_youtube_archive(
                        cached_broadcast,
                        stopped_at,
                        _is_current,
                        excluded_ids,
                        started_after,
                        stream_state["started_at"],
                        not use_recording,
                    )
                    with state_lock:
                        stream_state["resolved_archive"] = dict(archive)
                if not _is_current():
                    raise RuntimeError("OBS連携が停止されたためアーカイブ処理を中止しました")
                video_id = archive["video_id"]
                with state_lock:
                    state["claimed_ids"].add(video_id)
                    if video_id in state["processed_ids"]:
                        state["completed_epochs"].add(epoch)
                        _obs_append_status(
                            f"YouTubeアーカイブ {video_id} は処理済みのためスキップしました"
                        )
                        return
                    if video_id in state["inflight_ids"]:
                        _obs_append_status(
                            f"YouTubeアーカイブ {video_id} は別の処理で実行中のためスキップしました"
                        )
                        return
                    state["inflight_ids"].add(video_id)
                    owns_inflight = True

                chapters_enabled = bool(settings.get("enable_chapters", True))
                if use_recording and chapters_enabled:
                    _obs_append_status(
                        "OBS録画から生成したタイムスタンプをYouTubeアーカイブへ"
                        f"反映します: {archive['url']}"
                    )
                    try:
                        _append_obs_chapters_to_archive(
                            video_id,
                            getattr(local_outcome, "chapters_text", ""),
                            _is_current,
                        )
                    except Exception as exc:
                        with state_lock:
                            state["processed_ids"].add(video_id)
                            state["completed_epochs"].add(epoch)
                        chapters_path = getattr(
                            local_outcome,
                            "chapters_path",
                            "",
                        )
                        chapters_note = (
                            f" ({chapters_path})" if chapters_path else ""
                        )
                        _obs_append_status(
                            "YouTube概要欄へのタイムスタンプ反映エラー: "
                            f"{exc}。録画の切り抜きとタイムスタンプ"
                            f"{chapters_note}"
                            "は生成済みです。完成アーカイブのDL処理には"
                            "切り替えません"
                        )
                        return
                    with state_lock:
                        state["processed_ids"].add(video_id)
                        state["completed_epochs"].add(epoch)
                    _obs_append_status(
                        "OBS録画からの自動処理とYouTubeタイムスタンプ反映が"
                        f"完了しました: {archive['url']}"
                    )
                    return

                if use_recording:
                    with state_lock:
                        state["processed_ids"].add(video_id)
                        state["completed_epochs"].add(epoch)
                    generated = []
                    if bool(settings.get("enable_clips", True)):
                        generated.append("切り抜き")
                    if chapters_enabled:
                        generated.append("タイムスタンプ")
                    _obs_append_status(
                        "OBS録画から"
                        + "と".join(generated)
                        + "を生成しました（タイムスタンプ無効のため概要欄更新なし）"
                    )
                    return

                archive_settings = effective_settings
                if recording_primary:
                    archive_settings["auto_append_youtube"] = bool(
                        archive_settings.get("enable_chapters", True)
                    )
                if not _is_current():
                    raise RuntimeError("OBS連携が停止されたためアーカイブ処理を中止しました")
                if recording_primary:
                    _obs_append_status(
                        "OBS録画の保険として、YouTubeの再エンコード完了後に"
                        f"アーカイブをDLして処理します: {archive['url']}"
                    )
                else:
                    _obs_append_status(
                        "YouTubeの再エンコード完了後にアーカイブをDLして処理します: "
                        f"{archive['url']}"
                    )
                outcome = _run_obs_youtube_pipeline_outcome(
                    archive["url"],
                    archive_settings,
                )
                _obs_append_status(outcome.log)
                if not outcome.success:
                    raise RuntimeError(outcome.error or outcome.log)
                with state_lock:
                    state["processed_ids"].add(video_id)
                    state["completed_epochs"].add(epoch)
                _obs_append_status(
                    f"完成アーカイブから自動処理完了: {archive['url']}"
                )
            except Exception as exc:
                if not _is_current():
                    logger.info("OBS YouTube archive pipeline cancelled: %s", exc)
                    return
                logger.exception("OBS YouTube archive pipeline failed")
                if recording_primary:
                    _obs_append_status(f"録画優先モードの自動処理エラー: {exc}")
                    if (
                        local_outcome is not None
                        and getattr(local_outcome, "success", False)
                    ):
                        _obs_append_status(
                            "OBS録画の切り抜きとタイムスタンプファイルは生成済みです。"
                            "アーカイブDLには切り替えません"
                        )
                    else:
                        _obs_append_status(
                            "OBS録画と完成アーカイブの両方を使用できませんでした。"
                            "録画設定・YouTube認証・公開状態を確認してください"
                        )
                else:
                    _obs_append_status(f"YouTubeアーカイブ処理エラー: {exc}")
                    _obs_append_status(
                        "完成アーカイブ方式ではOBS録画へ切り替えません。"
                        "YouTube認証・公開状態・アーカイブ処理状態を確認してください"
                    )
            finally:
                with state_lock:
                    state["finishing_epochs"].discard(epoch)
                    if epoch in state["completed_epochs"]:
                        state["streams"].pop(epoch, None)
                        for path_key, reserved_epoch in list(
                            state["recording_reservations"].items()
                        ):
                            if reserved_epoch == epoch:
                                state["recording_reservations"].pop(
                                    path_key,
                                    None,
                                )
                                state["seen_record_paths"].add(
                                    (epoch, path_key)
                                )
                    if owns_inflight:
                        state["inflight_ids"].discard(video_id)
                if owns_pipeline_slot:
                    archive_pipeline_lock.release()
                _unregister_obs_worker(threading.current_thread())

        _spawn(_finish_worker)

    return (
        _on_recording_finished if recording_primary else None,
        _on_recording_stopped if recording_primary else None,
        _on_stream_started,
        _on_stream_finished,
    )


def _obs_make_recording_primary_callbacks(
    auto_process: bool,
    settings: dict,
    generation: int | None = None,
) -> tuple[
    Callable[[str], None],
    Callable[[str], None],
    Callable[..., None],
    Callable[[], None],
]:
    """Build shared recording/stream callbacks for recording-first automation."""
    recording_finished, recording_stopped, stream_started, stream_finished = (
        _obs_make_stream_pipeline_callbacks(
            auto_process,
            settings,
            generation,
            recording_primary=True,
        )
    )
    assert recording_finished is not None
    assert recording_stopped is not None
    return (
        recording_finished,
        recording_stopped,
        stream_started,
        stream_finished,
    )


def _obs_make_archive_callbacks(
    auto_process: bool,
    settings: dict,
    generation: int | None = None,
) -> tuple[Callable[..., None], Callable[[], None]]:
    """Build stream callbacks for completed-archive-only automation."""
    _, _, stream_started, stream_finished = _obs_make_stream_pipeline_callbacks(
        auto_process,
        settings,
        generation,
        recording_primary=False,
    )
    return stream_started, stream_finished


def _obs_make_callback(
    auto_process: bool, settings: dict, generation: int | None = None
) -> Callable[[str], None]:
    """Build the watcher callback: log the path, and (if auto) run the pipeline.

    ``generation`` is the watcher generation this callback belongs to. When set,
    the callback (and its worker) abort if a later start/stop has superseded that
    generation, so a stale callback never runs the pipeline with old settings.
    Callbacks built directly (generation=None, e.g. in tests) skip that gate.
    """
    def _is_current() -> bool:
        return generation is None or generation == _obs_generation

    def _callback(video_path: str) -> None:
        if not _is_current():
            return
        _obs_append_status(f"録画終了を検知: {video_path}")
        if not auto_process:
            _obs_append_status("自動処理が無効のため検知のみ記録しました")
            return

        def _worker():
            try:
                if not _is_current():
                    return
                local_settings = dict(settings)
                if not _obs_confirm_before_auto_process(
                    local_settings,
                    str(video_path),
                    _is_current,
                ):
                    if _is_current():
                        _obs_append_status(
                            f"自動生成をスキップしました: {video_path}"
                        )
                    return
                _obs_append_status(f"自動パイプライン開始: {video_path}")
                outcome = _run_obs_auto_pipeline_outcome(video_path, local_settings)
                _obs_append_status(outcome.log)
                if not outcome.success:
                    raise RuntimeError(outcome.error or outcome.log)
                _obs_append_status(f"自動処理完了: {video_path}")
            except Exception as e:
                logger.exception("OBS auto pipeline worker crashed")
                try:
                    _obs_append_status(f"自動パイプラインエラー: {e}")
                except Exception:
                    pass
            finally:
                _unregister_obs_worker(threading.current_thread())

        t = threading.Thread(target=_worker, daemon=True)
        _register_obs_worker(t)
        t.start()

    return _callback


def _stop_obs_watch_impl() -> str:
    """Stop the active watcher while the caller owns lifecycle serialization."""
    global _obs_watcher, _obs_generation
    with _obs_watcher_lock:
        watcher = _obs_watcher
        _obs_watcher = None
        _obs_generation += 1
    _obs_cancel_pending_confirmation()
    if watcher is None:
        msg = "OBS連携は停止中です"
        _obs_append_status(msg)
        return msg
    try:
        watcher.stop()
        status = watcher.status
    except Exception as e:
        status = f"停止エラー: {e}"
    _join_obs_workers()
    _obs_append_status(f"OBS連携を停止しました: {status}")
    return f"OBS連携を停止しました: {status}"


def stop_obs_watch() -> str:
    """Manually stop OBS integration and cancel any pending startup wait."""
    _obs_auto_connect_cancel.set()
    with _obs_start_lock:
        return _stop_obs_watch_impl()


def _start_obs_watch_impl(
    method: str,
    host: str,
    port,
    password: str,
    save_password: bool,
    stop_event: str,
    watch_folder: str,
    auto_process: bool,
    auto_append_youtube: bool,
    num_clips,
    output_mode: str,
    generate_shorts: bool,
    ai_provider: str,
    whisper_model: str,
    output_base_dir: str,
    obs_processing_settings: dict | None = None,
) -> str:
    """Implementation shared by manual and automatic OBS connection starts."""
    global _obs_watcher, _obs_generation
    # Stop any existing watcher first so re-clicking Start reconfigures cleanly.
    _stop_obs_watch_impl()

    try:
        import obs_integration
    except Exception as e:
        msg = f"obs_integration の import に失敗: {e}"
        _obs_append_status(msg)
        return msg

    trigger_method = (method or "websocket").lower()
    source_mode = (stop_event or "record").lower()
    if source_mode not in {"record", "stream"}:
        msg = f"自動処理の取得元が不正です: {source_mode}"
        _obs_append_status(msg)
        return msg
    if trigger_method == "folder" and source_mode != "record":
        msg = (
            "フォルダ監視はOBS録画方式（record）専用です。"
            "完成アーカイブ方式（stream）はWebSocketを選択してください"
        )
        _obs_append_status(msg)
        return msg
    recording_primary_mode = (
        trigger_method == "websocket" and source_mode == "record"
    )
    archive_only_mode = trigger_method == "websocket" and source_mode == "stream"
    youtube_linked_mode = recording_primary_mode or archive_only_mode

    # Build the settings dict from the dedicated OBS profile.  The profile is
    # separate from the Input/archive values, while old settings files still
    # fall back to those values until an OBS profile is saved.
    settings = load_defaults()
    has_saved_obs_profile = isinstance(settings.get("obs_processing"), dict) and bool(
        settings.get("obs_processing")
    )
    obs_profile = _obs_processing_settings_from_defaults(settings)
    if obs_processing_settings is not None:
        obs_profile = _normalise_obs_processing_settings(
            obs_processing_settings,
            defaults=settings,
        )
    try:
        GenerationModes(
            enable_clips=bool(obs_profile.get("enable_clips", True)),
            enable_shorts=bool(obs_profile.get("generate_shorts", False)),
            enable_chapters=bool(obs_profile.get("enable_chapters", True)),
        ).validate()
    except ValueError as mode_err:
        msg = f"OBS自動処理設定エラー: {mode_err}"
        _obs_append_status(msg)
        return msg
    settings.update(obs_profile)
    settings["obs_processing"] = obs_profile
    try:
        # These legacy arguments remain part of the public start signature for
        # compatibility with callers outside the UI.  The dedicated profile
        # wins when provided; otherwise they preserve the previous behavior.
        if obs_processing_settings is None and not has_saved_obs_profile:
            settings["num_clips"] = int(num_clips)
            if output_mode:
                settings["output_mode"] = output_mode
            settings["generate_shorts"] = bool(generate_shorts)
            obs_profile.update(
                {
                    "num_clips": settings["num_clips"],
                    "output_mode": settings["output_mode"],
                    "generate_shorts": settings["generate_shorts"],
                }
            )
            settings["obs_processing"] = obs_profile
    except (TypeError, ValueError):
        pass
    if ai_provider:
        settings["ai_provider"] = ai_provider
    if whisper_model:
        settings["whisper_model"] = whisper_model
    if output_base_dir is not None:
        settings["output_base_dir"] = output_base_dir
    profile_auto_append = obs_profile.get(
        "auto_append_youtube",
        auto_append_youtube,
    )
    chapters_enabled = bool(obs_profile.get("enable_chapters", True))
    settings["auto_append_youtube"] = (
        True
        if recording_primary_mode and auto_process and chapters_enabled
        else bool(profile_auto_append) and chapters_enabled
        if archive_only_mode
        else False
    )
    if (auto_append_youtube or bool(profile_auto_append)) and not chapters_enabled:
        _obs_append_status(
            "タイムスタンプ生成が無効のため、YouTube概要欄への自動追加を無効化しました"
        )
    if auto_append_youtube and trigger_method == "folder":
        _obs_append_status(
            "フォルダ監視では配信を特定できないため、YouTube概要欄への"
            "自動追加を無効化しました"
        )
    if recording_primary_mode and auto_process and chapters_enabled and not auto_append_youtube:
        _obs_append_status(
            "録画優先モードでは、生成したタイムスタンプを配信アーカイブへ"
            "反映する設定を自動的に有効化しました"
        )

    entered_password = password or ""
    config = {
        "host": host or "localhost",
        "port": int(port) if port not in (None, "") else 4455,
        # Only a checked save box may reuse the server-side secret. The secret
        # itself is never sent to the browser as a component initial value.
        "password": (
            entered_password
            if entered_password or not save_password
            else load_obs_password()
        ),
        "stop_event": source_mode,
        "watch_folder": watch_folder or "",
    }
    try:
        _save_obs_connection_defaults(
            trigger_method,
            config["host"],
            config["port"],
            config["password"],
            bool(save_password),
            source_mode,
            config["watch_folder"],
            bool(auto_process),
            processing_settings=obs_profile,
        )
    except Exception as exc:
        msg = f"OBS連携設定の保存に失敗しました: {exc}"
        _obs_append_status(msg)
        return msg
    if youtube_linked_mode and auto_process:
        youtube_requirement = (
            "OBS録画の保険とタイムスタンプ反映"
            if recording_primary_mode
            else "YouTubeアーカイブ連携"
        )
        try:
            auth = youtube_api.check_auth_status()
        except Exception as exc:
            msg = f"YouTube認証状態を確認できません: {exc}"
            _obs_append_status(msg)
            return msg
        if not auth.get("configured"):
            msg = (
                f"{youtube_requirement}には Settings で "
                "credentials.json の設定が必要です"
            )
            _obs_append_status(msg)
            return msg
        if not auth.get("authenticated"):
            msg = (
                f"{youtube_requirement}には Settings で"
                "YouTube認証が必要です"
            )
            _obs_append_status(msg)
            return msg

    with _obs_watcher_lock:
        _obs_generation += 1
        gen = _obs_generation
    recording_stopped = None
    archive_started = None
    archive_finished = None
    if recording_primary_mode:
        callback, recording_stopped, archive_started, archive_finished = (
            _obs_make_recording_primary_callbacks(
                bool(auto_process),
                settings,
                gen,
            )
        )
    else:
        callback = _obs_make_callback(bool(auto_process), settings, gen)
    if archive_only_mode:
        archive_started, archive_finished = _obs_make_archive_callbacks(
            bool(auto_process),
            settings,
            gen,
        )
    try:
        watcher = obs_integration.create_watcher(
            method,
            config,
            callback,
            on_recording_stopped=recording_stopped,
            on_stream_started=archive_started,
            on_stream_finished=archive_finished,
        )
    except Exception as e:
        msg = f"ウォッチャー生成エラー: {e}"
        _obs_append_status(msg)
        return msg

    with _obs_watcher_lock:
        _obs_watcher = watcher
    try:
        watcher.start()
    except Exception as e:
        _obs_append_status(f"OBS連携開始エラー: {e}")
        return f"OBS連携開始エラー: {e}"
    status = watcher.status
    _obs_append_status(f"OBS連携を開始: {status}")
    if (
        archive_started is not None
        and str(status).lower().startswith("connected")
        and not bool(getattr(watcher, "stream_status_checked", False))
        and not bool(getattr(watcher, "stream_active", False))
    ):
        # Compatibility fallback for older/mocked obsws-python clients that
        # cannot query GetStreamStatus. Current clients invoke the callback
        # only when output_active is true.
        archive_started(proactive=True)
    return status


def start_obs_watch(
    method: str,
    host: str,
    port,
    password: str,
    save_password: bool,
    stop_event: str,
    watch_folder: str,
    auto_process: bool,
    auto_append_youtube: bool,
    num_clips,
    output_mode: str,
    generate_shorts: bool,
    ai_provider: str,
    whisper_model: str,
    output_base_dir: str,
    obs_enable_clips=None,
    obs_clip_prompt=None,
    obs_enable_chapters=None,
    obs_chapter_prompt=None,
    obs_min_duration=None,
    obs_max_duration=None,
    obs_shorts_mode=None,
    obs_shorts_crop=None,
    obs_shorts_title=None,
    obs_generate_thumbnails=None,
    obs_audio_fusion=None,
    obs_audio_alpha=None,
    obs_karaoke=None,
    obs_auto_start_without_prompt_confirmation=None,
    obs_shorts_blur_strength=None,
    obs_shorts_title_position=None,
) -> str:
    """Manually (re)start OBS integration from the Gradio controls.

    Argument order MUST line up 1:1 with ``obs_start_btn.click(inputs=[...])``.
    A manual start cancels any pending startup wait so delayed automation can
    never overwrite settings the user just selected.
    """
    _obs_auto_connect_cancel.set()
    obs_processing_settings = None
    if any(
        value is not None
        for value in (
            obs_enable_clips,
            obs_clip_prompt,
            obs_enable_chapters,
            obs_chapter_prompt,
            obs_min_duration,
            obs_max_duration,
            obs_shorts_mode,
            obs_shorts_crop,
            obs_shorts_title,
            obs_generate_thumbnails,
            obs_audio_fusion,
            obs_audio_alpha,
            obs_karaoke,
            obs_auto_start_without_prompt_confirmation,
            obs_shorts_blur_strength,
            obs_shorts_title_position,
        )
    ):
        obs_processing_settings = _build_obs_processing_settings(
            obs_enable_clips,
            obs_clip_prompt,
            obs_enable_chapters,
            obs_chapter_prompt,
            auto_append_youtube,
            num_clips,
            obs_min_duration,
            obs_max_duration,
            output_mode,
            generate_shorts,
            obs_shorts_mode,
            obs_shorts_crop,
            obs_shorts_title,
            obs_generate_thumbnails,
            obs_audio_fusion,
            obs_audio_alpha,
            obs_karaoke,
            not bool(obs_auto_start_without_prompt_confirmation),
            shorts_blur_strength=obs_shorts_blur_strength,
            shorts_title_position=obs_shorts_title_position,
        )
    with _obs_start_lock:
        return _start_obs_watch_impl(
            method=method,
            host=host,
            port=port,
            password=password,
            save_password=save_password,
            stop_event=stop_event,
            watch_folder=watch_folder,
            auto_process=auto_process,
            auto_append_youtube=auto_append_youtube,
            num_clips=num_clips,
            output_mode=output_mode,
            generate_shorts=generate_shorts,
            ai_provider=ai_provider,
            whisper_model=whisper_model,
            output_base_dir=output_base_dir,
            obs_processing_settings=obs_processing_settings,
        )


def _wait_for_obs_websocket(
    host: str,
    port: int,
    *,
    timeout: float | None,
    retry_interval: float,
) -> bool:
    """Wait until OBS' TCP endpoint accepts connections or startup is canceled."""
    deadline = (
        None
        if timeout is None
        else time.monotonic() + max(0.0, float(timeout))
    )
    interval = max(0.05, float(retry_interval))

    while not _obs_auto_connect_cancel.is_set():
        remaining = (
            None
            if deadline is None
            else max(0.0, deadline - time.monotonic())
        )
        try:
            with socket.create_connection(
                (host, int(port)),
                timeout=(
                    0.5
                    if remaining is None
                    else max(0.05, min(0.5, remaining or 0.05))
                ),
            ):
                return True
        except (OSError, TypeError, ValueError):
            pass

        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            sleep_for = min(interval, remaining)
        else:
            sleep_for = interval
        if _obs_auto_connect_cancel.wait(timeout=sleep_for):
            return False
    return False


def start_obs_watch_from_defaults(
    *,
    wait_timeout: float | None = 30.0,
    retry_interval: float = 0.5,
) -> str | None:
    """Start OBS integration from saved settings when the opt-in is enabled.

    This function is intended to run on a daemon thread. All failures are
    converted to status text so application startup remains fail-open.
    """
    settings = load_defaults()
    if settings.get("obs_auto_connect_on_startup") is not True:
        return None

    method = str(settings.get("obs_trigger_method") or "websocket").lower()
    host = str(settings.get("obs_host") or "localhost")
    try:
        port = int(settings.get("obs_port", 4455))
    except (TypeError, ValueError):
        msg = "OBS自動連携を開始できません: WebSocket Portが不正です"
        _obs_append_status(msg)
        return msg

    if method == "websocket":
        _obs_append_status(
            f"OBS WebSocketの準備完了を待っています: {host}:{port}"
        )
        if not _wait_for_obs_websocket(
            host,
            port,
            timeout=wait_timeout,
            retry_interval=retry_interval,
        ):
            if _obs_auto_connect_cancel.is_set():
                msg = "手動操作を優先し、起動時のOBS自動連携をキャンセルしました"
            else:
                waited = (
                    f"{float(wait_timeout):g}秒"
                    if wait_timeout is not None
                    else ""
                )
                msg = (
                    f"OBS WebSocketを{waited}待ちましたが"
                    "準備完了を確認できませんでした。OBS連携タブから手動で"
                    "開始するか、WebSocketサーバー設定を確認してください"
                )
            _obs_append_status(msg)
            return msg

    with _obs_start_lock:
        if _obs_auto_connect_cancel.is_set():
            msg = "手動操作を優先し、起動時のOBS自動連携をキャンセルしました"
            _obs_append_status(msg)
            return msg
        try:
            return _start_obs_watch_impl(
                method=method,
                host=host,
                port=port,
                password="",
                save_password=bool(load_obs_password()),
                stop_event=settings.get("obs_stop_event", "record"),
                watch_folder=settings.get("obs_watch_folder", ""),
                auto_process=bool(settings.get("obs_auto_process", True)),
                auto_append_youtube=bool(
                    settings.get("auto_append_youtube", False)
                ),
                num_clips=settings.get("num_clips", 5),
                output_mode=settings.get("output_mode", "combined"),
                generate_shorts=bool(settings.get("generate_shorts", False)),
                ai_provider=settings.get("ai_provider", "gemini"),
                whisper_model=settings.get("whisper_model", "large-v3"),
                output_base_dir=settings.get("output_base_dir", ""),
            )
        except Exception as exc:
            logger.exception("OBS startup auto-connect failed")
            msg = f"OBS自動連携の開始に失敗しました: {exc}"
            _obs_append_status(msg)
            return msg


def _run_obs_auto_connect_worker(
    wait_timeout: float | None,
    retry_interval: float,
) -> None:
    """Daemon-thread target with a final fail-open safety boundary."""
    global _obs_auto_connect_thread
    try:
        start_obs_watch_from_defaults(
            wait_timeout=wait_timeout,
            retry_interval=retry_interval,
        )
    except Exception as exc:
        logger.exception("OBS auto-connect worker crashed")
        _obs_append_status(f"OBS自動連携の開始に失敗しました: {exc}")
    finally:
        with _obs_auto_connect_lock:
            if _obs_auto_connect_thread is threading.current_thread():
                _obs_auto_connect_thread = None


def schedule_obs_auto_connect(
    *,
    wait_timeout: float | None = None,
    retry_interval: float = 0.5,
) -> threading.Thread | None:
    """Wait in the background for OBS, then connect at most one watcher."""
    global _obs_auto_connect_thread
    try:
        enabled = load_defaults().get("obs_auto_connect_on_startup") is True
    except Exception as exc:
        logger.exception("OBS auto-connect settings could not be loaded")
        _obs_append_status(f"OBS自動連携の設定を読み込めませんでした: {exc}")
        return None
    if not enabled:
        return None

    with _obs_auto_connect_lock:
        if (
            _obs_auto_connect_thread is not None
            and _obs_auto_connect_thread.is_alive()
        ):
            return _obs_auto_connect_thread
        _obs_auto_connect_cancel.clear()
        try:
            thread = threading.Thread(
                target=_run_obs_auto_connect_worker,
                args=(wait_timeout, retry_interval),
                daemon=True,
                name="obs-auto-connect",
            )
            _obs_auto_connect_thread = thread
            thread.start()
            return thread
        except Exception as exc:
            _obs_auto_connect_thread = None
            logger.exception("OBS auto-connect thread could not be started")
            _obs_append_status(f"OBS自動連携を予約できませんでした: {exc}")
            return None


def _legacy_one_shot_handler(
    input_url: str,
    input_file,
    enable_clips: bool,
    clip_prompt: str,
    enable_chapters: bool,
    chapter_prompt: str,
    auto_append_youtube: bool,
    num_clips: int,
    output_mode: str,
    generate_shorts: bool,
    shorts_mode: str,
    shorts_crop: str,
    shorts_title: bool,
    generate_zip: bool,
    ai_provider: str,
    ai_model: str,
    api_key: str,
    min_duration: int,
    max_duration: int,
    whisper_model: str,
    language: str,
    font_name: str,
    font_size: int,
    font_color: str,
    upload_to_drive: bool,
    output_base_dir: str = "",
    generate_thumbnails: bool = False,
    audio_fusion: bool = False,
    audio_alpha: float = 0.35,
    karaoke: bool = False,
    progress=gr.Progress(),
):
    """Main processing pipeline for the web UI."""
    logs = []

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)

    try:
        input_value = str(input_url or "").strip()
        source_kind = "local" if input_file is not None else (
            get_url_source(input_value) or ("url" if input_value else "local")
        )
        if source_kind == "twitch":
            enable_chapters = False
            chapter_prompt = ""
        # Validate generation modes — at least one must be enabled
        modes = GenerationModes(
            enable_clips=bool(enable_clips),
            enable_shorts=bool(generate_shorts),
            enable_chapters=bool(enable_chapters),
            clip_prompt=clip_prompt or "",
            chapter_prompt=chapter_prompt or "",
        )
        try:
            modes.validate()
        except ValueError as mode_err:
            return ProcessResult(log=f"Error: {mode_err}").as_gradio_outputs()
        log(
            f"Modes: clips={modes.enable_clips}, shorts={modes.enable_shorts}, "
            f"chapters={modes.enable_chapters}"
        )
        if source_kind == "twitch":
            log("Twitch入力: タイムスタンプ生成とYouTube概要欄への追記をスキップ")

        # Pre-validate YouTube auth before starting the heavy pipeline.
        # We only want to discover an auth problem AFTER download/transcribe
        # when the user explicitly asked for the auto-append step.
        if auto_append_youtube and source_kind != "twitch":
            yt_status = youtube_api.check_auth_status()
            if not yt_status["configured"]:
                return ProcessResult(
                    log=(
                        "Error: 概要欄に自動追加が有効ですが credentials.json が未設定です。"
                        "Settings タブの『YouTube API 認証』で配置手順を確認してください。"
                    ),
                ).as_gradio_outputs()
            if not yt_status["authenticated"]:
                return ProcessResult(
                    log=(
                        "Error: YouTube 認証が切れています。Settings タブの"
                        "『YouTube API 認証』で『認証する』を押して再認証してください。"
                    ),
                ).as_gradio_outputs()
            log(f"YouTube auth pre-check: {youtube_api.auth_status_summary()}")

        # Create ONE output directory that is reused for download + processing,
        # so both the source video and the generated clips live together (and
        # are covered by a single Drive upload).
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Each run gets its own timestamped subfolder inside the effective
        # base dir resolved from the Settings-tab textbox (or defaulted to
        # <repo>/output/ when empty). Keep the resolver in one place so the
        # UI's live-updating "実際の保存先" display stays in lockstep with
        # what actually gets written to disk.
        base_dir = resolve_output_base(output_base_dir)
        output_dir = base_dir / f"output_{timestamp}"
        output_dir.mkdir(parents=True, exist_ok=True)
        log(f"Output base: {base_dir}")

        # Capture the source video ID (only meaningful for YouTube URL input).
        # We use this later for the optional auto-append-to-YouTube step.
        youtube_video_id: str | None = None
        if source_kind == "youtube":
            youtube_video_id = youtube_api.extract_video_id(input_value)
            if youtube_video_id:
                log(f"YouTube video id: {youtube_video_id}")

        # Determine input source
        if input_file is not None:
            original_path = Path(input_file)
            log(f"Local file: {original_path.name}")
            # Gradio temp paths may contain Japanese characters that break ffprobe on Windows.
            try:
                str(original_path).encode("ascii")
                video_path = original_path
            except UnicodeEncodeError:
                safe_dir = output_dir / "_safe"
                safe_dir.mkdir(parents=True, exist_ok=True)
                safe_name = f"input{original_path.suffix}"
                video_path = safe_dir / safe_name
                shutil.copy2(original_path, video_path)
                log(f"Copied to safe path: {video_path}")
        elif input_value:
            progress(0.05, desc="Downloading video...")
            video_path = download_video(input_value, output_dir / "source")
            log(f"Downloaded: {video_path.name}")
        else:
            return ProcessResult(
                log="Error: URLを入力するかファイルをアップロードしてください",
            ).as_gradio_outputs()

        # Step 1: Video info
        progress(0.1, desc="[Step 1/6] Analyzing video...")
        log(f"[Step 1/6] Analyzing video: {video_path}")
        video_info = get_video_info(video_path)
        log(f"  Resolution: {video_info['width']}x{video_info['height']}, FPS: {video_info['fps']:.2f}, Duration: {video_info['duration']:.0f}s")

        # Step 2: Transcription
        progress(0.15, desc="[Step 2/6] Transcribing audio...")
        log("[Step 2/6] Transcribing... (this may take a while)")
        segments = transcribe(video_path, whisper_model, language)
        transcript_text = segments_to_text(segments)

        transcript_path = output_dir / "transcript.txt"
        transcript_path.write_text(transcript_text, encoding="utf-8")
        log(f"  Transcription complete: {len(segments)} segments")

        # Step 3: Highlight detection
        progress(0.5, desc="[Step 3/6] Detecting highlights...")
        provider_name = {"claude": "Claude", "openai": "ChatGPT", "gemini": "Gemini"}.get(ai_provider, ai_provider)
        log(f"[Step 3/6] Analyzing with {provider_name}...")
        font_config = FontConfig(
            font_name=font_name,
            font_size=font_size,
            font_color=font_color,
        )

        highlights = detect_highlights(
            transcript_text,
            num_clips=num_clips,
            min_duration=min_duration,
            max_duration=max_duration,
            custom_prompt=modes.active_prompt,
            ai_provider=ai_provider,
            api_key=api_key,
            ai_model=ai_model,
        )

        if audio_fusion:
            alpha = float(audio_alpha if audio_alpha is not None else 0.35)
            log(f"  Applying audio excitement fusion (alpha={alpha:.2f})")
            highlights = fuse_audio_energy(
                video_path,
                highlights,
                alpha=alpha,
                min_duration=min_duration,
                max_duration=max_duration,
            )

        highlights_summary = ""
        for i, h in enumerate(highlights, 1):
            highlights_summary += f"**{i}. {h['title']}**\n"
            highlights_summary += f"   {h['start']} → {h['end']} ({h['duration']:.0f}s)\n"
            highlights_summary += f"   {h['reason']}\n\n"

        log(f"  Found {len(highlights)} highlights")

        # Steps 4–6 produce normal clips and Shorts independently, then XML.
        # A chapters-only run still keeps the earlier highlight detection result.
        clip_paths: list[Path] = []
        srt_paths: list[Path] = []
        shorts_paths: list[Path] = []
        shorts_srt_paths: list[Path] = []
        shorts_title_srt_paths: list[Path] = []
        shorts_ass_paths: list[Path] = []
        thumbnail_paths: list[Path] = []

        clips_dir = output_dir / "clips"
        shorts_dir = output_dir / "shorts"

        if modes.enable_clips:
            # Step 4: Extract clips (normal landscape, no burn-in — Premiere edits SRT separately)
            progress(0.6, desc="[Step 4/6] Extracting clips...")
            log("[Step 4/6] Extracting clips...")
            clip_paths = extract_clips(video_path, highlights, clips_dir)
            log(f"  Extracted {len(clip_paths)} clips")

            # Step 5: Subtitles for clips (SRT for Premiere captions)
            progress(0.7, desc="[Step 5/6] Generating subtitles...")
            log("[Step 5/6] Generating subtitles...")
            srt_paths = generate_all_srts(segments, highlights, clips_dir)
            log(f"  Generated {len(srt_paths)} SRT files")

        # Shorts (9:16) are independent from normal clip output.
        if modes.enable_shorts:
            progress(0.75, desc="Generating shorts (9:16) with burned-in subtitles...")
            shorts_dir.mkdir(parents=True, exist_ok=True)
            shorts_srt_paths = generate_all_srts(
                segments,
                highlights,
                shorts_dir,
                shorts=True,
            )
            shorts_title_srt_paths = generate_all_short_title_srts(
                highlights,
                shorts_dir,
            )
            if karaoke:
                shorts_ass_paths = generate_all_karaoke_ass(
                    segments, highlights, shorts_dir, font_config,
                )
            shorts_paths = extract_clips(
                video_path, highlights, shorts_dir,
                shorts=True,
                srt_paths=shorts_srt_paths,
                karaoke=bool(karaoke),
                ass_paths=shorts_ass_paths,
                font_config=font_config,
                crop_x=shorts_crop,
                shorts_mode=shorts_mode,
                shorts_title=shorts_title,
            )
            subtitle_kind = "ASS karaoke" if karaoke else "SRT"
            log(f"  Generated {len(shorts_paths)} shorts with {subtitle_kind} subtitles ({font_config.font_name} @ {font_config.font_size}pt)")
            log(
                "  Generated "
                f"{len(shorts_srt_paths) + len(shorts_title_srt_paths)} "
                "editable Short SRT files (archive + title)"
            )

        if generate_thumbnails and (modes.enable_clips or modes.enable_shorts):
            progress(0.8, desc="Generating thumbnail candidates...")
            if modes.enable_shorts:
                thumbnail_paths = generate_thumbnail_candidates(
                    video_path, highlights, shorts_dir,
                    vertical=True,
                    crop_x=shorts_crop,
                    shorts_mode=shorts_mode,
                    font_config=font_config,
                )
                log(f"  Generated {len(thumbnail_paths)} vertical thumbnail candidates")
            else:
                thumbnail_paths = generate_thumbnail_candidates(
                    video_path, highlights, clips_dir,
                    font_config=font_config,
                )
                log(f"  Generated {len(thumbnail_paths)} thumbnail candidates")

        if clip_paths or shorts_paths:
            # Step 6: Premiere Pro XML
            progress(0.85, desc="[Step 6/6] Exporting XML...")
            log("[Step 6/6] Exporting Premiere Pro XML...")
            if output_mode == "combined":
                generate_combined_xml(
                    clip_paths,
                    highlights,
                    video_info,
                    output_dir / "project.xml",
                    project_name=video_path.stem,
                    source_video_path=video_path,
                    shorts_paths=shorts_paths,
                )
                log("  Premiere Pro XML (combined mode) exported")
            else:
                generate_individual_xmls(
                    clip_paths,
                    highlights,
                    video_info,
                    clips_dir if clip_paths else shorts_dir,
                    source_video_path=video_path,
                    shorts_paths=shorts_paths,
                )
                log("  Premiere Pro XML (individual mode) exported")
        elif not modes.enable_clips and not modes.enable_shorts:
            log("[Skip 4-6] Video generation disabled — chapters-only run")

        # Google Drive upload
        drive_link = ""
        if upload_to_drive:
            progress(0.9, desc="Uploading to Google Drive...")
            if drive_is_configured():
                log("Uploading to Google Drive...")
                result = upload_output_directory(output_dir)
                drive_link = result.get("folder_link", "")
                log(f"  Google Drive: {drive_link}")
            else:
                log("Google Drive: credentials.json が未設定のためスキップ")

        # Create zip for download (optional)
        zip_path = None
        if generate_zip:
            progress(0.95, desc="Creating download archive...")
            zip_path = shutil.make_archive(str(output_dir), "zip", str(output_dir))
            log(f"  ZIP created: {zip_path}")

        # タイムスタンプ (概要欄) text — auto-chapter on upload.
        # Only generated when the chapters mode is enabled.
        chapters_text = ""
        if modes.enable_chapters:
            try:
                video_duration = float(video_info.get("duration", 0))
                chapters_text = generate_chapter_text(highlights, video_duration=video_duration)
                chapters_path = output_dir / "chapters.txt"
                write_chapter_file(highlights, chapters_path, video_duration=video_duration)
                log(f"Chapters saved: {chapters_path}")
            except Exception as ch_err:
                log(f"Chapter generation failed: {ch_err}")
        else:
            reason = (
                "Twitch入力ではタイムスタンプを生成しません"
                if source_kind == "twitch"
                else "タイムスタンプ (概要欄) 生成を無効化"
            )
            log(f"[Skip chapters] {reason}")

        # Auto-append to YouTube video description.
        # Only runs when: chapters generated AND user enabled it AND we have a
        # video id (URL input, not a local file upload).
        if auto_append_youtube and source_kind == "youtube" and modes.enable_chapters and chapters_text:
            if not youtube_video_id:
                log("[Skip auto-append] URL 入力ではないため YouTube 概要欄への自動追記はスキップ")
            elif not youtube_api.is_configured():
                log("[Skip auto-append] credentials.json 未設定のため YouTube 概要欄への自動追記はスキップ")
            else:
                progress(0.97, desc="YouTube 概要欄に自動追加中...")
                try:
                    yt_service = youtube_api.get_youtube_service()
                    youtube_api.update_video_description(
                        yt_service, youtube_video_id, chapters_text, position="prepend",
                    )
                    log(f"  YouTube 概要欄に自動追加: video_id={youtube_video_id}")
                except Exception as yt_err:
                    tb = traceback.format_exc()
                    logger.error(f"YouTube 概要欄更新失敗: {yt_err}\n{tb}")
                    log(f"  YouTube 概要欄更新失敗: {yt_err} (他の出力は維持)")

        log(f"\nDone! Output: {output_dir}")

        return ProcessResult(
            log="\n".join(logs),
            highlights=highlights_summary,
            download_path=zip_path,
            drive_link=drive_link,
            chapters_text=chapters_text,
        ).as_gradio_outputs()

    except subprocess.CalledProcessError as e:
        err_detail = f"Command failed: {e.cmd}\nReturn code: {e.returncode}"
        if e.stdout:
            err_detail += f"\nstdout: {e.stdout[:500]}"
        if e.stderr:
            err_detail += f"\nstderr: {e.stderr[:500]}"
        logger.error(err_detail)
        log(f"\nError (subprocess): {err_detail}")
        return ProcessResult(log="\n".join(logs)).as_gradio_outputs()
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Error: {e}\n{tb}")
        log(f"\nError: {e}")
        log(tb)
        return ProcessResult(log="\n".join(logs)).as_gradio_outputs()


# Gradio 6.0 moved `theme` / `css` from the Blocks constructor to launch().
# Keep them as module-level constants so every launch() call (web_app + launcher)
# applies the same look without re-triggering the deprecation warning.
APP_THEME = gr.themes.Soft()
APP_CSS = """
        .main-title { text-align: center; margin-bottom: 0.5em; }
        .subtitle { text-align: center; color: #666; margin-bottom: 1.5em; }
        .obs-password-heading {
            align-items: center;
            flex-wrap: wrap;
            gap: 0.75rem;
            margin-bottom: 0.35rem;
        }
        .obs-password-title {
            flex: 1 1 12rem;
            min-width: 12rem;
        }
        .obs-password-title span {
            display: inline-flex;
            padding: 0.3rem 0.55rem;
            border-radius: 0.5rem;
            background: var(--primary-500, #4f46e5);
            color: white;
            font-weight: 600;
            line-height: 1.35;
        }
        .obs-password-save {
            flex: 0 0 auto !important;
            width: max-content !important;
            min-width: 0 !important;
        }
        .obs-password-save label {
            margin: 0 !important;
            white-space: nowrap;
        }
        .gradio-container {
            --input-workspace-gap: 1rem;
            --input-source-settings-gap: 1.25rem;
            --input-source-tint: color-mix(
                in srgb,
                var(--block-background-fill) 90%,
                var(--primary-500) 10%
            );
            --input-source-border: color-mix(
                in srgb,
                var(--border-color-primary) 65%,
                var(--primary-500) 35%
            );
        }
        .input-source-row,
        .input-settings-grid {
            align-items: stretch;
            gap: var(--input-workspace-gap);
        }
        .input-settings-grid {
            margin-top: var(--input-source-settings-gap);
        }
        .input-source-control {
            background: var(--input-source-tint) !important;
            border-color: var(--input-source-border) !important;
        }
        .input-url-column,
        .input-file-column,
        .input-core-settings-column,
        .input-shorts-settings-column,
        .input-actions-column,
        .obs-trigger-column,
        .obs-connection-settings-column,
        .obs-connection-actions-column {
            min-width: 0 !important;
        }
        .input-settings-grid input[type="number"] {
            -moz-appearance: textfield;
        }
        .input-settings-grid input[type="number"]::-webkit-inner-spin-button,
        .input-settings-grid input[type="number"]::-webkit-outer-spin-button {
            -webkit-appearance: none;
            margin: 0;
        }
        .input-settings-title {
            margin: 0.1rem 0 -0.15rem !important;
        }
        .input-core-settings-column > .input-settings-title,
        .input-shorts-settings-column > .input-settings-title {
            overflow: visible !important;
        }
        .input-settings-title h3 {
            font-size: 1rem !important;
            line-height: 1.4 !important;
            margin: 0 !important;
        }
        .input-actions-column {
            margin-top: 0.5rem;
        }
        @media (min-width: 900px) {
            .obs-connection-workspace {
                display: grid !important;
                grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
                grid-template-areas: "trigger connection" "actions connection";
                grid-template-rows: max-content 1fr;
                align-items: start;
                gap: var(--input-workspace-gap);
            }
            .obs-trigger-column {
                grid-area: trigger;
            }
            .obs-connection-settings-column {
                grid-area: connection;
            }
            .obs-connection-actions-column {
                grid-area: actions;
            }
        }
        @media (max-width: 899px) {
            .input-source-row,
            .input-settings-grid,
            .obs-connection-workspace {
                flex-direction: column !important;
            }
            .input-url-column,
            .input-file-column,
            .input-core-settings-column,
            .input-shorts-settings-column,
            .input-actions-column,
            .obs-trigger-column,
            .obs-connection-settings-column,
            .obs-connection-actions-column {
                flex: 1 1 auto !important;
                min-width: 0 !important;
                width: 100% !important;
            }
        }
        footer { display: none !important; }
        a[href*="gradio.app"] { display: none !important; }
        """

GEMINI_API_KEY_GUIDE_MD = """
**約2分・無料枠あり。** クレジットカード登録は不要です。

1. [Google AI Studio の API Keys](https://aistudio.google.com/apikey) を開いてログイン
2. **[APIキーを作成]** をクリック（プロジェクトは新規作成でOK）
3. 表示されたキーを **[コピー]**
4. 上の「APIキー」欄へ貼り付け、**[💾 このキーを保存]** をクリック

> Gemini APIキーはAI分析用です。YouTube・Drive用の `credentials.json` とは別物です。
> キーは他人に見せないでください。`429` エラー時は少し待って再実行します。

詳しい画面説明が必要な場合は、アプリフォルダの `SETUP_GUIDE.html` を開いてください。
"""

GOOGLE_CREDENTIALS_SETUP_GUIDE_MD = """
### Google Drive / YouTube の準備

**初回だけ必要です。YouTubeとDriveは同じJSONを使えます。**

1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクトを作成し、そのプロジェクトへ切り替える
2. [APIライブラリ](https://console.cloud.google.com/apis/library) で **YouTube Data API v3** を有効化（Driveも使うなら **Google Drive API** も有効化）
3. [Google Auth Platform](https://console.cloud.google.com/auth/overview) の初期設定で、対象を **「外部」** にして作成
4. [対象ページ](https://console.cloud.google.com/auth/audience) の「テストユーザー」から、認証に使うGmailを追加
5. [クライアント](https://console.cloud.google.com/auth/clients) で **[クライアントを作成] → [デスクトップ アプリ]** を選び、作成直後に **[JSON をダウンロード]**
6. ダウンロードしたJSONを下の欄へドロップし、**[認証する]** をクリック

> `credentials.json` は機密情報です。共有・公開しないでください。
> Gemini APIキーとは別物です。迷った場合は `SETUP_GUIDE.html`、詳しいトラブル対処は `CREDENTIALS_SETUP.txt` を参照してください。
"""

GOOGLE_OAUTH_UNVERIFIED_GUIDE_MD = """
#### 「このアプリは Google で確認されていません」と表示された場合

自分で作成した OAuth クライアントを、自分の Google アカウントで使っている場合は、
テスト中にこの警告が表示されることがあります。

1. Google の警告画面左下にある **[詳細]** をクリック
2. `[アプリ名] に移動（安全ではないページ）` をクリック
3. アクセス内容を確認し、問題なければ **[続行]** または **[許可]** をクリック

> 自分で作成した覚えのないアプリでは進まず、**[安全なページに戻る]** を選んでください。

#### 進めない／「アクセスをブロック」「403: access_denied」と表示された場合

認証に使う Gmail がテストユーザーに登録されているか確認します。

1. [Google Auth Platform の「対象」ページを開く](https://console.cloud.google.com/auth/audience)
2. 画面上部で、`credentials.json` を作成した**正しいプロジェクト**を選択
3. 公開ステータスが **「テスト」** であることを確認
4. 「テストユーザー」で **[+ ユーザーを追加]** をクリック
5. 認証に使う Gmail アドレスを入力して **[保存]**
6. Google のエラー画面を閉じ、このアプリで **[認証する]** をもう一度クリック

「テストユーザー」欄がない場合は、公開ステータスが「本番」になっていないか確認してください。
本番ではテストユーザーの追加は不要です。
"""


def _named_params(func):
    """Return the set of explicitly-named parameters of ``func``.

    Returns ``None`` when the signature cannot be introspected, so callers can
    treat that as "unknown — don't filter". Never raises: signature
    introspection runs at import time and a failure here must not brick the app.
    """
    try:
        return set(inspect.signature(func).parameters)
    except (ValueError, TypeError):
        return None


def _split_theme_kwargs():
    """Route theme/css to wherever the installed Gradio version accepts them.

    Gradio moved ``theme``/``css`` between ``gr.Blocks()`` and ``launch()``
    across major versions (older: Blocks constructor; some 6.x: launch()).
    Mismatching the location raises ``TypeError: ... unexpected keyword
    argument 'theme'`` at launch. We introspect both signatures and prefer
    the Blocks constructor when it accepts the param, falling back to
    launch(); if neither names it the param is dropped (default look, with a
    warning) rather than crashing.
    """
    blocks_params = _named_params(gr.Blocks.__init__)
    launch_params = _named_params(gr.Blocks.launch)
    blocks_kwargs, launch_kwargs = {}, {}
    for name, value in (("theme", APP_THEME), ("css", APP_CSS)):
        if blocks_params is not None and name in blocks_params:
            blocks_kwargs[name] = value
        elif launch_params is not None and name in launch_params:
            launch_kwargs[name] = value
        else:
            logger.warning(
                f"gradio {gr.__version__}: '{name}' not accepted by Blocks() "
                f"nor launch(); using default appearance."
            )
    return blocks_kwargs, launch_kwargs


def safe_launch_kwargs(**kwargs):
    """Drop ``launch()`` kwargs the installed Gradio version doesn't accept.

    The reported crash was a version-skewed ``launch()`` kwarg (``theme``).
    Other kwargs we pass (``ssr_mode``, ``inbrowser``) are equally version
    -dependent, so filter every keyword against the live ``launch`` signature.
    When the signature can't be introspected, or exposes ``**kwargs``, pass
    everything through unchanged.
    """
    params = _named_params(gr.Blocks.launch)
    if params is None:
        return kwargs
    try:
        has_var_kw = any(
            p.kind == p.VAR_KEYWORD
            for p in inspect.signature(gr.Blocks.launch).parameters.values()
        )
    except (ValueError, TypeError):
        has_var_kw = False
    if has_var_kw:
        return kwargs
    accepted = {k: v for k, v in kwargs.items() if k in params}
    dropped = sorted(set(kwargs) - set(accepted))
    if dropped:
        logger.warning(
            f"gradio {gr.__version__}: launch() ignores unsupported kwargs {dropped}."
        )
    return accepted


# Computed once at import time; consumed by create_ui() (Blocks) and every
# launch() call site (web_app __main__ + launcher.py).
BLOCKS_THEME_KWARGS, LAUNCH_THEME_KWARGS = _split_theme_kwargs()


def _startup_auth_status_for_ui() -> str:
    """Full auth probe, run per page load off the server-startup path.

    check_auth_status() may perform a silent network token refresh and
    imports the heavy google stack on first use, so it must not run
    synchronously while create_ui() builds the Blocks graph (that would
    block every client's first paint on network I/O). Wiring this as an
    app.load() handler instead runs it once per page load, after the UI
    is already visible. Also feeds the console log (replacing the old
    startup-thread probe) so both surfaces come from one check.
    """
    try:
        summary = youtube_api.auth_status_summary()
    except Exception as e:
        logger.warning(f"YouTube auth startup probe failed: {e}")
        return f"確認失敗: {e}"
    logger.info(f"YouTube auth: {summary}")
    return summary


def create_ui():
    """Create the Gradio web interface."""
    defaults = load_defaults()
    obs_processing_defaults = _obs_processing_settings_from_defaults(defaults)

    with gr.Blocks(
        title="Clip Extractor - 配信切り抜き自動生成",
        analytics_enabled=False,
        **BLOCKS_THEME_KWARGS,
    ) as app:
        gr.HTML("<h1 class='main-title'>Clip Extractor</h1>")
        gr.HTML("<p class='subtitle'>YouTube / Twitch配信アーカイブから切り抜きショート動画を自動生成</p>")

        with gr.Tabs():
            # --- Input Tab ---
            with gr.Tab("Input / 入力"):
                # Normal clips, Shorts, and timestamps are independent outputs.
                # Any video output uses the clip-side prompt for detection.
                gr.HTML("<h3>生成モード / Generation Modes（アーカイブ入力用）</h3>")
                gr.HTML(
                    "<p style='color:#666; margin-top:-0.5em; margin-bottom:0.5em;'>"
                    "切り抜き動画・ショート動画・タイムスタンプのうち少なくとも1つを"
                    "有効にしてください。動画を生成する場合は切り抜き用プロンプトが"
                    "使われます。</p>"
                )
                with gr.Row():
                    with gr.Column():
                        enable_clips = gr.Checkbox(
                            label="切り抜き動画を生成",
                            value=defaults.get("enable_clips", True),
                            info="クリップ抽出 + SRT + Premiere XML を出力",
                        )
                        clip_prompt = gr.Textbox(
                            label="切り抜き用プロンプト (任意)",
                            value=defaults.get("clip_prompt", ""),
                            placeholder="例: 面白いシーンだけ選んで、ゲーム実況の名場面を中心に",
                            lines=2,
                        )
                    with gr.Column():
                        enable_chapters = gr.Checkbox(
                            label="タイムスタンプ(概要欄)を生成",
                            value=defaults.get("enable_chapters", True),
                            info="YouTube 自動チャプター有効の 0:00 形式テキストを出力",
                        )
                        chapter_prompt = gr.Textbox(
                            label="タイムスタンプ用プロンプト (任意)",
                            value=defaults.get("chapter_prompt", ""),
                            placeholder="例: 話題が切り替わる節目だけを抜き出して",
                            lines=2,
                            info="切り抜き動画とショート動画が両方無効のときだけ使われます",
                        )
                        auto_append_youtube = gr.Checkbox(
                            label="概要欄に自動追加 (YouTube)",
                            value=defaults.get("auto_append_youtube", False),
                            info="URL入力時のみ有効。初回は credentials.json 配置 + ブラウザ認証が必要",
                        )
                        gr.Markdown(
                            """
<details>
<summary>💡 <b>推奨フロー</b> — 初めて使う時はこれ (クリックで展開)</summary>

**認証が通っている = 自動追加される状態** ではありません。
上のチェックボックスが ON で、かつ Generate を押した時にだけ 1 回追記されます。
最初の 1〜2 本は以下の順で試すのがおすすめ:

1. **まず上のチェックは OFF のまま Generate**
   → `output_*/chapters.txt` に書かれたタイムスタンプを目視確認
   (プロンプト次第でイマイチな章立てになる場合あり)
2. **内容 OK なら、上の ☑ を ON に戻して同じ URL で再 Generate**
   → YouTube 側の概要欄先頭に追記される
3. もし結果が気に入らなかった場合:
   - YouTube Studio で該当動画の概要欄を直接編集して戻す
   - または `タイムスタンプ用プロンプト` を調整して再実行
     (※ 再実行するたびに先頭に prepend されるので、手動で古い分を削除してからがおすすめ)

**注意点**:
- 対象は**自分がアップロードした動画のみ** (`youtube.force-ssl` scope の制限)。切り抜きや他人の動画には追記不可
- ローカル mp4 を投げた場合は自動スキップ (URL 入力が必須)
- 追記は「既存の概要欄の先頭に prepend」。既存本文は消えません
- 1 回の追記で YouTube クォータを 50 units 消費 (1日 10,000 units で 約 200 本)

</details>
"""
                        )

                with gr.Row(elem_classes="input-source-row"):
                    with gr.Column(
                        scale=1,
                        elem_classes="input-url-column",
                    ):
                        input_url = gr.Textbox(
                            label="動画URL（YouTube / Twitch）",
                            placeholder="https://youtube.com/... または https://twitch.tv/videos/...",
                            info="YouTube/TwitchのURLを貼り付けると自動でダウンロードします。Twitch入力ではタイムスタンプを生成しません",
                            elem_classes="input-source-control",
                        )
                    with gr.Column(
                        scale=1,
                        elem_classes="input-file-column",
                    ):
                        input_file = gr.File(
                            label="ローカルファイル",
                            file_types=["video"],
                            type="filepath",
                            height=128,
                            elem_classes="input-source-control",
                        )

                with gr.Row(elem_classes="input-settings-grid"):
                    with gr.Column(
                        scale=1,
                        elem_classes="input-core-settings-column",
                    ):
                        gr.Markdown(
                            "### クリップ・出力設定",
                            elem_classes="input-settings-title",
                        )
                        num_clips = gr.Number(
                            minimum=1, maximum=50, value=defaults["num_clips"],
                            precision=0,
                            label="クリップ数",
                            info="1〜50 個。大きくしすぎると面白くないシーンも混ざりやすくなります (推奨: 3〜10)",
                        )
                        with gr.Row():
                            min_duration = gr.Number(
                                label="最小クリップ長 (秒)",
                                value=defaults["min_duration"],
                                precision=0,
                            )
                            max_duration = gr.Number(
                                label="最大クリップ長 (秒)",
                                value=defaults["max_duration"],
                                precision=0,
                            )
                        output_mode = gr.Radio(
                            choices=["combined", "individual"],
                            value=defaults.get("output_mode", "combined"),
                            label="出力モード",
                            info="combined: 1つのXMLに全シーケンス / individual: クリップごとに別XML",
                        )
                        generate_thumbnails = gr.Checkbox(
                            label="サムネイル候補を生成 / Generate thumbnail candidates",
                            value=defaults.get("generate_thumbnails", False),
                            info="各クリップからタイトル入りの代表フレーム画像を生成します",
                        )
                        audio_fusion = gr.Checkbox(
                            label="音声盛り上がり融合 / Audio excitement fusion",
                            value=defaults.get("audio_fusion", False),
                            info="音量や急な盛り上がりを使ってクリップ順位を再調整します / Re-rank clips using loudness and sudden audio peaks",
                        )
                        audio_alpha = gr.Slider(
                            0.0, 1.0,
                            value=defaults.get("audio_alpha", 0.35),
                            step=0.05,
                            label="音声重み alpha / Audio weight",
                        )
                        generate_zip = gr.Checkbox(
                            label="ZIPファイルを生成",
                            value=False,
                            info="出力をZIPにまとめてダウンロード可能にする",
                        )
                        upload_to_drive = gr.Checkbox(
                            label="Google Drive にアップロード",
                            value=False,
                            info="要: credentials.json の設定",
                        )

                    with gr.Column(
                        scale=1,
                        elem_classes="input-shorts-settings-column",
                    ):
                        gr.Markdown(
                            "### ショート動画設定",
                            elem_classes="input-settings-title",
                        )
                        generate_shorts = gr.Checkbox(
                            label="ショート動画 (9:16) を生成",
                            value=defaults.get("generate_shorts", False),
                            info="通常の切り抜きがOFFでも生成できます。字幕入り縦型クリップを shorts/ に出力します",
                        )
                        shorts_mode = gr.Radio(
                            choices=["pad", "blur", "crop"],
                            value=defaults.get("shorts_mode", "pad"),
                            label="ショート動画の変換モード",
                            info="pad（推奨）: 上下を黒帯にして全体表示 / blur: 全体表示＋ぼかし背景 / crop: 左右を切って拡大",
                        )
                        shorts_blur_strength = gr.Slider(
                            minimum=0,
                            maximum=50,
                            step=1,
                            value=defaults.get("shorts_blur_strength", 20),
                            label="背景のぼかし強度",
                            info="blurモードの背景だけに反映。0=ぼかしなし / 50=強いぼかし",
                            visible=defaults.get("shorts_mode", "pad") == "blur",
                        )
                        shorts_mode.change(
                            fn=shorts_blur_visibility,
                            inputs=shorts_mode,
                            outputs=shorts_blur_strength,
                        )
                        shorts_crop = gr.Radio(
                            choices=["center", "left", "right"],
                            value=defaults.get("shorts_crop", "center"),
                            label="ショート動画のクロップ位置",
                            info="crop モードで縦型に切り出す時の横位置。center=中央 / left=左寄せ / right=右寄せ",
                        )
                        shorts_title = gr.Checkbox(
                            label="ショート冒頭にタイトルを表示",
                            value=defaults.get("shorts_title", True),
                            info="各ショートの最初の4秒だけタイトルを焼き込みます",
                        )
                        shorts_title_position = gr.Radio(
                            choices=[
                                ("上側の帯", "top"),
                                ("下側の帯", "bottom"),
                                ("クリップ上に重ねる", "overlay"),
                            ],
                            value=defaults.get("shorts_title_position", "top"),
                            label="タイトルの配置",
                            info="pad/blurでは上下の余白か映像中央を選択。cropでは画面上部・下部・中央になります",
                        )
                        karaoke = gr.Checkbox(
                            label="ワード単位カラオケ字幕 / Word-level karaoke captions",
                            value=defaults.get("karaoke", False),
                            info="ショート動画の焼き込み字幕を単語ごとにハイライトします / Highlight burned-in Shorts captions word by word",
                        )

                with gr.Column(elem_classes="input-actions-column"):
                    with gr.Row():
                        detect_btn = gr.Button(
                            "STEP 1：AIがおすすめ箇所を抽出",
                            variant="primary",
                            size="lg",
                        )
                        render_btn = gr.Button(
                            "STEP 2：クリップを書き出し",
                            variant="secondary",
                            size="lg",
                        )

                    auto_run_both = gr.Checkbox(
                        label="STEP 1 のあと STEP 2 まで自動で実行する",
                        value=False,
                        info="チェックすると、AI抽出 (STEP 1) が終わり次第そのままクリップ書き出し (STEP 2) まで一気に進めます。レビューで手直ししたい場合はオフのままにしてください。",
                    )

                    with gr.Row():
                        input_save_defaults_btn = gr.Button(
                            "現在のInput設定をデフォルトに保存",
                            variant="secondary",
                            size="sm",
                        )
                        input_save_defaults_msg = gr.Textbox(
                            label="",
                            interactive=False,
                            show_label=False,
                            lines=1,
                        )

                session_state = gr.State({})
                highlights_state = gr.State([])
                premiere_job_state = gr.State(None)

                with gr.Group(visible=False) as review_panel:
                    gr.Markdown("## クリップレビュー / Clip Review")
                    status = gr.Markdown("")

                    @gr.render(inputs=highlights_state)
                    def render_review_rows(highlights):
                        for idx, highlight in enumerate(highlights or []):
                            video_duration = float(highlight.get("_video_duration") or 0.0)
                            start_value = float(highlight.get("start_sec", 0.0))
                            end_value = float(highlight.get("end_sec", start_value))
                            title_value = highlight.get("title", "")

                            with gr.Row():
                                with gr.Column(scale=2):
                                    preview_video = gr.Video(
                                        label=f"Preview {idx + 1} / プレビュー {idx + 1}",
                                        interactive=False,
                                    )
                                    preview_btn = gr.Button(
                                        "このクリップをプレビュー / Preview this clip",
                                        variant="secondary",
                                    )
                                with gr.Column(scale=3):
                                    with gr.Row():
                                        start_input = gr.Number(
                                            label="開始秒 / Start sec",
                                            value=start_value,
                                            precision=3,
                                        )
                                        end_input = gr.Number(
                                            label="終了秒 / End sec",
                                            value=end_value,
                                            precision=3,
                                        )
                                    seek_slider = gr.Slider(
                                        0,
                                        video_duration,
                                        value=start_value,
                                        step=0.1,
                                        label="粗調整 / Coarse seek",
                                    )
                                    title_input = gr.Textbox(
                                        label="タイトル / Title",
                                        value=title_value,
                                        lines=1,
                                    )

                            edit_inputs = [session_state, start_input, end_input, title_input]
                            edit_outputs = [session_state]
                            start_input.change(
                                fn=lambda session, start, end, title, i=idx: _apply_review_edit_event_session_only(session, i, start, end, title),
                                inputs=edit_inputs,
                                outputs=edit_outputs,
                            )
                            end_input.change(
                                fn=lambda session, start, end, title, i=idx: _apply_review_edit_event_session_only(session, i, start, end, title),
                                inputs=edit_inputs,
                                outputs=edit_outputs,
                            )
                            title_input.input(
                                fn=lambda session, start, end, title, i=idx: _apply_review_edit_event_session_only(session, i, start, end, title),
                                inputs=edit_inputs,
                                outputs=edit_outputs,
                            )
                            title_input.change(
                                fn=lambda session, start, end, title, i=idx: _apply_review_edit_event_session_only(session, i, start, end, title),
                                inputs=edit_inputs,
                                outputs=edit_outputs,
                            )
                            seek_slider.change(
                                fn=lambda session, seek, end, title, i=idx: _apply_review_edit_event_session_only(session, i, seek, end, title),
                                inputs=[session_state, seek_slider, end_input, title_input],
                                outputs=edit_outputs,
                            )
                            preview_btn.click(
                                fn=lambda session, start, end, i=idx: render_preview_clip(session, i, start, end),
                                inputs=[session_state, start_input, end_input],
                                outputs=preview_video,
                                concurrency_limit=1,
                            )

            # --- OBS連携 Tab ---
            with gr.Tab("OBS連携 / OBS"):
                with gr.Accordion(
                    "配信終了で自動切り抜き — 設定手順・動作説明",
                    open=False,
                ):
                    gr.Markdown(
                        "**OBS録画を既定の素材**としてすぐ処理し、録画を取得できなかった時だけ"
                        "YouTubeの完成アーカイブを保険として使います。タイムスタンプ生成がONなら、"
                        "録画から処理できた場合も同じ配信のYouTube概要欄へ反映します。\n\n"
                        "#### ① OBSで「配信と同時に録画」を設定（最初に1回）\n"
                        "1. OBSの **設定 → 出力** を開き、**出力モードを「詳細」** にする\n"
                        "2. **録画** タブで録画出力先を確認し、録画フォーマットを"
                        " **MKV（推奨）** にする\n"
                        "3. 録画エンコーダーを **「配信エンコーダーを使用」"
                        "（Use stream encoder）** にする\n"
                        "4. OBSの **設定 → 一般 → 出力** で"
                        " **「配信時に自動的に録画する」** をONにする\n"
                        "5. 一度テスト配信し、配信開始と同時に録画タイマーも動き、終了後に"
                        "録画出力先へファイルができることを確認する\n\n"
                        "> 配信と録画の開始時刻を合わせるため、自動録画をONにしてください。"
                        "設定の参考: [OBS Standard Recording Output Guide]"
                        "(https://obsproject.com/kb/standard-recording-output-guide) / "
                        "[OBS Studio Overview](https://obsproject.com/kb/obs-studio-overview)\n\n"
                        "#### ② OBS WebSocketを有効化（推奨方式）\n"
                        "1. OBSメニュー → **ツール → WebSocketサーバー設定** を開く\n"
                        "2. **「WebSocketサーバーを有効にする」** をONにする\n"
                        "3. **「接続情報を表示」** でサーバーIP・ポート（既定4455）・"
                        "パスワードを確認して適用する\n\n"
                        "> OBSが起動しているだけでは接続されません。WebSocketサーバーも"
                        "有効にしてください。\n\n"
                        "#### ③ このタブの設定\n"
                        "下の **Host / Port / Password** を OBS の接続情報と同じ値にして、"
                        "**「OBS連携 開始」** を押してください"
                        "(同じ PC なら Host は `localhost` のまま、Port は `4455`)。\n\n"
                        "切り抜き数・長さ・ショート動画の設定は、下の"
                        " **OBS自動処理用の切り抜き設定** で Input とは別に指定できます。\n\n"
                        "- **record（既定）**: OBS録画をすぐ処理。配信終了後60秒以内に"
                        "安定した録画が見つからない、または録画処理が失敗した時だけ、"
                        "再エンコード完了後のYouTubeアーカイブをDLして処理します\n"
                        "- 録画処理が成功した場合、アーカイブをDLし直さず、録画から生成した"
                        "タイムスタンプだけをYouTube概要欄へ自動反映します\n"
                        "- **stream**: OBS録画を使わず、YouTube完成アーカイブだけを処理します\n"
                        "- WebSocketの自動処理には Settings の **YouTube認証** が必要です。"
                        "アーカイブは **公開または限定公開** にしてください\n"
                        "- 完成アーカイブ待機は最大6時間です。超えた場合は後日 **Input** "
                        "タブへ完成アーカイブURLを貼って生成できます\n"
                        "- 配信直後のpost-live DVR（再エンコード前映像）は使用しません\n"
                        "- 自動処理ONの `record` では、タイムスタンプ生成がONの場合に"
                        "概要欄への反映を自動的に有効化します\n\n"
                        "#### フォルダ監視方式(WebSocket を使わない代替)\n"
                        "**検知方式** を `folder` にして OBS の録画出力先フォルダを指定すると、"
                        "新規動画ファイルの書き込み完了を検知して自動処理します"
                        "(OBS WebSocket 不要)。"
                        "`folder` はローカル録画専用で、配信を特定できないため、"
                        "アーカイブへの"
                        "フォールバックとYouTube概要欄への自動反映は行いません。\n\n"
                        "> OBS Studioの同時起動と起動時の自動連携は、"
                        " **Settings / 設定 → OBS Studio 起動・自動連携** "
                        "でそれぞれON/OFFできます。"
                    )
                with gr.Row(elem_classes="obs-connection-workspace"):
                    with gr.Column(
                        scale=1,
                        elem_classes="obs-trigger-column",
                    ):
                        obs_trigger_radio = gr.Radio(
                            ["websocket", "folder"],
                            label="検知方式 / Trigger",
                            value=defaults.get("obs_trigger_method", "websocket"),
                            info="websocket=OBS WebSocket, folder=フォルダ監視",
                        )
                        obs_stop_event_radio = gr.Radio(
                            [
                                (
                                    "OBS録画優先：失敗時のみ完成アーカイブ",
                                    "record",
                                ),
                                (
                                    "YouTube完成アーカイブのみ：再エンコード後",
                                    "stream",
                                ),
                            ],
                            label="自動処理の取得元",
                            value=defaults.get("obs_stop_event", "record"),
                            info=(
                                "推奨: record=OBS録画を即処理し、失敗時だけ"
                                "完成アーカイブを使用"
                            ),
                        )
                        obs_auto_process = gr.Checkbox(
                            label="検知後に自動で切り抜き/チャプター生成まで実行",
                            value=bool(defaults.get("obs_auto_process", True)),
                        )
                    with gr.Column(
                        scale=1,
                        elem_classes="obs-connection-settings-column",
                    ):
                        obs_host = gr.Textbox(
                            label="WebSocket Host",
                            value=defaults.get("obs_host", "localhost"),
                        )
                        obs_port = gr.Number(
                            label="WebSocket Port",
                            value=defaults.get("obs_port", 4455),
                            precision=0,
                        )
                        (
                            obs_password_placeholder,
                            obs_password_info,
                        ) = _obs_password_ui_copy(bool(load_obs_password()))
                        with gr.Row(elem_classes="obs-password-heading"):
                            gr.HTML(
                                "<span>WebSocket Password</span>",
                                container=False,
                                padding=False,
                                elem_classes="obs-password-title",
                            )
                            obs_save_password = gr.Checkbox(
                                label="Passwordを保存",
                                value=True,
                                container=False,
                                scale=0,
                                min_width=0,
                                elem_classes="obs-password-save",
                            )
                        obs_password = gr.Textbox(
                            label="WebSocket Password",
                            show_label=False,
                            value="",
                            type="password",
                            placeholder=obs_password_placeholder,
                            info=obs_password_info,
                        )
                        obs_watch_folder = gr.Textbox(
                            label="録画出力フォルダ (folder 方式 / またはパス補完用)",
                            value=defaults.get("obs_watch_folder", ""),
                            info=(
                                "ボタンから選ぶか、folder 方式で監視する"
                                "フォルダの絶対パスを直接入力します。"
                            ),
                        )
                        obs_browse_folder_btn = gr.Button(
                            "📁 録画出力フォルダを選択…",
                            variant="secondary",
                        )
                        obs_browse_folder_btn.click(
                            fn=pick_obs_watch_folder_dialog,
                            inputs=obs_watch_folder,
                            outputs=obs_watch_folder,
                        )

                    with gr.Column(
                        scale=1,
                        elem_classes="obs-connection-actions-column",
                    ):
                        with gr.Row():
                            obs_start_btn = gr.Button(
                                "OBS連携 開始",
                                variant="primary",
                            )
                            obs_stop_btn = gr.Button("OBS連携 停止")
                            obs_refresh_btn = gr.Button("状態を更新")

                        obs_status_box = gr.Textbox(
                            label="OBS連携ステータス",
                            lines=8,
                            interactive=False,
                            value="",
                        )

                with gr.Accordion(
                    "OBS自動処理の生成設定",
                    open=True,
                ):
                    gr.Markdown(
                        "Inputタブのアーカイブ用設定とは別に保存されます。"
                        "OBS連携開始時に保存され、次回の起動時自動連携でも使われます。"
                        "切り抜き・ショート・タイムスタンプは個別にON/OFFできます。"
                        "3つのうち少なくとも1つはONにしてください。"
                    )
                    with gr.Row():
                        with gr.Column():
                            obs_enable_clips = gr.Checkbox(
                                label="切り抜き動画を生成",
                                value=obs_processing_defaults["enable_clips"],
                                info="OBS録画から切り抜きを生成します",
                            )
                            obs_clip_prompt = gr.Textbox(
                                label="切り抜き用プロンプト (任意)",
                                value=obs_processing_defaults["clip_prompt"],
                                placeholder="例: 面白いシーンだけ選んで、ゲーム実況の名場面を中心に",
                                lines=2,
                            )
                            obs_enable_chapters = gr.Checkbox(
                                label="タイムスタンプ(概要欄)を生成",
                                value=obs_processing_defaults["enable_chapters"],
                                info="配信終了後にYouTube概要欄へ反映します",
                            )
                            obs_chapter_prompt = gr.Textbox(
                                label="タイムスタンプ用プロンプト (任意)",
                                value=obs_processing_defaults["chapter_prompt"],
                                placeholder="例: 話題が切り替わる節目だけを抜き出して",
                                lines=2,
                                info="切り抜き動画とショート動画が両方無効のときだけ使われます",
                            )
                            obs_auto_append_youtube = gr.Checkbox(
                                label="概要欄に自動追加 (YouTube)",
                                value=obs_processing_defaults["auto_append_youtube"],
                                info="配信アーカイブの概要欄へ生成したタイムスタンプを追加します",
                            )
                            obs_auto_start_without_prompt_confirmation = gr.Checkbox(
                                label="プロンプトの入力を確認しないで自動で生成開始",
                                value=not bool(
                                    obs_processing_defaults[
                                        "confirm_before_auto_process"
                                    ]
                                ),
                                info=(
                                    "ONの場合は確認を表示せず、保存済みプロンプトを使って"
                                    "開始します。未入力ならLLMにお任せします"
                                ),
                            )
                        with gr.Column():
                            obs_num_clips = gr.Number(
                                minimum=1,
                                maximum=50,
                                value=obs_processing_defaults["num_clips"],
                                precision=0,
                                label="クリップ数",
                                info="OBS自動処理で生成する個数（1〜50）",
                            )
                            with gr.Row():
                                obs_min_duration = gr.Number(
                                    label="最小クリップ長 (秒)",
                                    value=obs_processing_defaults["min_duration"],
                                    precision=0,
                                )
                                obs_max_duration = gr.Number(
                                    label="最大クリップ長 (秒)",
                                    value=obs_processing_defaults["max_duration"],
                                    precision=0,
                                )
                            obs_output_mode = gr.Radio(
                                choices=["combined", "individual"],
                                value=obs_processing_defaults["output_mode"],
                                label="出力モード",
                                info="combined: 1つのXMLに全シーケンス / individual: クリップごとに別XML",
                            )

                    with gr.Row():
                        with gr.Column():
                            obs_generate_shorts = gr.Checkbox(
                                label="ショート動画 (9:16) を生成",
                                value=obs_processing_defaults["generate_shorts"],
                                info="通常の切り抜きがOFFでも生成できます。字幕入り縦型クリップを shorts/ に出力します",
                            )
                            obs_shorts_mode = gr.Radio(
                                choices=["pad", "blur", "crop"],
                                value=obs_processing_defaults["shorts_mode"],
                                label="ショート動画の変換モード",
                                info="pad（推奨）: 上下を黒帯にして全体表示 / blur: 全体表示＋ぼかし背景 / crop: 左右を切って拡大",
                            )
                            obs_shorts_blur_strength = gr.Slider(
                                minimum=0,
                                maximum=50,
                                step=1,
                                value=obs_processing_defaults[
                                    "shorts_blur_strength"
                                ],
                                label="背景のぼかし強度",
                                info="blurモードの背景だけに反映。0=ぼかしなし / 50=強いぼかし",
                                visible=(
                                    obs_processing_defaults["shorts_mode"]
                                    == "blur"
                                ),
                            )
                            obs_shorts_mode.change(
                                fn=shorts_blur_visibility,
                                inputs=obs_shorts_mode,
                                outputs=obs_shorts_blur_strength,
                            )
                            obs_shorts_crop = gr.Radio(
                                choices=["center", "left", "right"],
                                value=obs_processing_defaults["shorts_crop"],
                                label="ショート動画のクロップ位置",
                                info="cropモードでの横位置。center=中央 / left=左 / right=右",
                            )
                            obs_shorts_title = gr.Checkbox(
                                label="ショート冒頭にタイトルを表示",
                                value=obs_processing_defaults["shorts_title"],
                                info="各ショートの最初の4秒だけタイトルを焼き込みます",
                            )
                            obs_shorts_title_position = gr.Radio(
                                choices=[
                                    ("上側の帯", "top"),
                                    ("下側の帯", "bottom"),
                                    ("クリップ上に重ねる", "overlay"),
                                ],
                                value=obs_processing_defaults[
                                    "shorts_title_position"
                                ],
                                label="タイトルの配置",
                                info="pad/blurでは上下の余白か映像中央を選択",
                            )
                        with gr.Column():
                            obs_generate_thumbnails = gr.Checkbox(
                                label="サムネイル候補を生成",
                                value=obs_processing_defaults["generate_thumbnails"],
                                info="各クリップから代表フレーム画像を生成します",
                            )
                            obs_audio_fusion = gr.Checkbox(
                                label="音声盛り上がり融合",
                                value=obs_processing_defaults["audio_fusion"],
                                info="音量や急な盛り上がりを使ってクリップ順位を再調整します",
                            )
                            obs_audio_alpha = gr.Slider(
                                0.0,
                                1.0,
                                value=obs_processing_defaults["audio_alpha"],
                                step=0.05,
                                label="音声重み alpha",
                            )
                            obs_karaoke = gr.Checkbox(
                                label="ワード単位カラオケ字幕",
                                value=obs_processing_defaults["karaoke"],
                                info="ショート動画の字幕を単語ごとにハイライトします",
                            )

                    with gr.Row():
                        obs_save_processing_btn = gr.Button(
                            "OBS用設定を保存",
                            variant="secondary",
                        )
                        obs_save_processing_msg = gr.Textbox(
                            label="",
                            show_label=False,
                            interactive=False,
                        )
                    obs_save_processing_btn.click(
                        fn=save_obs_processing_defaults,
                        inputs=[
                            obs_enable_clips,
                            obs_clip_prompt,
                            obs_enable_chapters,
                            obs_chapter_prompt,
                            obs_auto_append_youtube,
                            obs_num_clips,
                            obs_min_duration,
                            obs_max_duration,
                            obs_output_mode,
                            obs_generate_shorts,
                            obs_shorts_mode,
                            obs_shorts_crop,
                            obs_shorts_title,
                            obs_generate_thumbnails,
                            obs_audio_fusion,
                            obs_audio_alpha,
                            obs_karaoke,
                            obs_auto_start_without_prompt_confirmation,
                            obs_shorts_blur_strength,
                            obs_shorts_title_position,
                        ],
                        outputs=obs_save_processing_msg,
                    )

                with gr.Group(visible=False) as obs_confirmation_group:
                    obs_confirmation_message = gr.Markdown("")
                    obs_confirmation_prompt = gr.Textbox(
                        label="今回の生成プロンプト",
                        placeholder=(
                            "今回だけ使う指示を入力。空のままなら「そのまま生成開始」を選択"
                        ),
                        lines=3,
                    )
                    with gr.Row():
                        obs_confirm_btn = gr.Button(
                            "そのまま生成開始",
                            variant="primary",
                        )
                        obs_confirm_with_prompt_btn = gr.Button(
                            "プロンプトを入力して開始",
                            variant="secondary",
                        )
                        obs_skip_btn = gr.Button(
                            "今回は生成しない",
                            variant="secondary",
                        )
                    obs_confirmation_request_token = gr.State("")

                # Live status updates. gr.Timer exists in Gradio 6.x; fall back
                # to the manual refresh button on older versions (signature
                # inspection pattern, same as safe_launch_kwargs / theme split).
                _obs_timer = None
                if hasattr(gr, "Timer"):
                    try:
                        _obs_timer = gr.Timer(value=3.0)
                    except Exception:
                        _obs_timer = None
                if _obs_timer is not None:
                    _obs_timer.tick(fn=_obs_status_poll, outputs=obs_status_box)
                    _obs_timer.tick(
                        fn=_obs_confirmation_poll,
                        inputs=[obs_confirmation_request_token],
                        outputs=[
                            obs_confirmation_group,
                            obs_confirmation_message,
                            obs_confirmation_prompt,
                            obs_confirmation_request_token,
                        ],
                    )

            # --- Settings Tab ---
            with gr.Tab("Settings / 設定"):
                with gr.Row():
                    with gr.Column():
                        gr.HTML("<h3>AI Model / 分析AI</h3>")
                        ai_provider = gr.Dropdown(
                            choices=["claude", "openai", "gemini"],
                            value=defaults["ai_provider"],
                            label="AIプロバイダー",
                            info="Claude: CLI(サブスク) / OpenAI: APIキー必要 / Gemini: 無料枠あり",
                        )
                        ai_model = gr.Dropdown(
                            choices=[],
                            value="",
                            label="モデル",
                            allow_custom_value=True,
                            info="空欄でデフォルト (Claude=CLI, OpenAI=gpt-4.1, Gemini=gemini-2.5-flash)",
                        )
                        saved_api_key = load_gemini_api_key()
                        api_key = gr.Textbox(
                            label="APIキー",
                            value=saved_api_key,
                            placeholder="OpenAI / Gemini のAPIキーを入力",
                            type="password",
                            info="Claudeは入力不要。保存すると次回から自動で読み込みます。",
                        )
                        save_api_key_btn = gr.Button(
                            "💾 このキーを保存 (.gemini_key)",
                            variant="secondary",
                            size="sm",
                        )
                        save_api_key_btn.click(
                            fn=save_gemini_api_key,
                            inputs=api_key,
                            outputs=None,
                        )
                        with gr.Accordion(
                            "📘 Gemini APIキーの取得手順 — 4ステップ",
                            open=False,
                        ):
                            gr.Markdown(GEMINI_API_KEY_GUIDE_MD)

                        def update_models(provider):
                            if provider == "openai":
                                return gr.update(choices=["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini", "o3", "o3-mini"], value="gpt-4.1")
                            elif provider == "gemini":
                                return gr.update(choices=["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"], value="gemini-2.5-flash")
                            else:
                                return gr.update(choices=[], value="")

                        ai_provider.change(fn=update_models, inputs=ai_provider, outputs=ai_model)

                    with gr.Column():
                        gr.HTML("<h3>出力先 / Output Destination</h3>")
                        _saved_base = (defaults.get("output_base_dir", "") or "").strip()
                        _initial_path = _saved_base or str(resolve_output_base(""))
                        with gr.Row():
                            browse_output_btn = gr.Button(
                                "📁 保存先フォルダを選択…",
                                variant="primary",
                                scale=1,
                            )
                            open_output_btn = gr.Button(
                                "📂 現在のフォルダを開く",
                                variant="secondary",
                                scale=1,
                            )
                        output_base_dir = gr.Textbox(
                            label="現在の保存先",
                            value=_initial_path,
                            info="上のボタンから選ぶか、直接パスを編集できます。空欄にすると clip-extractor/output/ に戻ります。各 Generate ごとに output_<日時>/ サブフォルダが自動生成されます。",
                        )
                        browse_output_btn.click(
                            fn=pick_folder_dialog,
                            inputs=output_base_dir,
                            outputs=output_base_dir,
                        )
                        open_output_btn.click(
                            fn=open_output_folder,
                            inputs=output_base_dir,
                            outputs=None,
                        )

                        gr.HTML("<h3 style='margin-top: 1.5em;'>Adobe Premiere Pro 連携</h3>")
                        premiere_executable_path = gr.Textbox(
                            label="Premiere Pro実行ファイルのパス",
                            value=defaults.get("premiere_executable_path", ""),
                            placeholder=r"C:\Program Files\Adobe\Adobe Premiere Pro 2026\Adobe Premiere Pro.exe",
                            info="空欄なら自動検出します。検出できない場合だけ Adobe Premiere Pro.exe のフルパスを指定してください。",
                        )
                        with gr.Row():
                            premiere_install_settings_btn = gr.Button(
                                "連携プラグインをインストール",
                                variant="primary",
                            )
                            premiere_refresh_settings_btn = gr.Button(
                                "連携状態を更新",
                                variant="secondary",
                            )
                        premiere_settings_status = gr.Textbox(
                            label="Premiere連携ステータス",
                            value=get_bridge_status_text(),
                            interactive=False,
                            lines=3,
                        )
                        premiere_install_settings_btn.click(
                            fn=install_premiere_plugin_ui,
                            outputs=premiere_settings_status,
                        )
                        premiere_refresh_settings_btn.click(
                            fn=get_bridge_status_text,
                            outputs=premiere_settings_status,
                        )

                        gr.HTML(
                            "<h3 style='margin-top: 1.5em;'>"
                            "OBS Studio 起動・自動連携</h3>"
                        )
                        obs_launch_on_startup = gr.Checkbox(
                            label="Clip Extractor起動時にOBS Studioも起動",
                            value=bool(defaults.get("obs_launch_on_startup", False)),
                            info="「デフォルトに設定」で保存後、次回起動から有効になります。OBSが起動中なら二重起動しません。",
                        )
                        obs_auto_connect_on_startup = gr.Checkbox(
                            label="起動時にOBS連携も自動開始",
                            value=bool(
                                defaults.get(
                                    "obs_auto_connect_on_startup",
                                    True,
                                )
                            ),
                            info=(
                                "OBS WebSocketへ自動接続します。"
                                "OBSが後から起動した場合も待機を続け、"
                                "配信開始を検知すると"
                                "OBS連携タブの保存済み設定で処理を開始します。"
                                "Passwordが必要な場合は同タブで保存してください。"
                            ),
                        )
                        obs_executable_path = gr.Textbox(
                            label="OBS実行ファイルのパス",
                            value=defaults.get("obs_executable_path", ""),
                            placeholder=r"例: C:\...\obs64.exe",
                            info="obs64.exe のフルパスを貼り付けます。空欄なら自動検出。専用の同時起動ショートカットでもこのパスを使います。",
                        )

                with gr.Row():
                    with gr.Column():
                        gr.HTML("<h3>Whisper Settings</h3>")
                        whisper_model = gr.Dropdown(
                            choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"],
                            value=defaults["whisper_model"],
                            label="Whisper Model",
                            info="大きいモデルほど精度が高いが時間がかかる",
                        )
                        language = gr.Dropdown(
                            choices=["ja", "en", "zh", "ko", "auto"],
                            value=defaults["language"],
                            label="言語",
                        )

                with gr.Row():
                    with gr.Column():
                        gr.HTML("<h3>Font Settings / 字幕フォント</h3>")
                        system_fonts = get_system_fonts_cached()
                        # The bundled bold gothic (fonts/NotoSansJP-Bold.otf) is
                        # not installed system-wide, so surface it explicitly as the
                        # first choice and the default.
                        BUNDLED_FONT = "Noto Sans JP"
                        font_choices = [BUNDLED_FONT] + [f for f in system_fonts if f != BUNDLED_FONT]
                        saved_font = defaults["font_name"]
                        default_font = saved_font if saved_font in font_choices else BUNDLED_FONT
                        font_name = gr.Dropdown(
                            choices=font_choices,
                            value=default_font,
                            label="フォント名",
                            allow_custom_value=True,
                            info="先頭の「Noto Sans JP」は同梱のBold（700・商用利用可）で、ショート字幕向けの既定。その他はPCにインストール済みのフォント、直接入力も可。",
                        )
                        with gr.Row():
                            font_size = gr.Number(
                                label="フォントサイズ", value=defaults["font_size"], precision=0,
                            )
                            font_color = gr.ColorPicker(
                                label="フォント色", value=defaults["font_color"],
                            )

                with gr.Row():
                    with gr.Column():
                        gr.HTML("<h3>YouTube API 認証</h3>")
                        gr.HTML(
                            "<p style='color:#666; margin-top:-0.5em;'>"
                            "概要欄への自動追加を使う前にここで認証してください。"
                            "起動時にトークンの状態を自動確認し、切れていたら再認証を促します。</p>"
                        )
                        yt_auth_status_box = gr.Textbox(
                            label="認証ステータス",
                            value=youtube_api.auth_status_placeholder(),
                            interactive=False,
                        )
                        with gr.Row():
                            yt_refresh_btn = gr.Button(
                                "ステータス更新", variant="secondary"
                            )

                        # --- Step 1: credentials.json setup (developer-side OAuth client) ---
                        gr.HTML("<h4>① credentials.json を取得・配置</h4>")
                        gr.HTML(
                            "<p style='color:#666; margin-top:-0.5em;'>"
                            "まだ無い場合は Google Cloud Console でデスクトップアプリ用の "
                            "OAuth クライアントを作成し、ダウンロードした JSON をここにドロップしてください。<br/>"
                            f"<small>保存先 (OS ユーザー設定ディレクトリ): <code>{youtube_api.CREDENTIALS_PATH}</code></small></p>"
                        )
                        with gr.Accordion(
                            "📘 credentials.json の取得手順 — 6ステップ",
                            open=False,
                        ):
                            gr.Markdown(GOOGLE_CREDENTIALS_SETUP_GUIDE_MD)
                        creds_upload = gr.File(
                            label="credentials.json (ドラッグ＆ドロップ可)",
                            file_types=[".json"],
                            type="filepath",
                        )
                        with gr.Row():
                            creds_open_console_btn = gr.Button(
                                "Google Cloud Console を開く",
                                variant="secondary",
                            )
                        creds_setup_msg = gr.Textbox(
                            label="セットアップメッセージ",
                            interactive=False,
                            value="",
                        )

                        # --- Step 2: OAuth actions ---
                        gr.HTML("<h4>② 認証アクション</h4>")
                        with gr.Row():
                            yt_auth_btn = gr.Button("認証する", variant="primary")
                            yt_revoke_btn = gr.Button("認証解除", variant="secondary")
                        with gr.Accordion(
                            "⚠️ Google に『確認されていません』と表示された場合",
                            open=False,
                        ):
                            gr.Markdown(GOOGLE_OAUTH_UNVERIFIED_GUIDE_MD)

                        def _yt_install_creds(src_path):
                            msg = youtube_api.install_credentials_from_file(src_path)
                            return msg, youtube_api.auth_status_summary()

                        def _yt_open_console():
                            import webbrowser
                            try:
                                webbrowser.open(youtube_api.GOOGLE_CLOUD_CONSOLE_URL)
                                return (
                                    "ブラウザで Google Cloud Console を開きました。\n"
                                    "1) YouTube Data API v3 を『有効にする』\n"
                                    "2) Google Auth Platform を設定し、自分をテストユーザーに追加\n"
                                    "3) 『クライアント』で『デスクトップ アプリ』を作成\n"
                                    "4) ダウンロードした JSON を上の欄にドロップ"
                                )
                            except Exception as _e:
                                return f"ブラウザ起動失敗: {_e} / URL: {youtube_api.GOOGLE_CLOUD_CONSOLE_URL}"

                        def _yt_do_auth():
                            try:
                                ok = youtube_api.ensure_authenticated(force_reauth=False)
                                if not ok:
                                    return (
                                        "credentials.json が見つかりません。"
                                        "上の『credentials.json』欄にファイルをドロップしてから、"
                                        "もう一度『認証する』を押してください。"
                                    )
                                return youtube_api.auth_status_summary()
                            except Exception as _e:
                                return f"認証失敗: {_e}"

                        def _yt_do_revoke():
                            removed = youtube_api.revoke_auth()
                            head = "認証解除しました: " if removed else "トークンは元々ありません: "
                            return head + youtube_api.auth_status_summary()

                        def _yt_do_refresh():
                            return youtube_api.auth_status_summary()

                        creds_upload.upload(
                            fn=_yt_install_creds,
                            inputs=creds_upload,
                            outputs=[creds_setup_msg, yt_auth_status_box],
                        )
                        creds_open_console_btn.click(
                            fn=_yt_open_console,
                            outputs=creds_setup_msg,
                        )
                        yt_auth_btn.click(fn=_yt_do_auth, outputs=yt_auth_status_box)
                        yt_revoke_btn.click(fn=_yt_do_revoke, outputs=yt_auth_status_box)
                        yt_refresh_btn.click(fn=_yt_do_refresh, outputs=yt_auth_status_box)

                with gr.Row():
                    save_defaults_btn = gr.Button("デフォルトに設定", variant="secondary")
                    save_defaults_msg = gr.Textbox(label="", interactive=False, show_label=False)

                save_defaults_btn.click(
                    fn=save_defaults,
                    inputs=[ai_provider, ai_model,
                            enable_clips, enable_chapters, clip_prompt, chapter_prompt,
                            auto_append_youtube,
                            num_clips, output_mode, generate_shorts, shorts_mode, shorts_crop, shorts_title,
                            min_duration, max_duration,
                            whisper_model, language,
                            font_name, font_size, font_color,
                            output_base_dir,
                            generate_thumbnails,
                            audio_fusion, audio_alpha,
                            karaoke,
                            shorts_blur_strength,
                            shorts_title_position,
                            premiere_executable_path,
                            obs_launch_on_startup,
                            obs_executable_path,
                            obs_auto_connect_on_startup],
                    outputs=save_defaults_msg,
                )

                input_save_defaults_btn.click(
                    fn=save_defaults,
                    inputs=[ai_provider, ai_model,
                            enable_clips, enable_chapters, clip_prompt, chapter_prompt,
                            auto_append_youtube,
                            num_clips, output_mode, generate_shorts, shorts_mode, shorts_crop, shorts_title,
                            min_duration, max_duration,
                            whisper_model, language,
                            font_name, font_size, font_color,
                            output_base_dir,
                            generate_thumbnails,
                            audio_fusion, audio_alpha,
                            karaoke,
                            shorts_blur_strength,
                            shorts_title_position,
                            premiere_executable_path,
                            obs_launch_on_startup,
                            obs_executable_path,
                            obs_auto_connect_on_startup],
                    outputs=input_save_defaults_msg,
                )

            # --- Output Tab ---
            with gr.Tab("Output / 出力"):
                with gr.Row():
                    with gr.Column(scale=2):
                        log_output = gr.Textbox(
                            label="Processing Log",
                            lines=15,
                            interactive=False,
                        )
                    with gr.Column(scale=1):
                        highlights_output = gr.Markdown(
                            label="Detected Highlights",
                        )

                with gr.Row():
                    download_output = gr.File(label="Download (ZIP)")
                    drive_link_output = gr.Textbox(
                        label="Google Drive Link",
                        interactive=False,
                    )

                with gr.Row():
                    chapters_output = gr.Textbox(
                        label="タイムスタンプ (概要欄)",
                        info="先頭が必ず 0:00 から始まるため、YouTube がアップロード時に自動でチャプターとして認識します。そのままコピーして動画の概要欄に貼り付けるか、『概要欄に自動追加』を有効にして API で直接反映させてください。",
                        lines=8,
                        interactive=False,
                    )

                with gr.Group():
                    gr.Markdown(
                        "### Adobe Premiere Proへ送る\n"
                        "書き出した動画を現在のプロジェクトへ読み込み、"
                        "元動画をV1、通常切り抜きをV2、同時生成したショートをV3へ"
                        "元の時刻に合わせて配置します。初回だけ連携プラグインを導入してください。"
                    )
                    with gr.Row():
                        premiere_edit_btn = gr.Button(
                            "Premiere Proで編集",
                            variant="primary",
                            size="lg",
                        )
                        premiere_include_shorts = gr.Checkbox(
                            label="ショート動画も読み込む",
                            value=True,
                        )
                    with gr.Row():
                        premiere_install_output_btn = gr.Button(
                            "連携プラグインをインストール",
                            variant="secondary",
                        )
                        premiere_refresh_output_btn = gr.Button(
                            "連携状態を更新",
                            variant="secondary",
                        )
                    premiere_output_status = gr.Textbox(
                        label="Premiere連携ステータス",
                        value=get_bridge_status_text(),
                        interactive=False,
                        lines=4,
                    )
                    premiere_edit_btn.click(
                        fn=request_premiere_edit_ui,
                        inputs=[
                            premiere_job_state,
                            premiere_include_shorts,
                            premiere_executable_path,
                        ],
                        outputs=premiere_output_status,
                        concurrency_limit=1,
                    )
                    premiere_install_output_btn.click(
                        fn=install_premiere_plugin_ui,
                        outputs=premiere_output_status,
                    )
                    premiere_refresh_output_btn.click(
                        fn=get_bridge_status_text,
                        outputs=premiere_output_status,
                    )

                    _premiere_timer = None
                    if hasattr(gr, "Timer"):
                        try:
                            _premiere_timer = gr.Timer(value=2.0)
                        except Exception:
                            _premiere_timer = None
                    if _premiere_timer is not None:
                        _premiere_timer.tick(
                            fn=get_bridge_status_text,
                            outputs=premiere_output_status,
                        )

        detect_event = detect_btn.click(
            fn=clear_premiere_job_state,
            outputs=premiere_job_state,
        ).then(
            fn=detect_phase,
            inputs=[
                input_url, input_file,
                enable_clips, clip_prompt, enable_chapters, chapter_prompt,
                num_clips, ai_provider, ai_model, api_key,
                min_duration, max_duration,
                whisper_model, language,
                audio_fusion, audio_alpha,
                output_base_dir,
                generate_shorts,
            ],
            outputs=[session_state, status, review_panel],
            concurrency_limit=1,
        )
        detect_event.then(
            fn=highlights_for_review,
            inputs=session_state,
            outputs=highlights_state,
        ).then(
            fn=maybe_render_phase,
            inputs=[
                auto_run_both,
                session_state,
                output_mode,
                generate_shorts,
                shorts_mode,
                shorts_crop,
                shorts_title,
                generate_zip,
                upload_to_drive,
                auto_append_youtube,
                font_name,
                font_size,
                font_color,
                generate_thumbnails,
                karaoke,
                shorts_blur_strength,
                shorts_title_position,
            ],
            outputs=[
                log_output,
                highlights_output,
                download_output,
                drive_link_output,
                chapters_output,
                premiere_job_state,
            ],
            concurrency_limit=1,
        )

        render_btn.click(
            fn=clear_premiere_job_state,
            outputs=premiere_job_state,
        ).then(
            fn=render_phase,
            inputs=[
                session_state,
                output_mode,
                generate_shorts,
                shorts_mode,
                shorts_crop,
                shorts_title,
                generate_zip,
                upload_to_drive,
                auto_append_youtube,
                font_name,
                font_size,
                font_color,
                generate_thumbnails,
                karaoke,
                shorts_blur_strength,
                shorts_title_position,
            ],
            outputs=[
                log_output,
                highlights_output,
                download_output,
                drive_link_output,
                chapters_output,
                premiere_job_state,
            ],
            concurrency_limit=1,
        )

        # --- OBS連携ボタン配線 ---
        # inputs order MUST match start_obs_watch() signature 1:1 (Gradio
        # passes them positionally; any skew silently corrupts every value).
        obs_start_btn.click(
            fn=start_obs_watch,
            inputs=[
                obs_trigger_radio,
                obs_host,
                obs_port,
                obs_password,
                obs_save_password,
                obs_stop_event_radio,
                obs_watch_folder,
                obs_auto_process,
                obs_auto_append_youtube,
                obs_num_clips,
                obs_output_mode,
                obs_generate_shorts,
                ai_provider,
                whisper_model,
                output_base_dir,
                obs_enable_clips,
                obs_clip_prompt,
                obs_enable_chapters,
                obs_chapter_prompt,
                obs_min_duration,
                obs_max_duration,
                obs_shorts_mode,
                obs_shorts_crop,
                obs_shorts_title,
                obs_generate_thumbnails,
                obs_audio_fusion,
                obs_audio_alpha,
                obs_karaoke,
                obs_auto_start_without_prompt_confirmation,
                obs_shorts_blur_strength,
                obs_shorts_title_position,
            ],
            outputs=obs_status_box,
        )
        obs_stop_btn.click(
            fn=stop_obs_watch,
            inputs=[],
            outputs=obs_status_box,
        )
        obs_refresh_btn.click(
            fn=_obs_status_poll,
            inputs=[],
            outputs=obs_status_box,
        )
        obs_refresh_btn.click(
            fn=_obs_confirmation_poll,
            inputs=[obs_confirmation_request_token],
            outputs=[
                obs_confirmation_group,
                obs_confirmation_message,
                obs_confirmation_prompt,
                obs_confirmation_request_token,
            ],
        )
        obs_confirm_btn.click(
            fn=_obs_confirm_generation,
            inputs=[],
            outputs=[
                obs_confirmation_group,
                obs_confirmation_message,
                obs_confirmation_prompt,
                obs_status_box,
            ],
        )
        obs_confirm_with_prompt_btn.click(
            fn=_obs_confirm_generation_with_prompt,
            inputs=[obs_confirmation_prompt],
            outputs=[
                obs_confirmation_group,
                obs_confirmation_message,
                obs_confirmation_prompt,
                obs_status_box,
            ],
        )
        obs_skip_btn.click(
            fn=_obs_skip_generation,
            inputs=[],
            outputs=[
                obs_confirmation_group,
                obs_confirmation_message,
                obs_confirmation_prompt,
                obs_status_box,
            ],
        )
        # Instructions
        with gr.Accordion("使い方 / How to Use", open=False):
            gr.Markdown("""
### 基本的な使い方
1. **Input** タブでYouTube/Twitch URLを貼り付けるか、動画ファイルをアップロード
2. クリップ数や出力モードを設定
3. **STEP 1：AIがおすすめ箇所を抽出** → 内容を確認
4. **STEP 2：クリップを書き出し** をクリック
5. **Output** タブで結果を確認し、必要ならPremiere Proへ送る

### 分析 AI の準備 (Gemini を使う場合)
Gemini は**無料枠あり・クレカ登録不要**で一番手軽です。
1. 🔗 [aistudio.google.com/apikey](https://aistudio.google.com/apikey) を開き Google アカウントでログイン (個人 Gmail 推奨)
2. **[+ APIキーを作成]** → プロジェクトは新規作成で OK → キーをコピー
3. Settings タブの「APIキー」欄に貼り付け → **[💾 このキーを保存]**

画面説明は Settings タブの **「📘 Gemini APIキーの取得手順」** にあります。

> ⚠️ この Gemini API キーと、下で説明する `credentials.json` (YouTube/Drive 連携用) は**別物**です。

### Premiere Pro での読み込み
初回だけ、Output または Settings の
**「連携プラグインをインストール」**を押し、Creative Cloudで許可して
Premiere Proを再起動します。

1. STEP 2の書き出し完了後、Outputの **「Premiere Proで編集」** をクリック
2. Premiere Proが自動起動し、書き出した動画を現在のプロジェクトへ読み込み
3. 各動画のシーケンスが作成され、最初のシーケンスが開く
4. SRTファイルは必要に応じてキャプションとして読み込み、フォントや位置を調整

連携プラグインを使えない場合も、従来どおり
Premiere Pro → File → Import → `project.xml` で手動読み込みできます。

### Photoshopでテロップを編集する方法
1. SRTキャプションをタイムライン上で選択
2. 右クリック → 「グラフィックにアップグレード」でテキストレイヤーに変換
3. テキストレイヤーを右クリック → 「Adobe Photoshopで編集」
4. Photoshopでフォント・装飾・エフェクトを自由に編集
5. 保存するとPremiere Proに即反映

"""
            )
            gr.Markdown(GOOGLE_CREDENTIALS_SETUP_GUIDE_MD)
            gr.Markdown(
                """

### 生成モード（切り抜き / 概要欄 の独立選択）
- **両方 ON (デフォルト)**: 切り抜き動画・SRT・Premiere XML・概要欄テキストをまとめて出力。切り抜き側のプロンプトだけが使われます（概要欄プロンプトは無視）。
- **切り抜きのみ**: クリップ + SRT + XML を出力。概要欄テキストは生成されません。
- **概要欄のみ**: ハイライト検出を概要欄プロンプトで実行し、`chapters.txt` だけを出力。クリップ抽出・SRT・XML はスキップ。
- **両方 OFF**: エラーになります。1 つは有効にしてください。

### ショート動画のフォント設定（9:16 出力のみ）
1. Settings タブの Font Settings でフォント名・サイズ・色を選択
2. 「ショート動画 (9:16) を生成」をチェックして Generate
3. 出力された Shorts には字幕が焼き込まれ、そのまま YouTube Shorts / TikTok にアップロード可能
4. 通常の横クリップ（landscape）は字幕が焼き込まれず、Premiere Pro で SRT キャプションを自由に調整できる状態のまま

### タイムスタンプ (概要欄)
1. Generate 完了後、Output タブ下部の「タイムスタンプ (概要欄)」にチャプター形式の一覧が表示される（例: `0:00 イントロ` / `3:42 ハイライト1` …）
2. そのままコピーして YouTube アップロード時の概要欄に貼り付ける
3. 先頭が必ず `0:00` から始まるため YouTube が自動でチャプターとして認識し、動画プレイヤー上にチャプターマーカーが表示される
4. `output_*/chapters.txt` にも同じ内容が保存されている

### YouTube 概要欄への自動追加
1. Settings タブの「credentials.json の取得手順」に沿って設定し、**[認証する]** をクリック
2. Input タブで **「概要欄に自動追加」** をオン
3. YouTube URLから生成すると、タイムスタンプが動画の概要欄へ追加されます

認証で警告やブロックが出た場合は、Settings タブの
**「⚠️ Google に『確認されていません』と表示された場合」**を開いてください。
            """)

        app.load(fn=_startup_auth_status_for_ui, inputs=None, outputs=[yt_auth_status_box])

    return app


if __name__ == "__main__":
    from obs_launcher import launch_obs_from_settings

    _obs_launch_result = launch_obs_from_settings(SETTINGS_FILE)
    if _obs_launch_result is not None:
        _level = "OK" if _obs_launch_result.ok else "WARN"
        print(f"[{_level}] {_obs_launch_result.message}")
    schedule_obs_auto_connect()
    app = create_ui()
    app.queue()
    app.launch(**safe_launch_kwargs(
        server_name="0.0.0.0",
        server_port=7860,
        ssr_mode=False,
        **LAUNCH_THEME_KWARGS,
    ))
