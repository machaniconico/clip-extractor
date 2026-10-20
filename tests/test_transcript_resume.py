"""Reusing a saved transcript so a failed LLM step does not re-run Whisper."""

import json

import pytest

import web_app
from transcriber import (
    Segment,
    Word,
    find_reusable_transcript,
    load_transcript_file,
    segments_to_text,
    write_transcript_segments,
)

SEGMENTS = [
    Segment(start=0.0, end=30.0, text="saved transcript", words=[Word(0.0, 1.0, "saved")]),
    Segment(start=30.0, end=55.0, text="second"),
]


@pytest.fixture
def env(monkeypatch, tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        web_app,
        "get_video_info",
        lambda _p: {"width": 1280, "height": 720, "fps": 30.0, "duration": 60.0},
    )

    def no_transcribe(*_a):
        pytest.fail("Whisper must not run")

    monkeypatch.setattr(web_app, "transcribe", no_transcribe)

    def fail_detection(*_a, **_k):
        raise RuntimeError("stop after transcript")

    monkeypatch.setattr(web_app, "detect_highlights", fail_detection)
    return tmp_path, source


def _detect(tmp_path, source, resume=""):
    return web_app.detect_phase(
        input_url="",
        input_file=str(source),
        enable_clips=True,
        clip_prompt="",
        enable_chapters=False,
        chapter_prompt="",
        num_clips=1,
        ai_provider="gemini",
        ai_model="m",
        api_key="k",
        min_duration=30,
        max_duration=60,
        whisper_model="tiny",
        language="ja",
        audio_fusion=False,
        audio_alpha=0.35,
        output_base_dir=str(tmp_path / "out"),
        resume_transcript=resume,
        progress=lambda *_a, **_k: None,
    )


def test_json_round_trip_keeps_words_and_fingerprint(tmp_path):
    source = tmp_path / "v.mp4"
    source.write_bytes(b"abc")
    path = tmp_path / "transcript_segments.json"
    write_transcript_segments(path, SEGMENTS, source, "ja", "tiny")
    assert not (tmp_path / "transcript_segments.json.tmp").exists()
    segs, src = load_transcript_file(path)
    assert segs[0].words[0].text == "saved"
    assert src["size"] == 3 and src["name"] == "v.mp4"


def test_auto_reuse_skips_whisper_and_rewrites_transcripts(env):
    tmp_path, source = env
    old = tmp_path / "out" / "output_old"
    old.mkdir(parents=True)
    write_transcript_segments(old / "transcript_segments.json", SEGMENTS, source, "ja", "tiny")

    session, status, _panel = _detect(tmp_path, source)

    assert session["llm_failed"] is True
    assert "同じ録画の文字起こしを再利用" in status
    new_dirs = [d for d in (tmp_path / "out").glob("output_*") if d != old]
    assert len(new_dirs) == 1
    assert (new_dirs[0] / "transcript.txt").read_text(encoding="utf-8") == segments_to_text(SEGMENTS)
    assert (new_dirs[0] / "transcript_segments.json").is_file()


def test_auto_reuse_ignores_other_input(env):
    tmp_path, source = env
    old = tmp_path / "out" / "output_old"
    old.mkdir(parents=True)
    write_transcript_segments(old / "transcript_segments.json", SEGMENTS, source, "ja", "tiny")
    source.write_bytes(b"different size")
    with pytest.raises(pytest.fail.Exception):
        _detect(tmp_path, source)


def test_manual_resume_json_and_fingerprint_mismatch(env):
    tmp_path, source = env
    good = tmp_path / "good.json"
    write_transcript_segments(good, SEGMENTS, source, "ja", "tiny")
    _s, status, _p = _detect(tmp_path, source, str(good))
    assert "既存の文字起こしを再利用" in status

    bad = tmp_path / "bad.json"
    data = json.loads(good.read_text(encoding="utf-8"))
    data["source"]["size"] += 1
    bad.write_text(json.dumps(data), encoding="utf-8")
    _s, status, _p = _detect(tmp_path, source, str(bad))
    assert "別の動画" in status


