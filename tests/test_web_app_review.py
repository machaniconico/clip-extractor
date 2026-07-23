"""Review-phase tests for the two-step Gradio web app flow."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("gradio")

import web_app
from premiere_xml import generate_combined_xml
from subtitles import generate_srt
from transcriber import Segment


def _progress(*args, **kwargs):
    return None


def _session(tmp_path, highlights=None):
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    return {
        "output_dir": tmp_path,
        "video_path": video_path,
        "video_info": {"width": 1920, "height": 1080, "fps": 30.0, "duration": 20.0},
        "segments": [Segment(start=0.0, end=20.0, text="hello world")],
        "highlights": highlights or [
            {
                "start": "00:00:01.000",
                "end": "00:00:04.000",
                "start_sec": 1.0,
                "end_sec": 4.0,
                "duration": 3.0,
                "title": "Original",
                "reason": "test",
            }
        ],
        "youtube_video_id": None,
        "enable_clips": True,
        "enable_chapters": True,
        "modes": {
            "enable_clips": True,
            "enable_chapters": True,
            "clip_prompt": "",
            "chapter_prompt": "",
        },
        "logs": [],
    }


def test_apply_edits_to_session_clamps_and_corrects_ranges(tmp_path):
    session = _session(tmp_path)

    web_app.apply_edits_to_session(session, 0, -5, 30, "Edited")
    edited = session["highlights"][0]
    assert edited["start_sec"] == 0.0
    assert edited["end_sec"] == 20.0
    assert edited["duration"] == 20.0
    assert edited["title"] == "Edited"

    web_app.apply_edits_to_session(session, 0, 19.98, 1.0, "Inverted")
    edited = session["highlights"][0]
    assert 0.0 <= edited["start_sec"] < edited["end_sec"] <= 20.0
    assert edited["duration"] == pytest.approx(edited["end_sec"] - edited["start_sec"])


def test_review_edit_session_only_round_trip_preserves_edits(tmp_path):
    session = _session(tmp_path)

    updated = web_app._apply_review_edit_event_session_only(
        session, 0, 2.25, 6.75, "Typed title"
    )

    assert isinstance(updated, dict)
    edited = updated["highlights"][0]
    assert edited["start_sec"] == 2.25
    assert edited["end_sec"] == 6.75
    assert edited["duration"] == 4.5
    assert edited["title"] == "Typed title"

    review_rows = web_app.highlights_for_review(updated)
    assert review_rows[0]["start_sec"] == 2.25
    assert review_rows[0]["end_sec"] == 6.75
    assert review_rows[0]["title"] == "Typed title"

    round_tripped = web_app.apply_edits_to_session(
        updated,
        0,
        review_rows[0]["start_sec"],
        review_rows[0]["end_sec"],
        review_rows[0]["title"],
    )
    assert round_tripped["highlights"][0]["start_sec"] == 2.25
    assert round_tripped["highlights"][0]["end_sec"] == 6.75
    assert round_tripped["highlights"][0]["title"] == "Typed title"


def test_detect_phase_returns_session_state(monkeypatch, tmp_path):
    source = tmp_path / "downloaded.mp4"

    def fake_download(url, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"video")
        return source

    monkeypatch.setattr(web_app, "download_video", fake_download)
    monkeypatch.setattr(
        web_app,
        "get_video_info",
        lambda path: {"width": 1280, "height": 720, "fps": 30.0, "duration": 60.0},
    )
    monkeypatch.setattr(
        web_app,
        "transcribe",
        lambda path, model, language: [Segment(start=1.0, end=4.0, text="hello")],
    )
    monkeypatch.setattr(
        web_app,
        "detect_highlights",
        lambda *args, **kwargs: [
            {
                "start": "00:00:01.000",
                "end": "00:00:04.000",
                "start_sec": 1.0,
                "end_sec": 4.0,
                "duration": 3.0,
                "title": "Detected",
                "reason": "mock",
            }
        ],
    )
    monkeypatch.setattr(web_app.youtube_api, "extract_video_id", lambda url: "abc123")

    session, status_md, _panel_update = web_app.detect_phase(
        "https://youtube.com/watch?v=abc123",
        None,
        True,
        "",
        True,
        "",
        1,
        "gemini",
        "gemini-2.5-flash",
        "key",
        1,
        10,
        "tiny",
        "ja",
        False,
        0.35,
        str(tmp_path),
        progress=_progress,
    )

    assert "Detection Complete" in status_md
    assert session["segments"][0].text == "hello"
    assert session["highlights"][0]["title"] == "Detected"
    assert session["video_info"]["duration"] == 60.0
    assert session["output_dir"].parent == tmp_path
    assert session["youtube_video_id"] == "abc123"


def test_render_phase_uses_edited_highlights(monkeypatch, tmp_path):
    session = _session(tmp_path)
    web_app.apply_edits_to_session(session, 0, 2.5, 7.5, "Edited title")
    captured = {}

    def fake_extract(video_path, highlights, output_dir, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        captured.setdefault("calls", []).append([dict(h) for h in highlights])
        path = output_dir / "clip.mp4"
        path.write_bytes(b"clip")
        return [path]

    monkeypatch.setattr(web_app, "extract_clips", fake_extract)

    result = web_app.render_phase(
        session,
        "combined",
        False,
        "crop",
        "center",
        True,
        False,
        False,
        False,
        "Noto Sans JP",
        96,
        "#FFFFFF",
        False,
        False,
        progress=_progress,
    )

    assert "Edited title" in result[1]
    passed = captured["calls"][0][0]
    assert passed["start_sec"] == 2.5
    assert passed["end_sec"] == 7.5
    assert passed["title"] == "Edited title"
    assert len(result) == 6
    premiere_job = result[5]
    assert premiere_job["project_name"] == session["video_path"].stem
    assert [Path(path).name for path in premiere_job["clip_paths"]] == ["clip.mp4"]
    assert all(Path(path).is_absolute() for path in premiere_job["clip_paths"])
    assert Path(premiere_job["xml_paths"][0]).is_file()


def test_chapters_only_render_clears_stale_premiere_job(tmp_path):
    session = _session(tmp_path)
    session["modes"]["enable_clips"] = False
    session["_premiere_output"] = {"clip_paths": ["stale.mp4"]}

    result = web_app.render_phase(
        session,
        "combined",
        False,
        "crop",
        "center",
        True,
        False,
        False,
        False,
        "Noto Sans JP",
        96,
        "#FFFFFF",
        False,
        False,
        progress=_progress,
    )

    assert len(result) == 6
    assert result[5] is None
    assert "_premiere_output" not in session


def test_new_detect_without_auto_render_clears_previous_premiere_state():
    result = web_app.maybe_render_phase(
        False,
        {"video_path": "new-source.mp4"},
        "combined",
        False,
        "crop",
        "center",
        True,
        False,
        False,
        False,
        "Noto Sans JP",
        96,
        "#FFFFFF",
        False,
        False,
        progress=_progress,
    )

    assert len(result) == 6
    assert result[5] is None


def test_edited_highlights_flow_into_srt_and_xml_duration(tmp_path):
    edited_start = 3.0
    edited_end = 8.0
    highlights = [
        {
            "start_sec": edited_start,
            "end_sec": edited_end,
            "duration": edited_end - edited_start,
            "title": "Edited XML",
        }
    ]
    segments = [Segment(start=3.5, end=7.0, text="caption text")]
    srt_path = generate_srt(segments, edited_start, edited_end, tmp_path / "clip.srt")
    assert srt_path.exists()

    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"clip")
    xml_path = generate_combined_xml(
        [clip_path],
        highlights,
        {"width": 1920, "height": 1080, "fps": 30.0, "duration": 20.0},
        tmp_path / "project.xml",
    )
    root = ET.parse(xml_path).getroot()
    sequence_duration = int(root.find(".//sequence/duration").text)
    assert sequence_duration == int((edited_end - edited_start) * 30.0)


def test_render_preview_clip_uses_single_clipper_call(monkeypatch, tmp_path):
    session = _session(tmp_path)
    calls = []

    def fake_extract_clip(video_path, output_path, start_sec, end_sec):
        calls.append((video_path, output_path, start_sec, end_sec))
        output_path.write_bytes(b"preview")
        return output_path

    monkeypatch.setattr(web_app.clipper, "extract_clip", fake_extract_clip)

    preview_path = Path(web_app.render_preview_clip(session, 0, 4.0, 9.0))

    assert preview_path == tmp_path / "_preview" / "clip_0.mp4"
    assert preview_path.exists()
    assert calls == [(session["video_path"], preview_path, 4.0, 9.0)]
