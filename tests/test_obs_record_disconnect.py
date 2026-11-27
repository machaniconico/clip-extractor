"""録画中のファイル特定と、OBS切断時の録画救済を検証する。"""

import os
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import obs_integration as oi


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.wall_start = 1_800_000_000.0
        self.actions = {}
        self.limit = float("inf")
        self.stopped = False

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall_start + self.now

    def wait(self, timeout):
        end = self.now + timeout
        for instant in sorted(self.actions):
            if instant <= min(end, self.limit):
                self.now = instant
                self.actions.pop(instant)()
                if self.stopped:
                    return True
        self.now = min(end, self.limit)
        return self.stopped or self.now >= self.limit

    def is_set(self):
        return self.stopped or self.now >= self.limit

    def set(self):
        self.stopped = True


class _Obs:
    def __init__(self, directory):
        self.active = False
        self.paused = False
        self.duration = None
        self.reachable = True
        self.connect_error = None
        self.clients = []
        self.status_queries = []
        self.directory_queries = 0
        self.on_probe = None
        self.directory = directory

    def EventClient(self, **_kwargs):
        if self.connect_error is not None:
            raise self.connect_error
        ws = SimpleNamespace(connected=True)

        def disconnect():
            ws.connected = False

        client = SimpleNamespace(
            base_client=SimpleNamespace(ws=ws),
            callback=SimpleNamespace(register=lambda _fn: None),
            disconnect=disconnect,
        )
        self.clients.append(client)
        return client

    def ReqClient(self, **_kwargs):
        return SimpleNamespace(
            get_record_status=self.get_record_status,
            get_record_directory=self.get_record_directory,
        )

    def get_record_status(self):
        self.status_queries.append(self.active)
        if self.on_probe is not None:
            self.on_probe()
        return {
            "outputActive": self.active,
            "outputPaused": self.paused,
            "outputDuration": self.duration,
        }

    def get_record_directory(self):
        self.directory_queries += 1
        return SimpleNamespace(record_directory=str(self.directory))


class _Scenario:
    def __init__(self, monkeypatch, directory):
        self.clock = _Clock()
        self.obs = _Obs(directory)
        self.directory = directory
        self.workers = deque()
        self.calls = []
        self.stopped_at = []
        self.finished = []
        self.stability = lambda _path: True
        self.watcher = oi.ObsWebsocketWatcher(
            "localhost", 4455, "", self.on_finished,
            on_recording_stopped=self.on_stopped, auto_reconnect=False,
        )
        monkeypatch.setitem(sys.modules, "obsws_python", self.obs)
        monkeypatch.setattr(oi, "time", self.clock)
        monkeypatch.setattr(oi, "_RECOVERY_GRACE_SECONDS", 5)
        monkeypatch.setattr(oi, "_RECORD_IDENTIFY_INTERVAL_SECONDS", 2)
        monkeypatch.setattr(oi, "_RECORD_IDENTIFY_SAMPLES", 4)
        monkeypatch.setattr(oi, "_RECOVERY_STABLE_ATTEMPTS", 2)
        monkeypatch.setattr(oi, "wait_until_file_stable", self.check_stability)
        monkeypatch.setattr(self.watcher, "_stop_event", self.clock)
        monkeypatch.setattr(self.watcher, "_spawn_worker", self.workers.append)
        monkeypatch.setattr(self.watcher, "_is_endpoint_reachable", lambda: self.obs.reachable)

    def on_finished(self, path):
        assert not self.watcher._record_state_lock.locked()
        self.calls.append(("finished", path))
        self.finished.append(path)

    def on_stopped(self, path):
        assert not self.watcher._record_state_lock.locked()
        self.calls.append(("stopped", path))
        self.stopped_at.append(self.clock.now)

    def check_stability(self, path, *, checks=None):
        self.calls.append(("stability", path))
        return self.stability(path)

    def assert_no_calls(self):
        assert self.calls == []

    def file(self, name, *, mtime=None):
        path = self.directory / name
        path.write_bytes(b"video")
        timestamp = self.clock.time() if mtime is None else mtime
        os.utime(path, (timestamp, timestamp))
        return str(path)

    def grow(self, path, *, mtime=None):
        with open(path, "ab") as stream:
            stream.write(b" more video")
        timestamp = self.clock.time() if mtime is None else mtime
        os.utime(path, (timestamp, timestamp))

    def started(self, path=None):
        self.obs.active = True
        self.obs.paused = False
        self.watcher.on_record_state_changed({
            "outputState": oi.OBS_WEBSOCKET_OUTPUT_STARTED, "outputPath": path,
        })

    def stopped(self, path):
        self.obs.active = False
        self.obs.paused = False
        self.watcher.on_record_state_changed(SimpleNamespace(
            output_state=oi.OBS_WEBSOCKET_OUTPUT_STOPPED, output_path=path,
        ))

    def file_changed(self, path):
        self.watcher.on_record_file_changed({"newOutputPath": path})

    def resumed(self):
        self.obs.paused = False
        self.watcher.on_record_state_changed({
            "outputState": oi.OBS_WEBSOCKET_OUTPUT_RESUMED,
        })

    def disconnect(self):
        self.obs.clients[-1].disconnect()
        self.obs.reachable = False

    def reconnect(self, *, active, duration=None, paused=False):
        self.obs.reachable = True
        self.obs.active = active
        self.obs.duration = duration
        self.obs.paused = paused

    def run_monitor(self, seconds):
        self.clock.limit = self.clock.now + seconds
        try:
            self.watcher._monitor_connection(self.obs)
        finally:
            self.clock.limit = float("inf")

    def drain_workers(self):
        while self.workers:
            self.workers.popleft()()


