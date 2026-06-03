"""Video clip extraction using FFmpeg."""

import logging
import subprocess
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import FontConfig

logger = logging.getLogger(__name__)

_SHORTS_PAD_FILTER = (
    "scale=1080:1920:force_original_aspect_ratio=decrease,"
    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
)
_SHORTS_BLUR_FILTER = (
    "split=2[bg][fg];"
    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,boxblur=20[bgblur];"
    "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgscaled];"
    "[bgblur][fgscaled]overlay=(W-w)/2:(H-h)/2"
)
_TITLE_FONT_SIZE = 80
_TITLE_WRAP_FULLWIDTH_CHARS = 14
_ZERO_WIDTH_JOINER = "\u200d"
_EMOJI_VARIATION_SELECTOR = "\ufe0f"
_JAPANESE_FONT_KEYWORDS = (
    "noto sans cjk jp",
    "noto sans jp",
    "noto serif cjk jp",
    "noto serif jp",
    "source han sans",
    "source han serif",
    "ipaexgothic",
    "ipaexmincho",
    "ipagothic",
    "ipamincho",
    "biz udgothic",
    "biz udpgothic",
    "biz udmincho",
    "biz udpmincho",
    "yu gothic",
    "yugothic",
    "meiryo",
    "ms gothic",
    "ms mincho",
    "m plus",
    "takao",
    "vl gothic",
)


class _DefaultTitleFontConfig:
    font_name = "Noto Sans JP"


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


def _shorts_crop_filter(crop_x: str = "center") -> str:
    """9:16 縦クロップ + 1080x1920 スケールの vf フィルタを生成。crop_x で横位置を選ぶ。"""
    w = "ih*9/16"
    if crop_x == "left":
        x = "0"
    elif crop_x == "right":
        x = "iw-ih*9/16"
    else:  # center (default)
        x = "(iw-ih*9/16)/2"
    return f"crop={w}:ih:{x}:0,scale=1080:1920"


def _shorts_base_vf(mode: str = "crop", crop_x: str = "center") -> str:
    """Return the base 9:16 Shorts vf chain for crop/blur/pad modes."""
    if mode == "crop":
        return _shorts_crop_filter(crop_x)
    if mode == "pad":
        return _SHORTS_PAD_FILTER
    if mode == "blur":
        return _SHORTS_BLUR_FILTER
    logger.warning("Unknown shorts_mode=%r; falling back to crop", mode)
    return _shorts_crop_filter(crop_x)


def _title_char_width(ch: str) -> int:
    """Display width where full-width Japanese characters count as 2."""
    return 2 if unicodedata.east_asian_width(ch) in {"F", "W", "A"} else 1


def _is_title_combining_mark(ch: str) -> bool:
    return unicodedata.category(ch) in {"Mn", "Mc", "Me"}


def _is_title_variation_selector(ch: str) -> bool:
    return ch in {"\ufe0e", _EMOJI_VARIATION_SELECTOR}


def _is_title_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _is_title_emoji_modifier(ch: str) -> bool:
    return 0x1F3FB <= ord(ch) <= 0x1F3FF


def _is_title_emoji_presentation(cluster: str) -> bool:
    if _EMOJI_VARIATION_SELECTOR in cluster:
        return True
    if "\ufe0e" in cluster:
        return False
    return any(
        _is_title_regional_indicator(ch) or 0x1F000 <= ord(ch) <= 0x1FAFF
        for ch in cluster
    )


def _title_cluster_width(cluster: str) -> int:
    """Display width for one minimal grapheme cluster."""
    if not cluster:
        return 0
    if any(
        _title_char_width(ch) == 2
        for ch in cluster
        if not (
            ch == _ZERO_WIDTH_JOINER
            or _is_title_combining_mark(ch)
            or _is_title_variation_selector(ch)
            or _is_title_emoji_modifier(ch)
        )
    ):
        return 2
    return 2 if _is_title_emoji_presentation(cluster) else 1


