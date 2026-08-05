"""Regression tests for highlight titles in generated clip filenames."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import clipper


def _capture_extract_paths(monkeypatch):
    captured = []

    def fake_extract_clip(_video_path, output_path, *_args, **_kwargs):
        captured.append(output_path)
        return output_path

    monkeypatch.setattr(clipper, "extract_clip", fake_extract_clip)
    return captured


@pytest.mark.parametrize(
    ("shorts", "expected_name"),
    [
        (False, "00h01m05s-00h01m35s_最高の逆転シーン.mp4"),
        (True, "00h01m05s-00h01m35s_最高の逆転シーン_short.mp4"),
    ],
)
def test_extract_clips_appends_title_after_time_range(
    tmp_path, monkeypatch, shorts, expected_name
):
    captured = _capture_extract_paths(monkeypatch)
    highlights = [{
        "start_sec": 65,
        "end_sec": 95,
        "title": "最高の逆転シーン",
    }]

    paths = clipper.extract_clips(
        Path("source.mp4"), highlights, tmp_path, shorts=shorts
    )

    assert paths == captured
    assert paths[0].name == expected_name


def test_extract_clips_sanitizes_title_for_windows_filename(tmp_path, monkeypatch):
    _capture_extract_paths(monkeypatch)
    highlights = [{
        "start_sec": 0,
        "end_sec": 30,
        "title": '勝利: 3/2? "すごい" | ラスト*\n場面.',
    }]

    paths = clipper.extract_clips(Path("source.mp4"), highlights, tmp_path)

    assert paths[0].name == "00h00m00s-00h00m30s_勝利_3_2_すごい_ラスト_場面.mp4"


def test_extract_clips_keeps_legacy_name_when_title_is_blank(tmp_path, monkeypatch):
    _capture_extract_paths(monkeypatch)
    highlights = [{"start_sec": 0, "end_sec": 30, "title": " \n "}]

    paths = clipper.extract_clips(Path("source.mp4"), highlights, tmp_path)

    assert paths[0].name == "00h00m00s-00h00m30s.mp4"


def test_extract_clips_retains_milliseconds_to_avoid_range_collision(
    tmp_path, monkeypatch
):
    _capture_extract_paths(monkeypatch)
    highlights = [
        {"start_sec": 1.1, "end_sec": 2.1, "title": "same"},
        {"start_sec": 1.9, "end_sec": 2.9, "title": "same"},
    ]

    paths = clipper.extract_clips(Path("source.mp4"), highlights, tmp_path)

    assert paths[0].name.startswith("00h00m01s100ms-00h00m02s100ms")
    assert paths[1].name.startswith("00h00m01s900ms-00h00m02s900ms")
    assert paths[0] != paths[1]


def test_extract_clips_disambiguates_identical_ranges_before_render(
    tmp_path, monkeypatch
):
    captured = _capture_extract_paths(monkeypatch)
    highlights = [
        {"start_sec": 1, "end_sec": 2, "title": "same"},
        {"start_sec": 1, "end_sec": 2, "title": "same"},
    ]

    paths = clipper.extract_clips(Path("source.mp4"), highlights, tmp_path)

    assert len({path.name.casefold() for path in paths}) == 2
    assert paths[1].name.endswith("_dup02.mp4")
    assert paths == captured


@pytest.mark.parametrize(
    ("title", "shorts"),
    [
        ("長" * 500, False),
        ("😀" * 200, True),
    ],
)
def test_extract_clips_limits_filename_to_cross_platform_component_limits(
    tmp_path, monkeypatch, title, shorts
):
    _capture_extract_paths(monkeypatch)
    highlights = [{"start_sec": 0, "end_sec": 30, "title": title}]

    paths = clipper.extract_clips(
        Path("source.mp4"), highlights, tmp_path, shorts=shorts
    )

    filename = paths[0].name
    assert len(filename.encode("utf-16-le")) // 2 <= 255
    assert len(filename.encode("utf-8")) <= 255
    assert filename.endswith("_short.mp4" if shorts else ".mp4")
    assert "\ufffd" not in filename
