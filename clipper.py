"""Video clip extraction using FFmpeg."""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import uuid
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from user_media import UserMediaAsset, validate_user_media

from video_effects import (
    ClipEffectPlan,
    EffectPreset,
    VfxAnchor,
    VfxOptions,
    has_owned_effects_manifest,
    prepare_vfx_assets,
    resolve_clip_effect_plan,
    validate_effects_manifest_target,
    write_effects_manifest,
)

if TYPE_CHECKING:
    from config import FontConfig

logger = logging.getLogger(__name__)


class _VfxBatchTransaction:
    """Restore the previous clip set if an effect batch fails part-way."""

    def __init__(self, output_dir: Path, output_paths: Sequence[Path]) -> None:
        self.output_dir = output_dir.resolve()
        self.output_paths = tuple(path.resolve() for path in output_paths)
        self.backup_dir: Path | None = None
        self.backups: list[tuple[Path, Path]] = []
        self.preexisting: set[Path] = set()

    def begin(self) -> None:
        self.backup_dir = Path(
            tempfile.mkdtemp(prefix=".vfx-batch-", dir=str(self.output_dir))
        )
        try:
            for index, output in enumerate(self.output_paths):
                if output.parent != self.output_dir:
                    raise ValueError(f"VFX output is outside its batch folder: {output}")
                if output.is_symlink():
                    raise ValueError(f"VFX output cannot replace a link: {output}")
                if not output.exists():
                    continue
                if not output.is_file():
                    raise ValueError(f"VFX output target is not a file: {output}")
                backup = self.backup_dir / f"{index:04d}{output.suffix}"
                shutil.copy2(output, backup)
                self.backups.append((backup, output))
                self.preexisting.add(output)
        except BaseException:
            self._discard_backups()
            raise

    def rollback(self) -> None:
        for output in self.output_paths:
            if output not in self.preexisting:
                output.unlink(missing_ok=True)
        for backup, output in reversed(self.backups):
            if backup.exists():
                os.replace(backup, output)
        self._discard_backups()

    def complete(self) -> None:
        self._discard_backups()

    def _discard_backups(self) -> None:
        if self.backup_dir is None:
            return
        for backup, _output in self.backups:
            backup.unlink(missing_ok=True)
        try:
            self.backup_dir.rmdir()
        except FileNotFoundError:
            pass
        self.backup_dir = None
        self.backups.clear()

_SHORTS_PAD_FILTER = (
    "scale=1080:1920:force_original_aspect_ratio=decrease,"
    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
)
_DEFAULT_SHORTS_BLUR_STRENGTH = 20.0
_MIN_SHORTS_BLUR_STRENGTH = 0.0
_MAX_SHORTS_BLUR_STRENGTH = 50.0
_SHORTS_BLUR_FILTER_TEMPLATE = (
    "split=2[bg][fg];"
    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,boxblur={blur_strength}[bgblur];"
    "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgscaled];"
    "[bgblur][fgscaled]overlay=(W-w)/2:(H-h)/2"
)
_SHORTS_FRAME_HEIGHT = 1920
_SHORTS_FRAME_WIDTH = 1080
_FFMPEG_SRT_PLAY_RES_Y = 288
_TITLE_FONT_SIZE = 80
_TITLE_MIN_FONT_SIZE = 32
_TITLE_WRAP_FULLWIDTH_CHARS = 14
_TITLE_BOX_BORDER = 24
_TITLE_SAFE_EDGE_MARGIN = 32
_TITLE_Y_BY_POSITION = {
    "top": "140",
    # Leave the bottom 300px clear for the archive/speech captions.
    "bottom": "h-text_h-360",
    "overlay": "(h-text_h)/2",
}
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

# Bundled subtitle font shipped in fonts/ so Shorts captions render in a bold
# gothic (Noto Sans JP Bold, weight 700) even on machines where
# no Japanese font is installed. Its internal family is "Noto Sans JP" with
# Bold style; libass gets the family plus Bold=-1 and a fontsdir, while drawtext
# loads the exact file path.
_BUNDLED_FONTS_DIR = Path(__file__).resolve().parent / "fonts"
_BUNDLED_DEFAULT_FONT_FILE = _BUNDLED_FONTS_DIR / "NotoSansJP-Bold.otf"
_BUNDLED_DEFAULT_FONT_FAMILY = "Noto Sans JP"
# Requests that should resolve straight to the bundled bold font file.
_BUNDLED_FONT_ALIASES = frozenset({
    "noto sans jp",
    "noto sans jp bold",
    "noto sans cjk jp bold",
    "noto sans jp black",
    "源ノ角ゴシック heavy",
    "源ノ角ゴシック",
})


