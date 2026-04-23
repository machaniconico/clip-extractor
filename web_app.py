#!/usr/bin/env python3
"""clip-extractor Web UI using Gradio."""

import json
import logging
import os
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

import gradio as gr

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

from config import FontConfig
from downloader import is_youtube_url, download_video
from transcriber import segments_to_text
from transcript_cache import transcribe_with_cache
from highlighter import detect_highlights
from clipper import extract_clips, get_video_info
from subtitles import generate_all_srts
from premiere_xml import generate_combined_xml, generate_individual_xmls
from drive_upload import upload_output_directory, is_configured as drive_is_configured
from chapter_generator import (
    generate_chapters,
    search_moments,
    format_chapters_for_youtube,
    format_moments_for_display,
)
import youtube_api

SETTINGS_FILE = Path(__file__).parent / "default_settings.json"
GEMINI_KEY_FILE = Path(__file__).parent / ".gemini_key"


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


def load_defaults() -> dict:
    """Load saved default settings."""
    defaults = {
        "ai_provider": "gemini", "ai_model": "gemini-3-flash-preview", "custom_prompt": "",
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


def save_defaults(ai_provider, ai_model, custom_prompt, num_clips,
                  min_duration, max_duration, whisper_model, language,
                  font_name, font_size, font_color):
    """Save current settings as defaults."""
    data = {
        "ai_provider": ai_provider, "ai_model": ai_model,
        "custom_prompt": custom_prompt, "num_clips": int(num_clips),
        "min_duration": int(min_duration), "max_duration": int(max_duration),
        "whisper_model": whisper_model, "language": language,
        "font_name": font_name, "font_size": int(font_size),
        "font_color": font_color,
    }
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return "Settings saved as default!"


def _resolve_video(input_url, input_file, output_dir, log):
    """Return a Path to the video file, downloading from URL when needed."""
    if input_file is not None:
        original_path = Path(input_file)
        log(f"Local file: {original_path.name}")
        try:
            str(original_path).encode("ascii")
            return original_path
        except UnicodeEncodeError:
            safe_dir = Path(original_path.parent / "_safe")
            safe_dir.mkdir(parents=True, exist_ok=True)
            safe_name = f"input{original_path.suffix}"
            video_path = safe_dir / safe_name
            shutil.copy2(original_path, video_path)
            log(f"Copied to safe path: {video_path}")
            return video_path
    if input_url and input_url.strip():
        video_path = download_video(input_url.strip(), output_dir / "source")
        log(f"Downloaded: {video_path.name}")
        return video_path
    return None


def process_video(
    input_url: str,
    input_file,
    num_clips: int,
    output_mode: str,
    generate_shorts: bool,
    generate_zip: bool,
    ai_provider: str,
    ai_model: str,
    api_key: str,
    custom_prompt: str,
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
    """Main processing pipeline for the clip-extraction tab."""
    logs = []

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(f"./output_{timestamp}")
        output_dir.mkdir(parents=True, exist_ok=True)

        progress(0.05, desc="Resolving input...")
        video_path = _resolve_video(input_url, input_file, output_dir, log)
        if video_path is None:
            return "Error: URLを入力するかファイルをアップロードしてください", "", None, ""

        progress(0.1, desc="[Step 1/6] Analyzing video...")
        log(f"[Step 1/6] Analyzing video: {video_path}")
        video_info = get_video_info(video_path)
        log(f"  Resolution: {video_info['width']}x{video_info['height']}, FPS: {video_info['fps']:.2f}, Duration: {video_info['duration']:.0f}s")

        progress(0.15, desc="[Step 2/6] Transcribing audio...")
        log("[Step 2/6] Transcribing... (this may take a while)")
        segments = transcribe_with_cache(
            video_path, whisper_model, language, input_url or "",
        )
        transcript_text = segments_to_text(segments)
        transcript_path = output_dir / "transcript.txt"
        transcript_path.write_text(transcript_text, encoding="utf-8")
        log(f"  Transcription complete: {len(segments)} segments")

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
            custom_prompt=custom_prompt,
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

        progress(0.6, desc="[Step 4/6] Extracting clips...")
        log("[Step 4/6] Extracting clips...")
        clips_dir = output_dir / "clips"
        clip_paths = extract_clips(video_path, highlights, clips_dir)
        log(f"  Extracted {len(clip_paths)} clips")

        shorts_paths = []
        if generate_shorts:
            progress(0.7, desc="Generating shorts (9:16)...")
            shorts_dir = output_dir / "shorts"
            shorts_paths = extract_clips(video_path, highlights, shorts_dir, shorts=True)
            log(f"  Generated {len(shorts_paths)} shorts")

        progress(0.8, desc="[Step 5/6] Generating subtitles...")
        log("[Step 5/6] Generating subtitles...")
        srt_paths = generate_all_srts(segments, highlights, clips_dir)
        shorts_srt_paths: list[Path] = []
        if generate_shorts and shorts_paths:
            shorts_srt_paths = generate_all_srts(segments, highlights, output_dir / "shorts")
        log(f"  Generated {len(srt_paths)} SRT files")

        progress(0.85, desc="[Step 6/6] Exporting XML...")
        log("[Step 6/6] Exporting Premiere Pro XML...")
        if output_mode == "combined":
            xml_path = output_dir / "project.xml"
            generate_combined_xml(
                clip_paths, srt_paths, highlights, video_info, xml_path,
                project_name=video_path.stem,
            )
            if generate_shorts and shorts_paths:
                shorts_video_info = {**video_info, "width": 1080, "height": 1920}
                generate_combined_xml(
                    shorts_paths, shorts_srt_paths, highlights, shorts_video_info,
                    output_dir / "project_shorts.xml",
                    project_name=f"{video_path.stem}_shorts",
                )
            log("  Premiere Pro XML (combined mode) exported")
        else:
            generate_individual_xmls(
                clip_paths, srt_paths, highlights, video_info, clips_dir,
            )
            if generate_shorts and shorts_paths:
                shorts_video_info = {**video_info, "width": 1080, "height": 1920}
                generate_individual_xmls(
                    shorts_paths, shorts_srt_paths, highlights,
                    shorts_video_info, output_dir / "shorts",
                )
            log("  Premiere Pro XML (individual mode) exported")

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

        log(f"\nDone! Output: {output_dir}")
        return "\n".join(logs), highlights_summary, zip_path, drive_link

    except subprocess.CalledProcessError as e:
        err_detail = f"Command failed: {e.cmd}\nReturn code: {e.returncode}"
        if e.stdout:
            err_detail += f"\nstdout: {e.stdout[:500]}"
        if e.stderr:
            err_detail += f"\nstderr: {e.stderr[:500]}"
        logger.error(err_detail)
        log(f"\nError (subprocess): {err_detail}")
        return "\n".join(logs), "", None, ""
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Error: {e}\n{tb}")
        log(f"\nError: {e}")
        log(tb)
        return "\n".join(logs), "", None, ""


# ---------------------------------------------------------------------------
# Timestamp generation tab
# ---------------------------------------------------------------------------


def process_timestamps(
    input_url: str,
    input_file,
    mode: str,
    prompt_text: str,
    ai_provider: str,
    ai_model: str,
    api_key: str,
    whisper_model: str,
    language: str,
    progress=gr.Progress(),
) -> tuple[str, str]:
    """Generate chapters or search moments. Returns (output_text, log_text)."""
    logs: list[str] = []

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)

    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir = Path(f"./output_ts_{timestamp}")
        work_dir.mkdir(parents=True, exist_ok=True)

        progress(0.05, desc="Resolving input...")
        video_path = _resolve_video(input_url, input_file, work_dir, log)
        if video_path is None:
            return "Error: URLを入力するかファイルをアップロードしてください", "\n".join(logs)

        progress(0.1, desc="Analyzing video...")
        video_info = get_video_info(video_path)
        duration = float(video_info.get("duration", 0.0))
        log(f"  Duration: {duration:.0f}s")

        progress(0.15, desc="Transcribing (cache-aware)...")
        segments = transcribe_with_cache(
            video_path, whisper_model, language, input_url or "",
        )
        transcript_text = segments_to_text(segments)
        log(f"  Transcript: {len(segments)} segments")

        progress(0.7, desc="Analyzing with AI...")

        if mode == "prompt_search":
            if not prompt_text or not prompt_text.strip():
                return "Error: プロンプト検索モードではプロンプトを入力してください", "\n".join(logs)
            moments = search_moments(
                transcript_text,
                prompt=prompt_text,
                ai_provider=ai_provider,
                api_key=api_key,
                ai_model=ai_model,
            )
            log(f"  Moments: {len(moments)} hits")
            output = format_moments_for_display(moments)
        else:
            chapters = generate_chapters(
                transcript_text,
                video_duration=duration,
                ai_provider=ai_provider,
                api_key=api_key,
                ai_model=ai_model,
                custom_instructions=prompt_text or "",
            )
            log(f"  Chapters: {len(chapters)}")
            output = format_chapters_for_youtube(chapters)

        progress(1.0, desc="Done")
        log("\nDone.")
        return output, "\n".join(logs)

    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"Timestamp generation error: {e}\n{tb}")
        log(f"\nError: {e}")
        return f"(生成に失敗しました)\n\n{e}", "\n".join(logs)


