"""SRT subtitle generation."""

from pathlib import Path

from clipper import format_time_range
from transcriber import Segment


def generate_srt(
    segments: list[Segment],
    clip_start: float,
    clip_end: float,
    output_path: Path,
) -> Path:
    """Generate SRT subtitle file for a clip's time range."""
    # Filter segments within the clip range
    clip_segments = [
        s for s in segments
        if s.end > clip_start and s.start < clip_end
    ]

    lines = []
    for i, seg in enumerate(clip_segments, 1):
        # Adjust timestamps relative to clip start
        start = max(0, seg.start - clip_start)
        end = min(clip_end - clip_start, seg.end - clip_start)

        lines.append(str(i))
        lines.append(f"{_srt_time(start)} --> {_srt_time(end)}")
        lines.append(seg.text)
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def generate_all_srts(
    segments: list[Segment],
    highlights: list[dict],
    output_dir: Path,
) -> list[Path]:
    """Generate SRT files for all clips."""
    srt_paths = []
    for h in highlights:
        range_str = format_time_range(h["start_sec"], h["end_sec"])
        srt_path = output_dir / f"{range_str}.srt"
        generate_srt(segments, h["start_sec"], h["end_sec"], srt_path)
        srt_paths.append(srt_path)
    return srt_paths


def _srt_time(seconds: float) -> str:
    """Format seconds to SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
