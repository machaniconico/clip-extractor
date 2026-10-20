"""Compact LLM transcript format; transcript.txt keeps the full format."""

from transcriber import Segment, segments_to_llm_text, segments_to_text


def _seg(start, end, text):
    return Segment(start=start, end=end, text=text)


def test_llm_text_uses_unpadded_hours_and_floored_seconds():
    text = segments_to_llm_text(
        [
            _seg(106.22, 106.92, "こんにちは"),
            _seg(0.99, 1.5, "最初"),
            _seg(5 * 3600 + 8 * 60 + 47.9, 5 * 3600 + 9 * 60, "終わり"),
        ]
    )
    assert text.split("\n") == [
        "[0:01:46] こんにちは",
        "[0:00:00] 最初",
        "[5:08:47] 終わり",
    ]


def test_llm_text_drops_only_consecutive_duplicates():
    text = segments_to_llm_text(
        [
            _seg(1, 2, "ありがとう"),
            _seg(2, 3, " ありがとう "),
            _seg(3, 4, "次"),
            _seg(4, 5, "ありがとう"),
        ]
    )
    assert text.split("\n") == [
        "[0:00:01] ありがとう",
        "[0:00:03] 次",
        "[0:00:04] ありがとう",
    ]


def test_llm_text_is_empty_for_no_segments():
    assert segments_to_llm_text([]) == ""


def test_full_transcript_format_is_unchanged():
    assert segments_to_text([_seg(106.22, 106.92, "x"), _seg(1, 2, "x")]) == (
        "[00:01:46.220 -> 00:01:46.920] x\n[00:00:01.000 -> 00:00:02.000] x"
    )


def test_parse_round_trip_and_malformed_lines():
    from transcriber import parse_transcript_text

    segs = [_seg(106.22, 106.92, "こんばんは"), _seg(3600.5, 3601, "[注] a -> b")]
    parsed = parse_transcript_text(segments_to_text(segs))
    assert [(s.start, s.end, s.text) for s in parsed] == [
        (106.22, 106.92, "こんばんは"),
        (3600.5, 3601.0, "[注] a -> b"),
    ]
    noisy = "\n\ngarbage\n" + segments_to_text(segs) + "\n[bad -> bad] x\n"
    assert len(parse_transcript_text(noisy)) == 2


def test_parse_without_valid_lines_raises_japanese_error():
    import pytest
    from transcriber import parse_transcript_text

    with pytest.raises(ValueError, match="文字起こし"):
        parse_transcript_text("nothing here\n")