@pytest.fixture
def scenario(monkeypatch, tmp_path):
    s = _Scenario(monkeypatch, tmp_path)
    yield s
    s.watcher.stop()


@pytest.mark.parametrize("mid_attach", [False, True])
def test_identifies_growing_file_during_recording(scenario, monkeypatch, mid_attach):
    s = scenario
    record = s.file("record.MKV")
    s.file("latest.mp4", mtime=s.clock.time() + 10)
    stale = s.file("stale.flv", mtime=s.clock.time() - 90)
    unsupported = s.file("unrelated.txt")

    # Windowsの作成時刻が現在でも、60秒前に始まった録画を除外しない。
    original_stat = Path.stat

    def stat_with_birth_time(path, **kwargs):
        result = original_stat(path, **kwargs)
        if str(path) == record:
            return SimpleNamespace(
                st_size=result.st_size, st_mtime=result.st_mtime,
                st_mode=result.st_mode, st_dev=result.st_dev, st_ino=result.st_ino,
                st_ctime=s.clock.time(), st_birthtime=s.clock.time(),
            )
        return result

    monkeypatch.setattr(Path, "stat", stat_with_birth_time)
    if mid_attach:
        s.obs.active = True
        s.obs.duration = 60_000
    s.watcher.start()
    if not mid_attach:
        s.started()
    assert s.obs.directory_queries == 0

    def grow_files():
        s.grow(record)
        s.grow(stale, mtime=s.clock.time() - 90)
        s.grow(unsupported)

    s.clock.actions[1] = grow_files
    s.drain_workers()
    assert s.obs.directory_queries == 1
    assert s.watcher._record_path == record

    s.disconnect()
    s.clock.actions[s.clock.now + 4] = lambda: s.file("copied-after-disconnect.mkv")
    s.run_monitor(7)
    s.drain_workers()
    assert s.finished == [record]


def test_unidentified_recording_never_scans_after_disconnect(scenario, caplog):
    s = scenario
    s.watcher.start()
    s.file("old.mp4")
    s.started()
    s.disconnect()
    s.clock.actions[4] = lambda: s.file("later-copy.mov")
    s.run_monitor(7)
    s.drain_workers()
    assert s.calls == []
    assert "録画中にファイルを特定できなかった" in caplog.text


@pytest.mark.parametrize("returns", [2, 4])
def test_blip_prioritizes_reconnect_even_at_first_poll_past_grace(scenario, returns):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()
    s.clock.actions[returns] = lambda: s.reconnect(active=True, duration=60_000)
    s.run_monitor(8)
    assert len(s.obs.clients) == 2
    assert s.watcher._record_active
    assert s.watcher._recovery_pending is None
    assert s.calls == []
    s.stopped(path)
    s.drain_workers()
    assert s.calls == [("stopped", path), ("stability", path), ("finished", path)]


def test_obs_quits_then_late_stopped_for_same_normalized_key(scenario):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()
    s.clock.actions[4] = s.assert_no_calls
    s.run_monitor(7)
    s.drain_workers()
    assert s.stopped_at == [6]
    assert s.calls == [("stopped", path), ("stability", path), ("finished", path)]
    s.stopped(f"{s.directory}/./record.mkv")
    s.drain_workers()
    assert s.finished == [path]
    assert s.stopped_at == [6]

    s.started(path)
    s.stopped(path)
    s.drain_workers()
    assert s.finished == [path, path]


def test_paused_recording_survives_blip(scenario):
    s = scenario
    a = s.file("a.mkv")
    s.watcher.start()
    s.started(a)
    session = s.watcher._record_session
    s.disconnect()
    s.clock.actions[2] = lambda: s.reconnect(active=True, paused=True, duration=500)
    s.run_monitor(4)
    s.drain_workers()
    assert s.calls == []
    assert s.obs.directory_queries == 0
    assert s.watcher._recovery_pending is None
    assert s.watcher._record_session == session
    s.grow(a)
    s.stopped(a)
    s.drain_workers()
    assert s.finished == [a]


