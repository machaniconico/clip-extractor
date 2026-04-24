#!/usr/bin/env python3
"""clip-extractor: Auto-generate highlight clips from YouTube archives for Premiere Pro."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from chapters import generate_chapter_text, write_chapter_file
from config import FontConfig
from downloader import is_youtube_url, download_video
from transcriber import transcribe, segments_to_text
from highlighter import detect_highlights
from clipper import extract_clips, get_video_info
from subtitles import generate_all_srts
from premiere_xml import generate_combined_xml, generate_individual_xmls
from modes import GenerationModes


def main():
    parser = argparse.ArgumentParser(
        description="YouTube配信アーカイブから切り抜きショート動画を自動生成",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  python main.py https://youtube.com/watch?v=xxxxx
  python main.py ./archive.mp4 --shorts
  python main.py ./archive.mp4 --mode individual --clips 3
  python main.py ./archive.mp4 --prompt "面白いシーンだけ選んで"
  python main.py ./archive.mp4 --font-config my_fonts.json
        """,
    )

    parser.add_argument("input", help="YouTube URL or local video file path")
    parser.add_argument("-o", "--output", default=None, help="Output directory (default: auto-generated)")
    parser.add_argument("-n", "--clips", type=int, default=5, help="Number of clips to extract (default: 5)")
    parser.add_argument("-m", "--mode", choices=["combined", "individual"], default="combined",
                        help="Output mode: combined (1 XML, multiple sequences) or individual (separate XMLs)")
    parser.add_argument("-s", "--shorts", action="store_true", help="Also generate 9:16 vertical shorts")
    parser.add_argument("-p", "--prompt", default="", help="Custom prompt for highlight detection")
    parser.add_argument("--min-duration", type=int, default=30, help="Minimum clip duration in seconds")
    parser.add_argument("--max-duration", type=int, default=90, help="Maximum clip duration in seconds")
    parser.add_argument("--whisper-model", default="large-v3", help="Whisper model size (default: large-v3)")
    parser.add_argument("--language", default="ja", help="Language code (default: ja)")
    parser.add_argument("--font-config", default=None, help="Path to font config JSON file")
    parser.add_argument("--no-clips", action="store_true",
                        help="切り抜き生成を無効化 (概要欄テキストのみ生成)")
    parser.add_argument("--no-chapters", action="store_true",
                        help="概要欄テキスト生成を無効化 (切り抜きのみ)")
    parser.add_argument("--chapter-prompt", default="",
                        help="概要欄専用プロンプト (--no-clips 時のみ使用)")

    args = parser.parse_args()

    # Validate generation modes — at least one side must be enabled.
    modes = GenerationModes(
        enable_clips=not args.no_clips,
        enable_chapters=not args.no_chapters,
        clip_prompt=args.prompt or "",
        chapter_prompt=args.chapter_prompt or "",
    )
    try:
        modes.validate()
    except ValueError as mode_err:
        parser.error(str(mode_err))
    print(f"Modes: clips={modes.enable_clips}, chapters={modes.enable_chapters}")

    # Setup config
    font_config = FontConfig()
    if args.font_config:
        font_config = FontConfig.from_file(Path(args.font_config))

    # Determine output directory
    if args.output:
        output_dir = Path(args.output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(f"./output_{timestamp}")

    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {output_dir}")

    # Step 1: Get video file
    if is_youtube_url(args.input):
        video_path = download_video(args.input, output_dir / "source")
    else:
        video_path = Path(args.input)
        if not video_path.exists():
            print(f"Error: File not found: {video_path}", file=sys.stderr)
            sys.exit(1)

    # Step 2: Get video info
    print("\nAnalyzing video...")
    video_info = get_video_info(video_path)
    print(f"  Resolution: {video_info['width']}x{video_info['height']}")
    print(f"  FPS: {video_info['fps']:.2f}")
    print(f"  Duration: {video_info['duration']:.0f}s")

    # Step 3: Transcribe
    print("\n--- Transcription ---")
    segments = transcribe(video_path, args.whisper_model, args.language)
    transcript_text = segments_to_text(segments)

    # Save transcript
    transcript_path = output_dir / "transcript.txt"
    transcript_path.write_text(transcript_text, encoding="utf-8")
    print(f"Transcript saved: {transcript_path}")

    # Step 4: Detect highlights
    print("\n--- Highlight Detection ---")
    highlights = detect_highlights(
        transcript_text,
        num_clips=args.clips,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        custom_prompt=modes.active_prompt,
    )

    # Steps 5–8 are the clip pipeline. Skipped entirely when --no-clips.
    clip_paths = []
    srt_paths = []
    shorts_paths = []
    shorts_srt_paths = []
    clips_dir = output_dir / "clips"  # referenced later by XML + summary

    if modes.enable_clips:
        # Step 5: Extract clips (normal landscape, no subtitle burn-in — kept
        # unburned so the Premiere Pro editing flow can style SRT captions freely)
        print("\n--- Clip Extraction ---")
        clip_paths = extract_clips(video_path, highlights, clips_dir)

        # Step 6: Subtitles for clips (SRT, imported separately in Premiere)
        print("\n--- Subtitle Generation ---")
        srt_paths = generate_all_srts(segments, highlights, clips_dir)
        print(f"Generated {len(srt_paths)} SRT files")

        # Step 7: Shorts (9:16) with burned-in subtitles using font_config.
        # SRT must be generated into shorts_dir first so the burn-in step can
        # reference it via ffmpeg's subtitles filter.
        if args.shorts:
            print("\n--- Shorts Conversion (9:16) with burned-in subtitles ---")
            shorts_dir = output_dir / "shorts"
            shorts_dir.mkdir(parents=True, exist_ok=True)
            shorts_srt_paths = generate_all_srts(segments, highlights, shorts_dir)
            shorts_paths = extract_clips(
                video_path, highlights, shorts_dir,
                shorts=True,
                srt_paths=shorts_srt_paths,
                font_config=font_config,
            )
    else:
        print("\n[Skip 5-7] Clip generation disabled (--no-clips) — chapters-only run")

    # Step 8: Export Premiere Pro XML (only when clips are enabled)
    if modes.enable_clips:
        print("\n--- Premiere Pro XML Export ---")
        if args.mode == "combined":
            xml_path = output_dir / "project.xml"
            generate_combined_xml(
                clip_paths, srt_paths, highlights, video_info, xml_path,
                project_name=video_path.stem,
            )
            print(f"Combined XML: {xml_path}")

            if args.shorts and shorts_paths:
                shorts_xml_path = output_dir / "project_shorts.xml"
                shorts_video_info = {**video_info, "width": 1080, "height": 1920}
                generate_combined_xml(
                    shorts_paths, shorts_srt_paths, highlights, shorts_video_info,
                    shorts_xml_path, project_name=f"{video_path.stem}_shorts",
                )
                print(f"Shorts XML: {shorts_xml_path}")
        else:
            xml_paths = generate_individual_xmls(
                clip_paths, srt_paths, highlights, video_info, clips_dir,
            )
            print(f"Individual XMLs: {len(xml_paths)} files")

            if args.shorts and shorts_paths:
                shorts_video_info = {**video_info, "width": 1080, "height": 1920}
                generate_individual_xmls(
                    shorts_paths, shorts_srt_paths, highlights,
                    shorts_video_info, output_dir / "shorts",
                )

    # Step 9: Generate YouTube chapter description text (auto-chapter on upload)
    if modes.enable_chapters:
        print("\n--- Chapter Text (YouTube description) ---")
        chapters_path = output_dir / "chapters.txt"
        video_duration = float(video_info.get("duration", 0))
        chapters_text = generate_chapter_text(highlights, video_duration=video_duration)
        write_chapter_file(highlights, chapters_path, video_duration=video_duration)
        print(chapters_text)
        print(f"\nSaved: {chapters_path}")
    else:
        print("\n[Skip chapters] Chapter generation disabled (--no-chapters)")

    # Summary
    print("\n" + "=" * 50)
    print("Done!")
    print(f"Output: {output_dir}")
    print(f"Clips: {len(clip_paths)} files")
    if shorts_paths:
        print(f"Shorts: {len(shorts_paths)} files")
    print(f"SRT: {len(srt_paths)} files")
    print(f"Mode: {args.mode}")
    print()
    print("Premiere Proで開く:")
    if args.mode == "combined":
        print(f"  File > Import > {output_dir / 'project.xml'}")
    else:
        print(f"  File > Import > {clips_dir}/*.xml")
    print()
    print("SRT字幕の読み込み:")
    print("  File > Import > *.srt (キャプショントラックとして読み込み)")
    print("=" * 50)


if __name__ == "__main__":
    main()
