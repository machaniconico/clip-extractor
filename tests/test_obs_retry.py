"""Behavior tests for the manual OBS detection/generation retry flow."""

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("gradio")

import web_app


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for OBS retry state")


@pytest.fixture(autouse=True)
def _clear_obs_status():
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()
    yield
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()


def test_retry_button_explains_that_obs_must_be_started(monkeypatch):
    monkeypatch.setattr(web_app, "_obs_retry_handler", None)

    status = web_app.retry_obs_detection_flow()

    assert "先に「OBS連携 開始」を押してください" in status


def test_active_folder_retry_uses_existing_watcher_without_restarting(monkeypatch):
    class FakeFolderWatcher:
        def __init__(self):
            self.retry_calls = 0

        def retry_latest(self):
            self.retry_calls += 1
            return "最新録画を再検出: C:/recordings/latest.mkv"

    watcher = FakeFolderWatcher()
    monkeypatch.setattr(web_app, "_obs_generation", 17)
    monkeypatch.setattr(web_app, "_obs_watcher", watcher)
    handler = web_app._obs_make_active_retry_handler(
        "folder",
        True,
        17,
        watcher,
        None,
    )
    monkeypatch.setattr(web_app, "_obs_retry_handler", handler)

    status = web_app.retry_obs_detection_flow()

    assert watcher.retry_calls == 1
    assert "最新録画を再検出" in status


def test_stale_folder_retry_handler_does_not_dispatch(monkeypatch):
    class FakeFolderWatcher:
        retry_calls = 0

        def retry_latest(self):
            self.retry_calls += 1
            return "unexpected"

    watcher = FakeFolderWatcher()
    monkeypatch.setattr(web_app, "_obs_generation", 22)
    monkeypatch.setattr(web_app, "_obs_watcher", watcher)
    handler = web_app._obs_make_active_retry_handler(
        "folder",
        True,
        21,
        watcher,
        None,
    )

    assert "停止または更新" in handler()
    assert watcher.retry_calls == 0


def test_stop_clears_active_retry_handler(monkeypatch):
    class FakeWatcher:
        status = "connected"

        def stop(self):
            self.status = "stopped"

    watcher = FakeWatcher()
    monkeypatch.setattr(web_app, "_obs_watcher", watcher)
    monkeypatch.setattr(web_app, "_obs_retry_handler", lambda: "retry")

    web_app._stop_obs_watch_impl()

    assert web_app._obs_retry_handler is None


