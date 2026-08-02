"""Regression tests for SRT sidecars used by clips and Shorts."""

from subtitles import generate_all_short_title_srts, generate_all_srts
from transcriber import Segment


def test_srt_sidecar_name_matches_the_generated_clip_name(tmp_path):
    highlights = [{
        "start_sec": 10.0,
        "end_sec": 20.0,
        "title": "配信/テスト",
    }]
    segments = [Segment(start=10.0, end=12.0, text="字幕です")]

    paths = generate_all_srts(segments, highlights, tmp_path)

    assert [path.name for path in paths] == [
        "00h00m10s-00h00m20s_配信_テスト.srt",
    ]
    assert "字幕です" in paths[0].read_text(encoding="utf-8")


def test_shorts_srt_sidecar_name_matches_the_generated_short_name(tmp_path):
    highlights = [{
        "start_sec": 10.0,
        "end_sec": 20.0,
        "title": "配信テスト",
    }]
    segments = [Segment(start=10.0, end=12.0, text="字幕です")]

    paths = generate_all_srts(segments, highlights, tmp_path, shorts=True)

    assert [path.name for path in paths] == [
        "00h00m10s-00h00m20s_配信テスト_short_archive.srt",
    ]


def test_each_short_gets_separate_archive_and_title_srt_files(tmp_path):
    highlights = [{
        "start_sec": 10.0,
        "end_sec": 20.0,
        "title": "配信テスト",
    }]
    segments = [Segment(start=10.0, end=12.0, text="アーカイブ字幕")]

    archive_paths = generate_all_srts(segments, highlights, tmp_path, shorts=True)
    title_paths = generate_all_short_title_srts(highlights, tmp_path)

    assert {path.name for path in archive_paths + title_paths} == {
        "00h00m10s-00h00m20s_配信テスト_short_archive.srt",
        "00h00m10s-00h00m20s_配信テスト_short_title.srt",
    }
    assert "アーカイブ字幕" in archive_paths[0].read_text(encoding="utf-8")
    title_text = title_paths[0].read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:04,000" in title_text
    assert "配信テスト" in title_text


def test_same_time_range_with_different_titles_does_not_overwrite_srt(tmp_path):
    highlights = [
        {"start_sec": 10.1, "end_sec": 20.1, "title": "前半"},
        {"start_sec": 10.2, "end_sec": 20.2, "title": "後半"},
    ]
    segments = [Segment(start=10.0, end=12.0, text="字幕です")]

    paths = generate_all_srts(segments, highlights, tmp_path)

    assert paths[0] != paths[1]
    assert all(path.exists() for path in paths)
