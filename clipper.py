"""Video clip extraction using FFmpeg."""

import logging
import re
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
_WINDOWS_FILENAME_MAX_UTF16_UNITS = 255
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

# Bundled subtitle font shipped in fonts/ so Shorts captions render in a heavy
# gothic (Noto Sans JP Black / 源ノ角ゴシック Heavy 相当) even on machines where
# no Japanese font is installed. The internal family name is "Noto Sans JP Black"
# (verified via fc-scan); libass needs that exact name plus a fontsdir pointing
# at the bundle, while drawtext just loads the file by path.
_BUNDLED_FONTS_DIR = Path(__file__).resolve().parent / "fonts"
_BUNDLED_DEFAULT_FONT_FILE = _BUNDLED_FONTS_DIR / "NotoSansJP-Black.ttf"
_BUNDLED_DEFAULT_FONT_FAMILY = "Noto Sans JP Black"
# Requests that should resolve straight to the bundled heavy font file.
_BUNDLED_FONT_ALIASES = frozenset({
    "noto sans jp black",
    "源ノ角ゴシック heavy",
    "源ノ角ゴシック",
})


def _bundled_default_fontfile() -> str | None:
    """Absolute path to the bundled heavy JP font, or None if it isn't present."""
    return str(_BUNDLED_DEFAULT_FONT_FILE) if _BUNDLED_DEFAULT_FONT_FILE.is_file() else None


class _DefaultTitleFontConfig:
    font_name = _BUNDLED_DEFAULT_FONT_FAMILY


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


def _bundled_fontsdir_option() -> str:
    """`:fontsdir='...'` fragment so libass can load the bundled font, else ''."""
    if _BUNDLED_FONTS_DIR.is_dir():
        return f":fontsdir='{_escape_subtitles_path(_BUNDLED_FONTS_DIR)}'"
    return ""


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
    return f"subtitles='{escaped}'{_bundled_fontsdir_option()}:force_style='{style}'"


def _build_ass_subtitles_filter(ass_path: Path) -> str:
    escaped = _escape_subtitles_path(ass_path)
    return f"subtitles='{escaped}'{_bundled_fontsdir_option()}"


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
    requested_lower = requested.lower()
    bundled = _bundled_default_fontfile()

    # The bundled heavy font is requested by name (the new default) — use the
    # file directly. This also makes drawtext titles work on Windows, where
    # fc-list is absent and font-by-name resolution would otherwise fail.
    if bundled and requested_lower in _BUNDLED_FONT_ALIASES:
        return bundled

    fonts = _fontconfig_fonts()
    if not fonts:
        if bundled:
            return bundled
        logger.warning(
            "No fontconfig fonts found; drawtext will use font=%r and may render tofu",
            requested or "Sans",
        )
        return None

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

    # Last resort: the bundled heavy font beats tofu when nothing else matches.
    if bundled:
        logger.warning(
            "No matching Japanese font via fc-list for %r; using bundled heavy font: %s",
            requested or "Sans",
            bundled,
        )
        return bundled

    logger.warning(
        "No Japanese fontfile found via fc-list; drawtext will use font=%r and may render tofu",
        requested or "Sans",
    )
    return None


def _title_drawtext_parts(title: str, font_config: "FontConfig") -> list[str]:
    """Build shared drawtext options for title overlays."""
    wrapped_title = _wrap_title_text(title)
    if not wrapped_title:
        return []

    font_name = getattr(font_config, "font_name", _BUNDLED_DEFAULT_FONT_FAMILY) or _BUNDLED_DEFAULT_FONT_FAMILY
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
    ])
    return parts


def _build_title_drawtext(title: str, font_config: "FontConfig") -> str:
    """Build a drawtext filter that shows the clip title for the first 4 seconds."""
    parts = _title_drawtext_parts(title, font_config)
    if not parts:
        return ""

    parts.append("enable='lt(t\\,4)'")
    return "drawtext=" + ":".join(parts)


def _build_thumbnail_drawtext(title: str, font_config: "FontConfig") -> str:
    """Build a drawtext filter for a still thumbnail title overlay."""
    parts = _title_drawtext_parts(title, font_config)
    if not parts:
        return ""

    return "drawtext=" + ":".join(parts)