def test_paused_recording_processed_once_after_resume(scenario):
    s = scenario
    a = s.file("a.mkv")
    s.watcher.start()
    s.started(a)
    session = s.watcher._record_session
    s.disconnect()
    s.clock.actions[2] = lambda: s.reconnect(active=True, paused=True, duration=500)
    s.run_monitor(4)
    s.resumed()
    s.clock.actions[s.clock.now + 1] = lambda: s.grow(a)
    s.drain_workers()
    assert s.watcher._record_session == session
    assert s.watcher._record_path == a
    assert s.calls == []
    s.stopped(a)
    s.drain_workers()
    assert s.finished == [a]
    assert len(s.stopped_at) == 1


def test_resume_restarts_identification_for_unknown_path(scenario):
    s = scenario
    a = s.file("a.mkv")
    s.watcher.start()
    s.started()
    session = s.watcher._record_session
    s.disconnect()
    s.clock.actions[2] = lambda: s.reconnect(active=True, paused=True, duration=500)
    s.run_monitor(4)
    s.drain_workers()
    assert s.obs.directory_queries == 0
    assert s.watcher._record_path is None
    s.resumed()
    s.clock.actions[s.clock.now + 1] = lambda: s.grow(a)
    s.drain_workers()
    assert s.obs.directory_queries == 1
    assert s.watcher._record_path == a
    assert s.watcher._record_session == session
    assert s.calls == []


def test_reconnect_idle_dispatches_before_grace(scenario):
    s = scenario
    path = s.file("record.mp4")
    s.watcher.start()
    s.started(path)
    s.disconnect()
    s.clock.actions[2] = lambda: s.reconnect(active=False)
    s.run_monitor(4)
    s.drain_workers()
    assert s.stopped_at == [3]
    assert s.finished == [path]


@pytest.mark.parametrize("started_event", [False, True])
def test_new_recording_after_outage_has_its_own_session(scenario, started_event):
    s = scenario
    a = s.file("a.mkv")
    b = s.file("b.mkv")
    s.watcher.start()
    s.started(a)
    original_session = s.watcher._record_session
    s.disconnect()

    def return_with_new_recording():
        s.reconnect(active=True, duration=60_000)
        if started_event:
            s.obs.on_probe = lambda: s.started(b)

    s.clock.actions[2] = return_with_new_recording
    s.run_monitor(4)
    if not started_event:
        assert s.calls == []
        assert s.watcher._record_session == original_session
        s.clock.actions[5] = lambda: s.grow(b)
    s.drain_workers()
    assert s.finished == [a]
    assert s.watcher._record_session == original_session + 1
    assert s.watcher._record_path == b
    s.stopped(b)
    s.drain_workers()
    assert s.finished == [a, b]


def test_file_changed_during_probe_updates_continuing_session(scenario):
    s = scenario
    a = s.file("first-part.mkv")
    b = s.file("second-part.mkv")
    s.watcher.start()
    s.started(a)
    session = s.watcher._record_session
    s.disconnect()

    def return_during_same_recording():
        s.reconnect(active=True, duration=500)
        s.obs.on_probe = lambda: s.file_changed(b)

    s.clock.actions[2] = return_during_same_recording
    s.run_monitor(4)
    assert s.watcher._recovery_pending is None
    assert s.watcher._record_session == session
    assert s.watcher._record_path == b
    assert s.calls == []
    s.obs.on_probe = None
    s.disconnect()
    s.run_monitor(7)
    s.drain_workers()
    assert s.finished == [b]


@pytest.mark.parametrize("offline_attempted", [False, True])
def test_permanent_reconnect_failure_does_not_process_pending(scenario, caplog, offline_attempted):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()
    if offline_attempted:
        s.run_monitor(7)
    s.obs.connect_error = RuntimeError("authentication failed")
    s.reconnect(active=False)
    s.run_monitor(4)
    s.drain_workers()
    assert s.finished == []
    assert s.watcher._recovery_pending is None
    assert not s.watcher.reconnecting
    assert "録画の終了を確認できなかったため、自動処理しません" in caplog.text


@pytest.mark.parametrize("active_on_success", [False, True])
def test_failed_probe_retries_on_healthy_client_past_grace(scenario, active_on_success):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()

    def fail_until_after_grace():
        if s.clock.now < 6:
            raise RuntimeError("応答なし")

    def return_with_failed_probe():
        s.reconnect(active=active_on_success, duration=60_000)
        s.obs.on_probe = fail_until_after_grace

    s.clock.actions[2] = return_with_failed_probe
    s.clock.actions[5.5] = s.assert_no_calls
    s.run_monitor(7)
    s.drain_workers()
    assert len(s.obs.clients) == 2
    assert len(s.obs.status_queries) == 5
    assert s.watcher._recovery_pending is None
    if active_on_success:
        assert s.calls == []
        s.stopped(path)
        s.drain_workers()
    else:
        assert s.stopped_at == [6]
    assert s.finished == [path]


