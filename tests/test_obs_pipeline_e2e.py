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
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

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


def test_output_directories_are_unique_for_simultaneous_archives(tmp_path):
    first = web_app._create_output_dir(tmp_path)
    second = web_app._create_output_dir(tmp_path)

    assert first != second
    assert first.is_dir()
    assert second.is_dir()


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


def test_obs_youtube_pipeline_uses_url_and_forces_clips_and_chapters(
    tmp_path, monkeypatch
):
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    captured = {}

    def fake_detect_phase(*args, **kwargs):
        captured["detect_args"] = args
        return ({
            "video_path": "downloaded.mp4",
            "output_dir": tmp_path,
        }, "detected", {})

    def fake_render_phase(*args, **kwargs):
        captured["render_args"] = args
        session = args[0]
        clip_path = tmp_path / "clips" / "clip.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"clip")
        chapters_path = tmp_path / "chapters.txt"
        chapters_path.write_text("0:00 Intro", encoding="utf-8")
        session["_obs_render_outcome"] = {
            "clip_paths": [str(clip_path)],
            "chapters_path": str(chapters_path),
            "chapters_text": "0:00 Intro",
            "youtube_append_requested": False,
            "youtube_append_succeeded": None,
        }
        return ("rendered", "", None, "", "0:00 Intro")

    monkeypatch.setattr(web_app, "detect_phase", fake_detect_phase)
    monkeypatch.setattr(web_app, "render_phase", fake_render_phase)

    result = web_app.run_obs_youtube_pipeline(
        url,
        {"enable_clips": False, "enable_chapters": False},
    )

    detect_args = captured["detect_args"]
    assert detect_args[0] == url
    assert detect_args[1] is None
    assert detect_args[2] is True  # enable_clips is mandatory for archive mode
    assert detect_args[4] is True  # chapters/timestamps are mandatory too
    assert "Render 完了" in result


def test_obs_archive_callback_runs_without_local_recording(monkeypatch):
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()

    pipeline_called = threading.Event()
    captured = {}

    def fake_resolve(cached_broadcast, stopped_at, is_current, *_args, **_kwargs):
        captured["cached_broadcast"] = cached_broadcast
        assert is_current()
        return {
            "video_id": "dQw4w9WgXcQ",
            "title": "配信アーカイブ",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        }

    def _capture_pipeline(url, settings):
        captured["url"] = url
        captured["settings"] = settings
        pipeline_called.set()

    def fake_pipeline(url, settings):
        _capture_pipeline(url, settings)
        return "[OBS] Render 完了"

    def fake_pipeline_outcome(url, settings):
        _capture_pipeline(url, settings)
        return SimpleNamespace(
            log="[OBS] Render 完了",
            success=True,
            error="",
        )

    monkeypatch.setattr(
        web_app, "_resolve_obs_youtube_archive", fake_resolve, raising=False
    )
    monkeypatch.setattr(
        web_app, "run_obs_youtube_pipeline", fake_pipeline, raising=False
    )
    monkeypatch.setattr(
        web_app,
        "_run_obs_youtube_pipeline_outcome",
        fake_pipeline_outcome,
        raising=False,
    )

    _started, finished = web_app._obs_make_archive_callbacks(
        auto_process=True,
        settings={"enable_clips": False, "enable_chapters": False},
    )
    finished(None)

    assert pipeline_called.wait(timeout=5), web_app._obs_status_text()
    assert captured["cached_broadcast"] is None
    assert captured["url"].endswith("dQw4w9WgXcQ")
    assert captured["settings"]["enable_clips"] is True
    assert captured["settings"]["enable_chapters"] is True
    assert "配信終了を検知" in web_app._obs_status_text()


