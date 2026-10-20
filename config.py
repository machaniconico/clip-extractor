"""Configuration handling for clip-extractor."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("clip-extractor.config")

_TRUE_SETTING_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_SETTING_STRINGS = frozenset({"false", "0", "no", "off", ""})


def setting_bool(value, default: bool = False, key: str = "") -> bool:
    """Coerce a boolean read from a saved settings file.

    ``bool("false")`` is True, so hand-edited or legacy files must not go through
    it.  bool is returned as-is, numbers are truthy when non-zero (legacy
    behaviour), and strings are matched case-insensitively.  ``None`` (explicit
    or missing) and unrecognised values yield ``default``; the latter also logs a
    warning naming the setting but never echoing its value.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_SETTING_STRINGS:
            return True
        if text in _FALSE_SETTING_STRINGS:
            return False
    logger.warning(
        "設定値を真偽値として解釈できないため既定値を使用します: %s", key or "(unknown)"
    )
    return default


DEFAULT_FONT_CONFIG = {
    # Bundled Noto Sans JP Bold (700): readable, OFL-licensed Shorts captions.
    "font_name": "Noto Sans JP",
    "font_size": 96,
    "font_color": "#FFFFFF",
    "outline_color": "#000000",
    "outline_width": 3,
    "position": "bottom",
    "margin_bottom": 60,
}


@dataclass
class FontConfig:
    font_name: str = "Noto Sans JP"
    # 96 is tuned for 1080×1920 Shorts; matches default_settings.json and
    # DEFAULT_FONT_CONFIG (the previous 48 default was a stale leftover).
    font_size: int = 96
    font_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 3
    position: str = "bottom"
    margin_bottom: int = 60

    @classmethod
    def from_file(cls, path: Path) -> "FontConfig":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return {
            "font_name": self.font_name,
            "font_size": self.font_size,
            "font_color": self.font_color,
            "outline_color": self.outline_color,
            "outline_width": self.outline_width,
            "position": self.position,
            "margin_bottom": self.margin_bottom,
        }


@dataclass
class AppConfig:
    input_path: str = ""
    output_dir: Path = field(default_factory=lambda: Path("./output"))
    num_clips: int = 5
    clip_min_duration: int = 60
    clip_max_duration: int = 90
    output_mode: str = "combined"  # "combined" or "individual"
    shorts: bool = False
    shorts_crop: str = "center"  # "center" | "left" | "right"
    shorts_mode: str = "pad"  # "crop" | "blur" | "pad"
    shorts_blur_strength: float = 20
    shorts_title: bool = True
    shorts_title_position: str = "top"  # "top" | "bottom" | "overlay"
    generate_thumbnails: bool = False
    audio_fusion: bool = False
    audio_alpha: float = 0.35
    karaoke: bool = False
    highlight_prompt: str = ""
    font_config: FontConfig = field(default_factory=FontConfig)
    whisper_model: str = "large-v3"
    language: str = "ja"
    # --- OBS integration ---
    obs_enabled: bool = False
    obs_trigger_method: str = "websocket"   # "websocket" | "folder"
    obs_host: str = "localhost"
    obs_port: int = 4455
    obs_password: str = ""
    obs_stop_event: str = "record"          # "record" | "stream"
    obs_watch_folder: str = ""
    obs_auto_process: bool = True