def _bundled_default_fontfile() -> str | None:
    """Absolute path to the bundled heavy JP font, or None if it isn't present."""
    return str(_BUNDLED_DEFAULT_FONT_FILE) if _BUNDLED_DEFAULT_FONT_FILE.is_file() else None


class _DefaultTitleFontConfig:
    font_name = _BUNDLED_DEFAULT_FONT_FAMILY


def _is_bundled_default_font_name(font_name: str) -> bool:
    return str(font_name or "").strip().lower() in _BUNDLED_FONT_ALIASES


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


def _srt_style_scale(value: float) -> float:
    """Convert a 1080x1920 pixel setting to FFmpeg's implicit SRT PlayRes."""
    return float(value) * _FFMPEG_SRT_PLAY_RES_Y / _SHORTS_FRAME_HEIGHT


def _format_style_number(value: float) -> str:
    return f"{value:g}"


def _build_force_style(font_config: "FontConfig") -> str:
    """Build body-caption style for FFmpeg's implicit 384x288 SRT canvas."""
    alignment = 8 if getattr(font_config, "position", "bottom") == "top" else 2
    font_size = _format_style_number(_srt_style_scale(font_config.font_size))
    outline_width = _format_style_number(
        _srt_style_scale(font_config.outline_width)
    )
    margin_v = round(_srt_style_scale(font_config.margin_bottom))
    parts = [
        f"FontName={font_config.font_name}",
        f"FontSize={font_size}",
        f"PrimaryColour={_hex_to_ass_color(font_config.font_color)}",
        f"OutlineColour={_hex_to_ass_color(font_config.outline_color)}",
        f"Outline={outline_width}",
        f"Alignment={alignment}",
        f"MarginV={margin_v}",
        "BorderStyle=1",
    ]
    if _is_bundled_default_font_name(font_config.font_name):
        parts.append("Bold=-1")
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


def _coerce_shorts_blur_strength(value) -> float:
    """Return a finite blur radius clamped to the range exposed by the UI."""
    try:
        strength = float(value)
    except (TypeError, ValueError):
        strength = _DEFAULT_SHORTS_BLUR_STRENGTH
    if strength != strength:  # NaN
        strength = _DEFAULT_SHORTS_BLUR_STRENGTH
    return min(
        _MAX_SHORTS_BLUR_STRENGTH,
        max(_MIN_SHORTS_BLUR_STRENGTH, strength),
    )


def _shorts_blur_filter(blur_strength=_DEFAULT_SHORTS_BLUR_STRENGTH) -> str:
    strength = _format_style_number(_coerce_shorts_blur_strength(blur_strength))
    return _SHORTS_BLUR_FILTER_TEMPLATE.format(blur_strength=strength)


_SHORTS_BLUR_FILTER = _shorts_blur_filter()


def _shorts_base_vf(
    mode: str = "pad",
    crop_x: str = "center",
    blur_strength=_DEFAULT_SHORTS_BLUR_STRENGTH,
) -> str:
    """Return the base 9:16 Shorts vf chain for crop/blur/pad modes."""
    if mode == "crop":
        return _shorts_crop_filter(crop_x)
    if mode == "pad":
        return _SHORTS_PAD_FILTER
    if mode == "blur":
        return _shorts_blur_filter(blur_strength)
    logger.warning("Unknown shorts_mode=%r; falling back to pad", mode)
    return _SHORTS_PAD_FILTER