def test_resolve_cached_archive_waits_for_complete_before_processing(monkeypatch):
    service = object()
    lifecycle_states = iter(["live", "complete"])
    lifecycle_calls = []

    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", lambda: service)

    def fake_lifecycle(actual_service, video_id):
        assert actual_service is service
        lifecycle_calls.append(video_id)
        return next(lifecycle_states)

    monkeypatch.setattr(
        web_app.youtube_api,
        "get_broadcast_lifecycle_status",
        fake_lifecycle,
        raising=False,
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "get_archive_processing_state",
        lambda _service, _video_id: {
            "ready": True,
            "failed": False,
            "processing_status": "succeeded",
            "upload_status": "processed",
            "privacy_status": "public",
        },
    )
    monkeypatch.setattr(
        web_app,
        "_obs_wait_for_poll",
        lambda _seconds, _is_current: None,
        raising=False,
    )

    result = web_app._resolve_obs_youtube_archive(
        {"video_id": "dQw4w9WgXcQ", "title": "archive"},
        datetime.now(timezone.utc),
        lambda: True,
    )

    assert lifecycle_calls == ["dQw4w9WgXcQ", "dQw4w9WgXcQ"]
    assert result["url"].endswith("dQw4w9WgXcQ")


def test_archive_api_transient_errors_are_retried(monkeypatch):
    service = object()
    services = iter([TimeoutError("temporary connection timeout"), service])
    lifecycle_results = iter([TimeoutError("temporary timeout"), "complete"])
    processing_results = iter([
        TimeoutError("temporary timeout"),
        {
            "ready": True,
            "failed": False,
            "processing_status": "succeeded",
            "upload_status": "processed",
            "privacy_status": "public",
        },
    ])

    def fake_service():
        result = next(services)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", fake_service)

    def fake_lifecycle(_service, _video_id):
        result = next(lifecycle_results)
        if isinstance(result, Exception):
            raise result
        return result

    def fake_processing(_service, _video_id):
        result = next(processing_results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        web_app.youtube_api,
        "get_broadcast_lifecycle_status",
        fake_lifecycle,
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "get_archive_processing_state",
        fake_processing,
    )
    monkeypatch.setattr(
        web_app,
        "_obs_wait_for_poll",
        lambda _seconds, _is_current: None,
    )

    result = web_app._resolve_obs_youtube_archive(
        {"video_id": "retry-api", "title": "archive"},
        datetime.now(timezone.utc),
        lambda: True,
    )

    assert result["video_id"] == "retry-api"


def test_youtube_403_rate_limit_is_retryable_but_quota_exhaustion_is_not():
    class FakeHttpError(Exception):
        def __init__(self, reason):
            super().__init__(reason)
            self.resp = SimpleNamespace(status=403)
            self.error_details = [{"reason": reason}]

    assert web_app._is_retriable_youtube_api_error(
        FakeHttpError("rateLimitExceeded")
    )
    assert web_app._is_retriable_youtube_api_error(
        FakeHttpError("userRateLimitExceeded")
    )
    assert not web_app._is_retriable_youtube_api_error(
        FakeHttpError("quotaExceeded")
    )


def test_proactive_capture_failure_recovers_at_stream_stop(monkeypatch):
    service = object()
    services = iter([TimeoutError("capture failed"), service])
    pipeline_done = threading.Event()
    completed_cutoffs = []
    monitor_started_before = datetime.now(timezone.utc)

    def fake_service():
        result = next(services)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", fake_service)
    monkeypatch.setattr(
        web_app.youtube_api,
        "find_active_broadcast",
        lambda _service, **_kwargs: None,
    )
    def fake_completed(_service, **kwargs):
        completed_cutoffs.append(kwargs["ended_after"])
        return {
            "video_id": "recovered-stream",
            "title": "recovered",
            "actual_start_time": "2026-07-22T10:00:00Z",
            "actual_end_time": "2026-07-22T10:10:00Z",
        }

    monkeypatch.setattr(
        web_app.youtube_api,
        "find_recent_completed_broadcast",
        fake_completed,
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "get_broadcast_lifecycle_status",
        lambda _service, _video_id: "complete",
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "get_archive_processing_state",
        lambda _service, _video_id: {
            "ready": True,
            "failed": False,
            "processing_status": "succeeded",
            "upload_status": "processed",
            "privacy_status": "public",
        },
    )
    monkeypatch.setattr(
        web_app,
        "_run_obs_youtube_pipeline_outcome",
        lambda *_args, **_kwargs: (
            pipeline_done.set()
            or SimpleNamespace(log="[OBS] Render 完了", success=True, error="")
        ),
    )

    started, finished = web_app._obs_make_archive_callbacks(True, {})
    started(proactive=True)
    finished(None)

    assert pipeline_done.wait(timeout=5)
    assert completed_cutoffs
    assert completed_cutoffs[0] >= monitor_started_before


