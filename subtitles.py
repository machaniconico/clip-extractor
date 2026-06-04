"""SRT and ASS subtitle generation."""

from pathlib import Path

from clipper import (
    _TITLE_WRAP_FULLWIDTH_CHARS,
    _hex_to_ass_color,
    _title_cluster_width,
    _title_grapheme_clusters,
    format_time_range,
)
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


def generate_karaoke_ass(
    segments: list[Segment],
    clip_start: float,
    clip_end: float,
    output_path: Path,
    font_config,
) -> Path:
    """Generate ASS karaoke subtitles with per-word timing for a clip."""
    clip_segments = [
        s for s in segments
        if s.end > clip_start and s.start < clip_end
    ]

    lines = [_build_ass_header(font_config)]
    for seg in clip_segments:
        raw_words = getattr(seg, "words", [])
        if raw_words:
            word_items = []
            for word in raw_words:
                if word.end <= clip_start or word.start >= clip_end:
                    continue
                start_abs = max(clip_start, word.start)
                end_abs = min(clip_end, word.end)
                had_leading_space = word.text[:1] == " "
                text = _strip_leading_ascii_space(word.text)
                if not text:
                    continue
                word_items.append((
                    start_abs - clip_start,
                    end_abs - clip_start,
                    max(1, round((end_abs - start_abs) * 100)),
                    text,
                    had_leading_space,
                ))

            if not word_items:
                continue

            start = word_items[0][0]
            end = word_items[-1][1]
            text = _build_karaoke_text(word_items)
        else:
            start = max(0, seg.start - clip_start)
            end = min(clip_end - clip_start, seg.end - clip_start)
            text = _wrap_ass_plain_text(seg.text)

        if end < start:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{text}"
        )

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def generate_all_karaoke_ass(
    segments: list[Segment],
    highlights: list[dict],
    output_dir: Path,
    font_config,
) -> list[Path]:
    """Generate ASS karaoke subtitle files for all clips."""
    ass_paths = []
    for h in highlights:
        range_str = format_time_range(h["start_sec"], h["end_sec"])
        ass_path = output_dir / f"{range_str}.ass"
        generate_karaoke_ass(segments, h["start_sec"], h["end_sec"], ass_path, font_config)
        ass_paths.append(ass_path)
    return ass_paths


def _ass_time(seconds: float) -> str:
    """Format seconds to ASS timestamp: H:MM:SS.cc."""
    centiseconds = max(0, int(round(seconds * 100)))
    h = centiseconds // 360000
    centiseconds %= 360000
    m = centiseconds // 6000
    centiseconds %= 6000
    s = centiseconds // 100
    cs = centiseconds % 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape_ass_text(value: str) -> str:
    """Neutralize ASS override delimiters and slash commands in visible text."""
    return (
        (value or "")
        .replace("\\", "\uFF3C")
        .replace("{", "\uFF5B")
        .replace("}", "\uFF5D")
    )


def _build_ass_header(font_config) -> str:
    """Build an authored ASS header pinned to the 1080x1920 Shorts frame."""
    alignment = 8 if getattr(font_config, "position", "bottom") == "top" else 2
    primary = _hex_to_ass_color(getattr(font_config, "font_color", "#FFFFFF"))
    secondary = _hex_to_ass_color("#777777")
    if secondary == primary:
        secondary = _hex_to_ass_color("#555555")
    outline = _hex_to_ass_color(getattr(font_config, "outline_color", "#000000"))
    font_name = getattr(font_config, "font_name", "Noto Sans JP")
    font_size = getattr(font_config, "font_size", 96)
    outline_width = getattr(font_config, "outline_width", 3)
    margin_v = getattr(font_config, "margin_bottom", 60)

    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,{font_name},{font_size},{primary},{secondary},{outline},"
            f"&H80000000&,0,0,0,0,100,100,0,0,1,{outline_width},0,"
            f"{alignment},40,40,{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])


def _srt_time(seconds: float) -> str:
    """Format seconds to SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _strip_leading_ascii_space(value: str) -> str:
    """Remove faster-whisper's leading ASCII token spacer without touching CJK."""
    return (value or "").lstrip(" ")


def _ass_display_width(value: str) -> int:
    return sum(
        _title_cluster_width(cluster)
        for cluster in _title_grapheme_clusters(value)
    )


def _build_karaoke_text(word_items: list[tuple[float, float, int, str, bool]]) -> str:
    limit = _TITLE_WRAP_FULLWIDTH_CHARS * 2
    current_width = 0
    parts: list[str] = []

    prev_end = word_items[0][0]
    for w_start, w_end, duration_cs, word_text, had_leading_space in word_items:
        gap_cs = round((w_start - prev_end) * 100)
        if gap_cs > 0:
            parts.append(r"{\k" + str(gap_cs) + "}")

        visible_text = (" " if current_width and had_leading_space else "") + word_text
        word_width = _ass_display_width(visible_text)
        if current_width and current_width + word_width > limit:
            parts.append(r"\N")
            current_width = 0
            visible_text = word_text
            word_width = _ass_display_width(visible_text)
        escaped = _escape_ass_text(visible_text)
        parts.append(r"{\k" + str(duration_cs) + "}" + escaped)
        current_width += word_width
        prev_end = w_end

    return "".join(parts)


def _wrap_ass_plain_text(value: str) -> str:
    limit = _TITLE_WRAP_FULLWIDTH_CHARS * 2
    lines: list[str] = []
    current = ""
    current_width = 0

    for cluster in _title_grapheme_clusters(value or ""):
        cluster_width = _title_cluster_width(cluster)
        if current and current_width + cluster_width > limit:
            lines.append(_escape_ass_text(current.rstrip()))
            current = ""
            current_width = 0
            if cluster.lstrip(" ") != cluster:
                continue
        current += cluster
        current_width += cluster_width

    if current:
        lines.append(_escape_ass_text(current.rstrip()))

    return r"\N".join(line for line in lines if line)
