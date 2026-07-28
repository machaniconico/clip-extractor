"""Startup regression tests for optional automatic OBS integration."""

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("gradio")

import web_app


@pytest.fixture(autouse=True)
def _reset_auto_connect_state():
    web_app._obs_auto_connect_cancel.clear()
    yield
    web_app._obs_auto_connect_cancel.set()


def _write_settings(tmp_path: Path, **overrides) -> Path:
    settings = {
        "obs_auto_connect_on_startup": True,
        "obs_trigger_method": "websocket",
        "obs_host": "localhost",
        "obs_port": 4455,
        "obs_stop_event": "record",
        "obs_watch_folder": "",
        "obs_auto_process": False,
        "auto_append_youtube": False,
        "num_clips": 7,
        "output_mode": "individual",
        "generate_shorts": True,
        "ai_provider": "gemini",
        "whisper_model": "large-v3",
        "output_base_dir": "C:/clips",
    }
    settings.update(overrides)
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps(settings, ensure_ascii=False),
        encoding="utf-8",
    )
    return settings_file


def _isolate_settings(monkeypatch, tmp_path, **overrides):
    settings_file = _write_settings(tmp_path, **overrides)
    password_file = tmp_path / ".obs_password"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", password_file)
    return password_file


def test_auto_connect_off_is_a_noop(monkeypatch, tmp_path):
    _isolate_settings(
        monkeypatch,
        tmp_path,
        obs_auto_connect_on_startup=False,
    )
    monkeypatch.setattr(
        web_app,
        "_wait_for_obs_websocket",
        lambda *_args, **_kwargs: pytest.fail("must not wait while disabled"),
    )
    monkeypatch.setattr(
        web_app,
        "start_obs_watch",
        lambda *_args, **_kwargs: pytest.fail("must not connect while disabled"),
    )

    assert web_app.start_obs_watch_from_defaults() is None


def test_auto_connect_waits_then_starts_with_saved_defaults(monkeypatch, tmp_path):
    password_file = _isolate_settings(
        monkeypatch,
        tmp_path,
        obs_host="obs-host",
        obs_port=4456,
        obs_watch_folder="C:/recordings",
    )
    password_file.write_text("secret-pw", encoding="utf-8")
    waits = []
    calls = []

    def fake_wait(host, port, *, timeout, retry_interval):
        waits.append((host, port, timeout, retry_interval))
        return True

    def fake_start(**kwargs):
        calls.append(kwargs)
        return "connected"

    monkeypatch.setattr(web_app, "_wait_for_obs_websocket", fake_wait)
    monkeypatch.setattr(web_app, "_start_obs_watch_impl", fake_start)

    result = web_app.start_obs_watch_from_defaults(
        wait_timeout=12.0,
        retry_interval=0.25,
    )

    assert result == "connected"
    assert waits == [("obs-host", 4456, 12.0, 0.25)]
    assert calls == [{
        "method": "websocket",
        "host": "obs-host",
        "port": 4456,
        "password": "",
        "save_password": True,
        "stop_event": "record",
        "watch_folder": "C:/recordings",
        "auto_process": False,
        "auto_append_youtube": False,
        "num_clips": 7,
        "output_mode": "individual",
        "generate_shorts": True,
        "ai_provider": "gemini",
        "whisper_model": "large-v3",
        "output_base_dir": "C:/clips",
    }]


def test_auto_connect_timeout_is_nonfatal(monkeypatch, tmp_path):
    _isolate_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(
        web_app,
        "_wait_for_obs_websocket",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        web_app,
        "_start_obs_watch_impl",
        lambda *_args, **_kwargs: pytest.fail("must not connect before ready"),
    )

    result = web_app.start_obs_watch_from_defaults(
        wait_timeout=0.1,
        retry_interval=0.01,
    )

    assert "OBS WebSocket" in result
    assert "0.1秒" in result
    assert "手動" in result


def test_websocket_readiness_retries_until_port_accepts(monkeypatch):
    calls = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_create_connection(address, timeout):
        calls.append((address, timeout))
        if len(calls) < 3:
            raise ConnectionRefusedError("starting")
        return FakeConnection()

    monkeypatch.setattr(
        web_app.socket,
        "create_connection",
        fake_create_connection,
    )

    assert web_app._wait_for_obs_websocket(
        "localhost",
        4455,
        timeout=0.3,
        retry_interval=0.01,
    ) is True
    assert len(calls) == 3
    assert all(address == ("localhost", 4455) for address, _timeout in calls)


def test_folder_auto_connect_does_not_wait_for_websocket(monkeypatch, tmp_path):
    _isolate_settings(
        monkeypatch,
        tmp_path,
        obs_trigger_method="folder",
        obs_watch_folder="C:/recordings",
    )
    calls = []
    monkeypatch.setattr(
        web_app,
        "_wait_for_obs_websocket",
        lambda *_args, **_kwargs: pytest.fail("folder mode must not use TCP wait"),
    )
    monkeypatch.setattr(
        web_app,
        "_start_obs_watch_impl",
        lambda **kwargs: calls.append(kwargs) or "監視中",
    )

    result = web_app.start_obs_watch_from_defaults()

    assert result == "監視中"
    assert calls[0]["method"] == "folder"


def test_scheduler_is_daemon_nonblocking_and_deduplicated(monkeypatch, tmp_path):
    _isolate_settings(monkeypatch, tmp_path)
    started = threading.Event()
    release = threading.Event()

    def fake_auto_start(**_kwargs):
        started.set()
        release.wait(timeout=2)
        return "connected"

    monkeypatch.setattr(web_app, "start_obs_watch_from_defaults", fake_auto_start)

    first = web_app.schedule_obs_auto_connect()
    assert first is not None
    assert started.wait(timeout=1)
    second = web_app.schedule_obs_auto_connect()

    assert second is first
    assert first.daemon is True
    assert first.name == "obs-auto-connect"

    release.set()
    first.join(timeout=1)
    assert first.is_alive() is False


def test_manual_stop_cancels_pending_startup_wait(monkeypatch, tmp_path):
    _isolate_settings(monkeypatch, tmp_path)
    waiting = threading.Event()

    def fake_wait(*_args, **_kwargs):
        waiting.set()
        web_app._obs_auto_connect_cancel.wait(timeout=2)
        return False

    monkeypatch.setattr(web_app, "_wait_for_obs_websocket", fake_wait)

    thread = web_app.schedule_obs_auto_connect(wait_timeout=5)
    assert thread is not None
    assert waiting.wait(timeout=1)

    web_app.stop_obs_watch()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert "キャンセル" in web_app._obs_status_text()


def test_scheduler_thread_failure_does_not_break_app_startup(monkeypatch, tmp_path):
    _isolate_settings(monkeypatch, tmp_path)

    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(web_app.threading, "Thread", FailingThread)

    assert web_app.schedule_obs_auto_connect() is None
    assert "予約できません" in web_app._obs_status_text()
