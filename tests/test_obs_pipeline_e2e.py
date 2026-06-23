"""End-to-end integration tests for the OBS auto-pipeline.

These tests close the last gap in the OBS integration: OBS detection->path is
already verified on real hardware, but until now nothing proved that *after*
detection the existing detect->render chain actually produces a real clip file
on disk.

Strategy (deterministic, no API key / GPU / OBS needed):

* ffmpeg-generate a short synthetic video (testsrc + sine audio).
* Stub the two expensive/external steps that ``detect_phase`` pulls into the
  ``web_app`` namespace -- ``transcribe`` (Whisper) and ``detect_highlights``
  (LLM) -- via ``monkeypatch.setattr(web_app, ...)``.
* Everything downstream (ffprobe via ``get_video_info``, ffmpeg cutting via
  ``extract_clips``) runs for real, so a genuine .mp4 lands in
  ``<output_dir>/clips/``.

Run with:
    /mnt/d/workspace/clip-extractor/.venv/bin/python -m pytest \
        tests/test_obs_pipeline_e2e.py -v
"""

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import web_app
from transcriber import Segment


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
ffmpeg_required = pytest.mark.skipif(
    not HAS_FFMPEG, reason="ffmpeg/ffprobe not on PATH"
)

# Clip range used by the highlight stub: 5.0s -> 37.0s == 32s, which is
# >= the 30s min_duration we configure, so the highlight survives filtering.
CLIP_START = 5.0
CLIP_END = 37.0
EXPECTED_CLIP_DURATION = CLIP_END - CLIP_START  # 32.0s
SYNTH_DURATION = 40  # seconds; must comfortably exceed CLIP_END


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