def test_resolver_excludes_broadcasts_completed_before_this_stream(monkeypatch):
    service = object()
    stopped_at = datetime.now(timezone.utc)
    completed_calls = []

    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", lambda: service)
    active_cutoffs = []

    def fake_active(_service, started_before=None, started_after=None):
        active_cutoffs.append((started_before, started_after))
        return None

    monkeypatch.setattr(web_app.youtube_api, "find_active_broadcast", fake_active)

    def fake_find_completed(
        _service,
        ended_after=None,
        exclude_video_ids=None,
        started_before=None,
        started_after=None,
    ):
        completed_calls.append((
            ended_after,
            set(exclude_video_ids or set()),
            started_before,
            started_after,
        ))
        return {
            "video_id": "current-stream",
            "title": "current",
            "actual_end_time": stopped_at.isoformat(),
        }

    monkeypatch.setattr(
        web_app.youtube_api,
        "find_recent_completed_broadcast",
        fake_find_completed,
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "get_broadcast_lifecycle_status",
        lambda _service, _video_id: "complete",
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "get_archive_processing_state",
        lambda _service, _video_id: {
            "ready": True,
            "failed": False,
            "processing_status": "succeeded",
            "upload_status": "processed",
            "privacy_status": "public",
        },
    )

    result = web_app._resolve_obs_youtube_archive(
        None,
        stopped_at,
        lambda: True,
        {"previous-stream"},
    )

    assert result["video_id"] == "current-stream"
    assert completed_calls
    ended_after, excluded, started_before, started_after = completed_calls[0]
    assert stopped_at.timestamp() - ended_after.timestamp() <= 120
    assert excluded == {"previous-stream"}
    assert started_before == stopped_at
    assert started_after is None
    assert active_cutoffs == [(stopped_at, None)]


def test_resolver_discards_excluded_cached_and_stale_active_ids(monkeypatch):
    service = object()
    stale = {
        "video_id": "first-stream",
        "title": "stale first",
        "actual_start_time": "2026-07-22T10:00:00Z",
    }

    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", lambda: service)
    monkeypatch.setattr(
        web_app.youtube_api,
        "find_active_broadcast",
        lambda _service, **_kwargs: dict(stale),
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "find_recent_completed_broadcast",
        lambda _service, **_kwargs: {
            "video_id": "second-stream",
            "title": "second",
            "actual_start_time": "2026-07-22T10:00:10Z",
            "actual_end_time": "2026-07-22T10:00:20Z",
        },
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "get_broadcast_lifecycle_status",
        lambda _service, _video_id: "complete",
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "get_archive_processing_state",
        lambda _service, _video_id: {
            "ready": True,
            "failed": False,
            "processing_status": "succeeded",
            "upload_status": "processed",
            "privacy_status": "public",
        },
    )

    result = web_app._resolve_obs_youtube_archive(
        stale,
        datetime.now(timezone.utc),
        lambda: True,
        {"first-stream"},
    )

    assert result["video_id"] == "second-stream"


def test_new_stream_clears_cached_broadcast_when_active_lookup_is_empty(monkeypatch):
    captures = iter([
        {"video_id": "first-stream", "title": "first"},
        None,
    ])
    capture_count = 0
    captures_done = threading.Event()
    resolved = threading.Event()
    resolved_cached = []

    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", lambda: object())

    active_lower_bounds = []

    def fake_active(_service, started_after=None):
        nonlocal capture_count
        active_lower_bounds.append(started_after)
        result = next(captures)
        capture_count += 1
        if capture_count == 2:
            captures_done.set()
        return result

    monkeypatch.setattr(web_app.youtube_api, "find_active_broadcast", fake_active)
    monkeypatch.setattr(
        web_app.youtube_api,
        "list_completed_broadcast_ids",
        lambda _service, **_kwargs: set(),
        raising=False,
    )

    def fake_resolve(cached_broadcast, *_args, **_kwargs):
        resolved_cached.append(cached_broadcast)
        resolved.set()
        raise RuntimeError("stop after state assertion")

    monkeypatch.setattr(web_app, "_resolve_obs_youtube_archive", fake_resolve)

    started, finished = web_app._obs_make_archive_callbacks(True, {})
    started()
    deadline = time.time() + 5
    while capture_count < 1 and time.time() < deadline:
        time.sleep(0.01)
    started()
    assert captures_done.wait(timeout=5)
    finished(None)

    assert resolved.wait(timeout=5)
    assert resolved_cached == [None]
    assert len(active_lower_bounds) == 2
    assert all(bound is not None for bound in active_lower_bounds)
    deadline = time.time() + 5
    while "stop after state assertion" not in web_app._obs_status_text() and time.time() < deadline:
        time.sleep(0.01)


