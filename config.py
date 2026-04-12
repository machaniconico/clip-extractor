"""Configuration handling for clip-extractor."""

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_FONT_CONFIG = {
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
    font_size: int = 48
    font_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 3
    position: str = "bottom"
    margin_bottom: int = 60

    @classmethod
    def from_file(cls, path: Path) -> "FontConfig":
        with open(path) as f:
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
    clip_min_duration: int = 30
    clip_max_duration: int = 90
    output_mode: str = "combined"  # "combined" or "individual"
    shorts: bool = False
    highlight_prompt: str = ""
    font_config: FontConfig = field(default_factory=FontConfig)
    whisper_model: str = "large-v3"
    language: str = "ja"
