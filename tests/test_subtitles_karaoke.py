"""Regression tests for ASS karaoke subtitle generation."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import clipper
import subtitles
from config import FontConfig
from transcriber import Segment, Word


def _font_config() -> FontConfig:
    return FontConfig(
        font_name="Noto Sans JP",
        font_size=96,
        font_color="#FFFFFF",
        outline_color="#000000",
        outline_width=3,
        position="bottom",
        margin_bottom=60,
    )


def _dialogue_lines(value: str) -> list[str]:
    return [line for line in value.splitlines() if line.startswith("Dialogue:")]


def _dialogue_text(line: str) -> str:
    return line.split(",", 9)[9]


def _karaoke_centiseconds(line: str) -> list[int]:
    return [int(value) for value in re.findall(r"\{\\k(\d+)\}", line)]


def test_generate_karaoke_ass_writes_style_dialogue_and_k_tokens(tmp_path):
    segments = [
        Segment(
            start=10.0,
            end=11.0,
            text="hello world",
            words=[
                Word(10.0, 10.4, " hello"),
                Word(10.4, 11.0, " world"),
            ],
        ),
    ]

    out = subtitles.generate_karaoke_ass(segments, 10.0, 11.0, tmp_path / "clip.ass", _font_config())
    value = out.read_text(encoding="utf-8")
    dialogue = _dialogue_lines(value)[0]

    assert "[V4+ Styles]" in value
    assert "SecondaryColour" in value
    assert "Style: Default,Noto Sans JP,96" in value
    assert "&H777777&" in value
    assert dialogue.startswith("Dialogue: 0,0:00:00.00,0:00:01.00,Default")
    assert r"{\k" in dialogue
    assert sum(_karaoke_centiseconds(dialogue)) == round((11.0 - 10.0) * 100)


def test_ass_time_formats_centiseconds():
    assert subtitles._ass_time(0.0) == "0:00:00.00"
    assert subtitles._ass_time(3661.5) == "1:01:01.50"


def test_clip_relative_rebasing_range_filtering_and_boundary_clamp(tmp_path):
    segments = [
        Segment(
            start=9.5,
            end=11.2,
            text="range",
            words=[
                Word(9.6, 9.9, " before"),
                Word(9.9, 10.2, " edge"),
                Word(10.2, 10.5, " next"),
                Word(11.0, 11.2, " after"),
            ],
        ),
        Segment(
            start=10.5,
            end=10.8,
            text="later",
            words=[Word(10.5, 10.8, " later")],
        ),
    ]

    out = subtitles.generate_karaoke_ass(segments, 10.0, 11.0, tmp_path / "clip.ass", _font_config())
    lines = _dialogue_lines(out.read_text(encoding="utf-8"))

    assert lines[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.50,Default")
    assert r"{\k20}edge" in lines[0]
    assert r"{\k30}next" in lines[0]
    assert "before" not in lines[0]
    assert "after" not in lines[0]
    assert lines[1].startswith("Dialogue: 0,0:00:00.50,0:00:00.80,Default")


def test_escape_ass_text_neutralizes_override_syntax():
    escaped = subtitles._escape_ass_text(r"a{b}\N")

    assert "a" in escaped
    assert "b" in escaped
    assert "N" in escaped
    assert "{" not in escaped
    assert "}" not in escaped
    assert "\\" not in escaped


def test_empty_words_fallback_emits_static_dialogue(tmp_path):
    segments = [Segment(start=5.0, end=6.0, text="full {text}", words=[])]

    out = subtitles.generate_karaoke_ass(segments, 5.0, 6.0, tmp_path / "clip.ass", _font_config())
    line = _dialogue_lines(out.read_text(encoding="utf-8"))[0]
    text = _dialogue_text(line)

    assert line.startswith("Dialogue: 0,0:00:00.00,0:00:01.00,Default")
    assert r"{\k" not in text
    assert "full" in text
    assert "text" in text
    assert "{" not in text
    assert "}" not in text


def test_japanese_tokens_latin_leading_space_strip_and_wrap(tmp_path):
    chars = list("あいうえおかきくけこさしすせそ")
    words = [
        Word(i / 10, (i + 1) / 10, char)
        for i, char in enumerate(chars)
    ]
    words.append(Word(1.5, 1.6, " hello"))
    segments = [Segment(start=0.0, end=1.6, text="".join(chars), words=words)]

    out = subtitles.generate_karaoke_ass(segments, 0.0, 2.0, tmp_path / "clip.ass", _font_config())
    text = _dialogue_text(_dialogue_lines(out.read_text(encoding="utf-8"))[0])

    assert r"{\k10}あ{\k10}い" in text
    assert r"\N" in text
    assert " hello" not in text
    assert r"{\k10}hello" in text


def test_build_ass_subtitles_filter_uses_authored_ass_without_force_style():
    filt = clipper._build_ass_subtitles_filter(Path("C:/Users/x/a'b.ass"))

    assert filt.startswith("subtitles='")
    assert "C\\:/Users/x/a\\'b.ass" in filt
    assert "force_style" not in filt