def push_description_to_youtube(url: str, description_text: str) -> str:
    """Upload description text to the YouTube video identified by URL."""
    if not url or not url.strip():
        return "❌ YouTube URLを入力してください"
    if not description_text or not description_text.strip():
        return "❌ 概要欄の本文が空です"
    video_id = youtube_api.extract_video_id(url)
    if not video_id:
        return "❌ YouTubeの動画URLを認識できませんでした"
    if not youtube_api.is_authenticated():
        return "❌ YouTube API 未認証です。設定タブで認証してください"
    result = youtube_api.update_description(video_id, description_text)
    if result.get("ok"):
        return f"✅ 概要欄を更新しました (video_id: {video_id})"
    return f"❌ 更新に失敗: {result.get('error')}"


def send_timestamps_to_clip_tab(output_text: str) -> str:
    """Return a custom_prompt payload that guides the clip AI using timestamps."""
    if not output_text or not output_text.strip():
        return ""
    header = "以下のタイムスタンプ付近のシーンを優先的に切り抜いてください。各行が候補区間です。\n"
    return header + output_text.strip()


# ---------------------------------------------------------------------------
# YouTube auth handlers
# ---------------------------------------------------------------------------


def _escape_markdown(text: str) -> str:
    """Escape characters that Markdown renders as formatting."""
    return (
        text.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("`", "\\`")
    )


