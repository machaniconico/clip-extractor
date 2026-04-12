"""Video clip extraction using FFmpeg."""

import subprocess
from pathlib import Path


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


def extract_clip(
    video_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    shorts: bool = False,
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

    if shorts:
        # 9:16 vertical crop from center
        cmd.extend([
            "-vf", "crop=ih*9/16:ih,scale=1080:1920",
        ])

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
) -> list[Path]:
    """Extract all highlight clips."""
    output_dir.mkdir(parents=True, exist_ok=True)
    clip_paths = []

    for i, h in enumerate(highlights, 1):
        suffix = "_short" if shorts else ""
        range_str = format_time_range(h["start_sec"], h["end_sec"])
        clip_name = f"{range_str}{suffix}.mp4"
        clip_path = output_dir / clip_name

        print(f"Extracting clip {i}/{len(highlights)}: {h['title']}...")
        extract_clip(video_path, clip_path, h["start_sec"], h["end_sec"], shorts)
        clip_paths.append(clip_path)

    return clip_paths


def _parse_fps(fps_str: str) -> float:
    """Parse frame rate string like '30/1' or '29.97'."""
    if "/" in fps_str:
        num, den = fps_str.split("/")
        return int(num) / int(den) if int(den) != 0 else 30.0
    return float(fps_str)