def test_consecutive_streams_resolve_in_order_and_exclude_first_id(monkeypatch):
    captures = iter([
        {"video_id": "first-stream", "title": "first"},
        None,
    ])
    capture_count = 0
    capture_done = threading.Event()
    first_resolver_entered = threading.Event()
    release_first_resolver = threading.Event()
    second_resolver_entered = threading.Event()
    pipelines_done = threading.Event()
    resolve_exclusions = []
    pipeline_urls = []

    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", lambda: object())

    def fake_active(_service, **_kwargs):
        nonlocal capture_count
        result = next(captures)
        capture_count += 1
        if capture_count == 2:
            capture_done.set()
        return result

    monkeypatch.setattr(web_app.youtube_api, "find_active_broadcast", fake_active)
    monkeypatch.setattr(
        web_app.youtube_api,
        "list_completed_broadcast_ids",
        lambda _service, **_kwargs: set(),
    )

    def fake_resolver(cached, _stopped, _current, excluded=None, *_args):
        resolve_exclusions.append(set(excluded or set()))
        if len(resolve_exclusions) == 1:
            assert cached["video_id"] == "first-stream"
            first_resolver_entered.set()
            assert release_first_resolver.wait(timeout=5)
            return {
                "video_id": "first-stream",
                "url": "https://www.youtube.com/watch?v=first-stream",
            }
        second_resolver_entered.set()
        return {
            "video_id": "second-stream",
            "url": "https://www.youtube.com/watch?v=second-stream",
        }

    def fake_pipeline(url, _settings):
        pipeline_urls.append(url)
        if len(pipeline_urls) == 2:
            pipelines_done.set()
        return SimpleNamespace(log="[OBS] Render 完了", success=True, error="")

    monkeypatch.setattr(web_app, "_resolve_obs_youtube_archive", fake_resolver)
    monkeypatch.setattr(web_app, "_run_obs_youtube_pipeline_outcome", fake_pipeline)

    started, finished = web_app._obs_make_archive_callbacks(True, {})
    started()
    deadline = time.time() + 5
    while capture_count < 1 and time.time() < deadline:
        time.sleep(0.01)
    finished(None)
    assert first_resolver_entered.wait(timeout=5)

    started()
    assert capture_done.wait(timeout=5)
    finished(None)
    time.sleep(0.1)
    assert not second_resolver_entered.is_set()

    release_first_resolver.set()
    assert second_resolver_entered.wait(timeout=5)
    assert pipelines_done.wait(timeout=5)
    assert resolve_exclusions[1] >= {"first-stream"}
    assert pipeline_urls == [
        "https://www.youtube.com/watch?v=first-stream",
        "https://www.youtube.com/watch?v=second-stream",
    ]


def test_archive_failure_is_retryable_for_the_same_video_id(monkeypatch):
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()
    calls = []
    resolve_calls = []
    second_call = threading.Event()

    def fake_resolve(*_args, **_kwargs):
        resolve_calls.append(1)
        return {
            "video_id": "retry-stream",
            "title": "retry",
            "url": "https://www.youtube.com/watch?v=retry-stream",
        }

    monkeypatch.setattr(web_app, "_resolve_obs_youtube_archive", fake_resolve)

    def _next_result():
        calls.append(len(calls) + 1)
        success = len(calls) > 1
        if success:
            second_call.set()
        return success

    def fake_pipeline(_url, _settings):
        return "[OBS] Render 完了" if _next_result() else "Error: first attempt failed"

    def fake_pipeline_outcome(_url, _settings):
        success = _next_result()
        return SimpleNamespace(
            log="[OBS] Render 完了" if success else "Error: first attempt failed",
            success=success,
            error="" if success else "first attempt failed",
        )

    monkeypatch.setattr(web_app, "run_obs_youtube_pipeline", fake_pipeline)
    monkeypatch.setattr(
        web_app,
        "_run_obs_youtube_pipeline_outcome",
        fake_pipeline_outcome,
        raising=False,
    )

    _started, finished = web_app._obs_make_archive_callbacks(True, {})
    finished(None)
    deadline = time.time() + 5
    while len(calls) < 1 and time.time() < deadline:
        time.sleep(0.01)
    deadline = time.time() + 5
    while "YouTubeアーカイブ処理エラー" not in web_app._obs_status_text() and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    finished(None)

    assert second_call.wait(timeout=5)
    assert calls == [1, 2]
    assert resolve_calls == [1]


