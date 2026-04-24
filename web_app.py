#!/usr/bin/env python3
"""clip-extractor Web UI using Gradio."""

import logging
import os
import shutil
import subprocess
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import gradio as gr


@dataclass
class ProcessResult:
    """Structured result from the processing pipeline.

    Fields line up with the Gradio outputs wired in generate_btn.click:
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
        """Order matches generate_btn.click(outputs=[...])."""
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

from config import FontConfig

SETTINGS_FILE = Path(__file__).parent / "default_settings.json"
GEMINI_KEY_FILE = Path(__file__).parent / ".gemini_key"


def load_gemini_api_key(env_var: str = "GEMINI_API_KEY") -> str:
    """Return the Gemini API key, preferring the environment variable.

    Env var > on-disk file. This lets CI / secret managers override the
    saved file without editing it, and keeps the key out of the project
    tree entirely when the env var is set. Falls back to the legacy
    .gemini_key file so existing installs keep working untouched.
    """
    val = os.environ.get(env_var, "").strip()
    if val:
        return val
    if GEMINI_KEY_FILE.exists():
        return GEMINI_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def load_defaults() -> dict:
    """Load saved default settings."""
    defaults = {
        "ai_provider": "gemini", "ai_model": "gemini-3-flash-preview",
        "enable_clips": True, "enable_chapters": True,
        "clip_prompt": "", "chapter_prompt": "",
        "auto_append_youtube": False,
        "num_clips": 5, "min_duration": 30, "max_duration": 90,
        "output_mode": "combined", "generate_shorts": False,
        "whisper_model": "large-v3", "language": "ja",
        "font_name": "Noto Sans JP", "font_size": 96, "font_color": "#FFFFFF",
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
                  num_clips, min_duration, max_duration,
                  whisper_model, language,
                  font_name, font_size, font_color):
    """Save current settings as defaults."""
    data = {
        "ai_provider": ai_provider, "ai_model": ai_model,
        "enable_clips": bool(enable_clips), "enable_chapters": bool(enable_chapters),
        "clip_prompt": clip_prompt, "chapter_prompt": chapter_prompt,
        "auto_append_youtube": bool(auto_append_youtube),
        "num_clips": int(num_clips),
        "min_duration": int(min_duration), "max_duration": int(max_duration),
        "whisper_model": whisper_model, "language": language,
        "font_name": font_name, "font_size": int(font_size),
        "font_color": font_color,
    }
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "Settings saved as default!"
from chapters import generate_chapter_text, write_chapter_file
from downloader import download_video
from transcriber import transcribe, segments_to_text
from highlighter import detect_highlights
from clipper import extract_clips, get_video_info
from subtitles import generate_all_srts
from premiere_xml import generate_combined_xml, generate_individual_xmls
from drive_upload import upload_output_directory, is_configured as drive_is_configured
from modes import GenerationModes
import youtube_api


def process_video(
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
        output_dir = Path(f"./output_{timestamp}")
        output_dir.mkdir(parents=True, exist_ok=True)

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

            # Shorts (9:16) — generate SRTs first, then extract with font-styled burn-in
            if generate_shorts:
                progress(0.75, desc="Generating shorts (9:16) with burned-in subtitles...")
                shorts_dir = output_dir / "shorts"
                shorts_dir.mkdir(parents=True, exist_ok=True)
                shorts_srt_paths = generate_all_srts(segments, highlights, shorts_dir)
                shorts_paths = extract_clips(
                    video_path, highlights, shorts_dir,
                    shorts=True,
                    srt_paths=shorts_srt_paths,
                    font_config=font_config,
                )
                log(f"  Generated {len(shorts_paths)} shorts with {font_config.font_name} @ {font_config.font_size}pt")

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


def create_ui():
    """Create the Gradio web interface."""
    defaults = load_defaults()

    # Startup auth probe — silent (no browser). Just log the current state
    # so the user sees in the console whether their YouTube token is ready.
    try:
        _yt_status = youtube_api.check_auth_status()
        if _yt_status["authenticated"]:
            logger.info("YouTube auth: 認証済み (token 有効)")
        elif _yt_status["expired"]:
            logger.warning(
                f"YouTube auth: 期限切れ — Settings タブで再認証してください "
                f"({_yt_status.get('error') or ''})"
            )
        elif _yt_status["configured"]:
            logger.info("YouTube auth: 未認証 (credentials.json は配置済、初回認証が必要)")
        else:
            logger.info("YouTube auth: 未設定 (credentials.json なし — auto-append 無効)")
    except Exception as _yt_err:
        logger.warning(f"YouTube auth startup probe failed: {_yt_err}")

    with gr.Blocks(
        title="Clip Extractor - 配信切り抜き自動生成",
        analytics_enabled=False,
        theme=gr.themes.Soft(),
        css="""
        .main-title { text-align: center; margin-bottom: 0.5em; }
        .subtitle { text-align: center; color: #666; margin-bottom: 1.5em; }
        footer { display: none !important; }
        a[href*="gradio.app"] { display: none !important; }
        """,
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
                        num_clips = gr.Slider(
                            minimum=1, maximum=10, value=defaults["num_clips"], step=1,
                            label="クリップ数",
                        )
                        output_mode = gr.Radio(
                            choices=["combined", "individual"],
                            value=defaults.get("output_mode", "combined"),
                            label="出力モード",
                            info="combined: 1つのXMLに全シーケンス / individual: クリップごとに別XML",
                        )
                        generate_shorts = gr.Checkbox(
                            label="ショート動画 (9:16) も生成",
                            value=False,
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
                            info="空欄でデフォルト (Claude=CLI, OpenAI=gpt-4.1, Gemini=gemini-3-flash-preview)",
                        )
                        saved_api_key = load_gemini_api_key()
                        api_key = gr.Textbox(
                            label="APIキー",
                            value=saved_api_key,
                            placeholder="OpenAI / Gemini のAPIキーを入力",
                            type="password",
                            info="Claudeの場合は不要 (CLI使用)",
                        )

                        def update_models(provider):
                            if provider == "openai":
                                return gr.update(choices=["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini", "o3", "o3-mini"], value="gpt-4.1")
                            elif provider == "gemini":
                                return gr.update(choices=["gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"], value="gemini-3-flash-preview")
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
                        system_fonts = get_system_fonts()
                        saved_font = defaults["font_name"]
                        default_font = saved_font if saved_font in system_fonts else (system_fonts[0] if system_fonts else "Noto Sans JP")
                        font_name = gr.Dropdown(
                            choices=system_fonts,
                            value=default_font,
                            label="フォント名",
                            allow_custom_value=True,
                            info="PCにインストール済みの全フォント + 直接入力可",
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
                            value=youtube_api.auth_status_summary(),
                            interactive=False,
                        )

                        # --- Step 1: credentials.json setup (developer-side OAuth client) ---
                        gr.HTML("<h4>① credentials.json を取得・配置</h4>")
                        gr.HTML(
                            "<p style='color:#666; margin-top:-0.5em;'>"
                            "まだ無い場合は Google Cloud Console でデスクトップアプリ用の "
                            "OAuth クライアントを作成し、ダウンロードした JSON をここにドロップしてください。<br/>"
                            f"<small>保存先 (OS ユーザー設定ディレクトリ): <code>{youtube_api.CREDENTIALS_PATH}</code></small></p>"
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
                            yt_refresh_btn = gr.Button("ステータス更新", variant="secondary")

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
                            num_clips, min_duration, max_duration,
                            whisper_model, language,
                            font_name, font_size, font_color],
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
                        show_copy_button=True,
                        interactive=False,
                    )

        # Generate button
        generate_btn = gr.Button(
            "Generate Clips / 生成開始",
            variant="primary",
            size="lg",
        )

        generate_btn.click(
            fn=process_video,
            inputs=[
                input_url, input_file,
                enable_clips, clip_prompt, enable_chapters, chapter_prompt,
                auto_append_youtube,
                num_clips, output_mode,
                generate_shorts, generate_zip, ai_provider, ai_model, api_key,
                min_duration, max_duration,
                whisper_model, language, font_name, font_size, font_color,
                upload_to_drive,
            ],
            outputs=[log_output, highlights_output, download_output, drive_link_output, chapters_output],
            concurrency_limit=1,
        )

        # Instructions
        with gr.Accordion("使い方 / How to Use", open=False):
            gr.Markdown("""
### 基本的な使い方
1. **Input** タブでYouTube URLを貼り付けるか、動画ファイルをアップロード
2. クリップ数や出力モードを設定
3. **Generate Clips** ボタンをクリック
4. **Output** タブで結果を確認、ZIPファイルをダウンロード

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

### Google Drive アップロード
1. [Google Cloud Console](https://console.cloud.google.com/) でプロジェクト作成
2. Drive API を有効化
3. OAuth 2.0 クライアントID（デスクトップアプリ）を作成
4. `credentials.json` をダウンロードして Settings タブの「credentials.json」欄にドラッグ＆ドロップ（ユーザー設定ディレクトリ `%APPDATA%/clip-extractor/`（Windows）/ `~/.config/clip-extractor/`（Linux）/ `~/Library/Application Support/clip-extractor/`（macOS）に自動配置）
5. 初回実行時にブラウザで認証

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
#### 初回セットアップ（UI 完結・手動コピー不要）
1. Settings タブの「YouTube API 認証」セクションで「Google Cloud Console を開く」ボタンをクリック
2. ブラウザで開かれたページから YouTube Data API v3 を**有効化**
3. 左メニュー「認証情報」→「認証情報を作成」→「OAuth クライアント ID」→ アプリの種類で**デスクトップアプリ**を選択して作成
4. ダウンロードされた `client_secret_*.json` を、Settings タブの「credentials.json」欄にドラッグ＆ドロップ（検証後に自動で正しい場所に配置されます）
5. 「認証する」ボタン → ブラウザで Google アカウント承認 → `youtube_token.json` がユーザー設定ディレクトリに自動生成（`%APPDATA%/clip-extractor/` などプロジェクト外で管理）
6. Input タブの「概要欄に自動追加」をチェックして Generate

#### 仕様
- URL 入力のみ対応（ローカルファイル時は自動スキップ）
- 該当動画の概要欄にタイムスタンプが prepend（先頭挿入）される
- scope は `youtube.force-ssl` — 自分がアップロード済みの動画のみ更新可能

### 認証切れの挙動
- 起動時にコンソールログへ状態が表示される（認証済み / 期限切れ / 未認証 / 未設定）
- Generate 実行前にも pre-validation が走り、認証が切れていれば早期にエラー表示（長い処理が無駄にならない）
- 期限切れなら Settings タブから「認証する」を押すだけで再認証可能
            """)

    return app


if __name__ == "__main__":
    app = create_ui()
    app.queue()
    app.launch(
        server_name="0.0.0.0",
        server_port=8080,
        ssr_mode=False,
    )
