"""Unit tests for highlighter.py JSON extractor + timestamp parser."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from highlighter import _extract_json_object, _parse_timestamp


# ----- _extract_json_object -----

def test_extract_json_plain():
    assert _extract_json_object('{"highlights": []}') == {"highlights": []}


def test_extract_json_with_preamble():
    text = 'Here is the JSON:\n{"highlights": [{"title": "A"}]}'
    assert _extract_json_object(text) == {"highlights": [{"title": "A"}]}


def test_extract_json_with_postamble():
    text = '{"x": 1}\n以上が結果です'
    assert _extract_json_object(text) == {"x": 1}


def test_extract_json_code_fence_json_tag():
    text = '```json\n{"x": 1}\n```'
    assert _extract_json_object(text) == {"x": 1}


def test_extract_json_code_fence_generic():
    text = '```\n{"x": 1}\n```'
    assert _extract_json_object(text) == {"x": 1}


def test_extract_json_nested_braces():
    assert _extract_json_object('{"a": {"b": {"c": 1}}}') == {"a": {"b": {"c": 1}}}


def test_extract_json_picks_first_valid_when_multiple():
    text = 'Answer: {"x": 1} and also {"y": 2}'
    assert _extract_json_object(text) == {"x": 1}


def test_extract_json_skips_invalid_tries_next():
    # First {...} is not valid JSON; scanner must move on to the next one
    text = '{ bad } and then {"good": true}'
    assert _extract_json_object(text) == {"good": True}


def test_extract_json_handles_string_with_braces():
    text = '{"text": "she said {hello}"}'
    assert _extract_json_object(text) == {"text": "she said {hello}"}


def test_extract_json_handles_escaped_quote_in_string():
    text = r'{"text": "she said \"hi\" loudly"}'
    assert _extract_json_object(text) == {"text": 'she said "hi" loudly'}


def test_extract_json_returns_none_on_empty():
    assert _extract_json_object("") is None
    assert _extract_json_object(None) is None


def test_extract_json_returns_none_on_no_json():
    assert _extract_json_object("no json here at all") is None


def test_extract_json_realistic_gemini_response():
    """Gemini often adds ```json fence + preamble + trailing newline."""
    text = '''Sure, here are the highlights you requested:

```json
{
  "highlights": [
    {"start": "0:00:30.000", "end": "0:01:00.000", "title": "オープニング", "reason": "導入"},
    {"start": "0:05:00.000", "end": "0:05:45.000", "title": "ハイライト1", "reason": "盛り上がり"}
  ]
}
```

ご確認ください。'''
    result = _extract_json_object(text)
    assert result is not None
    assert "highlights" in result
    assert len(result["highlights"]) == 2
    assert result["highlights"][0]["title"] == "オープニング"


# ----- _parse_timestamp -----

def test_parse_timestamp_hhmmss_dot():
    assert _parse_timestamp("01:02:03.456") == 3723.456


def test_parse_timestamp_hhmmss_comma():
    # SRT-style comma separator must be normalized to dot
    assert _parse_timestamp("01:02:03,456") == 3723.456


def test_parse_timestamp_mmss():
    assert _parse_timestamp("02:30") == 150.0


def test_parse_timestamp_float_only():
    assert _parse_timestamp("42.5") == 42.5


def test_parse_timestamp_zero():
    assert _parse_timestamp("0:00:00.000") == 0.0


def test_parse_timestamp_whitespace_stripped():
    assert _parse_timestamp("  01:30  ") == 90.0


def run_all():
    test_extract_json_plain()
    test_extract_json_with_preamble()
    test_extract_json_with_postamble()
    test_extract_json_code_fence_json_tag()
    test_extract_json_code_fence_generic()
    test_extract_json_nested_braces()
    test_extract_json_picks_first_valid_when_multiple()
    test_extract_json_skips_invalid_tries_next()
    test_extract_json_handles_string_with_braces()
    test_extract_json_handles_escaped_quote_in_string()
    test_extract_json_returns_none_on_empty()
    test_extract_json_returns_none_on_no_json()
    test_extract_json_realistic_gemini_response()
    test_parse_timestamp_hhmmss_dot()
    test_parse_timestamp_hhmmss_comma()
    test_parse_timestamp_mmss()
    test_parse_timestamp_float_only()
    test_parse_timestamp_zero()
    test_parse_timestamp_whitespace_stripped()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
