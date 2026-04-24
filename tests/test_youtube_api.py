"""Unit tests for youtube_api.py extractor + description merge."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from youtube_api import extract_video_id, _merge_description


def test_extract_id_standard_watch_url():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_id_short_url():
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42") == "dQw4w9WgXcQ"


def test_extract_id_with_extra_query_params():
    assert extract_video_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s&feature=youtu.be"
    ) == "dQw4w9WgXcQ"
    assert extract_video_id(
        "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_extract_id_shorts_and_embed():
    assert extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_id_returns_none_on_invalid():
    assert extract_video_id("") is None
    assert extract_video_id("not a url") is None
    assert extract_video_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert extract_video_id("https://youtube.com/") is None
    # 10-char id (too short) must not match
    assert extract_video_id("https://youtu.be/dQw4w9WgXc") is None


def test_merge_prepend_default():
    out = _merge_description("existing body", "0:00 イントロ\n1:23 A", "prepend")
    assert out == "0:00 イントロ\n1:23 A\n\nexisting body"


def test_merge_prepend_empty_existing():
    out = _merge_description("", "0:00 イントロ", "prepend")
    assert out == "0:00 イントロ"


def test_merge_append():
    out = _merge_description("existing", "0:00 A", "append")
    assert out == "existing\n\n0:00 A"


def test_merge_append_empty_existing():
    out = _merge_description("", "0:00 A", "append")
    assert out == "0:00 A"


def test_merge_replace():
    out = _merge_description("existing long body", "0:00 A", "replace")
    assert out == "0:00 A"


def test_merge_prepend_strips_existing_leading_blank():
    # Existing body with leading blank line should not double up after prepend
    out = _merge_description("\n\nbody", "0:00 A", "prepend")
    assert out == "0:00 A\n\nbody"


def test_merge_none_and_empty_inputs():
    assert _merge_description(None, None, "prepend") == ""
    assert _merge_description(None, "0:00 A", "prepend") == "0:00 A"
    assert _merge_description("existing", None, "prepend") == "existing"


def run_all():
    test_extract_id_standard_watch_url()
    test_extract_id_short_url()
    test_extract_id_with_extra_query_params()
    test_extract_id_shorts_and_embed()
    test_extract_id_returns_none_on_invalid()
    test_merge_prepend_default()
    test_merge_prepend_empty_existing()
    test_merge_append()
    test_merge_append_empty_existing()
    test_merge_replace()
    test_merge_prepend_strips_existing_leading_blank()
    test_merge_none_and_empty_inputs()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