def test_failed_probe_checks_deadline_if_endpoint_becomes_unreachable(scenario):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()

    def fail():
        raise RuntimeError("応答なし")

    def return_with_failed_probe():
        s.reconnect(active=True)
        s.obs.on_probe = fail

    s.clock.actions[2] = return_with_failed_probe
    s.clock.actions[4.5] = lambda: setattr(s.obs, "reachable", False)
    s.run_monitor(7)
    s.drain_workers()
    assert s.stopped_at == [5]
    assert s.finished == [path]


def test_unstable_recovery_does_not_consume_real_stopped_event(scenario):
    s = scenario
    path = s.file("record.mov")
    s.watcher.start()
    s.started(path)
    s.disconnect()
    s.stability = lambda _path: False
    s.run_monitor(7)
    s.drain_workers()
    assert s.calls == [("stopped", path), ("stability", path), ("stability", path)]
    assert not s.watcher._handled_record_keys

    s.stability = lambda _path: True
    s.stopped(path)
    s.drain_workers()
    assert s.calls[-2:] == [("stability", path), ("finished", path)]
    assert len(s.stopped_at) == 1
    assert s.finished == [path]


def test_active_reconnect_while_waiting_for_stability_abandons_recovery(scenario):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()
    s.run_monitor(7)
    s.reconnect(active=True, duration=60_000)
    s.drain_workers()
    assert s.finished == []
    s.run_monitor(4)
    assert s.watcher._record_active
    s.stopped(path)
    s.drain_workers()
    assert s.finished == [path]
    assert len(s.stopped_at) == 1


def test_stopped_during_probe_invalidates_stale_response(scenario):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()

    def return_with_stopped_event():
        s.reconnect(active=True, duration=60_000)

        def emit_stopped_before_reply():
            s.stopped(path)
            s.obs.active = True

        s.obs.on_probe = emit_stopped_before_reply

    s.clock.actions[2] = return_with_stopped_event
    s.run_monitor(4)
    s.drain_workers()
    assert not s.watcher._record_active
    assert s.watcher._recovery_pending is None
    assert s.finished == [path]


def test_stop_cancels_pending_and_late_events_cannot_rearm(scenario):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()
    s.clock.actions[4] = s.watcher.stop
    s.run_monitor(7)
    seq = s.watcher._record_seq
    s.started(path)
    s.file_changed(path)
    s.stopped(path)
    s.drain_workers()
    assert s.watcher._record_seq == seq
    assert s.watcher._recovery_pending is None
    assert not s.watcher._record_active
    assert s.watcher._record_started_at is None
    assert s.watcher._record_path is None
    assert s.calls == []


def test_stop_during_stability_prevents_finished_callback(scenario):
    s = scenario
    path = s.file("record.mkv")
    s.watcher.start()
    s.started(path)
    s.disconnect()
    s.run_monitor(7)

    def stop_during_check(_path):
        s.watcher.stop()
        return True

    s.stability = stop_during_check
    s.drain_workers()
    assert s.calls == [("stopped", path), ("stability", path)]
    assert not s.watcher._handled_record_keys


@pytest.mark.parametrize("paused", [True, False])
def test_recording_finished_offline_is_processed_when_next_recording_stops(scenario, paused):
    """切断中に録画Aが終わりBが始まった場合、BのSTOPPEDでAも取りこぼさない。"""
    s = scenario
    a = s.file("a.mkv")
    s.watcher.start()
    s.started(a)
    s.disconnect()
    b = s.file("b.mkv")
    s.clock.actions[2] = lambda: s.reconnect(active=True, duration=500, paused=paused)
    s.run_monitor(4)
    if not paused:
        s.clock.actions[s.clock.now + 1] = lambda: s.grow(b)
    s.drain_workers()
    s.stopped(b)
    s.drain_workers()
    assert sorted(s.finished) == sorted([a, b])


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_alias_path_stopped_does_not_process_recovered_recording_twice(scenario, tmp_path):
    """別名パス(シンボリックリンク)でSTOPPEDが届いても同じ録画を二重処理しない。"""
    s = scenario
    a = s.file("a.mkv")
    alias_dir = tmp_path / "alias"
    try:
        os.symlink(Path(a).parent, alias_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink not permitted")
    s.watcher.start()
    s.started(str(alias_dir / Path(a).name))
    s.disconnect()
    s.clock.actions[2] = lambda: s.reconnect(active=True, duration=500, paused=True)
    s.run_monitor(4)
    s.drain_workers()
    s.stopped(a)
    s.drain_workers()
    assert len(s.finished) == 1
