"""Video clip extraction using FFmpeg."""

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import FontConfig


def get_video_info(video_path: Path) -> dict:
    """Get video metadata using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
    import json
    data = json.loads(result.stdout)

    video_stream = next(
        (s for s in data.get("streams", []) if s["codec_type"] == "video"), {}
    )
    return {
        "width": int(video_stream.get("width", 1920)),
        "height": int(video_stream.get("height", 1080)),
        "fps": _parse_fps(video_stream.get("r_frame_rate", "30/1")),
        "duration": float(data.get("format", {}).get("duration", 0)),
    }


def _hex_to_ass_color(hex_color: str) -> str:
    """#RRGGBB を ASS スタイル用の &HBBGGRR& に変換。"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 6:
        r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
        return f"&H{b.upper()}{g.upper()}{r.upper()}&"
    return "&HFFFFFF&"


def _escape_subtitles_path(p: Path) -> str:
    """ffmpeg subtitles filter 用の path エスケープ。

    Windows path の backslash を forward slash に変え、ドライブレター等の
    コロンをエスケープする。filter syntax 上の一重引用符も保護する。
    """
    s = str(p)
    s = s.replace("\\", "/")
    s = s.replace(":", r"\:")
    s = s.replace("'", r"\'")
    return s


def _build_force_style(font_config: "FontConfig") -> str:
    """font_config から ffmpeg subtitles filter の force_style 文字列を構築。"""
    alignment = 8 if getattr(font_config, "position", "bottom") == "top" else 2
    parts = [
        f"FontName={font_config.font_name}",
        f"FontSize={font_config.font_size}",
        f"PrimaryColour={_hex_to_ass_color(font_config.font_color)}",
        f"OutlineColour={_hex_to_ass_color(font_config.outline_color)}",
        f"Outline={font_config.outline_width}",
        f"Alignment={alignment}",
        f"MarginV={font_config.margin_bottom}",
        "BorderStyle=1",
    ]
    return ",".join(parts)


def _build_subtitles_filter(srt_path: Path, font_config: "FontConfig") -> str:
    escaped = _escape_subtitles_path(srt_path)
    style = _build_force_style(font_config)
    return f"subtitles='{escaped}':force_style='{style}'"


def extract_clip(
    video_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    shorts: bool = False,
    srt_path: Path | None = None,
    font_config: "FontConfig | None" = None,
) -> Path:
    """Extract a clip from the video."""
    duration = end_sec - start_sec

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", str(video_path),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
    ]

    vf_filters = []
    if shorts:
        vf_filters.append("crop=ih*9/16:ih,scale=1080:1920")
    if shorts and srt_path is not None and font_config is not None:
        vf_filters.append(_build_subtitles_filter(srt_path, font_config))
    if vf_filters:
        cmd.extend(["-vf", ",".join(vf_filters)])

    cmd.append(str(output_path))
    subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def format_time_range(start_sec: float, end_sec: float) -> str:
    """Format time range as HHhMMmSSs-HHhMMmSSs for filenames."""
    def fmt(sec: float) -> str:
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        return f"{h:02d}h{m:02d}m{s:02d}s"
    return f"{fmt(start_sec)}-{fmt(end_sec)}"


def extract_clips(
    video_path: Path,
    highlights: list[dict],
    output_dir: Path,
    shorts: bool = False,
    srt_paths: list[Path] | None = None,
    font_config: "FontConfig | None" = None,
) -> list[Path]:
    """Extract all highlight clips."""
    output_dir.mkdir(parents=True, exist_ok=True)
    clip_paths = []

    for i, h in enumerate(highlights, 1):
        suffix = "_short" if shorts else ""
        range_str = format_time_range(h["start_sec"], h["end_sec"])
        clip_name = f"{range_str}{suffix}.mp4"
        clip_path = output_dir / clip_name

        srt_path = srt_paths[i - 1] if srt_paths and i - 1 < len(srt_paths) else None

        print(f"Extracting clip {i}/{len(highlights)}: {h['title']}...")
        extract_clip(
            video_path,
            clip_path,
            h["start_sec"],
            h["end_sec"],
            shorts,
            srt_path=srt_path,
            font_config=font_config,
        )
        clip_paths.append(clip_path)

    return clip_paths


def _parse_fps(fps_str: str) -> float:
    """Parse frame rate string like '30/1' or '29.97'."""
    if "/" in fps_str:
        num, den = fps_str.split("/")
        return int(num) / int(den) if int(den) != 0 else 30.0
    return float(fps_str)


if __name__ == "__main__":
    # Self-test: verify _hex_to_ass_color and _build_force_style
    assert _hex_to_ass_color("#FFFFFF") == "&HFFFFFF&", "white conversion"
    assert _hex_to_ass_color("#000000") == "&H000000&", "black conversion"
    assert _hex_to_ass_color("#FF0000") == "&H0000FF&", "red BGR swap"
    assert _hex_to_ass_color("#00FF00") == "&H00FF00&", "green BGR"
    assert _hex_to_ass_color("#0000FF") == "&HFF0000&", "blue BGR swap"

    from config import FontConfig
    fc = FontConfig(font_name="Noto Sans JP", font_size=96, font_color="#FFFFFF",
                    outline_color="#000000", outline_width=3, position="bottom",
                    margin_bottom=60)
    style = _build_force_style(fc)
    for expected in ["FontName=Noto Sans JP", "FontSize=96",
                     "PrimaryColour=&HFFFFFF&", "OutlineColour=&H000000&",
                     "Alignment=2", "MarginV=60"]:
        assert expected in style, f"missing: {expected} in {style}"

    from pathlib import Path
    filt = _build_subtitles_filter(Path("C:/Users/x/clip.srt"), fc)
    assert filt.startswith("subtitles='C\\:/Users/x/clip.srt'"), f"bad escape: {filt}"
    assert "force_style='FontName=Noto Sans JP" in filt, f"style missing: {filt}"

    print("clipper.py self-test: all assertions passed")