def test_completed_archive_ignores_duplicate_stop_event(monkeypatch):
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()
    calls = []
    completed = threading.Event()

    monkeypatch.setattr(
        web_app,
        "_resolve_obs_youtube_archive",
        lambda *_args, **_kwargs: {
            "video_id": "one-stream",
            "url": "https://www.youtube.com/watch?v=one-stream",
        },
    )

    def fake_pipeline(_url, _settings):
        calls.append(1)
        completed.set()
        return SimpleNamespace(log="[OBS] Render 完了", success=True, error="")

    monkeypatch.setattr(web_app, "_run_obs_youtube_pipeline_outcome", fake_pipeline)

    _started, finished = web_app._obs_make_archive_callbacks(True, {})
    finished(None)
    assert completed.wait(timeout=5)
    deadline = time.time() + 5
    while "アーカイブ自動処理完了" not in web_app._obs_status_text() and time.time() < deadline:
        time.sleep(0.01)
    finished(None)
    time.sleep(0.1)

    assert calls == [1]


def test_archive_pipeline_does_not_start_after_watcher_is_stopped(monkeypatch):
    pipeline_called = threading.Event()
    with web_app._obs_watcher_lock:
        web_app._obs_generation = 41

    def fake_resolve(*_args, **_kwargs):
        with web_app._obs_watcher_lock:
            web_app._obs_generation = 42
        return {
            "video_id": "cancelled-stream",
            "url": "https://www.youtube.com/watch?v=cancelled-stream",
        }

    def fake_pipeline(*_args, **_kwargs):
        pipeline_called.set()
        return "[OBS] Render 完了"

    def fake_pipeline_outcome(*_args, **_kwargs):
        pipeline_called.set()
        return SimpleNamespace(log="[OBS] Render 完了", success=True, error="")

    monkeypatch.setattr(web_app, "_resolve_obs_youtube_archive", fake_resolve)
    monkeypatch.setattr(web_app, "run_obs_youtube_pipeline", fake_pipeline)
    monkeypatch.setattr(
        web_app,
        "_run_obs_youtube_pipeline_outcome",
        fake_pipeline_outcome,
        raising=False,
    )

    _started, finished = web_app._obs_make_archive_callbacks(
        True,
        {},
        generation=41,
    )
    finished(None)
    time.sleep(0.2)

    assert not pipeline_called.is_set()
    with web_app._obs_watcher_lock:
        web_app._obs_generation = 0


def test_archive_poll_wait_is_interrupted_when_watcher_stops(monkeypatch):
    states = iter([True, False])
    monkeypatch.setattr(web_app.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="OBS連携が停止"):
        web_app._obs_wait_for_poll(60, lambda: next(states))


def test_archive_local_fallback_forces_clips_and_timestamps(tmp_path, monkeypatch):
    import obs_integration

    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()
    recording = tmp_path / "fallback.mp4"
    recording.write_bytes(b"recording")
    fallback_done = threading.Event()
    captured = {}
    fallback_calls = []

    monkeypatch.setattr(
        web_app,
        "_resolve_obs_youtube_archive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("archive failed")),
    )
    monkeypatch.setattr(obs_integration, "wait_until_file_stable", lambda *_a, **_k: True)

    def fake_fallback(path, settings):
        fallback_calls.append(1)
        captured["path"] = path
        captured["settings"] = settings
        fallback_done.set()
        return SimpleNamespace(log="[OBS] Render 完了", success=True, error="")

    monkeypatch.setattr(web_app, "_run_obs_auto_pipeline_outcome", fake_fallback)

    _started, finished = web_app._obs_make_archive_callbacks(
        True,
        {
            "enable_clips": False,
            "enable_chapters": False,
            "auto_append_youtube": True,
        },
    )
    finished(str(recording))

    assert fallback_done.wait(timeout=5)
    assert captured["path"] == str(recording)
    assert captured["settings"]["enable_clips"] is True
    assert captured["settings"]["enable_chapters"] is True
    assert captured["settings"]["auto_append_youtube"] is False
    deadline = time.time() + 5
    while "ローカル録画の自動処理完了" not in web_app._obs_status_text() and time.time() < deadline:
        time.sleep(0.01)
    finished(str(recording))
    time.sleep(0.1)
    assert fallback_calls == [1]


