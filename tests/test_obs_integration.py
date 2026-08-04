"""Tests for obs_integration — run without OBS / without the optional deps.

obsws-python and watchdog are NOT installed in CI, so the module must import
cleanly (lazy imports) and the watchers must report missing deps via
``status`` rather than raising. Behavioural tests drive the event handlers /
file-stability helper directly and stub the third-party libs where needed.
"""

import os
import sys
import threading
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import obs_integration as oi


# --------------------------------------------------------------------------
# wait_until_file_stable
# --------------------------------------------------------------------------

def test_wait_until_file_stable_stable(tmp_path, monkeypatch):
    p = tmp_path / "done.mp4"
    p.write_bytes(b"video-data")
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    assert oi.wait_until_file_stable(p, checks=2, interval=0.0) is True


def test_wait_until_file_stable_writing(tmp_path, monkeypatch):
    p = tmp_path / "growing.mp4"
    p.write_bytes(b"x")

    def grow(_secs):
        with open(p, "ab") as f:
            f.write(b"y")

    monkeypatch.setattr(oi.time, "sleep", grow)
    assert oi.wait_until_file_stable(p, checks=2, interval=0.0) is False


def test_wait_until_file_stable_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    assert oi.wait_until_file_stable(tmp_path / "nope.mp4", checks=2) is False


def test_wait_until_file_stable_empty_file(tmp_path, monkeypatch):
    p = tmp_path / "empty.mp4"
    p.write_bytes(b"")
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    # zero-size file is not considered a finished recording
    assert oi.wait_until_file_stable(p, checks=2, interval=0.0) is False


# --------------------------------------------------------------------------
# FolderWatcher
# --------------------------------------------------------------------------

def test_folder_watcher_fires_callback_with_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    mp4 = tmp_path / "rec.mp4"
    mp4.write_bytes(b"video")

    received: list[str] = []
    done = threading.Event()

    def cb(path):
        received.append(path)
        done.set()

    w = oi.FolderWatcher(tmp_path, cb)
    w._handle_event(str(mp4))
    assert done.wait(timeout=5), "callback did not fire"
    assert len(received) == 1
    assert Path(received[0]).is_absolute()
    assert Path(received[0]).resolve() == mp4.resolve()
    w.stop()
    assert w.status == "stopped"