def _effect_overlay_position(anchor: VfxAnchor) -> tuple[str, str]:
    horizontal = {
        VfxAnchor.TOP_LEFT: "W*0.04",
        VfxAnchor.LEFT: "W*0.04",
        VfxAnchor.BOTTOM_LEFT: "W*0.04",
        VfxAnchor.TOP: "(W-w)/2",
        VfxAnchor.CENTER: "(W-w)/2",
        VfxAnchor.BOTTOM: "(W-w)/2",
        VfxAnchor.TOP_RIGHT: "W-w-W*0.04",
        VfxAnchor.RIGHT: "W-w-W*0.04",
        VfxAnchor.BOTTOM_RIGHT: "W-w-W*0.04",
    }
    vertical = {
        VfxAnchor.TOP_LEFT: "H*0.06",
        VfxAnchor.TOP: "H*0.06",
        VfxAnchor.TOP_RIGHT: "H*0.06",
        VfxAnchor.LEFT: "(H-h)/2",
        VfxAnchor.CENTER: "(H-h)/2",
        VfxAnchor.RIGHT: "(H-h)/2",
        VfxAnchor.BOTTOM_LEFT: "H-h-H*0.06",
        VfxAnchor.BOTTOM: "H-h-H*0.06",
        VfxAnchor.BOTTOM_RIGHT: "H-h-H*0.06",
    }
    return horizontal[anchor], vertical[anchor]


def _between_expr(start: float, end: float) -> str:
    return (
        "between(t\\,"
        f"{_format_style_number(start)}\\,{_format_style_number(end)})"
    )


def _complex_effect_graph(
    plan: ClipEffectPlan,
    clip_duration: float,
    *,
    shorts: bool,
    shorts_mode: str,
    crop_x: str,
    shorts_blur_strength,
    post_filters: list[str],
) -> str:
    """Build the one-pass graph used only when an effect plan is enabled."""

    segments: list[str] = []
    if shorts and shorts_mode == "blur":
        strength = _format_style_number(
            _coerce_shorts_blur_strength(shorts_blur_strength)
        )
        segments.extend(
            [
                "[0:v:0]setpts=PTS-STARTPTS,split=2[vfx_bg][vfx_fg]",
                "[vfx_bg]scale=1080:1920:force_original_aspect_ratio=increase,"
                f"crop=1080:1920,boxblur={strength}[vfx_bgblur]",
                "[vfx_fg]scale=1080:1920:force_original_aspect_ratio=decrease"
                "[vfx_fgscaled]",
                "[vfx_bgblur][vfx_fgscaled]overlay=(W-w)/2:(H-h)/2[scene0]",
            ]
        )
    elif shorts:
        base_filter = _shorts_base_vf(
            shorts_mode,
            crop_x,
            shorts_blur_strength,
        )
        segments.append(f"[0:v:0]setpts=PTS-STARTPTS,{base_filter}[scene0]")
    else:
        segments.append("[0:v:0]setpts=PTS-STARTPTS[scene0]")

    current = "scene0"
    if plan.effect_preset is EffectPreset.PUNCH:
        punch_end = min(
            clip_duration,
            plan.cue_seconds + min(0.2, plan.duration_seconds),
        )
        enabled = _between_expr(plan.cue_seconds, punch_end)
        segments.extend(
            [
                f"[{current}]split=2[punchbase][punchsrc]",
                "[punchsrc]scale=iw*1.08:ih*1.08:flags=bicubic,"
                "crop=iw/1.08:ih/1.08:(iw-ow)/2:(ih-oh)/2[punchzoom]",
                "[punchbase][punchzoom]overlay=x=0:y=0:eof_action=pass:"
                f"repeatlast=0:enable='{enabled}'[effectout]",
            ]
        )
        current = "effectout"
    elif plan.effect_preset is EffectPreset.FLASH:
        flash_end = min(
            clip_duration,
            plan.cue_seconds + min(0.16, plan.duration_seconds),
        )
        enabled = _between_expr(plan.cue_seconds, flash_end)
        segments.append(
            f"[{current}]drawbox=x=0:y=0:w=iw:h=ih:color=white@0.35:"
            f"t=fill:enable='{enabled}'[effectout]"
        )
        current = "effectout"

    if plan.asset is not None:
        cue = plan.cue_seconds
        end = min(clip_duration, cue + plan.duration_seconds)
        visible_duration = max(0.001, end - cue)
        scale = plan.scale_percent / 100.0
        opacity = plan.opacity_percent / 100.0
        alpha_fade = min(0.12, visible_duration / 2.0)
        fade_out_start = max(0.0, visible_duration - alpha_fade)
        x_expr, y_expr = _effect_overlay_position(plan.anchor)
        enabled = _between_expr(cue, end)
        segments.extend(
            [
                "[1:v:0]setpts=PTS-STARTPTS,"
                f"scale=iw*{_format_style_number(scale)}:"
                f"ih*{_format_style_number(scale)}:flags=lanczos,"
                "format=rgba,"
                f"colorchannelmixer=aa={_format_style_number(opacity)},"
                f"trim=duration={_format_style_number(visible_duration)},"
                f"fade=t=in:st=0:d={_format_style_number(alpha_fade)}:alpha=1,"
                f"fade=t=out:st={_format_style_number(fade_out_start)}:"
                f"d={_format_style_number(alpha_fade)}:alpha=1,"
                f"setpts=PTS-STARTPTS+{_format_style_number(cue)}/TB[vfxtimed]",
                f"[{current}][vfxtimed]overlay=x={x_expr}:y={y_expr}:"
                "eof_action=pass:repeatlast=0:"
                f"enable='{enabled}'[vfxout]",
            ]
        )
        current = "vfxout"

    for index, filter_text in enumerate(post_filters):
        output_label = f"post{index}"
        segments.append(f"[{current}]{filter_text}[{output_label}]")
        current = output_label
    if plan.effect_preset is EffectPreset.FADE:
        # Fade the completed frame, including VFX, title, and subtitles.  A
        # scene-only fade would leave overlays visible over a black frame.
        fade_duration = min(0.25, clip_duration / 2.0)
        fade_out_start = max(0.0, clip_duration - fade_duration)
        segments.append(
            f"[{current}]fade=t=in:st=0:d={_format_style_number(fade_duration)},"
            f"fade=t=out:st={_format_style_number(fade_out_start)}:"
            f"d={_format_style_number(fade_duration)}[effectout]"
        )
        current = "effectout"
    segments.append(f"[{current}]format=yuv420p[vout]")
    return ";".join(segments)


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


