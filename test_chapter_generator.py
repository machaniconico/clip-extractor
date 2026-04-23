"""Unit tests for chapter_generator."""

from __future__ import annotations

import json

import pytest

from chapter_generator import (
    Chapter,
    Moment,
    enforce_youtube_chapter_rules,
    format_chapters_for_youtube,
    format_moments_for_display,
    generate_chapters,
    search_moments,
)


def _fake_llm(response: str):
    def caller(system_prompt, user_prompt):
        return response
    return caller


def _fake_llm_chapters(rows):
    payload = json.dumps({"chapters": rows}, ensure_ascii=False)
    return _fake_llm(payload)


def _fake_llm_moments(rows):
    payload = json.dumps({"moments": rows}, ensure_ascii=False)
    return _fake_llm(payload)


def test_generate_chapters_parses_json_response():
    llm = _fake_llm_chapters([
        {"start": "00:00:00", "title": "オープニング"},
        {"start": "00:05:00", "title": "ボス戦"},
        {"start": "00:12:30", "title": "雑談"},
    ])
    chapters = generate_chapters(
        "dummy transcript",
        video_duration=900.0,
        llm_caller=llm,
    )
    assert len(chapters) == 3
    assert chapters[0].start_sec == 0.0
    assert chapters[0].title == "オープニング"
    assert chapters[1].start_sec == 300.0
    assert chapters[2].title == "雑談"


def test_generate_chapters_forces_first_chapter_to_zero():
    llm = _fake_llm_chapters([
        {"start": "00:00:45", "title": "トーク開始"},
        {"start": "00:05:00", "title": "企画"},
        {"start": "00:15:00", "title": "締め"},
    ])
    chapters = generate_chapters("t", video_duration=1800.0, llm_caller=llm)
    assert chapters[0].start_sec == 0.0


def test_generate_chapters_fills_to_minimum_three():
    llm = _fake_llm_chapters([
        {"start": "00:00:00", "title": "A"},
    ])
    chapters = generate_chapters("t", video_duration=1200.0, llm_caller=llm)
    assert len(chapters) >= 3


def test_generate_chapters_on_empty_response_yields_defaults():
    llm = _fake_llm_chapters([])
    chapters = generate_chapters("t", video_duration=1200.0, llm_caller=llm)
    assert len(chapters) >= 3
    assert chapters[0].start_sec == 0.0


def test_generate_chapters_dedupes_close_entries():
    llm = _fake_llm_chapters([
        {"start": "00:00:00", "title": "A"},
        {"start": "00:00:05", "title": "B (too close)"},  # dropped
        {"start": "00:00:30", "title": "C"},
        {"start": "00:02:00", "title": "D"},
    ])
    chapters = generate_chapters("t", video_duration=600.0, llm_caller=llm)
    starts = [c.start_sec for c in chapters]
    assert starts == sorted(starts)
    for a, b in zip(starts, starts[1:]):
        assert b - a >= 10.0


def test_generate_chapters_invalid_json_raises():
    def bad_llm(s, u):
        return "this is not json at all"
    with pytest.raises(ValueError):
        generate_chapters("t", 600.0, llm_caller=bad_llm)


def test_format_chapters_for_youtube_compact_under_hour():
    chapters = [
        Chapter(0.0, "A"),
        Chapter(65.0, "B"),
        Chapter(3675.0, "C"),
    ]
    out = format_chapters_for_youtube(chapters).splitlines()
    assert out[0] == "00:00 A"
    assert out[1] == "01:05 B"
    assert out[2] == "1:01:15 C"


def test_search_moments_returns_multiple_hits():
    llm = _fake_llm_moments([
        {"start": "00:01:30", "end": "00:02:00", "title": "敗北1", "excerpt": "やられた"},
        {"start": "00:15:00", "end": "00:15:45", "title": "敗北2", "excerpt": "また負け"},
    ])
    moments = search_moments("t", "敗北シーン", llm_caller=llm)
    assert len(moments) == 2
    assert moments[0].start_sec == 90.0
    assert moments[0].end_sec == 120.0
    assert moments[0].title == "敗北1"
    assert moments[1].excerpt == "また負け"


def test_search_moments_returns_empty_list_on_no_hits():
    llm = _fake_llm_moments([])
    moments = search_moments("t", "nothing matches", llm_caller=llm)
    assert moments == []


def test_search_moments_sorts_by_start():
    llm = _fake_llm_moments([
        {"start": "00:10:00", "end": "00:10:30", "title": "B", "excerpt": ""},
        {"start": "00:01:00", "end": "00:01:30", "title": "A", "excerpt": ""},
    ])
    moments = search_moments("t", "x", llm_caller=llm)
    assert moments[0].title == "A"
    assert moments[1].title == "B"


def test_search_moments_fixes_invalid_end():
    llm = _fake_llm_moments([
        {"start": "00:01:00", "end": "00:00:30", "title": "weird", "excerpt": ""},  # end < start
    ])
    moments = search_moments("t", "x", llm_caller=llm)
    assert moments[0].end_sec > moments[0].start_sec


def test_search_moments_empty_prompt_raises():
    with pytest.raises(ValueError):
        search_moments("t", "   ", llm_caller=_fake_llm_moments([]))


def test_format_moments_for_display_with_excerpt():
    moments = [
        Moment(start_sec=90.0, end_sec=120.0, title="A", excerpt="セリフA"),
        Moment(start_sec=3600.0, end_sec=3630.0, title="B", excerpt=""),
    ]
    out = format_moments_for_display(moments)
    assert "[01:30 - 02:00] A" in out
    assert " └ セリフA" in out
    assert "[1:00:00 - 1:00:30] B" in out


def test_format_moments_for_display_empty():
    assert format_moments_for_display([]) == "(該当なし)"


def test_enforce_youtube_rules_inserts_zero_chapter():
    chapters = [Chapter(120.0, "A"), Chapter(300.0, "B"), Chapter(600.0, "C")]
    fixed = enforce_youtube_chapter_rules(chapters, 1000.0)
    assert fixed[0].start_sec == 0.0


def test_enforce_youtube_rules_min_three_chapters():
    chapters = [Chapter(0.0, "Solo")]
    fixed = enforce_youtube_chapter_rules(chapters, 600.0)
    assert len(fixed) >= 3