def test_folder_watcher_ignores_non_video_extensions(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    txt = tmp_path / "notes.txt"
    txt.write_bytes(b"hi")

    received: list[str] = []
    w = oi.FolderWatcher(tmp_path, received.append)
    w._handle_event(str(txt))
    # No worker is spawned for non-matching extensions; give a brief grace
    # window then assert nothing arrived.
    assert not received
    w.stop()


def test_folder_watcher_retry_latest_dispatches_newest_supported_file(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    older = tmp_path / "older.mp4"
    newer = tmp_path / "newer.mkv"
    ignored = tmp_path / "newest.txt"
    older.write_bytes(b"older-video")
    newer.write_bytes(b"newer-video")
    ignored.write_bytes(b"not-a-video")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    os.utime(ignored, (3, 3))

    received: list[str] = []
    done = threading.Event()

    def cb(path):
        received.append(path)
        done.set()

    watcher = oi.FolderWatcher(tmp_path, cb)
    outcome = watcher.retry_latest()

    assert "最新録画を再検出" in outcome
    assert done.wait(timeout=5), "retry callback did not fire"
    assert received == [str(newer.resolve())]
    watcher.stop()


def test_folder_watcher_retry_latest_reports_empty_supported_set(tmp_path):
    (tmp_path / "notes.txt").write_bytes(b"not-a-video")
    received: list[str] = []
    watcher = oi.FolderWatcher(tmp_path, received.append)

    outcome = watcher.retry_latest()

    assert "再試行対象の録画ファイルがありません" in outcome
    assert received == []
    watcher.stop()


@pytest.mark.parametrize("remove_before_check", [False, True])
def test_folder_watcher_retry_latest_rejects_unstable_or_missing_file(
    tmp_path,
    monkeypatch,
    remove_before_check,
):
    recording = tmp_path / "latest.mp4"
    recording.write_bytes(b"video")
    received: list[str] = []
    stability_checked = threading.Event()

    def unstable_or_missing(path, *args, **kwargs):
        if remove_before_check:
            Path(path).unlink()
        stability_checked.set()
        return False

    monkeypatch.setattr(oi, "wait_until_file_stable", unstable_or_missing)
    watcher = oi.FolderWatcher(tmp_path, received.append)

    outcome = watcher.retry_latest()

    assert "最新録画を再検出" in outcome
    assert stability_checked.wait(timeout=5), "stability validation did not run"
    with watcher._workers_lock:
        workers = list(watcher._workers)
    for worker in workers:
        worker.join(timeout=5)
    assert received == []
    assert "ファイルが安定しません" in watcher.status
    watcher.stop()


def test_folder_watcher_start_missing_dep(tmp_path, monkeypatch):
    # Make watchdog unimportable.
    monkeypatch.setitem(sys.modules, "watchdog", None)
    monkeypatch.setitem(sys.modules, "watchdog.observers", None)
    monkeypatch.setitem(sys.modules, "watchdog.events", None)
    w = oi.FolderWatcher(tmp_path, lambda p: None)
    w.start()
    assert "error" in w.status
    w.stop()


def test_folder_watcher_start_stop_with_mocked_watchdog(tmp_path, monkeypatch):
    fake = types.ModuleType("watchdog")
    obs_mod = types.ModuleType("watchdog.observers")
    evt_mod = types.ModuleType("watchdog.events")

    class _Observer:
        def __init__(self):
            self.scheduled = []
            self.stopped = False

        def schedule(self, handler, path, recursive=False):
            self.scheduled.append((handler, path, recursive))

        def start(self):
            pass

        def stop(self):
            self.stopped = True

        def join(self, timeout=None):
            pass

    class _FSEventHandler:
        pass

    obs_mod.Observer = _Observer
    evt_mod.FileSystemEventHandler = _FSEventHandler
    fake.observers = obs_mod
    fake.events = evt_mod
    monkeypatch.setitem(sys.modules, "watchdog", fake)
    monkeypatch.setitem(sys.modules, "watchdog.observers", obs_mod)
    monkeypatch.setitem(sys.modules, "watchdog.events", evt_mod)

    w = oi.FolderWatcher(tmp_path, lambda p: None)
    w.start()
    assert w.status.startswith("監視中")
    observer = w._observer
    assert observer is not None
    assert observer.scheduled and observer.scheduled[0][1] == str(tmp_path)
    w.stop()
    assert w.status == "stopped"
    assert observer.stopped is True


# --------------------------------------------------------------------------
# ObsWebsocketWatcher
# --------------------------------------------------------------------------

def _stopped_event(path):
    return types.SimpleNamespace(
        output_state=oi.OBS_WEBSOCKET_OUTPUT_STOPPED,
        output_path=str(path),
    )


def test_obs_websocket_record_stopped_fires_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    rec = tmp_path / "obs_rec.mp4"
    rec.write_bytes(b"video")

    received: list[str] = []
    done = threading.Event()

    def cb(path):
        received.append(path)
        done.set()

    w = oi.ObsWebsocketWatcher("localhost", 4455, "pw", cb, stop_event="record")
    w.on_record_state_changed(_stopped_event(rec))
    assert done.wait(timeout=5), "callback did not fire"
    assert received == [str(rec)]
    w.stop()


def test_record_trigger_dispatches_recording_and_stream_lifecycle(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    rec = tmp_path / "obs_primary.mkv"
    rec.write_bytes(b"video")
    recording_paths: list[str] = []
    callback_order: list[str] = []
    recorded = threading.Event()
    stream_started = threading.Event()
    stream_finished = threading.Event()

    def on_recorded(path):
        callback_order.append("stable")
        recording_paths.append(path)
        recorded.set()

    def on_recording_stopped(_path):
        callback_order.append("stopped")

    w = oi.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        on_recorded,
        stop_event="record",
        on_recording_stopped=on_recording_stopped,
        on_stream_started=stream_started.set,
        on_stream_finished=stream_finished.set,
    )

    w.on_stream_state_changed(
        types.SimpleNamespace(output_state=oi.OBS_WEBSOCKET_OUTPUT_STARTED)
    )
    w.on_record_state_changed(_stopped_event(rec))
    w.on_stream_state_changed(
        types.SimpleNamespace(output_state=oi.OBS_WEBSOCKET_OUTPUT_STOPPED)
    )

    assert stream_started.wait(timeout=5)
    assert recorded.wait(timeout=5)
    assert stream_finished.wait(timeout=5)
    assert recording_paths == [str(rec)]
    assert callback_order == ["stopped", "stable"]
    w.stop()


def test_obs_websocket_stream_ignores_recording_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    rec = tmp_path / "obs_stream.mp4"
    rec.write_bytes(b"video")

    recording_paths: list[str] = []
    stream_finished = threading.Event()
    w = oi.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        recording_paths.append,
        stop_event="stream",
        on_stream_finished=stream_finished.set,
    )
    w.on_record_state_changed(_stopped_event(rec))
    assert recording_paths == []
    assert not stream_finished.is_set()

    w.on_stream_state_changed(
        types.SimpleNamespace(output_state=oi.OBS_WEBSOCKET_OUTPUT_STOPPED)
    )
    assert stream_finished.wait(timeout=5)
    assert recording_paths == []
    w.stop()


def test_obs_websocket_stream_callbacks_work_without_recording(monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    started = threading.Event()
    finished = threading.Event()
    finish_calls = []

    def on_stream_finished():
        finish_calls.append(True)
        finished.set()

    w = oi.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        lambda _path: None,
        stop_event="stream",
        on_stream_started=started.set,
        on_stream_finished=on_stream_finished,
    )
    w.on_stream_state_changed(
        types.SimpleNamespace(output_state="OBS_WEBSOCKET_OUTPUT_STARTED")
    )
    assert started.wait(timeout=5), "stream-start callback did not fire"

    w.on_stream_state_changed(
        types.SimpleNamespace(output_state=oi.OBS_WEBSOCKET_OUTPUT_STOPPED)
    )
    assert finished.wait(timeout=5), "stream-finished callback did not fire"
    assert finish_calls == [True]
    w.stop()


def test_stream_lifecycle_callbacks_preserve_obs_event_order(monkeypatch):
    order = []
    deferred_workers = []
    w = oi.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        lambda _path: None,
        stop_event="stream",
        on_stream_started=lambda: order.append("start"),
        on_stream_finished=lambda: order.append("stop"),
    )
    monkeypatch.setattr(w, "_spawn_worker", deferred_workers.append)

    w.on_stream_state_changed(
        types.SimpleNamespace(output_state=oi.OBS_WEBSOCKET_OUTPUT_STARTED)
    )
    w.on_stream_state_changed(
        types.SimpleNamespace(output_state=oi.OBS_WEBSOCKET_OUTPUT_STOPPED)
    )

    assert order == ["start", "stop"]
    assert deferred_workers == []
    w.stop()