@lru_cache(maxsize=256)
def _measure_title_width(
    wrapped_title: str,
    fontfile: str | None,
    font_size: int,
) -> float:
    """Estimate FFmpeg drawtext width using the same font at the target size."""
    lines = wrapped_title.splitlines() or [wrapped_title]

    if fontfile:
        try:
            from PIL import ImageFont

            font = ImageFont.truetype(fontfile, font_size)
            measured = max(float(font.getlength(line)) for line in lines)
            return measured + 2.0  # Allow for renderer rounding/hinting.
        except (ImportError, OSError, ValueError) as exc:
            logger.warning(
                "Could not measure title with fontfile %r; using a conservative fallback: %s",
                fontfile,
                exc,
            )

    max_units = max(
        sum(_title_cluster_width(cluster) for cluster in _title_grapheme_clusters(line))
        for line in lines
    )
    measured = (max_units / 2) * font_size * 1.1
    return measured + 2.0


def _fit_title_font_size(wrapped_title: str, fontfile: str | None) -> int:
    """Return the largest title size whose box stays inside the Shorts frame."""
    available_text_width = (
        _SHORTS_FRAME_WIDTH
        - 2 * _TITLE_SAFE_EDGE_MARGIN
        - 2 * _TITLE_BOX_BORDER
    )
    low = _TITLE_MIN_FONT_SIZE
    high = _TITLE_FONT_SIZE
    best = _TITLE_MIN_FONT_SIZE

    while low <= high:
        candidate = (low + high) // 2
        if _measure_title_width(wrapped_title, fontfile, candidate) <= available_text_width:
            best = candidate
            low = candidate + 1
        else:
            high = candidate - 1

    return best


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


def _title_drawtext_parts(
    title: str,
    font_config: "FontConfig",
    position: str = "top",
) -> list[str]:
    """Build shared drawtext options for title overlays."""
    wrapped_title = _wrap_title_text(title)
    if not wrapped_title:
        return []

    font_name = getattr(font_config, "font_name", _BUNDLED_DEFAULT_FONT_FAMILY) or _BUNDLED_DEFAULT_FONT_FAMILY
    fontfile = _resolve_title_fontfile(font_name)
    title_font_size = _fit_title_font_size(wrapped_title, fontfile)
    parts = [
        f"font='{_escape_drawtext_text(font_name)}'",
    ]
    if fontfile:
        parts.append(f"fontfile='{_escape_drawtext_path(fontfile)}'")
    title_y = _TITLE_Y_BY_POSITION.get(position, _TITLE_Y_BY_POSITION["top"])
    parts.extend([
        f"text='{_escape_drawtext_text(wrapped_title)}'",
        "expansion=none",
        "fontcolor=white",
        f"fontsize={title_font_size}",
        "x=(w-text_w)/2",
        f"y={title_y}",
        "fix_bounds=1",
        "box=1",
        "boxcolor=black@0.5",
        f"boxborderw={_TITLE_BOX_BORDER}",
    ])
    return parts


