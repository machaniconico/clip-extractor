#!/usr/bin/env python3
"""clip-extractor Web UI using Gradio."""

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
from downloader import is_youtube_url, download_video
from transcriber import transcribe, segments_to_text
from highlighter import detect_highlights
from clipper import extract_clips, get_video_info
from subtitles import generate_all_srts
from premiere_xml import generate_combined_xml, generate_individual_xmls
from drive_upload import upload_output_directory, is_configured as drive_is_configured


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
    """Main processing pipeline for the web UI."""
    logs = []

    def log(msg: str):
        logger.info(msg)
        logs.append(msg)

    try:
        # Create ONE output directory that is reused for download + processing,
        # so both the source video and the generated clips live together (and
        # are covered by a single Drive upload).
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(f"./output_{timestamp}")
        output_dir.mkdir(parents=True, exist_ok=True)

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
            return "Error: URLを入力するかファイルをアップロードしてください", "", None, ""

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

        # Step 4: Extract clips
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

        # Step 5: Subtitles
        progress(0.8, desc="[Step 5/6] Generating subtitles...")
        log("[Step 5/6] Generating subtitles...")
        srt_paths = generate_all_srts(segments, highlights, clips_dir)
        shorts_srt_paths: list[Path] = []
        if generate_shorts and shorts_paths:
            shorts_srt_paths = generate_all_srts(segments, highlights, output_dir / "shorts")
        log(f"  Generated {len(srt_paths)} SRT files")

        # Step 6: Premiere Pro XML
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


def create_ui():
    """Create the Gradio web interface."""
    defaults = load_defaults()

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
                        custom_prompt = gr.Textbox(
                            label="カスタムプロンプト (任意)",
                            placeholder="例: 面白いシーンだけ選んで、ゲーム実況の名場面を中心に",
                            lines=2,
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
                    save_defaults_btn = gr.Button("デフォルトに設定", variant="secondary")
                    save_defaults_msg = gr.Textbox(label="", interactive=False, show_label=False)

                save_defaults_btn.click(
                    fn=save_defaults,
                    inputs=[ai_provider, ai_model, custom_prompt, num_clips,
                            min_duration, max_duration, whisper_model, language,
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

        # Generate button
        generate_btn = gr.Button(
            "Generate Clips / 生成開始",
            variant="primary",
            size="lg",
        )

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
4. `credentials.json` をダウンロードして `clip-extractor/` フォルダに配置
5. 初回実行時にブラウザで認証
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