def test_manual_resume_txt_warns_and_checks_duration(env):
    tmp_path, source = env
    ok = tmp_path / "transcript.txt"
    ok.write_text(segments_to_text(SEGMENTS), encoding="utf-8")
    _s, status, _p = _detect(tmp_path, source, str(ok))
    assert "確認できません" in status
    assert "既存の文字起こしを再利用" in status

    long = tmp_path / "long.txt"
    long.write_text(
        segments_to_text([Segment(start=0, end=100, text="x")]), encoding="utf-8"
    )
    _s, status, _p = _detect(tmp_path, source, str(long))
    assert "動画の長さ" in status

    _s, status, _p = _detect(tmp_path, source, str(tmp_path / "missing.txt"))
    assert "見つかりません" in status


def test_auto_reuse_requires_same_model_and_language(env):
    tmp_path, source = env
    old = tmp_path / "out" / "output_old"
    old.mkdir(parents=True)
    write_transcript_segments(old / "transcript_segments.json", SEGMENTS, source, "en", "base")
    # _detect uses whisper_model="tiny", language="ja": neither matches
    with pytest.raises(pytest.fail.Exception):
        _detect(tmp_path, source)
    assert find_reusable_transcript(tmp_path / "out", source, "base", "en") is not None
    assert find_reusable_transcript(tmp_path / "out", source, "base", "auto") is not None
    assert find_reusable_transcript(tmp_path / "out", source, "base", None) is not None
    assert find_reusable_transcript(tmp_path / "out", source, "base", "ja") is None
    assert find_reusable_transcript(tmp_path / "out", source, "tiny", "en") is None


def test_reuse_preserves_original_metadata(env):
    tmp_path, source = env
    old = tmp_path / "out" / "output_old"
    old.mkdir(parents=True)
    write_transcript_segments(old / "transcript_segments.json", SEGMENTS, source, "ja", "tiny")
    manual = tmp_path / "manual.json"
    write_transcript_segments(manual, SEGMENTS, source, "en", "large-v3")

    _detect(tmp_path, source)
    new = [d for d in (tmp_path / "out").glob("output_*") if d != old][0]
    data = json.loads((new / "transcript_segments.json").read_text(encoding="utf-8"))
    assert (data["whisper_model"], data["language"]) == ("tiny", "ja")

    _detect(tmp_path, source, str(manual))
    newest = max(
        (d for d in (tmp_path / "out").glob("output_*") if d not in (old, new)),
        key=lambda d: d.stat().st_mtime,
    )
    data = json.loads((newest / "transcript_segments.json").read_text(encoding="utf-8"))
    assert (data["whisper_model"], data["language"]) == ("large-v3", "en")


def test_manual_resume_json_needs_name_or_mtime_match(env):
    tmp_path, source = env
    good = tmp_path / "good.json"
    write_transcript_segments(good, SEGMENTS, source, "ja", "tiny")
    data = json.loads(good.read_text(encoding="utf-8"))

    data["source"]["name"] = "other.mp4"  # mtime still matches
    one = tmp_path / "one.json"
    one.write_text(json.dumps(data), encoding="utf-8")
    _s, status, _p = _detect(tmp_path, source, str(one))
    assert "別の動画" not in status

    data["source"]["mtime"] += 100  # now neither matches
    two = tmp_path / "two.json"
    two.write_text(json.dumps(data), encoding="utf-8")
    _s, status, _p = _detect(tmp_path, source, str(two))
    assert "別の動画" in status and "transcript.txt" in status


def test_manual_resume_txt_logs_user_specified(env):
    tmp_path, source = env
    ok = tmp_path / "transcript.txt"
    ok.write_text(segments_to_text(SEGMENTS), encoding="utf-8")
    _s, status, _p = _detect(tmp_path, source, str(ok))
    assert "ユーザー指定" in status
