#!/usr/bin/env python3
"""clip-extractor Web UI using Gradio."""

import logging
import os
import sys
import shutil
import subprocess
import traceback
import inspect
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import gradio as gr


@dataclass
class ProcessResult:
    """Structured result from the processing pipeline.

    Fields line up with the Gradio outputs wired in render_btn.click:
    (log_output, highlights_output, download_output, drive_link_output,
    chapters_output). Building this dataclass instead of scattering raw
    5-tuples across every return statement keeps the field order in one
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

    def as_gradio_outputs(self) -> tuple:
        """Order matches render_btn.click(outputs=[...])."""
        return (
            self.log,
            self.highlights,
            self.download_path,
            self.drive_link,
            self.chapters_text,
        )

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


def load_defaults() -> dict:
    """Load saved default settings."""
    defaults = {
        "ai_provider": "gemini", "ai_model": "gemini-2.5-flash",
        "enable_clips": True, "enable_chapters": True,
        "clip_prompt": "", "chapter_prompt": "",
        "auto_append_youtube": False,
        "num_clips": 5, "min_duration": 30, "max_duration": 90,
        "output_mode": "combined", "generate_shorts": False,
        "shorts_mode": "crop", "shorts_crop": "center",
        "shorts_title": True, "generate_thumbnails": False,
        "audio_fusion": False, "audio_alpha": 0.35,
        "karaoke": False,
        "whisper_model": "large-v3", "language": "ja",
        "font_name": "Noto Sans JP Black", "font_size": 96, "font_color": "#FFFFFF",
        "output_base_dir": "",
    }
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update(saved)
        except Exception:
            pass
    return defaults


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
                  karaoke=False):
    """Save current settings as defaults."""
    data = {
        "ai_provider": ai_provider, "ai_model": ai_model,
        "enable_clips": bool(enable_clips), "enable_chapters": bool(enable_chapters),
        "clip_prompt": clip_prompt, "chapter_prompt": chapter_prompt,
        "auto_append_youtube": bool(auto_append_youtube),
        "num_clips": int(num_clips),
        "output_mode": output_mode, "generate_shorts": bool(generate_shorts),
        "shorts_mode": shorts_mode, "shorts_crop": shorts_crop,
        "shorts_title": bool(shorts_title),
        "min_duration": int(min_duration), "max_duration": int(max_duration),
        "whisper_model": whisper_model, "language": language,
        "font_name": font_name, "font_size": int(font_size),
        "font_color": font_color,
        "output_base_dir": (output_base_dir or "").strip(),
        "generate_thumbnails": bool(generate_thumbnails),
        "audio_fusion": bool(audio_fusion),
        "audio_alpha": float(audio_alpha),
        "karaoke": bool(karaoke),
    }
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "Settings saved as default!"


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


def pick_folder_dialog(current_value: str) -> str:
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

    fallback = current_value if (current_value or "").strip() else str(initial)

    if os.name == "nt":
        try:
            ps_cmd = (
                "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.SelectedPath = '{str(initial).replace(chr(39), chr(39)*2)}';"
                "$d.Description = '保存先フォルダを選択';"
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
                title="保存先フォルダを選択",
                initialdir=str(initial),
            )
            _root.destroy()
            if picked:
                return picked
        except Exception as exc:
            logger.warning(f"tkinter folder picker failed: {exc}")

    return fallback


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
from downloader import download_video
from transcriber import transcribe, segments_to_text
from highlighter import detect_highlights
from audio_energy import fuse_audio_energy
import clipper
from clipper import extract_clips, generate_thumbnails as generate_thumbnail_candidates, get_video_info
from subtitles import generate_all_karaoke_ass, generate_all_srts
from premiere_xml import generate_combined_xml, generate_individual_xmls
from drive_upload import upload_output_directory, is_configured as drive_is_configured
from modes import GenerationModes
import youtube_api


_MIN_REVIEW_CLIP_DURATION_SEC = 0.1


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
    progress=gr.Progress(),
):
    """Detection phase: validate, resolve input, transcribe, and find highlights."""
    logs = []

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)

    try:
        modes = GenerationModes(
            enable_clips=bool(enable_clips),
            enable_chapters=bool(enable_chapters),
            clip_prompt=clip_prompt or "",
            chapter_prompt=chapter_prompt or "",
        )
        try:
            modes.validate()
        except ValueError as mode_err:
            return {}, f"Error: {mode_err}", gr.update(visible=False)
        log(f"Modes: clips={modes.enable_clips}, chapters={modes.enable_chapters}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_dir = resolve_output_base(output_base_dir)
        output_dir = base_dir / f"output_{timestamp}"
        output_dir.mkdir(parents=True, exist_ok=True)
        log(f"Output base: {base_dir}")

        youtube_video_id: str | None = None
        if input_url and input_url.strip():
            youtube_video_id = youtube_api.extract_video_id(input_url.strip())
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
        elif input_url and input_url.strip():
            progress(0.05, desc="Downloading video...")
            video_path = download_video(input_url.strip(), output_dir / "source")
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
            "youtube_video_id": youtube_video_id,
            "enable_clips": modes.enable_clips,
            "enable_chapters": modes.enable_chapters,
            "modes": {
                "enable_clips": modes.enable_clips,
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
    progress=gr.Progress(),
):
    """Render phase: replay downstream output generation with edited highlights."""
    if not isinstance(session, dict) or not session.get("video_path"):
        return ProcessResult(
            log="Error: 先に Detect を実行してください / Run Detect before Render.",
        ).as_gradio_outputs()

    logs = list(session.get("logs") or [])

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
        mode_data = session.get("modes") or {}
        modes = GenerationModes(
            enable_clips=bool(mode_data.get("enable_clips", session.get("enable_clips", True))),
            enable_chapters=bool(mode_data.get("enable_chapters", session.get("enable_chapters", True))),
            clip_prompt=mode_data.get("clip_prompt", ""),
            chapter_prompt=mode_data.get("chapter_prompt", ""),
        )
        try:
            modes.validate()
        except ValueError as mode_err:
            return ProcessResult(log=f"Error: {mode_err}").as_gradio_outputs()

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
        shorts_ass_paths: list[Path] = []
        thumbnail_paths: list[Path] = []

        if modes.enable_clips:
            progress(0.6, desc="[Step 4/6] Extracting clips...")
            log("[Step 4/6] Extracting clips...")
            clips_dir = output_dir / "clips"
            clip_paths = extract_clips(video_path, highlights, clips_dir)
            log(f"  Extracted {len(clip_paths)} clips")

            progress(0.7, desc="[Step 5/6] Generating subtitles...")
            log("[Step 5/6] Generating subtitles...")
            srt_paths = generate_all_srts(segments, highlights, clips_dir)
            log(f"  Generated {len(srt_paths)} SRT files")

            if generate_shorts:
                progress(0.75, desc="Generating shorts (9:16) with burned-in subtitles...")
                shorts_dir = output_dir / "shorts"
                shorts_dir.mkdir(parents=True, exist_ok=True)
                if karaoke:
                    shorts_ass_paths = generate_all_karaoke_ass(
                        segments, highlights, shorts_dir, font_config,
                    )
                else:
                    shorts_srt_paths = generate_all_srts(segments, highlights, shorts_dir)
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

            if generate_thumbnails:
                progress(0.8, desc="Generating thumbnail candidates...")
                if generate_shorts:
                    thumbnail_dir = output_dir / "shorts"
                    thumbnail_paths = generate_thumbnail_candidates(
                        video_path, highlights, thumbnail_dir,
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

            progress(0.85, desc="[Step 6/6] Exporting XML...")
            log("[Step 6/6] Exporting Premiere Pro XML...")
            if output_mode == "combined":
                xml_path = output_dir / "project.xml"
                generate_combined_xml(
                    clip_paths, highlights, video_info, xml_path,
                    project_name=video_path.stem,
                )
                if generate_shorts and shorts_paths:
                    shorts_video_info = {**video_info, "width": 1080, "height": 1920}
                    generate_combined_xml(
                        shorts_paths, highlights, shorts_video_info,
                        output_dir / "project_shorts.xml",
                        project_name=f"{video_path.stem}_shorts",
                    )
                log("  Premiere Pro XML (combined mode) exported")
            else:
                generate_individual_xmls(
                    clip_paths, highlights, video_info, clips_dir,
                )
                if generate_shorts and shorts_paths:
                    shorts_video_info = {**video_info, "width": 1080, "height": 1920}
                    generate_individual_xmls(
                        shorts_paths, highlights,
                        shorts_video_info, output_dir / "shorts",
                    )
                log("  Premiere Pro XML (individual mode) exported")
        else:
            log("[Skip 4-6] Clip generation disabled — chapters-only run")

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
    progress=gr.Progress(),
):
    """Chain STEP 2 right after STEP 1 when the 'run both' checkbox is on.

    Returns no-op updates (leaving the STEP 2 output fields untouched) when the
    checkbox is off or when detection produced nothing renderable, so manual
    STEP 2 still behaves exactly as before.
    """
    if not auto_run or not isinstance(session, dict) or not session.get("video_path"):
        return tuple(gr.update() for _ in range(5))
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
        progress=progress,
    )


# ---------------------------------------------------------------------------
# OBS integration — bridge from "recording finished" to the existing
# detect→render pipeline. The watchers themselves live in obs_integration.py;
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
# Generation token: bumped on every start/stop so a callback created for a
# superseded watcher refuses to run the pipeline with stale settings.
_obs_generation = 0
# Auto-pipeline worker threads, tracked so stop can join finished ones and the
# lifecycle is observable (the watcher's own _spawn_worker does not see these).
_obs_pipeline_threads: list[threading.Thread] = []
_obs_status_lines: list[str] = []
_obs_status_lock = threading.Lock()
_OBS_STATUS_MAX = 80


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


def run_obs_auto_pipeline(video_path: str, settings: dict) -> str:
    """Run detect→render end-to-end on a local recording path.

    Thin bridge over the existing ``detect_phase`` / ``render_phase``: builds
    a fake file object whose ``.name`` is the recording path (detect_phase
    reads ``getattr(input_file, "name", ...)``), feeds a dummy progress, then
    drives render with the supplied ``settings`` dict. Returns an accumulated
    log/status string — never raises (errors are captured into the log).
    """
    logs: list[str] = []

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)

    try:
        if not video_path:
            return "OBS auto: 録画パスが空です"
        if not Path(video_path).exists():
            return f"OBS auto: ファイルが見つかりません: {video_path}"

        s = dict(settings)  # shallow copy; we only read
        fake_file = type("F", (), {"name": video_path})()
        progress = _DummyProgress()

        log(f"[OBS] Detect 開始: {video_path}")
        detect_result = detect_phase(
            "",  # input_url — local file, no URL
            fake_file,
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
            progress=progress,
        )
        # detect_phase returns (session, status_md, review_panel_update)
        session = detect_result[0] if isinstance(detect_result, tuple) and detect_result else None
        detect_status = detect_result[1] if isinstance(detect_result, tuple) and len(detect_result) > 1 else ""
        if not isinstance(session, dict) or not session.get("video_path"):
            return "\n".join(logs + [str(detect_status)])

        log("[OBS] Detect 完了 — Render 開始")
        render_result = render_phase(
            session,
            s.get("output_mode", "combined"),
            bool(s.get("generate_shorts", False)),
            s.get("shorts_mode", "crop"),
            s.get("shorts_crop", "center"),
            bool(s.get("shorts_title", True)),
            False,  # generate_zip — 自動処理では ZIP を作らない
            False,  # upload_to_drive — 自動処理では Drive 投稿しない
            bool(s.get("auto_append_youtube", False)),
            s.get("font_name", "Noto Sans JP Black"),
            _coerce_int(s.get("font_size", 96), 96),
            s.get("font_color", "#FFFFFF"),
            bool(s.get("generate_thumbnails", False)),
            bool(s.get("karaoke", False)),
            progress=progress,
        )
        # render_phase returns ProcessResult.as_gradio_outputs() = (log, highlights, dl, drive, chapters)
        render_log = render_result[0] if isinstance(render_result, tuple) and render_result else ""
        log("[OBS] Render 完了")
        return "\n".join(logs + [str(render_log)])
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"OBS auto pipeline error: {e}\n{tb}")
        return "\n".join(logs + [f"Error: {e}", tb])


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
                _obs_append_status(f"自動パイプライン開始: {video_path}")
                result_log = run_obs_auto_pipeline(video_path, settings)
                _obs_append_status(result_log)
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


def stop_obs_watch() -> str:
    """Stop the active OBS watcher (if any) and return its terminal status."""
    global _obs_watcher, _obs_generation
    with _obs_watcher_lock:
        watcher = _obs_watcher
        _obs_watcher = None
        _obs_generation += 1
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


def start_obs_watch(
    method: str,
    host: str,
    port,
    password: str,
    stop_event: str,
    watch_folder: str,
    auto_process: bool,
    num_clips,
    output_mode: str,
    generate_shorts: bool,
    ai_provider: str,
    whisper_model: str,
    output_base_dir: str,
) -> str:
    """(Re)start the OBS watcher with the given settings; return status text.

    Argument order MUST line up 1:1 with the ``obs_start_btn.click(inputs=[...])``
    list in create_ui() — Gradio passes them positionally and any skew silently
    corrupts every value. Settings the UI doesn't override are filled from
    load_defaults() so prompts/durations/fonts still apply.
    """
    global _obs_watcher, _obs_generation
    # Stop any existing watcher first so re-clicking Start reconfigures cleanly.
    stop_obs_watch()

    try:
        import obs_integration
    except Exception as e:
        msg = f"obs_integration の import に失敗: {e}"
        _obs_append_status(msg)
        return msg

    # Build the settings dict: saved defaults overlaid with the live UI values
    # the user is most likely to tweak per-run.
    settings = load_defaults()
    try:
        settings["num_clips"] = int(num_clips)
    except (TypeError, ValueError):
        pass
    if output_mode:
        settings["output_mode"] = output_mode
    settings["generate_shorts"] = bool(generate_shorts)
    if ai_provider:
        settings["ai_provider"] = ai_provider
    if whisper_model:
        settings["whisper_model"] = whisper_model
    if output_base_dir is not None:
        settings["output_base_dir"] = output_base_dir

    config = {
        "host": host or "localhost",
        "port": int(port) if port not in (None, "") else 4455,
        "password": password or "",
        "stop_event": stop_event or "stream",
        "watch_folder": watch_folder or "",
    }
    with _obs_watcher_lock:
        _obs_generation += 1
        gen = _obs_generation
    callback = _obs_make_callback(bool(auto_process), settings, gen)
    try:
        watcher = obs_integration.create_watcher(method, config, callback)
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
    return status


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
        # Validate generation modes — at least one must be enabled
        modes = GenerationModes(
            enable_clips=bool(enable_clips),
            enable_chapters=bool(enable_chapters),
            clip_prompt=clip_prompt or "",
            chapter_prompt=chapter_prompt or "",
        )
        try:
            modes.validate()
        except ValueError as mode_err:
            return ProcessResult(log=f"Error: {mode_err}").as_gradio_outputs()
        log(f"Modes: clips={modes.enable_clips}, chapters={modes.enable_chapters}")

        # Pre-validate YouTube auth before starting the heavy pipeline.
        # We only want to discover an auth problem AFTER download/transcribe
        # when the user explicitly asked for the auto-append step.
        if auto_append_youtube:
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
        if input_url and input_url.strip():
            youtube_video_id = youtube_api.extract_video_id(input_url.strip())
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
        elif input_url and input_url.strip():
            progress(0.05, desc="Downloading video...")
            video_path = download_video(input_url.strip(), output_dir / "source")
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

        # Steps 4–6 are the clip pipeline (extract → SRT → Shorts → XML).
        # When clip generation is disabled, we still keep the earlier highlight
        # detection result so Step 7 (chapters) can use it.
        clip_paths: list[Path] = []
        srt_paths: list[Path] = []
        shorts_paths: list[Path] = []
        shorts_srt_paths: list[Path] = []
        shorts_ass_paths: list[Path] = []
        thumbnail_paths: list[Path] = []

        if modes.enable_clips:
            # Step 4: Extract clips (normal landscape, no burn-in — Premiere edits SRT separately)
            progress(0.6, desc="[Step 4/6] Extracting clips...")
            log("[Step 4/6] Extracting clips...")
            clips_dir = output_dir / "clips"
            clip_paths = extract_clips(video_path, highlights, clips_dir)
            log(f"  Extracted {len(clip_paths)} clips")

            # Step 5: Subtitles for clips (SRT for Premiere captions)
            progress(0.7, desc="[Step 5/6] Generating subtitles...")
            log("[Step 5/6] Generating subtitles...")
            srt_paths = generate_all_srts(segments, highlights, clips_dir)
            log(f"  Generated {len(srt_paths)} SRT files")

            # Shorts (9:16) — generate subtitle assets first, then burn in.
            if generate_shorts:
                progress(0.75, desc="Generating shorts (9:16) with burned-in subtitles...")
                shorts_dir = output_dir / "shorts"
                shorts_dir.mkdir(parents=True, exist_ok=True)
                if karaoke:
                    shorts_ass_paths = generate_all_karaoke_ass(
                        segments, highlights, shorts_dir, font_config,
                    )
                else:
                    shorts_srt_paths = generate_all_srts(segments, highlights, shorts_dir)
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

            if generate_thumbnails:
                progress(0.8, desc="Generating thumbnail candidates...")
                if generate_shorts:
                    thumbnail_dir = output_dir / "shorts"
                    thumbnail_paths = generate_thumbnail_candidates(
                        video_path, highlights, thumbnail_dir,
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

            # Step 6: Premiere Pro XML
            progress(0.85, desc="[Step 6/6] Exporting XML...")
            log("[Step 6/6] Exporting Premiere Pro XML...")
            if output_mode == "combined":
                xml_path = output_dir / "project.xml"
                generate_combined_xml(
                    clip_paths, highlights, video_info, xml_path,
                    project_name=video_path.stem,
                )
                if generate_shorts and shorts_paths:
                    shorts_video_info = {**video_info, "width": 1080, "height": 1920}
                    generate_combined_xml(
                        shorts_paths, highlights, shorts_video_info,
                        output_dir / "project_shorts.xml",
                        project_name=f"{video_path.stem}_shorts",
                    )
                log("  Premiere Pro XML (combined mode) exported")
            else:
                generate_individual_xmls(
                    clip_paths, highlights, video_info, clips_dir,
                )
                if generate_shorts and shorts_paths:
                    shorts_video_info = {**video_info, "width": 1080, "height": 1920}
                    generate_individual_xmls(
                        shorts_paths, highlights,
                        shorts_video_info, output_dir / "shorts",
                    )
                log("  Premiere Pro XML (individual mode) exported")
        else:
            log("[Skip 4-6] Clip generation disabled — chapters-only run")

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
            log("[Skip chapters] タイムスタンプ (概要欄) 生成を無効化")

        # Auto-append to YouTube video description.
        # Only runs when: chapters generated AND user enabled it AND we have a
        # video id (URL input, not a local file upload).
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
        footer { display: none !important; }
        a[href*="gradio.app"] { display: none !important; }
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

    with gr.Blocks(
        title="Clip Extractor - 配信切り抜き自動生成",
        analytics_enabled=False,
        **BLOCKS_THEME_KWARGS,
    ) as app:
        gr.HTML("<h1 class='main-title'>Clip Extractor</h1>")
        gr.HTML("<p class='subtitle'>YouTube配信アーカイブから切り抜きショート動画を自動生成</p>")

        with gr.Tabs():
            # --- Input Tab ---
            with gr.Tab("Input / 入力"):
                # Generation-mode selector: users can keep both on, or run just
                # one side. When both are on, the clip-side prompt wins.
                gr.HTML("<h3>生成モード / Generation Modes</h3>")
                gr.HTML(
                    "<p style='color:#666; margin-top:-0.5em; margin-bottom:0.5em;'>"
                    "どちらか少なくとも 1 つは有効にしてください。両方有効の場合、"
                    "切り抜き側のプロンプトだけが使われます。</p>"
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
                            info="切り抜きが無効のときだけ使われます",
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

                with gr.Row():
                    with gr.Column(scale=2):
                        input_url = gr.Textbox(
                            label="YouTube URL",
                            placeholder="https://youtube.com/watch?v=...",
                            info="URLを貼り付けると自動でダウンロードします",
                        )
                        gr.HTML("<p style='text-align:center; color:#999;'>または</p>")
                        input_file = gr.File(
                            label="ローカルファイル",
                            file_types=["video"],
                            type="filepath",
                        )

                    with gr.Column(scale=1):
                        num_clips = gr.Number(
                            minimum=1, maximum=50, value=defaults["num_clips"],
                            precision=0,
                            label="クリップ数",
                            info="1〜50 個。大きくしすぎると面白くないシーンも混ざりやすくなります (推奨: 3〜10)",
                        )
                        output_mode = gr.Radio(
                            choices=["combined", "individual"],
                            value=defaults.get("output_mode", "combined"),
                            label="出力モード",
                            info="combined: 1つのXMLに全シーケンス / individual: クリップごとに別XML",
                        )
                        generate_shorts = gr.Checkbox(
                            label="ショート動画 (9:16) も生成",
                            value=defaults.get("generate_shorts", False),
                            info="字幕を焼き込んだ縦型クリップを shorts/ に追加出力。下の『デフォルトに設定』で保存されます",
                        )
                        shorts_mode = gr.Radio(
                            choices=["crop", "blur", "pad"],
                            value=defaults.get("shorts_mode", "crop"),
                            label="ショート動画の変換モード",
                            info="crop: 縦型に切り抜き / blur: ぼかし背景 / pad: 黒帯で全体表示",
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
                            info="各ショートの最初の4秒だけ、上部中央にタイトルを焼き込みます",
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
                        karaoke = gr.Checkbox(
                            label="ワード単位カラオケ字幕 / Word-level karaoke captions",
                            value=defaults.get("karaoke", False),
                            info="ショート動画の焼き込み字幕を単語ごとにハイライトします / Highlight burned-in Shorts captions word by word",
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

                session_state = gr.State({})
                highlights_state = gr.State([])

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
                            info="Claudeの場合は不要 (CLI使用)。下の『保存』ボタンで .gemini_key に書き出すと 次回起動時から自動読み込みされます。キーの取得方法は下の📘アコーディオン参照。",
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
                            "📘 Gemini APIキーの取得手順 (クリックで展開) — 無料・約2分",
                            open=False,
                        ):
                            gr.Markdown(
                                """
