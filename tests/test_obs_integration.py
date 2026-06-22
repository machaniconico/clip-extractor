"""Tests for obs_integration — run without OBS / without the optional deps.

obsws-python and watchdog are NOT installed in CI, so the module must import
cleanly (lazy imports) and the watchers must report missing deps via
``status`` rather than raising. Behavioural tests drive the event handlers /
file-stability helper directly and stub the third-party libs where needed.
"""

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


def test_obs_websocket_stream_uses_cached_record_path(tmp_path, monkeypatch):
    monkeypatch.setattr(oi.time, "sleep", lambda *a, **k: None)
    rec = tmp_path / "obs_stream.mp4"
    rec.write_bytes(b"video")

    received: list[str] = []
    done = threading.Event()

    def cb(path):
        received.append(path)
        done.set()

    w = oi.ObsWebsocketWatcher("localhost", 4455, "pw", cb, stop_event="stream")
    # Recording stops first → caches the path, but does NOT fire (trigger=stream).
    w.on_record_state_changed(_stopped_event(rec))
    assert received == [], "record event must not fire when trigger=stream"
    # Stream stops → fires using the cached recording path.
    w.on_stream_state_changed(
        types.SimpleNamespace(output_state=oi.OBS_WEBSOCKET_OUTPUT_STOPPED)
    )
    assert done.wait(timeout=5), "callback did not fire on stream stop"
    assert received == [str(rec)]
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


def test_create_watcher_folder():
    w = oi.create_watcher("folder", {"watch_folder": "/tmp"}, lambda p: None)
    assert isinstance(w, oi.FolderWatcher)


def test_create_watcher_unknown_raises():
    with pytest.raises(ValueError):
        oi.create_watcher("bogus", {}, lambda p: None)


def test_create_watcher_folder_reads_watch_dir_alias():
    w = oi.create_watcher("folder", {"watch_dir": "/tmp"}, lambda p: None)
    assert isinstance(w, oi.FolderWatcher)
