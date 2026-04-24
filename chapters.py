"""YouTube description chapter text generator.

Output conforms to YouTube's auto-chapter requirements:
- First line MUST start with '0:00'
- Timestamps are '<M:SS>' under 1h or '<H:MM:SS>' from 1h
- Lines are sorted ascending by timestamp
"""

from pathlib import Path


def format_chapter_timestamp(seconds: float, use_hours: bool = False) -> str:
    """Format a non-negative float seconds as 'M:SS' or 'H:MM:SS'.

    'M:SS' form uses plain minutes with no leading zero (e.g. '0:00', '12:34').
    'H:MM:SS' form zero-pads minutes and seconds (e.g. '1:02:03')."""
    total = max(0, int(round(seconds)))
    if use_hours:
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h}:{m:02d}:{s:02d}"
    total_m = total // 60
    s = total % 60
    return f"{total_m}:{s:02d}"


def generate_chapter_text(
    highlights: list[dict],
    video_duration: float = 0.0,
) -> str:
    """Build YouTube-compatible chapter text from highlight dicts.

    Each highlight dict is expected to have at least:
      - 'start_sec' (float): chapter start in seconds
      - 'title' (str, optional): chapter title

    Rules enforced:
      - First line always begins with '0:00'. If the first highlight's
        start_sec is > 0, a '0:00 イントロ' line is inserted.
      - Use H:MM:SS when video_duration >= 3600 or any highlight
        starts at >= 3600; otherwise M:SS.
      - Titles fall back to 'シーンN' (1-indexed) when missing."""
    if not highlights:
        return ""

    use_hours = float(video_duration) >= 3600 or any(
        float(h.get("start_sec", 0)) >= 3600 for h in highlights
    )

    lines: list[str] = []
    first_start = float(highlights[0].get("start_sec", 0))
    if first_start > 0:
        lines.append(f"{format_chapter_timestamp(0, use_hours)} イントロ")

    for i, h in enumerate(highlights, 1):
        start = float(h.get("start_sec", 0))
        title = h.get("title") or f"シーン{i}"
        ts = format_chapter_timestamp(start, use_hours=use_hours)
        lines.append(f"{ts} {title}")

    return "\n".join(lines)


def write_chapter_file(
    highlights: list[dict],
    output_path: Path,
    video_duration: float = 0.0,
) -> Path:
    """Write generated chapter text to the given path as UTF-8."""
    text = generate_chapter_text(highlights, video_duration=video_duration)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path