def _title_grapheme_clusters(text: str):
    """Yield minimal title grapheme clusters without third-party dependencies."""
    current = ""

    for ch in text:
        if not current:
            current = ch
            continue

        joins_previous = (
            ch == _ZERO_WIDTH_JOINER
            or current.endswith(_ZERO_WIDTH_JOINER)
            or _is_title_combining_mark(ch)
            or _is_title_variation_selector(ch)
            or _is_title_emoji_modifier(ch)
        )
        if joins_previous:
            current += ch
            continue

        if (
            _is_title_regional_indicator(ch)
            and len(current) == 1
            and _is_title_regional_indicator(current)
        ):
            current += ch
            continue

        yield current
        current = ch

    if current:
        yield current


def _wrap_title_text(title: str, fullwidth_chars: int = _TITLE_WRAP_FULLWIDTH_CHARS) -> str:
    """Wrap title at roughly 13-15 full-width characters per line."""
    limit = fullwidth_chars * 2
    lines: list[str] = []

    for raw_line in (title or "").strip().splitlines():
        current = ""
        current_width = 0
        for cluster in _title_grapheme_clusters(raw_line):
            cluster_width = _title_cluster_width(cluster)
            if current and current_width + cluster_width > limit:
                lines.append(current.rstrip())
                if cluster.lstrip() != cluster:
                    current = ""
                    current_width = 0
                else:
                    current = cluster
                    current_width = cluster_width
            else:
                current += cluster
                current_width += cluster_width
        if current:
            lines.append(current.rstrip())

    return "\n".join(line for line in lines if line)


def _escape_drawtext_text(value: str) -> str:
    """Escape text for ffmpeg drawtext option syntax."""
    escaped: list[str] = []
    for ch in value:
        if ch == "\n":
            # drawtext renders an actual newline (0x0A) as a line break; the
            # literal sequence "\n" would print a stray "n" instead. Keep the
            # real newline so _wrap_title_text's wrapping survives to the video.
            escaped.append("\n")
        elif ch in {"\\", ":", "'", "%"}:
            escaped.append("\\" + ch)
        else:
            escaped.append(ch)
    return "".join(escaped)


def _escape_drawtext_path(path: str) -> str:
    """Escape a fontfile path for ffmpeg drawtext option syntax."""
    normalized = path.replace("\\", "/")
    return _escape_drawtext_text(normalized)