def test_duplicate_stream_started_events_dispatch_once():
    starts = []
    w = oi.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        lambda _path: None,
        stop_event="stream",
        on_stream_started=lambda: starts.append("started"),
    )
    event = types.SimpleNamespace(
        output_state=oi.OBS_WEBSOCKET_OUTPUT_STARTED,
    )

    w.on_stream_state_changed(event)
    w.on_stream_state_changed(event)

    assert starts == ["started"]
    assert w.stream_active is True
    w.stop()


def test_obs_websocket_ignores_non_stopped_state(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    rec = tmp_path / "obs_running.mp4"
    rec.write_bytes(b"video")

    received: list[str] = []
    w = oi.ObsWebsocketWatcher("localhost", 4455, "pw", received.append, stop_event="record")
    w.on_record_state_changed(
        types.SimpleNamespace(
            output_state="OBS_WEBSOCKET_OUTPUT_STARTED",
            output_path=str(rec),
        )
    )
    assert received == []
    w.stop()


def test_obs_websocket_start_registers_callbacks(monkeypatch):
    fake = types.ModuleType("obsws_python")

    class _Callback:
        def __init__(self):
            self.registered = []

        def register(self, fn):
            self.registered.append(fn)

    class _EventClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.callback = _Callback()

        def unsubscribe(self):
            pass

        def disconnect(self):
            pass

    fake.EventClient = _EventClient
    fake.ReqClient = _EventClient
    monkeypatch.setitem(sys.modules, "obsws_python", fake)

    w = oi.ObsWebsocketWatcher("localhost", 4455, "pw", lambda p: None, stop_event="stream")
    w.start()
    assert w.status.startswith("connected")
    assert w._client is not None
    assert w._client.callback.registered == [w.on_record_state_changed, w.on_stream_state_changed]
    w.stop()
    assert w.status == "stopped"


def test_obs_websocket_start_detects_an_already_active_stream(monkeypatch):
    fake = types.ModuleType("obsws_python")
    starts = []
    request_clients = []

    class _Callback:
        def register(self, _fn):
            pass

    class _EventClient:
        def __init__(self, **_kwargs):
            self.callback = _Callback()

        def disconnect(self):
            pass

    class _ReqClient:
        def __init__(self, **_kwargs):
            self.disconnected = False
            request_clients.append(self)

        def get_stream_status(self):
            return types.SimpleNamespace(output_active=True)

        def disconnect(self):
            self.disconnected = True

    fake.EventClient = _EventClient
    fake.ReqClient = _ReqClient
    monkeypatch.setitem(sys.modules, "obsws_python", fake)

    w = oi.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        lambda _path: None,
        stop_event="stream",
        on_stream_started=lambda: starts.append("started"),
    )
    w.start()

    assert starts == ["started"]
    assert w.stream_status_checked is True
    assert w.stream_active is True
    assert request_clients[0].disconnected is True
    w.stop()