def _build_multiline_title_drawtext(
    title: str,
    font_config: "FontConfig",
    position: str,
    *,
    timed: bool,
) -> str:
    """Draw wrapped title lines separately so newlines never render as tofu."""
    wrapped_title = _wrap_title_text(title)
    lines = wrapped_title.splitlines()
    if len(lines) < 2:
        return ""

    font_name = getattr(font_config, "font_name", _BUNDLED_DEFAULT_FONT_FAMILY) or _BUNDLED_DEFAULT_FONT_FAMILY
    fontfile = _resolve_title_fontfile(font_name)
    title_font_size = _fit_title_font_size(wrapped_title, fontfile)
    line_height = round(title_font_size * 1.2)
    box_height = (line_height * len(lines)) + (2 * _TITLE_BOX_BORDER)

    if position == "bottom":
        box_y = f"ih-336-{box_height}"
        first_line_y = f"h-336-{box_height}+{_TITLE_BOX_BORDER}"
    elif position == "overlay":
        box_y = f"(ih-{box_height})/2"
        first_line_y = f"(h-{box_height})/2+{_TITLE_BOX_BORDER}"
    else:
        box_y = str(140 - _TITLE_BOX_BORDER)
        first_line_y = f"{box_y}+{_TITLE_BOX_BORDER}"

    enable = ":enable='lt(t\\,4)'" if timed else ""
    filters = [
        "drawbox="
        f"x={_TITLE_SAFE_EDGE_MARGIN}:"
        f"y={box_y}:"
        f"w=iw-{2 * _TITLE_SAFE_EDGE_MARGIN}:"
        f"h={box_height}:"
        "color=black@0.5:t=fill"
        f"{enable}"
    ]

    for index, line in enumerate(lines):
        parts = [f"font='{_escape_drawtext_text(font_name)}'"]
        if fontfile:
            parts.append(f"fontfile='{_escape_drawtext_path(fontfile)}'")
        parts.extend([
            f"text='{_escape_drawtext_text(line)}'",
            "expansion=none",
            "fontcolor=white",
            f"fontsize={title_font_size}",
            "x=(w-text_w)/2",
            f"y={first_line_y}+{index * line_height}",
            "fix_bounds=1",
        ])
        if timed:
            parts.append("enable='lt(t\\,4)'")
        filters.append("drawtext=" + ":".join(parts))

    return ",".join(filters)


def _build_title_drawtext(
    title: str,
    font_config: "FontConfig",
    position: str = "top",
) -> str:
    """Build a drawtext filter that shows the clip title for the first 4 seconds."""
    multiline = _build_multiline_title_drawtext(
        title,
        font_config,
        position,
        timed=True,
    )
    if multiline:
        return multiline

    parts = _title_drawtext_parts(title, font_config, position)
    if not parts:
        return ""

    parts.append("enable='lt(t\\,4)'")
    return "drawtext=" + ":".join(parts)