def _detect_scene_thumbnail_timestamp(
    video_path: Path | None,
    start_sec: float,
    end_sec: float,
) -> float | None:
    """Return the first scene-change timestamp in the clip window, if found."""
    if video_path is None:
        return None

    duration = max(0.0, end_sec - start_sec)
    if duration <= 0:
        return None

    cmd = [
        "ffmpeg", "-hide_banner",
        "-ss", str(start_sec),
        "-i", str(video_path),
        "-t", str(duration),
        "-vf", r"select='gt(scene\,0.4)',showinfo",
        "-frames:v", "1",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except Exception:
        return None

    match = re.search(r"pts_time:([-+]?\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        return None

    timestamp = float(match.group(1))
    if start_sec <= timestamp <= end_sec:
        return timestamp
    if 0 <= timestamp <= duration:
        return start_sec + timestamp
    return None


def _select_thumbnail_timestamp(
    start_sec: float,
    end_sec: float,
    strategy: str = "midpoint",
) -> float:
    """Select the representative timestamp for a thumbnail."""
    return _select_thumbnail_timestamp_for_video(None, start_sec, end_sec, strategy)


def _select_thumbnail_timestamp_for_video(
    video_path: Path | None,
    start_sec: float,
    end_sec: float,
    strategy: str = "midpoint",
) -> float:
    midpoint = (start_sec + end_sec) / 2
    if strategy == "midpoint":
        return midpoint

    if strategy == "scene":
        scene_timestamp = _detect_scene_thumbnail_timestamp(video_path, start_sec, end_sec)
        if scene_timestamp is not None:
            return scene_timestamp
        logger.warning(
            "No scene-change thumbnail frame found for %.3f-%.3f; "
            "falling back to midpoint %.3f",
            start_sec,
            end_sec,
            midpoint,
        )
        return midpoint

    logger.warning("Unknown thumbnail strategy=%r; falling back to midpoint", strategy)
    return midpoint


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
    karaoke: bool = False,
    ass_path: Path | None = None,
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
    if shorts and karaoke and ass_path is not None:
        vf_filters.append(_build_ass_subtitles_filter(ass_path))
    elif shorts and srt_path is not None and font_config is not None:
        vf_filters.append(_build_subtitles_filter(srt_path, font_config))
    if vf_filters:
        cmd.extend(["-vf", ",".join(vf_filters)])

    cmd.append(str(output_path))
    subprocess.run(cmd, capture_output=True, check=True)
    return output_path


def generate_thumbnail(
    video_path: Path,
    output_path: Path,
    start_sec: float,
    end_sec: float,
    *,
    vertical: bool = False,
    crop_x: str = "center",
    shorts_mode: str = "crop",
    title: str = "",
    font_config: "FontConfig | None" = None,
    strategy: str = "midpoint",
) -> Path:
    """Generate one representative still image for a highlight clip."""
    video_path = Path(video_path)
    output_path = Path(output_path)
    if strategy == "scene":
        timestamp = _select_thumbnail_timestamp_for_video(
            video_path,
            start_sec,
            end_sec,
            strategy,
        )
    else:
        timestamp = _select_thumbnail_timestamp(start_sec, end_sec, strategy)

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(timestamp),
        "-i", str(video_path),
        "-frames:v", "1",
    ]

    vf_filters: list[str] = []
    if vertical:
        vf_filters.append(_shorts_base_vf(shorts_mode, crop_x))

    drawtext = _build_thumbnail_drawtext(
        title,
        font_config or _DefaultTitleFontConfig(),
    )
    if drawtext:
        vf_filters.append(drawtext)
    if vf_filters:
        cmd.extend(["-vf", ",".join(vf_filters)])

    if output_path.suffix.lower() in {".jpg", ".jpeg"}:
        cmd.extend(["-q:v", "2"])

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


def _sanitize_filename_title(title: str) -> str:
    """Return a readable title safe to use in Windows clip filenames."""
    cleaned = re.sub(r"\s+", " ", str(title or "")).strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", cleaned)
    cleaned = re.sub(r"\s*_\s*", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip(" ._")


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate_title_utf16(title: str, max_units: int) -> str:
    """Trim a title without splitting emoji/grapheme clusters."""
    kept: list[str] = []
    used_units = 0
    for cluster in _title_grapheme_clusters(title):
        cluster_units = _utf16_units(cluster)
        if used_units + cluster_units > max_units:
            break
        kept.append(cluster)
        used_units += cluster_units
    return "".join(kept).rstrip(" ._")


def _build_clip_filename(range_str: str, title: str, shorts: bool) -> str:
    suffix = "_short" if shorts else ""
    safe_title = _sanitize_filename_title(title)
    if safe_title:
        fixed_parts = f"{range_str}_{suffix}.mp4"
        available_units = max(
            0,
            _WINDOWS_FILENAME_MAX_UTF16_UNITS - _utf16_units(fixed_parts),
        )
        safe_title = _truncate_title_utf16(safe_title, available_units)
    title_suffix = f"_{safe_title}" if safe_title else ""
    return f"{range_str}{title_suffix}{suffix}.mp4"


def generate_thumbnails(
    video_path: Path,
    highlights: list[dict],
    output_dir: Path,
    *,
    vertical: bool = False,
    crop_x: str = "center",
    shorts_mode: str = "crop",
    font_config: "FontConfig | None" = None,
    img_format: str = "png",
    strategy: str = "midpoint",
) -> list[Path]:
    """Generate thumbnail candidate images for all highlights."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_paths: list[Path] = []
    ext = (img_format or "png").strip().lower().lstrip(".") or "png"

    for i, h in enumerate(highlights, 1):
        range_str = format_time_range(h["start_sec"], h["end_sec"])
        thumbnail_path = output_dir / f"{range_str}_thumb.{ext}"

        print(f"Generating thumbnail {i}/{len(highlights)}: {h.get('title', '')}...")
        generate_thumbnail(
            video_path,
            thumbnail_path,
            h["start_sec"],
            h["end_sec"],
            vertical=vertical,
            crop_x=crop_x,
            shorts_mode=shorts_mode,
            title=h.get("title", ""),
            font_config=font_config,
            strategy=strategy,
        )
        thumbnail_paths.append(thumbnail_path)

    return thumbnail_paths


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
    karaoke: bool = False,
    ass_paths: list[Path] | None = None,
) -> list[Path]:
    """Extract all highlight clips."""
    output_dir.mkdir(parents=True, exist_ok=True)
    clip_paths = []

    for i, h in enumerate(highlights, 1):
        range_str = format_time_range(h["start_sec"], h["end_sec"])
        clip_name = _build_clip_filename(range_str, h.get("title", ""), shorts)
        clip_path = output_dir / clip_name

        srt_path = srt_paths[i - 1] if srt_paths and i - 1 < len(srt_paths) else None
        ass_path = ass_paths[i - 1] if ass_paths and i - 1 < len(ass_paths) else None

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
            karaoke=karaoke,
            ass_path=ass_path,
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
    # When the bundled font dir exists, a fontsdir hint is appended so libass can
    # load it even on machines without the font installed.
    dir_opt = _bundled_fontsdir_option()
    filt = _build_subtitles_filter(Path("C:/Users/x/clip.srt"), fc)
    assert filt.startswith("subtitles='C\\:/Users/x/clip.srt'"), f"bad escape: {filt}"
    assert "force_style='FontName=Noto Sans JP" in filt, f"style missing: {filt}"
    assert filt == f"subtitles='C\\:/Users/x/clip.srt'{dir_opt}:force_style='{_build_force_style(fc)}'", f"srt filter mismatch: {filt}"

    ass_filt = _build_ass_subtitles_filter(Path("C:/Users/x/clip.ass"))
    assert ass_filt == f"subtitles='C\\:/Users/x/clip.ass'{dir_opt}", f"bad ASS escape: {ass_filt}"
    assert "force_style" not in ass_filt, f"ASS filter must not force style: {ass_filt}"

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
    assert _select_thumbnail_timestamp(10, 30) == 20.0, "thumbnail midpoint mismatch"

    title_text = "A:B's 50% C\\D あいうえおかきくけこさしすせそ"
    title_f = _build_title_drawtext(title_text, fc)
    for expected in [
        "drawtext=", "font='Noto Sans JP'", "text='A\\:B\\'s 50\\% C\\\\D",
        "fontsize=80", "fontcolor=white", "box=1", "boxcolor=black@0.5",
        "boxborderw=24", "x=(w-text_w)/2", "y=140", "enable='lt(t\\,4)'",
    ]:
        assert expected in title_f, f"missing: {expected} in {title_f}"
    assert "\n" in title_f, f"title should wrap long text: {title_f}"
    assert r"\n" not in title_f, f"newline must be a real 0x0A, not literal: {title_f}"

    thumb_f = _build_thumbnail_drawtext(title_text, fc)
    assert thumb_f.startswith("drawtext="), f"thumbnail drawtext missing: {thumb_f}"
    assert "enable=" not in thumb_f, f"thumbnail drawtext must not use enable: {thumb_f}"
    for expected in [
        "font='Noto Sans JP'", "text='A\\:B\\'s 50\\% C\\\\D",
        "fontsize=80", "fontcolor=white", "box=1", "boxcolor=black@0.5",
        "boxborderw=24", "x=(w-text_w)/2", "y=140",
    ]:
        assert expected in thumb_f, f"missing thumbnail part: {expected} in {thumb_f}"
    assert title_f == f"{thumb_f}:enable='lt(t\\,4)'", "title/thumbnail style diverged"
    assert _build_thumbnail_drawtext("   \n", fc) == "", "empty thumbnail title should skip drawtext"

    print("clipper.py self-test: all assertions passed")