**所要時間: 約 2 分。クレジットカード登録は不要**で、無料枠だけで使い始められます。
🔰 PC 操作に慣れていない方は、アプリのフォルダにある **`SETUP_GUIDE.html`** (ダブルクリックで開く図解ガイド) がおすすめです。

> ⚠️ ここで取る「Gemini API キー」と、下の YouTube API 認証で使う
> 「credentials.json」は **まったくの別物** です。
> Gemini キー = AI 分析用 / credentials.json = YouTube・Drive 連携用。混同注意。

#### 取得手順 (Google AI Studio)

1. 🔗 [aistudio.google.com/apikey](https://aistudio.google.com/apikey) を開く
   (Google AI Studio の「API Keys」ページに直行します)
2. **Google アカウントでログイン**
   - ⚠️ 会社・学校の Google Workspace アカウントだと、組織の設定で
     AI Studio がブロックされていることがあります → **個人の Gmail を推奨**
3. 初回は**利用規約への同意**を求められるので同意
   (このとき「デフォルトプロジェクト」と初期キーが自動作成されることがあります —
   それが表示されたらそのまま使って OK)
4. **[+ APIキーを作成]** (Create API key) ボタンをクリック
5. プロジェクト選択を聞かれたら: よく分からなければ**新規作成**でOK
   (Google Cloud の知識は不要。名前を付けるだけ)
6. 生成されたキー (`AIza...` で始まる文字列) を **[コピー]**
7. この上の **「APIキー」欄に貼り付け** → **[💾 このキーを保存]** を押す
   → 次回起動から自動読み込みされます

#### ハマりポイント早見表

| 症状 / 疑問 | 答え |
|---|---|
| お金はかかる？ | **無料枠あり・クレカ登録不要**。無料のまま使えます |
| どのモデルが無料？ | 3つとも無料枠で使えます (`gemini-2.5-flash` 既定 / `flash-lite` / `2.5-pro`)。ただし **Pro は無料枠のレート上限が flash 系よりかなり厳しく 429 が出やすい** — 普段使いは既定の flash が無難 |
| `429` エラーが出た | 無料枠のレート制限超過。**数分〜しばらく待って再実行**すれば復活します |
| 昔作ったキーが急に弾かれる | 2026年の新セキュリティ移行で、**古い「制限なし」キーは 2026/6/19 から順次拒否**されます。AI Studio で**キーを新規作成し直す**のが最速 (新キーは自動で適切に制限済み) |
| キーをなくした / 漏れたかも | AI Studio の API Keys ページで削除して作り直し。**キーはパスワードと同じ扱い**で (人に見せない・公開リポジトリに書かない) |

保存先: このフォルダの `.gemini_key` ファイル (環境変数 `GEMINI_API_KEY` より優先)。
"""
                            )

                        def update_models(provider):
                            if provider == "openai":
                                return gr.update(choices=["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini", "o3", "o3-mini"], value="gpt-4.1")
                            elif provider == "gemini":
                                return gr.update(choices=["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"], value="gemini-2.5-flash")
                            else:
                                return gr.update(choices=[], value="")

                        ai_provider.change(fn=update_models, inputs=ai_provider, outputs=ai_model)

                    with gr.Column():
                        gr.HTML("<h3>Highlight Detection</h3>")
                        gr.HTML(
                            "<p style='color:#666; margin-top:-0.5em;'>"
                            "プロンプトは Input タブの各モード欄で指定します。ここでは"
                            "切り抜きの長さ範囲だけ指定。</p>"
                        )
                        with gr.Row():
                            min_duration = gr.Number(
                                label="最小クリップ長 (秒)", value=defaults["min_duration"], precision=0,
                            )
                            max_duration = gr.Number(
                                label="最大クリップ長 (秒)", value=defaults["max_duration"], precision=0,
                            )

                        gr.HTML("<h3 style='margin-top: 1.5em;'>出力先 / Output Destination</h3>")
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
                        # The bundled heavy gothic (fonts/NotoSansJP-Black.ttf) is
                        # not installed system-wide, so surface it explicitly as the
                        # first choice and the default.
                        BUNDLED_FONT = "Noto Sans JP Black"
                        font_choices = [BUNDLED_FONT] + [f for f in system_fonts if f != BUNDLED_FONT]
                        saved_font = defaults["font_name"]
                        default_font = saved_font if saved_font in font_choices else BUNDLED_FONT
                        font_name = gr.Dropdown(
                            choices=font_choices,
                            value=default_font,
                            label="フォント名",
                            allow_custom_value=True,
                            info="先頭の「Noto Sans JP Black」は同梱の極太ゴシック（ショート字幕向けの既定）。その他はPCにインストール済みのフォント、直接入力も可。",
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
                            "📘 credentials.json の取得手順 (クリックで展開) — 初めての方はこちら",
                            open=False,
                        ):
                            gr.Markdown(
                                """
**所要時間: 約 10 分** — 最初の 1 回だけ必要な作業です。
プロジェクト同梱の `CREDENTIALS_SETUP.txt` にも同じ手順があります。
🔰 PC 操作に慣れていない方は、アプリのフォルダにある **`SETUP_GUIDE.html`** (ダブルクリックで開く図解ガイド) がおすすめです。

> 💰 **料金について**: Google Drive API / YouTube Data API v3 ともに
> **個人利用の範囲では完全無料** です。クレジットカード登録も不要。
> YouTube 側にのみ「1日 10,000 units」のクォータがあり、概要欄更新は
> 1回 50 units なので **1日約 200 動画まで** は確実に無料で動きます。

---

#### 1. Google Cloud Console にアクセス
[https://console.cloud.google.com/](https://console.cloud.google.com/) を開く
（初回は Google アカウントでログイン）

> ⚠️ トップ画面の「**Create Gemini API key**」「**Get Agent Platform API Key**」は
> clip-extractor には**使いません** (別物の API キー)。今から作るのは
> OAuth 2.0 クライアント ID という別種類の認証情報です。

#### 2. 新しいプロジェクトを作成
1. **プロジェクトセレクタを開く**
   - 場所: 画面**最上部の青いバー**にある **「Google Cloud」のロゴ／文字のすぐ右側** にあるドロップダウン
   - 初回ログイン直後は「プロジェクトを選択」または「No organization」と表示されています
   - クリックするとポップアップが開きます
2. ポップアップ右上の **[+ 新しいプロジェクト]** ボタンをクリック
3. 作成フォームに入力:
   - プロジェクト名: `clip-extractor` (任意)
   - 組織: 個人 Gmail アカウントなら「組織なし」のまま
4. 青い **[作成]** ボタン → 数秒で完了通知
5. ★ 必須 ★ **作成したプロジェクトに切り替える**:
   - 右下の通知の「プロジェクトを選択」リンク、または同じプロジェクトセレクタをもう一度開いて一覧から `clip-extractor` をクリック
   - 確認: 上部のセレクタが **「clip-extractor」** 表示になっていれば OK
   - ⚠️ 切り替え忘れが以降の全操作を別プロジェクトで行わせる No.1 のトラップです

#### 3. 使う API を有効化
**左メニューの出し方**: 画面**左上の ≡ (三本線アイコン)** をクリック →
ドロワーから **[APIとサービス] → [ライブラリ]**

🔗 **最短**: 直接ライブラリを開く → [console.cloud.google.com/apis/library](https://console.cloud.google.com/apis/library)

**3-a. YouTube Data API v3 を有効化**:
1. ライブラリ画面の検索欄に `YouTube Data API v3` と入力
2. 出てきたカードをクリック (※ 似た名前の "YouTube Analytics API" / "YouTube Reporting API" は別物。必ず **Data API v3** を選ぶ)
3. 青い **[有効にする]** (Enable) ボタンを押す
4. 5〜30 秒でスピナーが止まり、画面が「**製品の詳細**」に切り替わる
5. 成功判定: 画面に緑の ✓ **API が有効です** が出る

**3-b. この画面で出てくる 3 つの表示 (★ 初心者ハマりポイント)**:
有効化後の画面には次の 3 つが並びます:

| 表示 | 意味 | 初回設定で押す？ |
|---|---|---|
| ✓ API が有効です (緑チェック) | ただのステータス表示 (ボタンではない) | — |
| **[管理]** ボタン | クォータ消費量・エラー率などの監視画面 | ❌ 押さない |
| **[この API を試す]** リンク | API Explorer (ブラウザから API を直接叩くツール) | ❌ 押さない |

→ **どれも押さずに画面から離れます。** 押すと別画面に飛んで迷子になります。

**3-c. Drive API も使う場合** (概要欄だけなら skip OK):
1. 左上の **← 戻る矢印** (ブラウザの戻るボタン) でライブラリに戻る
2. 検索欄に `Google Drive API` → カードをクリック → **[有効にする] (Enable)**
3. 同じく ✓ API が有効です が出たら成功

**3-d. 有効化済み → 次の画面へ** (画面 3 から先への進み方):
- ここに「次へ」ボタンはありません
- **(A) 初めて OAuth 設定する場合**: 手動で **左上の ≡ → API とサービス → OAuth 同意画面** に進む (新画面 **Google Auth Platform** に移動します) → 次ステップ #4 へ
- **(B) このプロジェクトで初期設定済み**の場合: #4 をスキップして #5 (OAuth クライアント作成) に直行 OK
- どちらも直接 URL を使うのが最速

#### 4. Google Auth Platform の初期設定 (初回のみ・ウィザード形式)
🔗 **直接 URL**: [console.cloud.google.com/auth/overview](https://console.cloud.google.com/auth/overview)
（2025 年に画面が刷新されました。古い解説記事の URL `…/apis/credentials/consent` を開いても、自動でこの新画面にリダイレクトされます）

初回は「Google Auth Platform はまだ構成されていません」と表示されるので、**[使ってみる]** (英語UI: [Get started]) をクリック。**4 ステップのウィザード**が始まります:

**4-a. アプリ情報**
1. **アプリ名**: `clip-extractor` と入力 (認証画面でユーザーに表示される名前)
2. **ユーザーサポートメール**: プルダウンから自分の Gmail を選択
3. **[次へ]** をクリック

**4-b. 対象 (Audience)**
1. **「外部 (External)」** を選択 (個人の Google アカウントだと「外部」しか選べません)
2. **[次へ]** をクリック

**4-c. 連絡先情報 → 完了**
1. **連絡先情報**: 自分のメールアドレスを入力 → **[次へ]**
2. **完了**: Google API サービスのポリシーへの同意にチェック → **[作成]**

> ℹ️ 旧 UI にあった「スコープ」「テストユーザー」のページはこのウィザードには**ありません**。
> スコープ登録は不要 (アプリ側で指定) ですが、テストユーザーだけは次の 4-d で必ず登録します。

**4-d. テストユーザーを追加** ★ ここが最重要・一番忘れやすい ★
1. ウィザードが終わったら、左サイドバーの **[対象]** (Audience) をクリック
   🔗 直接 URL: [console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience)
2. 画面中段の「テストユーザー」セクションで **[+ Add users]** / **[+ ユーザーを追加]** をクリック
3. ★ **認証に使う自分の Google アカウント (Gmail) を入力して [保存]** ★
   （新 UI ではこの登録がウィザードに含まれないため、飛ばしてしまうのが現在の No.1 トラップ。
   忘れると後の認証で「アクセスがブロックされました」エラーになります）

> ⏰ アプリが「テスト」状態だと発行トークンは 7 日で失効します。
> 再認証すれば継続利用可。恒久化したければ同じ [対象] ページの公開ステータスで **[アプリを公開]** →
> 本番モードに切り替え (個人用途なら審査通常不要)。

#### 5. OAuth クライアントを作成
🔗 **直接 URL**: [console.cloud.google.com/auth/clients](https://console.cloud.google.com/auth/clients)
（または Google Auth Platform 画面の左サイドバー **[クライアント]**）

1. ページ上部の **[+ クライアントを作成]** (CREATE CLIENT) をクリック
2. **アプリケーションの種類** で **★ デスクトップ アプリ ★** を選択
   （「ウェブ アプリケーション」を選ぶと redirect_uri_mismatch エラーの原因になります）
3. **名前** に `clip-extractor desktop` と入力 (任意の名前で可)
4. 青い **[作成]** ボタンをクリック

#### 6. JSON をダウンロード ★ このダイアログで必ず保存 ★
1. #5 の **[作成]** 直後に出る **「OAuth クライアントを作成しました」** ダイアログを確認
2. **[JSON をダウンロード]** (Download JSON) ボタンをクリック
3. ブラウザの既定ダウンロード先 (通常は **ダウンロード / Downloads フォルダ**) に
   `client_secret_<長い英数字>.apps.googleusercontent.com.json` という名前で保存されます

> ⚠️ **2025 年 6 月の仕様変更**: シークレット入りの完全な JSON を取得できるのは
> **この作成直後のダイアログ 1 回だけ**です (以降の一覧画面ではマスク表示され再ダウンロード不可)。
> 閉じてしまった場合は、クライアント詳細画面でシークレットを**リセット**して新しい JSON を
> 取得するか、クライアントを削除して #5 から作り直してください (何度でも無料)。

⚠️ このファイルは**機密情報**です。第三者に共有したり、公開リポジトリへ
コミットしたりしないでください (誤コミット防止のため、後述の配置先は
リポジトリ外のユーザー設定ディレクトリになっています)。

#### 7. credentials.json を配置する (= 配置)
**▼ 方法A (おすすめ): 下の欄にドラッグ & ドロップ 👇**
1. ダウンロードした `client_secret_….json` を、このページ下の
   **「credentials.json (ドラッグ＆ドロップ可)」** 欄にドロップ
   （ファイル名はリネーム不要 — 自動で `credentials.json` として保存されます）
2. 緑の成功メッセージが出て、上部ステータスが
   **「credentials.json 配置済」** に変われば完了
3. 保存先はユーザー設定ディレクトリ（このページ上部に実パスを表示しています）

**▼ 方法B: ファイルを手動で配置**
ダウンロードした JSON を `credentials.json` にリネームし、OS ごとの場所に置きます:

| OS | 配置パス |
|---|---|
| **Windows** | `%APPDATA%\\clip-extractor\\credentials.json`<br/>(通常 `C:\\Users\\<ユーザー名>\\AppData\\Roaming\\clip-extractor\\`) |
| **macOS** | `~/Library/Application Support/clip-extractor/credentials.json` |
| **Linux** | `~/.config/clip-extractor/credentials.json` |

配置確認 (CLI): `python main.py --youtube-status` / `python main.py --drive-status`

#### 8. 「② 認証アクション」で初回認証を実行
1. このページの **「② 認証アクション」** セクションの **[認証する]** ボタンを押す
2. 既定ブラウザで **Google の承認画面**が開きます
3. **#4-d でテストユーザー登録したアカウント** を選択
   （未登録のアカウントを選ぶと「アクセスがブロックされました」になります）
4. 「このアプリは Google で確認されていません」と出ても、
   自分で作ったアプリなので **[詳細] → [(安全でないページ) に移動]** で続行して OK
5. clip-extractor へのアクセス要求を **[許可]**
6. ステータスが **「認証済み」** に変われば完了。
   以降はトークンが自動更新されるので、手動操作は不要です

---

**困ったとき:** `CREDENTIALS_SETUP.txt` の「トラブルシューティング」節を参照。
よくある症状: テストユーザー未追加 / OAuth クライアントが「ウェブ アプリ」
になっている / JSON をダウンロードし忘れた (作成時のみ取得可) /
テスト公開のまま 7 日経過でトークン失効。
"""
                            )
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
                                    "2) 左の『認証情報』→『認証情報を作成』→『OAuth クライアント ID』\n"
                                    "3) アプリの種類: 『デスクトップアプリ』を選択して作成\n"
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
                            karaoke],
                    outputs=save_defaults_msg,
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

            # --- OBS連携 Tab ---
            with gr.Tab("OBS連携 / OBS"):
                gr.Markdown(
                    "### 配信終了で自動切り抜き\n"
                    "OBS での配信/録画終了を検知して、既存の切り抜き・チャプター生成パイプライン"
                    "(文字起こし → ハイライト検出 → チャプター/切り抜き生成)を自動実行します。\n\n"
                    "#### ① OBS 側の準備(最初に1回だけ)\n"
                    "**WebSocket 方式(推奨)** を使うには、OBS 側で WebSocket サーバーを有効にします:\n\n"
                    "1. OBS メニュー → **ツール(Tools)** → **WebSocket サーバー設定(WebSocket Server Settings)** を開く\n"
                    "2. **「WebSocket サーバーを有効にする」にチェック**を入れる\n"
                    "3. **「接続情報を表示(Show Connect Info)」** ボタンで **サーバー IP / ポート(既定 4455)/ パスワード** を確認できる\n"
                    "4. **OK / 適用** を押す(ポート・パスワードは初期値のままでOK)\n\n"
                    "> ⚠️ OBS が起動しているだけでは繋がりません。上記でサーバーを **有効化** する必要があります。\n\n"
                    "#### ② このタブの設定\n"
                    "下の **Host / Port / Password** を OBS の接続情報と同じ値にして、**「OBS連携 開始」** を押してください"
                    "(同じ PC なら Host は `localhost` のまま、Port は `4455`)。\n\n"
                    "- **検知する停止イベント**: `stream`=配信停止で発火 / `record`=録画停止で発火\n"
                    "- ⚠️ `stream`(配信停止)で使う場合は、**配信中に録画も同時に ON** にしてください"
                    "(処理対象のローカル録画ファイルが必要なため)\n\n"
                    "#### フォルダ監視方式(WebSocket を使わない代替)\n"
                    "**検知方式** を `folder` にして OBS の録画出力先フォルダを指定すると、"
                    "新規動画ファイルの書き込み完了を検知して自動処理します(OBS WebSocket 不要)。"
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        obs_trigger_radio = gr.Radio(
                            ["websocket", "folder"],
                            label="検知方式 / Trigger",
                            value=defaults.get("obs_trigger_method", "websocket"),
                            info="websocket=OBS WebSocket, folder=フォルダ監視",
                        )
                        obs_stop_event_radio = gr.Radio(
                            ["stream", "record"],
                            label="検知する停止イベント",
                            value=defaults.get("obs_stop_event", "stream"),
                            info="stream=配信停止で発火, record=録画停止で発火",
                        )
                        obs_auto_process = gr.Checkbox(
                            label="検知後に自動で切り抜き/チャプター生成まで実行",
                            value=bool(defaults.get("obs_auto_process", True)),
                        )
                    with gr.Column(scale=1):
                        obs_host = gr.Textbox(
                            label="WebSocket Host",
                            value=defaults.get("obs_host", "localhost"),
                        )
                        obs_port = gr.Number(
                            label="WebSocket Port",
                            value=defaults.get("obs_port", 4455),
                            precision=0,
                        )
                        obs_password = gr.Textbox(
                            label="WebSocket Password",
                            value=defaults.get("obs_password", ""),
                            type="password",
                        )
                        obs_watch_folder = gr.Textbox(
                            label="録画出力フォルダ (folder 方式 / またはパス補完用)",
                            value=defaults.get("obs_watch_folder", ""),
                            info="folder 方式で監視するフォルダの絶対パス",
                        )

                with gr.Row():
                    obs_start_btn = gr.Button("OBS連携 開始", variant="primary")
                    obs_stop_btn = gr.Button("OBS連携 停止")
                    obs_refresh_btn = gr.Button("状態を更新")

                obs_status_box = gr.Textbox(
                    label="OBS連携ステータス",
                    lines=12,
                    interactive=False,
                    value="",
                )

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

        detect_event = detect_btn.click(
            fn=detect_phase,
            inputs=[
                input_url, input_file,
                enable_clips, clip_prompt, enable_chapters, chapter_prompt,
                num_clips, ai_provider, ai_model, api_key,
                min_duration, max_duration,
                whisper_model, language,
                audio_fusion, audio_alpha,
                output_base_dir,
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
            ],
            outputs=[log_output, highlights_output, download_output, drive_link_output, chapters_output],
            concurrency_limit=1,
        )

        render_btn.click(
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
            ],
            outputs=[log_output, highlights_output, download_output, drive_link_output, chapters_output],
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
                obs_stop_event_radio,
                obs_watch_folder,
                obs_auto_process,
                num_clips,
                output_mode,
                generate_shorts,
                ai_provider,
                whisper_model,
                output_base_dir,
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

        # Instructions
        with gr.Accordion("使い方 / How to Use", open=False):
            gr.Markdown("""
### 基本的な使い方
1. **Input** タブでYouTube URLを貼り付けるか、動画ファイルをアップロード
2. クリップ数や出力モードを設定
3. **Generate Clips** ボタンをクリック
4. **Output** タブで結果を確認、ZIPファイルをダウンロード

### 分析 AI の準備 (Gemini を使う場合)
Gemini は**無料枠あり・クレカ登録不要**で一番手軽です。
1. 🔗 [aistudio.google.com/apikey](https://aistudio.google.com/apikey) を開き Google アカウントでログイン (個人 Gmail 推奨)
2. **[+ APIキーを作成]** → プロジェクトは新規作成で OK → キーをコピー
3. Settings タブの「APIキー」欄に貼り付け → **[💾 このキーを保存]**

詳しい手順とハマりポイント (無料で使えるモデル / 429 エラー / 古いキーが
2026年6月以降拒否される件など) は、Settings タブの
**「📘 Gemini APIキーの取得手順」** アコーディオンにまとめてあります。

> ⚠️ この Gemini API キーと、下で説明する `credentials.json` (YouTube/Drive 連携用) は**別物**です。

### Premiere Pro での読み込み
1. ダウンロードしたZIPを展開
2. Premiere Pro → File → Import → `project.xml` を選択
3. 各シーケンスにクリップが配置済み
4. SRTファイルをキャプションとしてインポート
5. フォント・位置・カット位置を自由に調整

### Photoshopでテロップを編集する方法
1. SRTキャプションをタイムライン上で選択
2. 右クリック → 「グラフィックにアップグレード」でテキストレイヤーに変換
3. テキストレイヤーを右クリック → 「Adobe Photoshopで編集」
4. Photoshopでフォント・装飾・エフェクトを自由に編集
5. 保存するとPremiere Proに即反映

### Google Drive アップロード / YouTube 概要欄自動更新 — 共通のセットアップ
**どちらの機能も 1 つの `credentials.json` で動きます。** 詳細な手順は
プロジェクト同梱の **`CREDENTIALS_SETUP.txt`** を参照するか、
Settings タブの「📘 credentials.json の取得手順」アコーディオンを展開してください。

> 💰 **料金**: 両 API とも **個人利用では無料** (クレカ登録不要)。
> YouTube 側のみ「1日 10,000 units」のクォータがあり、概要欄更新は
> 1回 50 units なので **1日約 200 動画** まで無料で動きます。
> Drive は API 呼び出し自体に課金なし (ストレージは Drive 容量を使用)。

**📘 より詳しい手順は** Settings タブの「📘 credentials.json の取得手順」
アコーディオン、またはプロジェクト同梱の **`CREDENTIALS_SETUP.txt`** を参照。
画面ごとに「何が見えるか」「どのボタンを押すか」「押した後どうなるか」まで記載しています。

手順 (細かく分けた 19 step — 初めての方用):

**▼ Phase 1: プロジェクト準備 (〜2 分)**

1. [https://console.cloud.google.com/](https://console.cloud.google.com/) を開く
   → Google アカウントでログイン
2. トップ画面の `Create Gemini API key` や `Get Agent Platform API Key` **は押さない** (別物)
3. **プロジェクトセレクタをクリック**
   - 場所: 画面最上部の青いバーにある **「Google Cloud」のロゴ／文字のすぐ右側** にあるドロップダウン
   - 初回は「プロジェクトを選択」または「No organization」と表示
4. ポップアップ右上の **[+ 新しいプロジェクト]** をクリック
5. プロジェクト名に `clip-extractor` (任意) を入力 → **[作成]**
6. 数秒で完了通知 → ★ **上部のセレクタで作成したプロジェクトに切り替え** ★
   - 右下の通知の「プロジェクトを選択」リンク、またはセレクタを再度開いて `clip-extractor` をクリック
   - 確認: 上部セレクタに **「clip-extractor」** が表示される
   - ⚠️ 切り替え忘れが以降の全操作を無効にする No.1 原因

**▼ Phase 2: API 有効化 (〜2 分)**

7. 🔗 [console.cloud.google.com/apis/library](https://console.cloud.google.com/apis/library) を開く
   (または左上 ≡ → API とサービス → ライブラリ)
8. 検索欄に `YouTube Data API v3` → **カードをクリック**
   (「YouTube Analytics API」「YouTube Reporting API」は別物なので選ばない)
9. 青い **[有効にする]** ボタンをクリック → 5〜30 秒でスピナーが止まる
10. 画面が「**製品の詳細**」に切り替わり、**✓ API が有効です** が出たら成功
    - ★ ここで出る「**管理**」ボタンと「**この API を試す**」ボタンは**押さない** ★
    - 「管理」= クォータ監視画面 / 「試す」= API Explorer — どちらも設定作業に無関係
11. Drive 機能も使うなら: **左上の ← 戻る矢印** でライブラリに戻り、
    `Google Drive API` で 8〜10 を繰り返す (使わないなら skip OK)

**▼ Phase 3: Google Auth Platform の初期設定 (〜3 分、初回のみ)**

※ 2025 年に画面が刷新され「**Google Auth Platform**」になりました。
古い記事の「OAuth 同意画面 → スコープ → テストユーザー」の 4 ページ構成とは違います。

12. 🔗 [console.cloud.google.com/auth/overview](https://console.cloud.google.com/auth/overview) を開く
    (または左上 ≡ → API とサービス → OAuth 同意画面。旧 URL からは自動リダイレクト)
13. 初回は「まだ構成されていません」表示 → **[使ってみる]** (Get started) をクリック
14. ウィザード **①アプリ情報**: アプリ名 `clip-extractor` + サポートメール (自分の Gmail) → **[次へ]**
15. ウィザード **②対象**: **外部** を選択 → **[次へ]**
    (個人 Gmail だと「外部」しか選べない)
16. ウィザード **③連絡先情報**: 自分のアドレス → **[次へ]** /
    **④完了**: ポリシー同意にチェック → **[作成]**
    (スコープのページはありません — アプリ側で指定するので登録不要)
17. ★ **最重要・一番忘れやすい** ★ テストユーザーを追加:
    - 左サイドバーの **[対象]** (Audience) をクリック
      (🔗 [console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience))
    - 「テストユーザー」セクションの **[+ Add users]** / **[+ ユーザーを追加]**
    - 自分の Google アカウントのメールアドレスを入力 → **[保存]**
    - ※ 新 UI ではウィザードに含まれないため飛ばしがち。忘れると認証時に
      「**アクセスがブロックされました**」(403: access_denied) が出ます

**▼ Phase 4: OAuth クライアント作成 (〜1 分)**

18. 🔗 [console.cloud.google.com/auth/clients](https://console.cloud.google.com/auth/clients) を開く
    (または左サイドバーの **[クライアント]**)
    - ページ上部の **[+ クライアントを作成]** をクリック
    - アプリケーションの種類: ★ **[デスクトップ アプリ]** ★
      (「ウェブ アプリケーション」を選ぶと後で `redirect_uri_mismatch` エラー)
    - 名前: `clip-extractor desktop` (任意)
    - **[作成]**

**▼ Phase 5: JSON をダウンロードして配置 (〜30 秒)**

19. 「**OAuth クライアントを作成しました**」ダイアログが出たら
    - ★ **閉じる前に必ず** ★ **[JSON をダウンロード]** をクリック
    - ファイル名は `client_secret_xxxxxxxx.json`
    - ⚠️ 2025年6月の仕様変更で、シークレット入り JSON は**作成直後の 1 回しか取得できません**。
      閉じてしまったら: クライアント詳細でシークレットをリセットして新 JSON を取得、
      またはクライアントを削除して 18 からやり直し (何度でも無料)
    - ダウンロードしたファイルを、Settings タブの
      **「credentials.json (ドラッグ＆ドロップ可)」欄にドロップ**
    - 緑の成功メッセージが出れば配置完了
    - Settings タブから **[認証する]** を押す → ブラウザで Google 承認 → 完了
    - または CLI: `python main.py --youtube-setup` / `python main.py --drive-setup`

配置先: `%APPDATA%/clip-extractor/`（Windows）/
`~/Library/Application Support/clip-extractor/`（macOS）/
`~/.config/clip-extractor/`（Linux）— プロジェクト外で管理されます。

### 生成モード（切り抜き / 概要欄 の独立選択）
- **両方 ON (デフォルト)**: 切り抜き動画・SRT・Premiere XML・概要欄テキストをまとめて出力。切り抜き側のプロンプトだけが使われます（概要欄プロンプトは無視）。
- **切り抜きのみ**: クリップ + SRT + XML を出力。概要欄テキストは生成されません。
- **概要欄のみ**: ハイライト検出を概要欄プロンプトで実行し、`chapters.txt` だけを出力。クリップ抽出・SRT・XML はスキップ。
- **両方 OFF**: エラーになります。1 つは有効にしてください。

### ショート動画のフォント設定（9:16 出力のみ）
1. Settings タブの Font Settings でフォント名・サイズ・色を選択
2. 「ショート動画 (9:16) も生成」をチェックして Generate
3. 出力された Shorts には字幕が焼き込まれ、そのまま YouTube Shorts / TikTok にアップロード可能
4. 通常の横クリップ（landscape）は字幕が焼き込まれず、Premiere Pro で SRT キャプションを自由に調整できる状態のまま

### タイムスタンプ (概要欄)
1. Generate 完了後、Output タブ下部の「タイムスタンプ (概要欄)」にチャプター形式の一覧が表示される（例: `0:00 イントロ` / `3:42 ハイライト1` …）
2. そのままコピーして YouTube アップロード時の概要欄に貼り付ける
3. 先頭が必ず `0:00` から始まるため YouTube が自動でチャプターとして認識し、動画プレイヤー上にチャプターマーカーが表示される
4. `output_*/chapters.txt` にも同じ内容が保存されている

### 概要欄に自動追加 (YouTube API)
#### 初回セットアップ
**初めての方へ**: Settings タブの「📘 credentials.json の取得手順」
アコーディオンを開くと、画面キャプチャ付きの手順が表示されます。
またプロジェクト同梱の `CREDENTIALS_SETUP.txt` に同じ内容があります。

最短手順:
1. Settings タブの「YouTube API 認証」セクションで「Google Cloud Console を開く」ボタンをクリック
2. **YouTube Data API v3** を有効化 → Google Auth Platform の初期設定ウィザード → **[対象] ページでテストユーザー追加** (忘れ注意)
3. 「デスクトップ アプリ」で OAuth クライアント作成 → **作成直後のダイアログで** JSON をダウンロード (後から再取得不可)
4. Settings タブの「credentials.json」欄にドラッグ＆ドロップ
5. 「認証する」→ ブラウザで Google 承認 → `youtube_token.json` が自動生成
6. Input タブの「概要欄に自動追加」をチェックして Generate

#### 仕様
- URL 入力のみ対応（ローカルファイル時は自動スキップ）
- 該当動画の概要欄にタイムスタンプが prepend（先頭挿入）される
- scope は `youtube.force-ssl` — 自分がアップロード済みの動画のみ更新可能

### よくある認証エラーと対処

**① 「アクセスをブロック: <アプリ名> は Google の審査プロセスを完了していません」 / 「エラー 403: access_denied」**
→ **テストユーザー未登録** が原因 (最頻出)。対処:
1. 🔗 [console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience) を開く
   (または Google Auth Platform 画面の左サイドバーから **[対象]** をクリック)
2. 「テストユーザー」の **[+ ユーザーを追加]** で自分の Gmail を入力 → 保存
   - ※ このセクションが出ない場合は画面上部の公開ステータスが「本番」になっている可能性 (本番なら誰でも認証可でテストユーザー不要)
3. **1〜2 分待ってから** 再度「認証する」を押す (反映タイムラグあり)
4. 次の画面で「このアプリは Google で確認されていません」と警告が出たら **[続行]** (自分のアプリなので安全)

**② 「redirect_uri_mismatch」エラー**
→ OAuth クライアントを「ウェブ アプリケーション」で作ってしまった可能性大。
対処: [クライアント] ページ ([console.cloud.google.com/auth/clients](https://console.cloud.google.com/auth/clients)) → 該当クライアントを削除 → **「デスクトップ アプリ」** で作り直し → 作成ダイアログで JSON をダウンロード

**③ 「invalid_grant」 / 「Token has been expired or revoked」**
→ 7 日経過でリフレッシュトークン失効 (テスト中 + 外部モード + sensitive scope の組み合わせ)。
対処: Settings タブの「認証する」を再度押す、または CLI で `python main.py --youtube-setup` / `python main.py --drive-setup`

### 認証切れの挙動
- 起動時にコンソールログへ状態が表示される（認証済み / 期限切れ / 未認証 / 未設定）
- Generate 実行前にも pre-validation が走り、認証が切れていれば早期にエラー表示（長い処理が無駄にならない）
- 期限切れなら Settings タブから「認証する」を押すだけで再認証可能
- CLI で状態確認: `python main.py --youtube-status` / `python main.py --drive-status`
            """)

        app.load(fn=_startup_auth_status_for_ui, inputs=None, outputs=[yt_auth_status_box])

    return app


if __name__ == "__main__":
    app = create_ui()
    app.queue()
    app.launch(**safe_launch_kwargs(
        server_name="0.0.0.0",
        server_port=7860,
        ssr_mode=False,
        **LAUNCH_THEME_KWARGS,
    ))
