"""Unit tests for chapters.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from chapters import format_chapter_timestamp, generate_chapter_text, write_chapter_file


def test_format_mmss():
    assert format_chapter_timestamp(0) == "0:00"
    assert format_chapter_timestamp(5) == "0:05"
    assert format_chapter_timestamp(65) == "1:05"
    assert format_chapter_timestamp(600) == "10:00"
    assert format_chapter_timestamp(59.9) == "1:00"  # rounds up at .9


def test_format_hhmmss():
    assert format_chapter_timestamp(0, use_hours=True) == "0:00:00"
    assert format_chapter_timestamp(3600, use_hours=True) == "1:00:00"
    assert format_chapter_timestamp(3665, use_hours=True) == "1:01:05"
    assert format_chapter_timestamp(7325, use_hours=True) == "2:02:05"


def test_empty_highlights():
    assert generate_chapter_text([]) == ""


def test_first_line_is_zero():
    highlights = [{"start_sec": 30, "title": "First scene"}]
    text = generate_chapter_text(highlights)
    lines = text.split("\n")
    assert lines[0].startswith("0:00 "), f"expected 0:00 prefix, got: {lines[0]!r}"
    # intro + one highlight = 2 lines
    assert len(lines) == 2


def test_intro_not_duplicated_when_start_is_zero():
    highlights = [
        {"start_sec": 0, "title": "Opening"},
        {"start_sec": 60, "title": "Middle"},
    ]
    text = generate_chapter_text(highlights)
    lines = text.split("\n")
    assert lines[0] == "0:00 Opening", f"got {lines[0]!r}"
    assert lines[1] == "1:00 Middle", f"got {lines[1]!r}"
    assert len(lines) == 2, f"unexpected extra intro: {text!r}"


def test_ascending_timestamps():
    highlights = [
        {"start_sec": 10, "title": "A"},
        {"start_sec": 100, "title": "B"},
        {"start_sec": 1000, "title": "C"},
    ]
    text = generate_chapter_text(highlights)
    lines = text.split("\n")
    assert lines[0] == "0:00 イントロ"
    assert lines[1] == "0:10 A"
    assert lines[2] == "1:40 B"
    assert lines[3] == "16:40 C"


def test_use_hours_threshold_triggered_by_video_duration():
    highlights = [{"start_sec": 30, "title": "X"}]
    text = generate_chapter_text(highlights, video_duration=4000)
    lines = text.split("\n")
    assert lines[0] == "0:00:00 イントロ", f"got {lines[0]!r}"
    assert lines[1] == "0:00:30 X", f"got {lines[1]!r}"


def test_use_hours_threshold_triggered_by_highlight_start():
    highlights = [
        {"start_sec": 100, "title": "A"},
        {"start_sec": 4000, "title": "B"},
    ]
    text = generate_chapter_text(highlights, video_duration=0)
    lines = text.split("\n")
    assert lines[0] == "0:00:00 イントロ"
    assert lines[1] == "0:01:40 A"
    assert lines[2] == "1:06:40 B"


def test_missing_title_fallback():
    highlights = [{"start_sec": 5}, {"start_sec": 50}]
    text = generate_chapter_text(highlights)
    lines = text.split("\n")
    assert lines[0] == "0:00 イントロ"
    assert lines[1] == "0:05 シーン1"
    assert lines[2] == "0:50 シーン2"


def test_write_chapter_file(tmp_dir: Path | None = None):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "chapters.txt"
        highlights = [{"start_sec": 0, "title": "Opening"}]
        result = write_chapter_file(highlights, out)
        assert result == out
        assert out.exists()
        assert out.read_text(encoding="utf-8") == "0:00 Opening"


def run_all():
    test_format_mmss()
    test_format_hhmmss()
    test_empty_highlights()
    test_first_line_is_zero()
    test_intro_not_duplicated_when_start_is_zero()
    test_ascending_timestamps()
    test_use_hours_threshold_triggered_by_video_duration()
    test_use_hours_threshold_triggered_by_highlight_start()
    test_missing_title_fallback()
    test_write_chapter_file()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
