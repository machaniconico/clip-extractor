"""Regression tests for thumbnail candidate helpers in clipper.py."""

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

clipper = pytest.importorskip("clipper")


def _font_config(font_name: str = "Noto Sans JP"):
    return type("FontConfig", (), {"font_name": font_name})()


@pytest.mark.parametrize(
    ("start_sec", "end_sec", "expected"),
    [
        (10, 30, 20.0),
        (0, 1, 0.5),
        (12.5, 13.5, 13.0),
        (-5.0, 5.0, 0.0),
    ],
)
def test_select_thumbnail_timestamp_midpoint(start_sec, end_sec, expected):
    assert clipper._select_thumbnail_timestamp(start_sec, end_sec) == expected
    assert clipper._select_thumbnail_timestamp(start_sec, end_sec, "midpoint") == expected


def test_thumbnail_drawtext_has_no_enable_clause(monkeypatch):
    monkeypatch.setattr(clipper, "_resolve_title_fontfile", lambda font_name: None)

    f = clipper._build_thumbnail_drawtext("A:B's 50% C\\D", _font_config())

    assert f.startswith("drawtext=")
    assert "font='Noto Sans JP'" in f
    assert "text='A\\:B\\'s 50\\% C\\\\D'" in f
    assert "fontsize=80" in f
    assert "fontcolor=white" in f
    assert "box=1" in f
    assert "boxcolor=black@0.5" in f
    assert "boxborderw=24" in f
    assert "x=(w-text_w)/2" in f
    assert "y=140" in f
    assert "enable=" not in f


def test_thumbnail_drawtext_shares_styling_with_title(monkeypatch):
    monkeypatch.setattr(clipper, "_resolve_title_fontfile", lambda font_name: None)
    font_config = _font_config()
    title = "タイトル"

    parts = clipper._title_drawtext_parts(title, font_config)
    title_filter = clipper._build_title_drawtext(title, font_config)
    thumbnail_filter = clipper._build_thumbnail_drawtext(title, font_config)

    assert thumbnail_filter == "drawtext=" + ":".join(parts)
    assert title_filter == f"{thumbnail_filter}:enable='lt(t\\,4)'"


def test_thumbnail_title_wraps_with_real_newline(monkeypatch):
    monkeypatch.setattr(clipper, "_resolve_title_fontfile", lambda font_name: None)
    title = "あいうえおかきくけこさしすせそたちつてと"

    f = clipper._build_thumbnail_drawtext(title, _font_config())

    assert "\n" in f
    assert r"\n" not in f


def test_thumbnail_drawtext_uses_fontfile_fallback(monkeypatch):
    monkeypatch.setattr(clipper, "_resolve_title_fontfile", lambda font_name: "/tmp/NotoSansJP.ttf")

    f = clipper._build_thumbnail_drawtext("タイトル", _font_config("Missing Japanese Font"))

    assert "font='Missing Japanese Font'" in f
    assert "fontfile='/tmp/NotoSansJP.ttf'" in f


@pytest.mark.parametrize("title", ["", "   ", " \n\t "])
def test_thumbnail_empty_title_returns_empty_drawtext(title):
    assert clipper._build_thumbnail_drawtext(title, _font_config()) == ""


def test_scene_strategy_falls_back_to_midpoint(monkeypatch, caplog):
    monkeypatch.setattr(
        clipper,
        "_detect_scene_thumbnail_timestamp",
        lambda video_path, start_sec, end_sec: None,
    )
    caplog.set_level(logging.WARNING, logger=clipper.logger.name)

    timestamp = clipper._select_thumbnail_timestamp(10, 30, "scene")

    assert timestamp == 20.0
    assert "falling back to midpoint" in caplog.text