def _youtube_status_markdown() -> str:
    if not youtube_api.is_configured():
        return "🔒 **client_secret.json が未アップロード**"
    if not youtube_api.is_authenticated():
        return "🔑 client_secret.json ✓ / **未認証**"
    channel = youtube_api.get_authenticated_user_info() or "(name unknown)"
    return f"✅ 認証済み: **{_escape_markdown(channel)}**"


def upload_client_secret(uploaded_file) -> str:
    if uploaded_file is None:
        return _youtube_status_markdown()
    try:
        youtube_api.save_client_secret(uploaded_file)
    except Exception as e:
        logger.error(f"Failed to save client_secret.json: {e}")
        return f"❌ 保存に失敗: {e}"
    return _youtube_status_markdown()


def run_youtube_auth() -> str:
    result = youtube_api.authenticate()
    if not result.get("ok"):
        return f"❌ {result.get('error')}"
    channel = result.get("channel") or "(unknown)"
    return f"✅ 認証成功: **{_escape_markdown(channel)}**"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def create_ui():
    """Create the Gradio web interface with a 3-tab layout."""
    defaults = load_defaults()

    with gr.Blocks(
        title="Clip Extractor - 配信切り抜き自動生成",
        analytics_enabled=False,
        css="""
        .main-title { text-align: center; margin-bottom: 0.5em; }
        .subtitle { text-align: center; color: #666; margin-bottom: 1.5em; }
        footer { display: none !important; }
        a[href*="gradio.app"] { display: none !important; }
        """,
    ) as app:
        gr.HTML("<h1 class='main-title'>Clip Extractor</h1>")
        gr.HTML("<p class='subtitle'>YouTube配信アーカイブから切り抜き & タイムスタンプを自動生成</p>")

        with gr.Tabs() as tabs:
            # ===== Tab 1: Clip Extraction =====
            with gr.Tab("✂️ 切り抜き生成", id="clip_tab") as clip_tab:
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

                custom_prompt = gr.Textbox(
                    label="カスタムプロンプト / タイムスタンプヒント",
                    placeholder="例: 面白いシーンだけ選んで / タイムスタンプ生成タブから送信するとここに入ります",
                    lines=4,
                )

                generate_btn = gr.Button(
                    "Generate Clips / 切り抜き生成開始",
                    variant="primary",
                    size="lg",
                )

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

            # ===== Tab 2: Timestamp Generator =====
            with gr.Tab("🕐 タイムスタンプ生成", id="timestamp_tab"):
                gr.Markdown(
                    "**配信アーカイブの章分け / シーン検索**\n\n"
                    "同じ動画を繰り返し検索しても文字起こしはキャッシュされるため、"
                    "2回目以降は速くタダで回せます（Gemini無料枠の範囲で）。"
                )

                with gr.Row():
                    with gr.Column(scale=2):
                        ts_input_url = gr.Textbox(
                            label="YouTube URL",
                            placeholder="https://youtube.com/watch?v=...",
                        )
                        ts_input_file = gr.File(
                            label="または ローカルファイル",
                            file_types=["video"],
                            type="filepath",
                        )

                    with gr.Column(scale=1):
                        ts_mode = gr.Radio(
                            choices=[
                                ("全体チャプター生成", "full_chapters"),
                                ("プロンプト検索", "prompt_search"),
                            ],
                            value="full_chapters",
                            label="モード",
                        )
                        ts_prompt = gr.Textbox(
                            label="プロンプト / 追加指示",
                            placeholder=(
                                "例1: クッキーランで負けた瞬間\n"
                                "例2: 新作ゲームの話題が出た部分\n"
                                "例3: スパチャに反応したとこ\n"
                                "全体モードでは追加指示として使われます"
                            ),
                            lines=4,
                        )

                ts_generate_btn = gr.Button(
                    "🚀 タイムスタンプ生成",
                    variant="primary",
                    size="lg",
                )

                ts_output = gr.Textbox(
                    label="生成結果（編集可能）",
                    lines=15,
                    interactive=True,
                )
                ts_log = gr.Textbox(
                    label="ログ",
                    lines=6,
                    interactive=False,
                )

                with gr.Row():
                    ts_copy_btn = gr.Button("📋 クリップボードにコピー", variant="secondary")
                    ts_push_btn = gr.Button("📤 YouTube 概要欄に反映", variant="secondary")
                    ts_send_btn = gr.Button("✂️ 切り抜きタブに送る", variant="secondary")

                ts_action_msg = gr.Markdown("")

            # ===== Tab 3: Settings =====
            with gr.Tab("⚙️ 設定", id="settings_tab"):
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
                        saved_api_key = ""
                        if GEMINI_KEY_FILE.exists():
                            saved_api_key = GEMINI_KEY_FILE.read_text(encoding="utf-8").strip()
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
                    save_defaults_btn = gr.Button("デフォルトに設定", variant="secondary")
                    save_defaults_msg = gr.Textbox(label="", interactive=False, show_label=False)

                save_defaults_btn.click(
                    fn=save_defaults,
                    inputs=[ai_provider, ai_model, custom_prompt, num_clips,
                            min_duration, max_duration, whisper_model, language,
                            font_name, font_size, font_color],
                    outputs=save_defaults_msg,
                )

                gr.HTML("<hr><h3>YouTube API 認証</h3>")
                gr.Markdown(
                    "概要欄を自動更新するには Google Cloud Console で YouTube Data API v3 を有効化し、"
                    "OAuth 2.0 クライアントID（デスクトップアプリ）の `client_secret.json` をアップロードしてください。"
                )
                with gr.Row():
                    yt_client_secret_upload = gr.File(
                        label="client_secret.json",
                        file_types=[".json"],
                        type="filepath",
                    )
                    yt_auth_btn = gr.Button("🔑 認証する", variant="primary")
                yt_status = gr.Markdown(_youtube_status_markdown())

                yt_client_secret_upload.change(
                    fn=upload_client_secret,
                    inputs=yt_client_secret_upload,
                    outputs=yt_status,
                )
                yt_auth_btn.click(
                    fn=run_youtube_auth,
                    outputs=yt_status,
                )

        # === Clip tab wiring ===
        generate_btn.click(
            fn=process_video,
            inputs=[
                input_url, input_file, num_clips, output_mode,
                generate_shorts, generate_zip, ai_provider, ai_model, api_key,
                custom_prompt, min_duration, max_duration,
                whisper_model, language, font_name, font_size, font_color,
                upload_to_drive,
            ],
            outputs=[log_output, highlights_output, download_output, drive_link_output],
            concurrency_limit=1,
        )

        # === Timestamp tab wiring ===
        ts_generate_btn.click(
            fn=process_timestamps,
            inputs=[ts_input_url, ts_input_file, ts_mode, ts_prompt,
                    ai_provider, ai_model, api_key, whisper_model, language],
            outputs=[ts_output, ts_log],
            concurrency_limit=1,
        )

        # Copy to clipboard via JS (Gradio 6.x does not support show_copy_button on Textbox).
        ts_copy_btn.click(
            fn=None,
            inputs=ts_output,
            outputs=None,
            js="(text) => { if (text) { navigator.clipboard.writeText(text); } return []; }",
        )

        def _push_wrapper(url, desc):
            return gr.update(value=push_description_to_youtube(url, desc))

        ts_push_btn.click(
            fn=_push_wrapper,
            inputs=[ts_input_url, ts_output],
            outputs=ts_action_msg,
        )

        def _send_wrapper(ts_text):
            prompt_value = send_timestamps_to_clip_tab(ts_text)
            if not prompt_value:
                return gr.update(), gr.update(value="⚠️ 先にタイムスタンプを生成してください")
            return (
                gr.update(value=prompt_value),
                gr.update(value="✅ 切り抜き生成タブの『カスタムプロンプト』に反映しました"),
            )

        ts_send_btn.click(
            fn=_send_wrapper,
            inputs=ts_output,
            outputs=[custom_prompt, ts_action_msg],
        )

        # Instructions accordion (stays at bottom of clip tab context)
        with gr.Accordion("使い方 / How to Use", open=False):
            gr.Markdown("""
### 基本フロー
1. **切り抜き生成タブ** で YouTube URL / ローカル動画を指定 → Generate
2. **タイムスタンプ生成タブ** で全体チャプター or プロンプト検索
3. 生成後、「📤 YouTube 概要欄に反映」で概要欄を自動更新
4. または「✂️ 切り抜きタブに送る」でタイムスタンプをヒントに切り抜き

### Premiere Pro での読み込み
1. ZIPを展開 → Premiere Pro で `project.xml` を Import
2. SRT字幕を File → Import でインポート

### YouTube概要欄 自動更新
1. Google Cloud Console で YouTube Data API v3 を有効化
2. OAuth 2.0 クライアントID（デスクトップ）→ `client_secret.json` ダウンロード
3. 設定タブ > YouTube API 認証 にアップロード → 認証するボタン
4. 以降は「📤 YouTube 概要欄に反映」で自動更新
            """)

    return app


if __name__ == "__main__":
    app = create_ui()
    app.queue()
    app.launch(
        server_name="0.0.0.0",
        server_port=8080,
        ssr_mode=False,
        theme=gr.themes.Soft(),
    )