def _make_synthetic_video(dest: Path, duration: int = SYNTH_DURATION) -> Path:
    """Generate a small synthetic video (testsrc + sine) via ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=15:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-shortest", "-pix_fmt", "yuv420p",
        str(dest),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    assert dest.exists() and dest.stat().st_size > 0, "synthetic video not created"
    return dest


def _ffprobe_duration(path: Path) -> float:
    """Return media duration in seconds via ffprobe."""
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, check=True, encoding="utf-8",
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def _stub_transcribe(monkeypatch):
    """Stub the (expensive) Whisper transcribe to return one cheap Segment."""
    def fake_transcribe(video_path, model_size="large-v3", language="ja"):
        return [Segment(start=0.0, end=float(SYNTH_DURATION), text="dummy transcript")]

    monkeypatch.setattr(web_app, "transcribe", fake_transcribe)


def _stub_detect_highlights(monkeypatch, start=CLIP_START, end=CLIP_END):
    """Stub the (LLM) highlight detector to return one well-formed highlight."""
    def fake_detect_highlights(transcript_text, **kwargs):
        return [
            {
                "start": "00:00:05",
                "end": "00:00:37",
                "start_sec": start,
                "end_sec": end,
                "duration": end - start,
                "title": "Test Highlight",
                "reason": "synthetic test clip",
            }
        ]

    monkeypatch.setattr(web_app, "detect_highlights", fake_detect_highlights)


def _auto_settings(tmp_path: Path) -> dict:
    """Settings dict for run_obs_auto_pipeline that avoids YouTube/Drive/shorts."""
    return {
        "enable_clips": True,
        "enable_chapters": False,   # no YouTube needed
        "num_clips": 1,
        "min_duration": 30,
        "max_duration": 90,
        "ai_provider": "gemini",
        "ai_model": "gemini-2.5-flash",
        "whisper_model": "large-v3",
        "language": "ja",
        "audio_fusion": False,
        "audio_alpha": 0.35,
        "output_base_dir": str(tmp_path),
        "output_mode": "individual",
        "generate_shorts": False,
        "shorts_title": False,
        "auto_append_youtube": False,
        "generate_thumbnails": False,
        "karaoke": False,
        "font_name": "Noto Sans JP Black",
        "font_size": 96,
        "font_color": "#FFFFFF",
    }


def _find_clips(tmp_path: Path) -> list[Path]:
    return [p for p in tmp_path.glob("output_*/clips/*.mp4")]


# --------------------------------------------------------------------------
# 1. Direct pipeline: path -> detect_phase -> render_phase -> real .mp4
# --------------------------------------------------------------------------

@ffmpeg_required
def test_obs_auto_pipeline_generates_real_clip(tmp_path, monkeypatch):
    video = _make_synthetic_video(tmp_path / "recording.mp4")
    _stub_transcribe(monkeypatch)
    _stub_detect_highlights(monkeypatch)

    settings = _auto_settings(tmp_path)
    log = web_app.run_obs_auto_pipeline(str(video), settings)

    # The chain reached the end (render completed) and did not error out.
    assert "Render 完了" in log, f"pipeline did not finish render:\n{log}"
    assert "Error:" not in log, f"pipeline reported an error:\n{log}"

    # A real, non-empty .mp4 clip was written.
    clips = _find_clips(tmp_path)
    assert clips, f"no clip produced under {tmp_path}; log:\n{log}"
    clip = clips[0]
    assert clip.stat().st_size > 0, "clip file is empty"

    # The clip really cut the requested ~32s range (allow encoder slack).
    dur = _ffprobe_duration(clip)
    assert abs(dur - EXPECTED_CLIP_DURATION) <= 2.0, (
        f"clip duration {dur:.2f}s not ~{EXPECTED_CLIP_DURATION}s"
    )


# --------------------------------------------------------------------------
# 2. Missing file -> graceful error string, no exception
# --------------------------------------------------------------------------

def test_obs_auto_pipeline_missing_file_returns_error(tmp_path):
    missing = tmp_path / "does_not_exist.mp4"
    # Must not raise; returns a human-readable error string.
    result = web_app.run_obs_auto_pipeline(str(missing), {})
    assert isinstance(result, str)
    assert "見つかりません" in result or "ファイル" in result
    assert not _find_clips(tmp_path), "no clips should be produced for a missing file"


def test_obs_auto_pipeline_empty_path_returns_error():
    result = web_app.run_obs_auto_pipeline("", {})
    assert isinstance(result, str)
    assert "パス" in result  # "録画パスが空です"


# --------------------------------------------------------------------------
# 3. Full OBS detection event -> callback -> worker thread -> real clip
# --------------------------------------------------------------------------

@ffmpeg_required
def test_obs_callback_to_clip_end_to_end(tmp_path, monkeypatch):
    """Exercise the literal 'OBS detection fires -> clip generated' path.

    Drives _obs_make_callback(auto_process=True, settings) -- the exact
    callback the watcher invokes -- with a real synthetic video, then waits
    for the spawned worker thread to finish and asserts a real clip exists.
    """
    video = _make_synthetic_video(tmp_path / "obs_event.mp4")
    _stub_transcribe(monkeypatch)
    _stub_detect_highlights(monkeypatch)

    # Reset the module-level shared status buffer for a clean assertion.
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()

    settings = _auto_settings(tmp_path)
    callback = web_app._obs_make_callback(auto_process=True, settings=settings)

    # Track worker threads spawned during the callback so we can join them.
    before = set(threading.enumerate())
    callback(str(video))

    # Wait (poll the shared status) until the worker signals completion.
    deadline = time.time() + 120
    completed = False
    while time.time() < deadline:
        status = web_app._obs_status_text()
        if f"自動処理完了: {video}" in status:
            completed = True
            break
        time.sleep(0.25)

    # Join any worker threads the callback spawned, for cleanliness.
    for t in set(threading.enumerate()) - before:
        if t is not threading.current_thread():
            t.join(timeout=10)

    final_status = web_app._obs_status_text()
    assert completed, f"worker did not complete in time; status:\n{final_status}"
    assert "録画終了を検知" in final_status
    assert "Render 完了" in final_status, f"render did not finish:\n{final_status}"

    clips = _find_clips(tmp_path)
    assert clips, f"OBS-event path produced no clip; status:\n{final_status}"
    assert clips[0].stat().st_size > 0
    dur = _ffprobe_duration(clips[0])
    assert abs(dur - EXPECTED_CLIP_DURATION) <= 2.0, (
        f"clip duration {dur:.2f}s not ~{EXPECTED_CLIP_DURATION}s"
    )


@ffmpeg_required
def test_obs_callback_detect_only_does_not_render(tmp_path, monkeypatch):
    """auto_process=False must log detection but NOT run the pipeline."""
    video = _make_synthetic_video(tmp_path / "detect_only.mp4")
    _stub_transcribe(monkeypatch)
    _stub_detect_highlights(monkeypatch)

    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()

    callback = web_app._obs_make_callback(auto_process=False, settings=_auto_settings(tmp_path))
    callback(str(video))
    # Detect-only is synchronous (no worker thread); a tiny grace window anyway.
    time.sleep(0.5)

    status = web_app._obs_status_text()
    assert "録画終了を検知" in status
    assert "自動処理が無効" in status
    assert not _find_clips(tmp_path), "no clips should be produced when auto_process=False"


# --------------------------------------------------------------------------
# 4. Generation gate: stale callbacks refuse, current callbacks run
# --------------------------------------------------------------------------

def test_stale_generation_callback_skips_pipeline(tmp_path):
    """A callback whose generation has been superseded must refuse to run.

    No ffmpeg needed: the generation gate fires before the first status append,
    so we simply assert the worker never starts (no status, no clip).
    """
    # Reset shared module state for test independence.
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()
    with web_app._obs_watcher_lock:
        web_app._obs_generation = 7  # current generation

    settings = _auto_settings(tmp_path)
    cb = web_app._obs_make_callback(True, settings, generation=3)  # stale
    cb(str(tmp_path / "irrelevant.mp4"))

    # Give the (refusing) worker a moment; it must not append anything.
    time.sleep(0.3)
    status = web_app._obs_status_text()
    assert "自動パイプライン開始" not in status
    assert "録画終了を検知" not in status
    assert not _find_clips(tmp_path), "stale callback must not produce clips"

    # Restore the generation so this test does not leak into others.
    with web_app._obs_watcher_lock:
        web_app._obs_generation = 0


@ffmpeg_required
def test_current_generation_callback_runs(tmp_path, monkeypatch):
    """A callback whose generation matches the current one runs the pipeline.

    Mirrors ``test_obs_callback_to_clip_end_to_end`` but pins the generation so
    the gate is exercised on the happy path too.
    """
    video = _make_synthetic_video(tmp_path / "obs_gen.mp4")
    _stub_transcribe(monkeypatch)
    _stub_detect_highlights(monkeypatch)

    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()
    with web_app._obs_watcher_lock:
        web_app._obs_generation = 5

    settings = _auto_settings(tmp_path)
    callback = web_app._obs_make_callback(auto_process=True, settings=settings, generation=5)

    before = set(threading.enumerate())
    callback(str(video))

    # Wait (poll the shared status) until the worker signals completion.
    deadline = time.time() + 120
    completed = False
    while time.time() < deadline:
        status = web_app._obs_status_text()
        if f"自動処理完了: {video}" in status:
            completed = True
            break
        time.sleep(0.25)

    # Join any worker threads the callback spawned, for cleanliness.
    for t in set(threading.enumerate()) - before:
        if t is not threading.current_thread():
            t.join(timeout=10)

    final_status = web_app._obs_status_text()
    assert completed, f"worker did not complete in time; status:\n{final_status}"
    assert "録画終了を検知" in final_status
    assert "Render 完了" in final_status, f"render did not finish:\n{final_status}"

    clips = _find_clips(tmp_path)
    assert clips, f"current-gen callback produced no clip; status:\n{final_status}"
    assert clips[0].stat().st_size > 0
    dur = _ffprobe_duration(clips[0])
    assert abs(dur - EXPECTED_CLIP_DURATION) <= 2.0, (
        f"clip duration {dur:.2f}s not ~{EXPECTED_CLIP_DURATION}s"
    )

    # Restore the generation so this test does not leak into others.
    with web_app._obs_watcher_lock:
        web_app._obs_generation = 0