def test_local_obs_failure_is_not_reported_as_complete(monkeypatch):
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()
    failed = threading.Event()

    def fake_outcome(_path, _settings):
        failed.set()
        return SimpleNamespace(
            log="Error: local pipeline failed",
            success=False,
            error="local pipeline failed",
        )

    monkeypatch.setattr(web_app, "_run_obs_auto_pipeline_outcome", fake_outcome)

    callback = web_app._obs_make_callback(True, {})
    callback("C:/recordings/failure.mp4")

    assert failed.wait(timeout=5)
    deadline = time.time() + 5
    while "自動パイプラインエラー" not in web_app._obs_status_text() and time.time() < deadline:
        time.sleep(0.01)
    status = web_app._obs_status_text()
    assert "自動処理完了: C:/recordings/failure.mp4" not in status


def test_obs_archive_requires_timestamp_file_and_text(tmp_path, monkeypatch):
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    monkeypatch.setattr(
        web_app,
        "detect_phase",
        lambda *_args, **_kwargs: ({
            "video_path": "downloaded.mp4",
            "output_dir": tmp_path,
        }, "detected", {}),
    )

    def fake_render(session, *_args, **_kwargs):
        clip_path = tmp_path / "clips" / "clip.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"clip")
        session["_obs_render_outcome"] = {
            "clip_paths": [str(clip_path)],
            "chapters_path": "",
            "chapters_text": "",
            "youtube_append_requested": False,
            "youtube_append_succeeded": None,
        }
        return ("Chapter generation failed: boom", "", None, "", "")

    monkeypatch.setattr(web_app, "render_phase", fake_render)

    result = web_app.run_obs_youtube_pipeline(url, {})

    assert "Error:" in result
    assert "タイムスタンプ" in result


def test_obs_archive_auto_append_failure_is_not_marked_complete(tmp_path, monkeypatch):
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    monkeypatch.setattr(
        web_app,
        "detect_phase",
        lambda *_args, **_kwargs: ({
            "video_path": "downloaded.mp4",
            "output_dir": tmp_path,
        }, "detected", {}),
    )

    def fake_render(session, *_args, **_kwargs):
        clip_path = tmp_path / "clips" / "clip.mp4"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        clip_path.write_bytes(b"clip")
        chapters_path = tmp_path / "chapters.txt"
        chapters_path.write_text("0:00 Intro", encoding="utf-8")
        session["_obs_render_outcome"] = {
            "clip_paths": [str(clip_path)],
            "chapters_path": str(chapters_path),
            "chapters_text": "0:00 Intro",
            "youtube_append_requested": True,
            "youtube_append_succeeded": False,
        }
        return ("YouTube 概要欄更新失敗: boom", "", None, "", "0:00 Intro")

    monkeypatch.setattr(web_app, "render_phase", fake_render)

    result = web_app.run_obs_youtube_pipeline(url, {"auto_append_youtube": True})

    assert "Error:" in result
    assert "概要欄" in result


def test_start_obs_stream_watch_wires_youtube_archive_callbacks(monkeypatch):
    import obs_integration

    captured = {}
    archive_started = lambda **_kwargs: None
    archive_finished = lambda _path: None

    class FakeWatcher:
        status = "connected"

        def start(self):
            captured["started"] = True

        def stop(self):
            self.status = "stopped"

    def fake_create_watcher(method, config, callback, **kwargs):
        captured.update({
            "method": method,
            "config": config,
            "recording_callback": callback,
            **kwargs,
        })
        return FakeWatcher()

    monkeypatch.setattr(
        web_app.youtube_api,
        "check_auth_status",
        lambda: {"configured": True, "authenticated": True},
    )
    monkeypatch.setattr(
        web_app,
        "_obs_make_archive_callbacks",
        lambda *_args, **_kwargs: (archive_started, archive_finished),
    )
    monkeypatch.setattr(obs_integration, "create_watcher", fake_create_watcher)

    status = web_app.start_obs_watch(
        "websocket", "localhost", 4455, "pw", "stream", "", True, False,
        5, "combined", False, "gemini", "large-v3", "",
    )

    assert status == "connected"
    assert captured["started"] is True
    assert captured["on_stream_started"] is archive_started
    assert captured["on_stream_finished"] is archive_finished
    web_app.stop_obs_watch()