def test_obs_websocket_start_does_not_report_an_inactive_stream(monkeypatch):
    fake = types.ModuleType("obsws_python")
    starts = []

    class _Callback:
        def register(self, _fn):
            pass

    class _EventClient:
        def __init__(self, **_kwargs):
            self.callback = _Callback()

        def disconnect(self):
            pass

    class _ReqClient:
        def __init__(self, **_kwargs):
            pass

        def get_stream_status(self):
            return types.SimpleNamespace(output_active=False)

        def disconnect(self):
            pass

    fake.EventClient = _EventClient
    fake.ReqClient = _ReqClient
    monkeypatch.setitem(sys.modules, "obsws_python", fake)

    w = oi.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        lambda _path: None,
        stop_event="stream",
        on_stream_started=lambda: starts.append("started"),
    )
    w.start()

    assert starts == []
    assert w.stream_status_checked is True
    assert w.stream_active is False
    w.stop()


def test_obs_websocket_start_missing_dep(monkeypatch):
    monkeypatch.setitem(sys.modules, "obsws_python", None)
    w = oi.ObsWebsocketWatcher("localhost", 4455, "pw", lambda p: None)
    w.start()
    assert "error" in w.status
    w.stop()
    assert w.status == "stopped"


def test_obs_websocket_start_connection_failure(monkeypatch):
    fake = types.ModuleType("obsws_python")

    class _Callback:
        def register(self, fn):
            pass

    class _EventClient:
        def __init__(self, **kwargs):
            raise ConnectionRefusedError("no OBS running")

    fake.EventClient = _EventClient
    fake.ReqClient = _EventClient
    monkeypatch.setitem(sys.modules, "obsws_python", fake)

    w = oi.ObsWebsocketWatcher("localhost", 4455, "pw", lambda p: None)
    w.start()
    assert "接続失敗" in w.status
    assert w._client is None
    w.stop()


# --------------------------------------------------------------------------
# create_watcher factory
# --------------------------------------------------------------------------

def test_create_watcher_websocket():
    w = oi.create_watcher(
        "websocket",
        {"host": "h", "port": 4455, "password": "p", "stop_event": "record"},
        lambda p: None,
    )
    assert isinstance(w, oi.ObsWebsocketWatcher)


def test_websocket_defaults_to_record_trigger_for_empty_stop_event():
    w = oi.create_watcher(
        "websocket",
        {"stop_event": ""},
        lambda _path: None,
    )

    assert w._trigger == "record"


def test_create_watcher_passes_optional_stream_callbacks():
    started = lambda: None
    finished = lambda: None
    w = oi.create_watcher(
        "websocket",
        {"stop_event": "stream"},
        lambda _path: None,
        on_stream_started=started,
        on_stream_finished=finished,
    )
    assert w._stream_started_callback is started
    assert w._stream_finished_callback is finished


def test_create_watcher_folder():
    w = oi.create_watcher("folder", {"watch_folder": "/tmp"}, lambda p: None)
    assert isinstance(w, oi.FolderWatcher)


def test_create_watcher_unknown_raises():
    with pytest.raises(ValueError):
        oi.create_watcher("bogus", {}, lambda p: None)


def test_create_watcher_folder_reads_watch_dir_alias():
    w = oi.create_watcher("folder", {"watch_dir": "/tmp"}, lambda p: None)
    assert isinstance(w, oi.FolderWatcher)


# --------------------------------------------------------------------------
# Stopped watcher must not fire the callback (stop-flag guard)
# --------------------------------------------------------------------------

def test_obs_websocket_stopped_does_not_fire_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    rec = tmp_path / "obs_stopped.mp4"
    rec.write_bytes(b"video")

    received: list[str] = []
    w = oi.ObsWebsocketWatcher("localhost", 4455, "pw", received.append, stop_event="record")
    w.stop()  # marks _stopped = True before any event arrives
    w.on_record_state_changed(_stopped_event(rec))

    # Join the dispatched worker so the assertion is deterministic.
    with w._workers_lock:
        workers = list(w._workers)
    for wt in workers:
        wt.join(timeout=5)
    assert received == [], "stopped watcher must not fire the callback"


def test_folder_watcher_stopped_does_not_fire_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    mp4 = tmp_path / "rec.mp4"
    mp4.write_bytes(b"video")

    received: list[str] = []
    w = oi.FolderWatcher(tmp_path, received.append)
    w.stop()  # marks _stopped = True before any event arrives
    w._handle_event(str(mp4))

    # Join the dispatched worker so the assertion is deterministic.
    with w._workers_lock:
        workers = list(w._workers)
    for wt in workers:
        wt.join(timeout=5)
    assert received == [], "stopped watcher must not fire the callback"