def test_folder_callback_blocks_concurrent_and_completed_duplicates(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_pipeline(path, _settings):
        calls.append(path)
        entered.set()
        assert release.wait(timeout=5)
        return web_app.ObsPipelineOutcome(log="done", success=True)

    monkeypatch.setattr(web_app, "_run_obs_auto_pipeline_outcome", fake_pipeline)
    callback = web_app._obs_make_callback(True, {})
    recording = "C:/recordings/duplicate.mkv"

    callback(recording)
    assert entered.wait(timeout=5)
    callback(recording)
    assert calls == [recording]
    release.set()
    _wait_until(lambda: f"自動処理完了: {recording}" in web_app._obs_status_text())

    callback(recording)
    time.sleep(0.05)
    assert calls == [recording]
    assert "処理済みのためスキップ" in web_app._obs_status_text()


def test_failed_folder_callback_can_retry_same_recording(monkeypatch):
    calls = []
    succeeded = threading.Event()

    def fake_pipeline(path, _settings):
        calls.append(path)
        if len(calls) == 1:
            return web_app.ObsPipelineOutcome(
                log="failed",
                success=False,
                error="first failure",
            )
        succeeded.set()
        return web_app.ObsPipelineOutcome(log="done", success=True)

    monkeypatch.setattr(web_app, "_run_obs_auto_pipeline_outcome", fake_pipeline)
    callback = web_app._obs_make_callback(True, {})
    recording = "C:/recordings/retry.mkv"

    callback(recording)
    _wait_until(lambda: "自動パイプラインエラー" in web_app._obs_status_text())
    callback(recording)

    assert succeeded.wait(timeout=5)
    assert calls == [recording, recording]


def test_archive_retry_requires_a_detected_stream_finish():
    _started, finished = web_app._obs_make_archive_callbacks(True, {})
    retry = getattr(finished, "_retry_detection_flow")

    assert "まだ配信終了を検知していない" in retry()


def test_archive_retry_reuses_original_detected_stop_time(monkeypatch):
    first_stop = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    retry_click = first_stop + timedelta(minutes=5)
    clock = iter((first_stop, retry_click))
    resolver_stop_times = []
    completed = threading.Event()

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            value = next(clock)
            return value if tz is not None else value.replace(tzinfo=None)

    def fake_resolve(_broadcast, stopped_at, *_args, **_kwargs):
        resolver_stop_times.append(stopped_at)
        if len(resolver_stop_times) == 1:
            raise RuntimeError("first archive lookup failed")
        return {
            "video_id": "original-stop-time",
            "url": "https://www.youtube.com/watch?v=original-stop-time",
        }

    monkeypatch.setattr(web_app, "datetime", FakeDateTime)
    monkeypatch.setattr(web_app, "_resolve_obs_youtube_archive", fake_resolve)
    monkeypatch.setattr(
        web_app,
        "_run_obs_youtube_pipeline_outcome",
        lambda *_args, **_kwargs: (
            completed.set()
            or web_app.ObsPipelineOutcome(log="archive done", success=True)
        ),
    )

    _started, finished = web_app._obs_make_archive_callbacks(True, {})
    finished()
    _wait_until(lambda: "YouTubeアーカイブ処理エラー" in web_app._obs_status_text())
    retry = getattr(finished, "_retry_detection_flow")
    deadline = time.time() + 5
    while time.time() < deadline:
        message = retry()
        if "現在処理中" not in message:
            break
        time.sleep(0.01)

    assert completed.wait(timeout=5)
    assert resolver_stop_times == [first_stop, first_stop]


def test_retry_keeps_successful_recording_and_retries_only_archive_link(
    monkeypatch,
    tmp_path,
):
    local_calls = []
    resolve_calls = []
    second_resolve = threading.Event()

    def fake_local(path, _settings):
        local_calls.append(path)
        return web_app.ObsPipelineOutcome(log="local done", success=True)

    def fake_resolve(*_args, **_kwargs):
        resolve_calls.append(1)
        if len(resolve_calls) == 1:
            raise RuntimeError("archive lookup failed")
        second_resolve.set()
        return {
            "video_id": "archive-after-local",
            "url": "https://www.youtube.com/watch?v=archive-after-local",
        }

    monkeypatch.setattr(web_app, "_run_obs_auto_pipeline_outcome", fake_local)
    monkeypatch.setattr(web_app, "_resolve_obs_youtube_archive", fake_resolve)

    recorded, recording_stopped, _started, finished = (
        web_app._obs_make_recording_primary_callbacks(
            True,
            {"enable_chapters": False},
        )
    )
    recording = tmp_path / "already-generated.mkv"
    recording.write_bytes(b"stable recording")
    recording_stopped(str(recording))
    recorded(str(recording))
    _wait_until(lambda: len(local_calls) == 1)
    finished()
    _wait_until(lambda: "録画優先モードの自動処理エラー" in web_app._obs_status_text())

    retry = getattr(finished, "_retry_detection_flow")
    retry_message = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        retry_message = retry()
        if "現在処理中" not in retry_message:
            break
        time.sleep(0.01)

    assert "アーカイブ連携だけ" in retry_message
    assert second_resolve.wait(timeout=5)
    assert local_calls == [str(recording)]
    assert resolve_calls == [1, 1]


def test_retry_does_not_start_second_recording_worker_after_finish_timeout(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(web_app, "_OBS_ARCHIVE_READY_TIMEOUT", 0.01)
    entered = threading.Event()
    release = threading.Event()
    local_calls = []

    def blocked_local(path, _settings):
        local_calls.append(path)
        entered.set()
        assert release.wait(timeout=5)
        return web_app.ObsPipelineOutcome(log="local done", success=True)

    monkeypatch.setattr(web_app, "_run_obs_auto_pipeline_outcome", blocked_local)
    recorded, recording_stopped, _started, finished = (
        web_app._obs_make_recording_primary_callbacks(
            True,
            {"enable_chapters": False},
        )
    )
    recording = tmp_path / "still-processing.mkv"
    recording.write_bytes(b"stable recording")
    recording_stopped(str(recording))
    recorded(str(recording))
    assert entered.wait(timeout=5)
    finished()
    _wait_until(lambda: "6時間以内に完了しませんでした" in web_app._obs_status_text())

    retry = getattr(finished, "_retry_detection_flow")
    retry_message = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        retry_message = retry()
        if "直近の検知対象は現在処理中" not in retry_message:
            break
        time.sleep(0.01)

    assert "OBS録画からの生成はまだ処理中" in retry_message
    assert local_calls == [str(recording)]
    release.set()


def test_retry_never_uses_raw_unstable_recording_candidate(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(web_app, "_OBS_RECORDING_EVENT_TIMEOUT", 0.01)
    archive_calls = []
    second_archive = threading.Event()
    local_calls = []

    monkeypatch.setattr(
        web_app,
        "_resolve_obs_youtube_archive",
        lambda *_args, **_kwargs: {
            "video_id": "raw-recording-fallback",
            "url": "https://www.youtube.com/watch?v=raw-recording-fallback",
        },
    )

    def fake_archive(url, _settings):
        archive_calls.append(url)
        if len(archive_calls) == 1:
            return web_app.ObsPipelineOutcome(
                log="archive failed",
                success=False,
                error="archive failed",
            )
        second_archive.set()
        return web_app.ObsPipelineOutcome(log="archive done", success=True)

    monkeypatch.setattr(web_app, "_run_obs_youtube_pipeline_outcome", fake_archive)
    monkeypatch.setattr(
        web_app,
        "_run_obs_auto_pipeline_outcome",
        lambda path, _settings: local_calls.append(path),
    )

    _recorded, recording_stopped, _started, finished = (
        web_app._obs_make_recording_primary_callbacks(True, {})
    )
    recording = tmp_path / "not-yet-stable.mkv"
    recording.write_bytes(b"still changing")
    recording_stopped(str(recording))
    finished()
    _wait_until(lambda: "録画優先モードの自動処理エラー" in web_app._obs_status_text())

    retry = getattr(finished, "_retry_detection_flow")
    deadline = time.time() + 5
    while time.time() < deadline:
        message = retry()
        if "現在処理中" not in message:
            break
        time.sleep(0.01)

    assert second_archive.wait(timeout=5)
    assert local_calls == []
    assert len(archive_calls) == 2


def test_stable_failed_recording_can_retry_without_stream_finish(
    monkeypatch,
    tmp_path,
):
    local_calls = []
    retried = threading.Event()

    def fake_local(path, _settings):
        local_calls.append(path)
        if len(local_calls) == 1:
            return web_app.ObsPipelineOutcome(
                log="local failed",
                success=False,
                error="local failed",
            )
        retried.set()
        return web_app.ObsPipelineOutcome(log="local done", success=True)

    monkeypatch.setattr(web_app, "_run_obs_auto_pipeline_outcome", fake_local)
    recorded, recording_stopped, _started, finished = (
        web_app._obs_make_recording_primary_callbacks(True, {})
    )
    recording = tmp_path / "recording-without-stream-event.mkv"
    recording.write_bytes(b"stable recording")
    recording_stopped(str(recording))
    recorded(str(recording))
    _wait_until(lambda: "OBS録画の処理に失敗" in web_app._obs_status_text())

    retry = getattr(finished, "_retry_detection_flow")
    assert "最新のOBS録画を再検出" in retry()
    assert retried.wait(timeout=5)
    assert local_calls == [str(recording), str(recording)]


def test_late_stable_recording_keeps_original_epoch_when_new_stream_starts(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(web_app, "_OBS_RECORDING_EVENT_TIMEOUT", 0.01)
    archive_entered = threading.Event()
    release_archive = threading.Event()
    local_called = threading.Event()

    monkeypatch.setattr(
        web_app.youtube_api,
        "get_youtube_service",
        lambda: object(),
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "find_active_broadcast",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "list_completed_broadcast_ids",
        lambda *_args, **_kwargs: set(),
    )
    monkeypatch.setattr(
        web_app,
        "_resolve_obs_youtube_archive",
        lambda *_args, **_kwargs: {
            "video_id": "original-epoch-archive",
            "url": "https://www.youtube.com/watch?v=original-epoch-archive",
        },
    )

    def fake_archive(_url, _settings):
        archive_entered.set()
        assert release_archive.wait(timeout=5)
        return web_app.ObsPipelineOutcome(log="archive done", success=True)

    monkeypatch.setattr(web_app, "_run_obs_youtube_pipeline_outcome", fake_archive)
    monkeypatch.setattr(
        web_app,
        "_run_obs_auto_pipeline_outcome",
        lambda *_args, **_kwargs: local_called.set(),
    )

    recorded, recording_stopped, started, finished = (
        web_app._obs_make_recording_primary_callbacks(True, {})
    )
    finished()
    assert archive_entered.wait(timeout=5)

    recording = tmp_path / "stream-a-late.mkv"
    recording.write_bytes(b"stable recording")
    recording_stopped(str(recording))
    started()
    recorded(str(recording))
    time.sleep(0.05)

    assert not local_called.is_set()
    release_archive.set()
    _wait_until(lambda: "完成アーカイブから自動処理完了" in web_app._obs_status_text())


def test_recording_retry_reuses_late_stable_file_after_archive_failure(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(web_app, "_OBS_RECORDING_EVENT_TIMEOUT", 0.01)
    archive_entered = threading.Event()
    release_archive = threading.Event()
    local_done = threading.Event()
    archive_calls = []
    local_paths = []

    monkeypatch.setattr(
        web_app,
        "_resolve_obs_youtube_archive",
        lambda *_args, **_kwargs: {
            "video_id": "retry-late-recording",
            "url": "https://www.youtube.com/watch?v=retry-late-recording",
        },
    )

    def fake_archive(url, _settings):
        archive_calls.append(url)
        archive_entered.set()
        assert release_archive.wait(timeout=5)
        return web_app.ObsPipelineOutcome(
            log="archive failed",
            success=False,
            error="archive failed",
        )

    def fake_local(path, _settings):
        local_paths.append(path)
        local_done.set()
        return web_app.ObsPipelineOutcome(log="local done", success=True)

    monkeypatch.setattr(web_app, "_run_obs_youtube_pipeline_outcome", fake_archive)
    monkeypatch.setattr(web_app, "_run_obs_auto_pipeline_outcome", fake_local)

    recorded, recording_stopped, _started, finished = (
        web_app._obs_make_recording_primary_callbacks(
            True,
            {"enable_chapters": False},
        )
    )
    finished()
    assert archive_entered.wait(timeout=5)

    recording = tmp_path / "late-but-stable.mkv"
    recording.write_bytes(b"stable recording")
    recording_stopped(str(recording))
    recorded(str(recording))
    time.sleep(0.05)
    assert not local_done.is_set()

    release_archive.set()
    _wait_until(lambda: "録画優先モードの自動処理エラー" in web_app._obs_status_text())
    retry = getattr(finished, "_retry_detection_flow")
    retry_message = ""
    deadline = time.time() + 5
    while time.time() < deadline:
        retry_message = retry()
        if "現在処理中" not in retry_message:
            break
        time.sleep(0.01)

    assert "最新のOBS録画を再検出" in retry_message
    assert local_done.wait(timeout=5)
    _wait_until(lambda: "タイムスタンプ無効" in web_app._obs_status_text())
    assert local_paths == [str(recording)]
    assert archive_calls == ["https://www.youtube.com/watch?v=retry-late-recording"]