def _build_thumbnail_drawtext(
    title: str,
    font_config: "FontConfig",
    position: str = "top",
) -> str:
    """Build a drawtext filter for a still thumbnail title overlay."""
    multiline = _build_multiline_title_drawtext(
        title,
        font_config,
        position,
        timed=False,
    )
    if multiline:
        return multiline

    parts = _title_drawtext_parts(title, font_config, position)
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
    shorts_mode: str = "pad",
    shorts_title: bool = True,
    title: str = "",
    karaoke: bool = False,
    ass_path: Path | None = None,
    shorts_blur_strength=_DEFAULT_SHORTS_BLUR_STRENGTH,
    shorts_title_position: str = "top",
    effect_plan: ClipEffectPlan | None = None,
) -> Path:
    """Extract a clip from the video."""
    duration = end_sec - start_sec

    legacy_cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_sec),
        "-i", str(video_path),
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
    ]

    vf_filters: list[str] = []
    if shorts:
        vf_filters.append(
            _shorts_base_vf(shorts_mode, crop_x, shorts_blur_strength)
        )
    if shorts and shorts_title and title:
        vf_filters.append(
            _build_title_drawtext(
                title,
                font_config or _DefaultTitleFontConfig(),
                shorts_title_position,
            )
        )
    if shorts and karaoke and ass_path is not None:
        vf_filters.append(_build_ass_subtitles_filter(ass_path))
    elif shorts and srt_path is not None and font_config is not None:
        vf_filters.append(_build_subtitles_filter(srt_path, font_config))
    if effect_plan is not None and not isinstance(effect_plan, ClipEffectPlan):
        raise TypeError("effect_plan must be a ClipEffectPlan")
    use_complex_effects = effect_plan is not None and effect_plan.enabled
    if use_complex_effects:
        if effect_plan.asset is not None:
            validated_asset = validate_user_media(effect_plan.asset)
            if isinstance(validated_asset, UserMediaAsset):
                effect_plan = ClipEffectPlan(
                    asset=validated_asset,
                    effect_preset=effect_plan.effect_preset,
                    cue_seconds=effect_plan.cue_seconds,
                    duration_seconds=effect_plan.duration_seconds,
                    anchor=effect_plan.anchor,
                    scale_percent=effect_plan.scale_percent,
                    opacity_percent=effect_plan.opacity_percent,
                )
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", str(video_path),
        ]
        if effect_plan.asset is not None:
            suffix = effect_plan.asset.path.suffix.lower()
            if suffix == ".png":
                cmd.extend(["-loop", "1", "-framerate", "30"])
            elif suffix == ".webm":
                cmd.extend(["-stream_loop", "-1"])
                if effect_plan.asset.video_has_alpha:
                    decoder = {
                        "vp9": "libvpx-vp9",
                        "vp8": "libvpx",
                    }.get(effect_plan.asset.video_codec)
                    if decoder:
                        cmd.extend(["-c:v", decoder])
            else:
                raise ValueError(f"Unsupported VFX file type: {suffix}")
            cmd.extend(["-i", str(effect_plan.asset.path)])

        post_filters = []
        if shorts and shorts_title and title:
            post_filters.append(
                _build_title_drawtext(
                    title,
                    font_config or _DefaultTitleFontConfig(),
                    shorts_title_position,
                )
            )
        if shorts and karaoke and ass_path is not None:
            post_filters.append(_build_ass_subtitles_filter(ass_path))
        elif shorts and srt_path is not None and font_config is not None:
            post_filters.append(_build_subtitles_filter(srt_path, font_config))
        graph = _complex_effect_graph(
            effect_plan,
            duration,
            shorts=shorts,
            shorts_mode=shorts_mode,
            crop_x=crop_x,
            shorts_blur_strength=shorts_blur_strength,
            post_filters=post_filters,
        )
        cmd.extend(
            [
                "-t", str(duration),
                "-filter_complex", graph,
                "-map", "[vout]",
                "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
            ]
        )
    else:
        cmd = legacy_cmd
        if vf_filters:
            cmd.extend(["-vf", ",".join(vf_filters)])

    if use_complex_effects:
        temporary_output = output_path.with_name(
            f".{output_path.stem}.vfx-{uuid.uuid4().hex}{output_path.suffix or '.mp4'}"
        )
        cmd.append(str(temporary_output))
        try:
            subprocess.run(cmd, capture_output=True, check=True)
            if not temporary_output.is_file():
                raise RuntimeError("FFmpeg did not create the temporary VFX output")
            os.replace(temporary_output, output_path)
        finally:
            temporary_output.unlink(missing_ok=True)
    else:
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
    shorts_mode: str = "pad",
    shorts_blur_strength=_DEFAULT_SHORTS_BLUR_STRENGTH,
    shorts_title_position: str = "top",
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
        vf_filters.append(
            _shorts_base_vf(shorts_mode, crop_x, shorts_blur_strength)
        )

    drawtext = _build_thumbnail_drawtext(
        title,
        font_config or _DefaultTitleFontConfig(),
        shorts_title_position,
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
    """Format a filename-safe range, retaining milliseconds when present."""
    def fmt(sec: float) -> str:
        total_ms = max(0, int(round(float(sec) * 1000)))
        h, remainder = divmod(total_ms, 3_600_000)
        m, remainder = divmod(remainder, 60_000)
        s, milliseconds = divmod(remainder, 1000)
        suffix = f"{milliseconds:03d}ms" if milliseconds else ""
        return f"{h:02d}h{m:02d}m{s:02d}s{suffix}"
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


def _build_clip_filename(
    range_str: str,
    title: str,
    shorts: bool,
    *,
    asset_suffix: str = "",
    extension: str = ".mp4",
) -> str:
    suffix = "_short" if shorts else ""
    if not extension.startswith("."):
        extension = f".{extension}"
    safe_title = _sanitize_filename_title(title)
    if safe_title:
        fixed_parts = f"{range_str}_{suffix}{asset_suffix}{extension}"
        available_units = max(
            0,
            _WINDOWS_FILENAME_MAX_UTF16_UNITS - _utf16_units(fixed_parts),
        )
        safe_title = _truncate_title_utf16(safe_title, available_units)
    title_suffix = f"_{safe_title}" if safe_title else ""
    return f"{range_str}{title_suffix}{suffix}{asset_suffix}{extension}"


def generate_thumbnails(
    video_path: Path,
    highlights: list[dict],
    output_dir: Path,
    *,
    vertical: bool = False,
    crop_x: str = "center",
    shorts_mode: str = "pad",
    shorts_blur_strength=_DEFAULT_SHORTS_BLUR_STRENGTH,
    shorts_title_position: str = "top",
    font_config: "FontConfig | None" = None,
    img_format: str = "png",
    strategy: str = "midpoint",
    protected_source_paths: Sequence[str | os.PathLike[str]] = (),
) -> list[Path]:
    """Generate thumbnail candidate images for all highlights."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_paths: list[Path] = []
    ext = (img_format or "png").strip().lower().lstrip(".") or "png"
    protected = tuple(
        Path(path).expanduser().resolve()
        for path in protected_source_paths
    )
    used_names: set[str] = set()

    for h in highlights:
        range_str = format_time_range(h["start_sec"], h["end_sec"])
        name = f"{range_str}_thumb.{ext}"
        collision_index = 2
        while name.casefold() in used_names:
            name = f"{range_str}_thumb_dup{collision_index:02d}.{ext}"
            collision_index += 1
        used_names.add(name.casefold())
        candidate = output_dir / name
        if any(_same_file_target(candidate, source) for source in protected):
            raise ValueError(
                f"Thumbnail output would overwrite a selected VFX source: {candidate}"
            )
        thumbnail_paths.append(candidate)

    for i, (h, thumbnail_path) in enumerate(
        zip(highlights, thumbnail_paths),
        1,
    ):
        print(f"Generating thumbnail {i}/{len(highlights)}: {h.get('title', '')}...")
        generate_thumbnail(
            video_path,
            thumbnail_path,
            h["start_sec"],
            h["end_sec"],
            vertical=vertical,
            crop_x=crop_x,
            shorts_mode=shorts_mode,
            shorts_blur_strength=shorts_blur_strength,
            shorts_title_position=shorts_title_position,
            title=h.get("title", ""),
            font_config=font_config,
            strategy=strategy,
        )
    return thumbnail_paths


def _same_file_target(candidate: Path, source: Path) -> bool:
    if candidate.resolve() == source.resolve():
        return True
    if candidate.exists() and source.exists():
        try:
            return os.path.samefile(candidate, source)
        except OSError:
            return False
    return False


def extract_clips(
    video_path: Path,
    highlights: list[dict],
    output_dir: Path,
    shorts: bool = False,
    srt_paths: list[Path] | None = None,
    font_config: "FontConfig | None" = None,
    crop_x: str = "center",
    shorts_mode: str = "pad",
    shorts_title: bool = True,
    karaoke: bool = False,
    ass_paths: list[Path] | None = None,
    shorts_blur_strength=_DEFAULT_SHORTS_BLUR_STRENGTH,
    shorts_title_position: str = "top",
    vfx_options: VfxOptions | None = None,
    prepared_vfx_assets: Sequence[UserMediaAsset] | None = None,
) -> list[Path]:
    """Extract all highlight clips."""
    output_dir.mkdir(parents=True, exist_ok=True)
    clip_paths = []
    options = vfx_options or VfxOptions()
    if not isinstance(options, VfxOptions):
        raise TypeError("vfx_options must be a VfxOptions")
    if prepared_vfx_assets is not None:
        if any(not isinstance(asset, UserMediaAsset) for asset in prepared_vfx_assets):
            raise TypeError("prepared_vfx_assets must contain UserMediaAsset values")
        vfx_assets = tuple(prepared_vfx_assets)
    elif options.applies_to(shorts=shorts):
        vfx_assets = prepare_vfx_assets(options)
    else:
        vfx_assets = ()
    planned_paths: list[Path] = []
    used_names: set[str] = set()
    for h in highlights:
        range_str = format_time_range(h["start_sec"], h["end_sec"])
        clip_name = _build_clip_filename(range_str, h.get("title", ""), shorts)
        collision_index = 2
        while clip_name.casefold() in used_names:
            clip_name = _build_clip_filename(
                range_str,
                h.get("title", ""),
                shorts,
                asset_suffix=f"_dup{collision_index:02d}",
            )
            collision_index += 1
        used_names.add(clip_name.casefold())
        planned_paths.append(output_dir / clip_name)

    effect_plans = [
        resolve_clip_effect_plan(
            options,
            h,
            float(h["end_sec"]) - float(h["start_sec"]),
            vfx_assets,
            shorts=shorts,
        )
        for h in highlights
    ]
    validate_effects_manifest_target(output_dir, effect_plans)
    # Removing VFX is a batch transition too. Protect the previous clip set
    # while its owned manifest is still present, even when every new plan is
    # disabled, so a mid-batch failure cannot mix old and new generations.
    needs_effect_transaction = any(plan.enabled for plan in effect_plans) or (
        has_owned_effects_manifest(output_dir)
    )
    transaction = (
        _VfxBatchTransaction(output_dir, planned_paths)
        if needs_effect_transaction
        else None
    )
    if transaction is not None:
        transaction.begin()
    try:
        for i, (h, clip_path, effect_plan) in enumerate(
            zip(highlights, planned_paths, effect_plans),
            1,
        ):
            srt_path = (
                srt_paths[i - 1]
                if srt_paths and i - 1 < len(srt_paths)
                else None
            )
            ass_path = (
                ass_paths[i - 1]
                if ass_paths and i - 1 < len(ass_paths)
                else None
            )

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
                shorts_blur_strength=shorts_blur_strength,
                shorts_title_position=shorts_title_position,
                effect_plan=effect_plan if effect_plan.enabled else None,
            )
            clip_paths.append(clip_path)

        write_effects_manifest(
            output_dir,
            clip_paths,
            effect_plans,
            options=options,
            shorts=shorts,
        )
    except BaseException:
        if transaction is not None:
            transaction.rollback()
        raise
    if transaction is not None:
        transaction.complete()
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
    for expected in ["FontName=Noto Sans JP", "FontSize=14.4",
                     "PrimaryColour=&HFFFFFF&", "OutlineColour=&H000000&",
                     "Outline=0.45", "Alignment=2", "MarginV=9"]:
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
        "drawbox=x=32:y=116:w=iw-64", "font='Noto Sans JP'",
        "text='A\\:B\\'s 50\\% C\\\\D", "fontcolor=white",
        "x=(w-text_w)/2", "enable='lt(t\\,4)'",
    ]:
        assert expected in title_f, f"missing: {expected} in {title_f}"
    assert title_f.count("drawtext=") == 2, f"title should wrap into filters: {title_f}"
    assert "fontsize=80" not in title_f, f"long title should auto-fit: {title_f}"
    assert "\n" not in title_f, f"newline must not reach drawtext: {title_f}"
    assert r"\n" not in title_f, f"literal newline escape must not render: {title_f}"

    thumb_f = _build_thumbnail_drawtext(title_text, fc)
    assert thumb_f.startswith("drawbox="), f"thumbnail title band missing: {thumb_f}"
    assert "enable=" not in thumb_f, f"thumbnail drawtext must not use enable: {thumb_f}"
    for expected in [
        "font='Noto Sans JP'", "text='A\\:B\\'s 50\\% C\\\\D",
        "fontcolor=white", "x=(w-text_w)/2",
    ]:
        assert expected in thumb_f, f"missing thumbnail part: {expected} in {thumb_f}"
    short_title = "タイトル"
    short_video_f = _build_title_drawtext(short_title, fc)
    short_thumb_f = _build_thumbnail_drawtext(short_title, fc)
    assert short_video_f == f"{short_thumb_f}:enable='lt(t\\,4)'", "single-line style diverged"
    assert _build_thumbnail_drawtext("   \n", fc) == "", "empty thumbnail title should skip drawtext"

    print("clipper.py self-test: all assertions passed")
