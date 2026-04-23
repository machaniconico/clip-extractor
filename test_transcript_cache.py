"""Unit tests for transcript_cache."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

import transcript_cache
from transcriber import Segment


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    """Redirect the cache dir into a temp path for test isolation."""
    def _get_cache_dir():
        d = tmp_path / "cache"
        d.mkdir(parents=True, exist_ok=True)
        return d
    monkeypatch.setattr(transcript_cache, "get_cache_dir", _get_cache_dir)
    return tmp_path / "cache"


def test_extract_video_id_from_watch_url():
    assert transcript_cache.extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_video_id_from_shortlink():
    assert transcript_cache.extract_video_id("https://youtu.be/abcDEF12345") == "abcDEF12345"


def test_extract_video_id_from_shorts_url():
    assert transcript_cache.extract_video_id("https://www.youtube.com/shorts/abcDEF12345") == "abcDEF12345"


def test_extract_video_id_from_live_url():
    assert transcript_cache.extract_video_id("https://www.youtube.com/live/abcDEF12345") == "abcDEF12345"


def test_extract_video_id_from_watch_with_extra_query():
    url = "https://www.youtube.com/watch?list=PLxxx&v=dQw4w9WgXcQ&t=30s"
    assert transcript_cache.extract_video_id(url) == "dQw4w9WgXcQ"


def test_extract_video_id_returns_none_for_non_youtube():
    assert transcript_cache.extract_video_id("https://example.com/video") is None
    assert transcript_cache.extract_video_id("") is None
    assert transcript_cache.extract_video_id(None) is None


def test_cache_key_uses_video_id_for_youtube():
    assert transcript_cache.cache_key_for_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_cache_key_hashes_non_youtube_url():
    key = transcript_cache.cache_key_for_url("https://example.com/ep1")
    assert key.startswith("url_") and len(key) == len("url_") + 16


def test_save_and_load_cached_roundtrip(tmp_cache):
    segments = [
        Segment(start=0.0, end=2.5, text="hello"),
        Segment(start=2.5, end=5.0, text="world"),
    ]
    path = transcript_cache.save_cached("vid1", "large-v3", "ja", segments)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["key"] == "vid1"
    assert len(payload["segments"]) == 2

    loaded, hit = transcript_cache.load_cached("vid1", "large-v3", "ja")
    assert hit is True
    assert len(loaded) == 2
    assert loaded[0].start == 0.0 and loaded[0].text == "hello"
    assert loaded[1].end == 5.0


def test_cache_miss_on_different_model(tmp_cache):
    segments = [Segment(start=0.0, end=1.0, text="x")]
    transcript_cache.save_cached("vid2", "large-v3", "ja", segments)
    loaded, hit = transcript_cache.load_cached("vid2", "medium", "ja")
    assert hit is False and loaded == []


def test_cache_miss_on_different_language(tmp_cache):
    segments = [Segment(start=0.0, end=1.0, text="x")]
    transcript_cache.save_cached("vid3", "large-v3", "ja", segments)
    loaded, hit = transcript_cache.load_cached("vid3", "large-v3", "en")
    assert hit is False and loaded == []


def test_cache_miss_for_unknown_key(tmp_cache):
    loaded, hit = transcript_cache.load_cached("nonexistent", "large-v3", "ja")
    assert hit is False and loaded == []


def test_corrupt_cache_file_is_ignored(tmp_cache):
    path = transcript_cache._cache_file("bad", "large-v3", "ja")
    path.write_text("not json", encoding="utf-8")
    loaded, hit = transcript_cache.load_cached("bad", "large-v3", "ja")
    assert hit is False and loaded == []


def test_transcribe_with_cache_hit_skips_real_call(tmp_cache):
    segments_cached = [Segment(start=0.0, end=1.0, text="cached")]
    transcript_cache.save_cached("dQw4w9WgXcQ", "large-v3", "ja", segments_cached)

    with mock.patch.object(transcript_cache, "transcribe") as mock_trans:
        result = transcript_cache.transcribe_with_cache(
            video_path=Path("/nonexistent.mp4"),
            model_size="large-v3",
            language="ja",
            video_url="https://youtu.be/dQw4w9WgXcQ",
        )
    assert mock_trans.call_count == 0
    assert len(result) == 1 and result[0].text == "cached"


def test_transcribe_with_cache_miss_calls_and_saves(tmp_cache):
    fake_segments = [Segment(start=0.0, end=2.0, text="fresh")]

    with mock.patch.object(transcript_cache, "transcribe", return_value=fake_segments) as mock_trans:
        result = transcript_cache.transcribe_with_cache(
            video_path=Path("/nonexistent.mp4"),
            model_size="medium",
            language="ja",
            video_url="https://youtu.be/newvideo123",
        )
    mock_trans.assert_called_once()
    assert result == fake_segments

    # Saved to cache
    loaded, hit = transcript_cache.load_cached("newvideo123", "medium", "ja")
    assert hit is True and len(loaded) == 1 and loaded[0].text == "fresh"


def test_transcribe_with_cache_without_url_skips_cache(tmp_cache):
    fake_segments = [Segment(start=0.0, end=1.0, text="x")]
    with mock.patch.object(transcript_cache, "transcribe", return_value=fake_segments) as mock_trans:
        result = transcript_cache.transcribe_with_cache(
            video_path=Path("/foo.mp4"),
            model_size="large-v3",
            language="ja",
            video_url="",
        )
    mock_trans.assert_called_once()
    assert result == fake_segments
    # No cache files should be created
    assert list(tmp_cache.glob("*.json")) == []


def test_clear_cache_removes_all(tmp_cache):
    transcript_cache.save_cached("a", "large-v3", "ja", [Segment(0.0, 1.0, "x")])
    transcript_cache.save_cached("b", "large-v3", "ja", [Segment(0.0, 1.0, "y")])
    removed = transcript_cache.clear_cache()
    assert removed == 2
    assert list(tmp_cache.glob("*.json")) == []