def test_start_obs_stream_watch_requires_youtube_auth(monkeypatch):
    import obs_integration

    create_called = False

    def fake_create_watcher(*_args, **_kwargs):
        nonlocal create_called
        create_called = True

    monkeypatch.setattr(
        web_app.youtube_api,
        "check_auth_status",
        lambda: {"configured": True, "authenticated": False, "expired": True},
    )
    monkeypatch.setattr(obs_integration, "create_watcher", fake_create_watcher)

    status = web_app.start_obs_watch(
        "websocket", "localhost", 4455, "pw", "stream", "", True, False,
        5, "combined", False, "gemini", "large-v3", "",
    )

    assert "YouTube" in status
    assert "認証" in status
    assert create_called is False


def test_start_obs_connection_failure_does_not_probe_youtube(monkeypatch):
    import obs_integration

    probed = threading.Event()

    class FakeWatcher:
        status = "接続失敗: OBS is offline"

        def start(self):
            pass

        def stop(self):
            self.status = "stopped"

    monkeypatch.setattr(
        web_app.youtube_api,
        "check_auth_status",
        lambda: {"configured": True, "authenticated": True},
    )
    monkeypatch.setattr(
        web_app,
        "_obs_make_archive_callbacks",
        lambda *_args, **_kwargs: (
            lambda **_start_kwargs: probed.set(),
            lambda _path: None,
        ),
    )
    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **_kwargs: FakeWatcher(),
    )

    status = web_app.start_obs_watch(
        "websocket", "localhost", 4455, "pw", "stream", "", True, False,
        5, "combined", False, "gemini", "large-v3", "",
    )

    assert status.startswith("接続失敗")
    assert not probed.is_set()
    web_app.stop_obs_watch()


@ffmpeg_required
def test_obs_youtube_archive_generates_clip_chapters_and_auto_appends(
    tmp_path, monkeypatch
):
    video = _make_synthetic_video(tmp_path / "youtube_archive.mp4")
    _stub_transcribe(monkeypatch)
    _stub_detect_highlights(monkeypatch)

    video_id = "dQw4w9WgXcQ"
    url = f"https://www.youtube.com/watch?v={video_id}"
    monkeypatch.setattr(web_app, "download_video", lambda _url, _out: video)
    monkeypatch.setattr(
        web_app.youtube_api,
        "check_auth_status",
        lambda: {
            "configured": True,
            "authenticated": True,
            "expired": False,
        },
    )
    monkeypatch.setattr(web_app.youtube_api, "is_configured", lambda: True)
    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", lambda: object())
    appended = []

    def fake_update(_service, actual_video_id, chapters_text, position="prepend"):
        appended.append((actual_video_id, chapters_text, position))
        return {}

    monkeypatch.setattr(web_app.youtube_api, "update_video_description", fake_update)

    settings = _auto_settings(tmp_path)
    settings["enable_clips"] = False
    settings["enable_chapters"] = False
    settings["auto_append_youtube"] = True
    outcome = web_app._run_obs_youtube_pipeline_outcome(url, settings)
    log = outcome.log

    assert outcome.success is True, outcome.error
    assert "Render 完了" in log, log
    clips = _find_clips(tmp_path)
    assert clips and clips[0].stat().st_size > 0
    chapter_files = list(tmp_path.glob("output_*/chapters.txt"))
    assert len(chapter_files) == 1
    chapters_text = chapter_files[0].read_text(encoding="utf-8")
    assert chapters_text.strip()
    assert outcome.chapters_text == chapters_text
    assert Path(outcome.chapters_path) == chapter_files[0]
    assert tuple(Path(path) for path in outcome.clip_paths) == tuple(clips)
    assert outcome.youtube_appended is True
    assert appended == [(video_id, chapters_text, "prepend")]


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
