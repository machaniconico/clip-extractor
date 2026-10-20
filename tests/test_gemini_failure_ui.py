"""Temporary Gemini failures leave actionable status and no raw API response."""

import pytest

import web_app
from highlighter import GeminiQuotaError, GeminiTemporaryError
from transcriber import Segment


@pytest.mark.parametrize("legacy", [False, True])
def test_gemini_outage_returns_safe_failure_status(monkeypatch, tmp_path, legacy):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        web_app,
        "get_video_info",
        lambda _path: {"width": 1280, "height": 720, "fps": 30.0, "duration": 60.0},
    )
    monkeypatch.setattr(
        web_app,
        "transcribe",
        lambda *_args: [Segment(start=0, end=30, text="saved transcript")],
    )

    def fail_detection(*_args, **_kwargs):
        raise GeminiTemporaryError(503) from RuntimeError("private-api-response")

    def unexpected_render(*_args, **_kwargs):
        pytest.fail("A failed detection must not render clips")

    monkeypatch.setattr(web_app, "detect_highlights", fail_detection)
    monkeypatch.setattr(web_app, "extract_clips", unexpected_render)
    kwargs = dict(
        input_url="",
        input_file=str(source),
        enable_clips=True,
        clip_prompt="",
        enable_chapters=False,
        chapter_prompt="",
        num_clips=1,
        ai_provider="gemini",
        ai_model="gemini-3.5-flash-lite",
        api_key="fake-api-key",
        min_duration=30,
        max_duration=60,
        whisper_model="tiny",
        language="ja",
        audio_fusion=False,
        audio_alpha=0.35,
        output_base_dir=str(tmp_path),
        progress=lambda *_args, **_kwargs: None,
    )
    if legacy:
        result = web_app._legacy_one_shot_handler(
            **kwargs,
            auto_append_youtube=False,
            output_mode="individual",
            generate_shorts=False,
            shorts_mode="pad",
            shorts_crop="center",
            shorts_title=False,
            generate_zip=False,
            font_name="Arial",
            font_size=24,
            font_color="white",
            upload_to_drive=False,
        )
        status = result[0]
        assert result[1:] == ("", None, "", "", None)
    else:
        session, status, panel = web_app.detect_phase(**kwargs)
        assert "highlights" not in session
        assert panel["visible"] is False
        assert "\n".join(session["logs"]) == status

    assert "HTTP 503" in status
    assert "4回" in status
    assert "数分待ってから再実行" in status
    assert "Traceback" not in status
    assert "private-api-response" not in status
    assert "fake-api-key" not in status
    transcripts = list(tmp_path.glob("output_*/transcript.txt"))
    assert len(transcripts) == 1
    assert "saved transcript" in transcripts[0].read_text(encoding="utf-8")


def test_gemini_quota_error_returns_status_and_flags_llm_failure(monkeypatch, tmp_path):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        web_app,
        "get_video_info",
        lambda _path: {"width": 1280, "height": 720, "fps": 30.0, "duration": 60.0},
    )
    monkeypatch.setattr(
        web_app,
        "transcribe",
        lambda *_args: [Segment(start=0, end=30, text="saved transcript")],
    )

    def fail_detection(*_args, **_kwargs):
        raise GeminiQuotaError(True, 52.8) from RuntimeError("private-api-response")

    monkeypatch.setattr(web_app, "detect_highlights", fail_detection)
    monkeypatch.setattr(
        web_app,
        "extract_clips",
        lambda *_a, **_k: pytest.fail("A failed detection must not render clips"),
    )
    session, status, panel = web_app.detect_phase(
        input_url="",
        input_file=str(source),
        enable_clips=True,
        clip_prompt="",
        enable_chapters=False,
        chapter_prompt="",
        num_clips=1,
        ai_provider="gemini",
        ai_model="gemini-3.5-flash-lite",
        api_key="fake-api-key",
        min_duration=30,
        max_duration=60,
        whisper_model="tiny",
        language="ja",
        audio_fusion=False,
        audio_alpha=0.35,
        output_base_dir=str(tmp_path),
        progress=lambda *_args, **_kwargs: None,
    )

    assert session["llm_failed"] is True
    assert "highlights" not in session
    assert panel["visible"] is False
    assert "利用枠" in status
    assert "private-api-response" not in status
    assert "fake-api-key" not in status
    transcripts = list(tmp_path.glob("output_*/transcript.txt"))
    assert len(transcripts) == 1
    assert "saved transcript" in transcripts[0].read_text(encoding="utf-8")
    # transcript.txt keeps the full timestamped format.
    assert "[00:00:00.000 -> 00:00:30.000] saved transcript" in transcripts[0].read_text(
        encoding="utf-8"
    )