@lru_cache(maxsize=1)
def _fontconfig_fonts() -> tuple[tuple[str, str], ...]:
    """Return (fontfile, family) rows from fc-list, or empty when unavailable."""
    try:
        result = subprocess.run(
            ["fc-list", ":", "file", "family"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except Exception as exc:
        logger.warning("Could not run fc-list for title font detection: %s", exc)
        return ()

    if result.returncode != 0:
        logger.warning("fc-list failed while detecting title fonts: %s", result.stderr.strip())
        return ()

    fonts: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        path_text, sep, family = line.partition(":")
        if not sep:
            continue
        path_text = path_text.strip()
        family = family.strip()
        if path_text and family and Path(path_text).exists():
            fonts.append((path_text, family))
    return tuple(fonts)


@lru_cache(maxsize=32)
def _resolve_title_fontfile(font_name: str) -> str | None:
    """Find a fontfile for the requested or fallback Japanese font, if possible."""
    requested = (font_name or "").strip()
    fonts = _fontconfig_fonts()
    if not fonts:
        logger.warning(
            "No fontconfig fonts found; drawtext will use font=%r and may render tofu",
            requested or "Sans",
        )
        return None

    requested_lower = requested.lower()
    if requested_lower:
        for path_text, family in fonts:
            families = [item.strip().lower() for item in family.split(",")]
            if requested_lower in families or requested_lower in family.lower():
                return path_text

    for path_text, family in fonts:
        haystack = f"{family} {Path(path_text).name}".lower()
        if any(keyword in haystack for keyword in _JAPANESE_FONT_KEYWORDS):
            if requested:
                logger.warning(
                    "Title font %r was not found by fc-list; using Japanese fontfile fallback: %s",
                    requested,
                    path_text,
                )
            return path_text

    logger.warning(
        "No Japanese fontfile found via fc-list; drawtext will use font=%r and may render tofu",
        requested or "Sans",
    )
    return None


def _build_title_drawtext(title: str, font_config: "FontConfig") -> str:
    """Build a drawtext filter that shows the clip title for the first 4 seconds."""
    wrapped_title = _wrap_title_text(title)
    if not wrapped_title:
        return ""

    font_name = getattr(font_config, "font_name", "Noto Sans JP") or "Noto Sans JP"
    fontfile = _resolve_title_fontfile(font_name)
    parts = [
        f"font='{_escape_drawtext_text(font_name)}'",
    ]
    if fontfile:
        parts.append(f"fontfile='{_escape_drawtext_path(fontfile)}'")
    parts.extend([
        f"text='{_escape_drawtext_text(wrapped_title)}'",
        "expansion=none",
        "fontcolor=white",
        f"fontsize={_TITLE_FONT_SIZE}",
        "x=(w-text_w)/2",
        "y=140",
        "box=1",
        "boxcolor=black@0.5",
        "boxborderw=24",
        "enable='lt(t\\,4)'",
    ])
    return "drawtext=" + ":".join(parts)


def extract_clip(
    video_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    shorts: bool = False,
    srt_path: Path | None = None,
    font_config: "FontConfig | None" = None,
    crop_x: str = "center",
    shorts_mode: str = "crop",
    shorts_title: bool = True,
    title: str = "",
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
        vf_filters.append(_shorts_base_vf(shorts_mode, crop_x))
    if shorts and shorts_title and title:
        vf_filters.append(_build_title_drawtext(title, font_config or _DefaultTitleFontConfig()))
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
    crop_x: str = "center",
    shorts_mode: str = "crop",
    shorts_title: bool = True,
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
            crop_x=crop_x,
            shorts_mode=shorts_mode,
            shorts_title=shorts_title,
            title=h.get("title", ""),
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

    # _shorts_crop_filter: center (default) / left / right horizontal positions
    center_f = _shorts_crop_filter("center")
    assert "(iw-ih*9/16)/2" in center_f, f"center x missing: {center_f}"
    assert "scale=1080:1920" in center_f, f"center scale missing: {center_f}"
    assert _shorts_base_vf("crop", "center") == center_f, "crop base must keep existing behavior"
    assert center_f == "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920", center_f

    left_f = _shorts_crop_filter("left")
    assert ":0:0" in left_f, f"left x missing: {left_f}"
    assert "scale=1080:1920" in left_f, f"left scale missing: {left_f}"

    right_f = _shorts_crop_filter("right")
    assert "iw-ih*9/16:0" in right_f, f"right x missing: {right_f}"
    assert "scale=1080:1920" in right_f, f"right scale missing: {right_f}"

    assert _shorts_base_vf("pad") == _SHORTS_PAD_FILTER, "pad base mismatch"
    assert _shorts_base_vf("blur") == _SHORTS_BLUR_FILTER, "blur base mismatch"

    title_f = _build_title_drawtext("A:B's 50% C\\D あいうえおかきくけこさしすせそ", fc)
    for expected in [
        "drawtext=", "font='Noto Sans JP'", "text='A\\:B\\'s 50\\% C\\\\D",
        "fontsize=80", "fontcolor=white", "box=1", "boxcolor=black@0.5",
        "boxborderw=24", "x=(w-text_w)/2", "y=140", "enable='lt(t\\,4)'",
    ]:
        assert expected in title_f, f"missing: {expected} in {title_f}"
    assert "\n" in title_f, f"title should wrap long text: {title_f}"
    assert r"\n" not in title_f, f"newline must be a real 0x0A, not literal: {title_f}"

    print("clipper.py self-test: all assertions passed")
