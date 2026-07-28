"""Regression tests for OBS connection settings persistence."""

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("gradio")

import obs_integration
import web_app

WEB_APP = Path(__file__).parent.parent / "web_app.py"


class _FakeWatcher:
    status = "connected"

    def start(self):
        pass

    def stop(self):
        self.status = "stopped"


def test_start_obs_watch_persists_password_for_next_launch(monkeypatch, tmp_path):
    settings_file = tmp_path / "default_settings.json"
    password_file = tmp_path / ".obs_password"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", password_file)
    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **_kwargs: _FakeWatcher(),
    )

    try:
        status = web_app.start_obs_watch(
            "websocket",
            "obs-host",
            4456,
            "secret-pw",
            True,
            "record",
            "C:/recordings",
            False,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
        )

        assert status == "connected"
        assert settings_file.exists()
        assert password_file.read_text(encoding="utf-8") == "secret-pw"
        assert "obs_password" not in json.loads(
            settings_file.read_text(encoding="utf-8")
        )
        reloaded = web_app.load_defaults()
        assert reloaded["obs_trigger_method"] == "websocket"
        assert reloaded["obs_host"] == "obs-host"
        assert reloaded["obs_port"] == 4456
        assert "obs_password" not in reloaded
        assert web_app.load_obs_password() == "secret-pw"
        assert reloaded["obs_stop_event"] == "record"
        assert reloaded["obs_watch_folder"] == "C:/recordings"
        assert reloaded["obs_auto_process"] is False
    finally:
        web_app.stop_obs_watch()


def test_general_defaults_save_preserves_saved_obs_password(monkeypatch, tmp_path):
    settings_file = tmp_path / "default_settings.json"
    password_file = tmp_path / ".obs_password"
    password_file.write_text("secret-pw", encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", password_file)

    web_app.save_defaults(
        "gemini",
        "gemini-2.5-flash",
        True,
        True,
        "",
        "",
        False,
        5,
        "combined",
        False,
        "crop",
        "center",
        True,
        30,
        90,
        "large-v3",
        "ja",
        "Noto Sans JP",
        96,
        "#FFFFFF",
        "",
        False,
        False,
        0.35,
        False,
        "",
        False,
        "",
    )

    assert "obs_password" not in web_app.load_defaults()
    assert web_app.load_obs_password() == "secret-pw"
    assert "obs_password" not in json.loads(
        settings_file.read_text(encoding="utf-8")
    )


def test_blank_password_reuses_saved_secret_server_side(monkeypatch, tmp_path):
    settings_file = tmp_path / "default_settings.json"
    password_file = tmp_path / ".obs_password"
    password_file.write_text("secret-pw", encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", password_file)
    captured = {}

    def fake_create_watcher(_method, config, _callback, **_kwargs):
        captured.update(config)
        return _FakeWatcher()

    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        fake_create_watcher,
    )

    try:
        status = web_app.start_obs_watch(
            "websocket",
            "localhost",
            4455,
            "",
            True,
            "record",
            "",
            False,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
        )

        assert status == "connected"
        assert captured["password"] == "secret-pw"
        assert password_file.read_text(encoding="utf-8") == "secret-pw"
    finally:
        web_app.stop_obs_watch()


def test_unsaved_password_is_session_only_and_removes_saved_secret(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "default_settings.json"
    password_file = tmp_path / ".obs_password"
    password_file.write_text("old-secret", encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", password_file)
    captured = {}

    def fake_create_watcher(_method, config, _callback, **_kwargs):
        captured.update(config)
        return _FakeWatcher()

    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        fake_create_watcher,
    )

    try:
        status = web_app.start_obs_watch(
            "websocket",
            "localhost",
            4455,
            "one-time-secret",
            False,
            "record",
            "",
            False,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
        )

        assert status == "connected"
        assert captured["password"] == "one-time-secret"
        assert password_file.exists() is False
        assert "obs_password" not in json.loads(
            settings_file.read_text(encoding="utf-8")
        )
    finally:
        web_app.stop_obs_watch()


def test_unsaved_blank_password_does_not_reuse_saved_secret(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "default_settings.json"
    password_file = tmp_path / ".obs_password"
    password_file.write_text("old-secret", encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", password_file)
    captured = {}

    def fake_create_watcher(_method, config, _callback, **_kwargs):
        captured.update(config)
        return _FakeWatcher()

    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        fake_create_watcher,
    )

    try:
        status = web_app.start_obs_watch(
            "websocket",
            "localhost",
            4455,
            "",
            False,
            "record",
            "",
            False,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
        )

        assert status == "connected"
        assert captured["password"] == ""
        assert password_file.exists() is False
    finally:
        web_app.stop_obs_watch()


def test_unsaved_password_delete_failure_aborts_before_watcher_creation(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "default_settings.json"
    password_file = tmp_path / ".obs_password"
    password_file.write_text("old-secret", encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", password_file)
    create_called = False

    def fail_to_delete(password):
        assert password == ""
        raise OSError("access denied")

    def fake_create_watcher(*_args, **_kwargs):
        nonlocal create_called
        create_called = True
        return _FakeWatcher()

    monkeypatch.setattr(web_app, "_save_obs_password", fail_to_delete)
    monkeypatch.setattr(obs_integration, "create_watcher", fake_create_watcher)

    status = web_app.start_obs_watch(
        "websocket",
        "localhost",
        4455,
        "one-time-secret",
        False,
        "record",
        "",
        False,
        False,
        5,
        "combined",
        False,
        "gemini",
        "large-v3",
        "",
    )

    assert "保存に失敗" in status
    assert "access denied" in status
    assert create_called is False
    assert password_file.read_text(encoding="utf-8") == "old-secret"


def test_obs_password_is_never_rendered_as_a_textbox_initial_value():
    module = ast.parse(WEB_APP.read_text(encoding="utf-8"))

    for node in ast.walk(module):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "obs_password"
            for target in node.targets
        ):
            continue
        assert isinstance(node.value, ast.Call), ast.dump(node.value)
        keywords = {
            keyword.arg: keyword.value
            for keyword in node.value.keywords
        }
        assert isinstance(keywords["value"], ast.Constant)
        assert keywords["value"].value == ""
        assert isinstance(keywords["info"], ast.Constant)
        assert "保存済み" in keywords["info"].value
        assert "空欄" in keywords["info"].value
        break
    else:
        raise AssertionError("obs_password Textbox assignment not found")

    source = WEB_APP.read_text(encoding="utf-8")
    assert 'label="Passwordを保存"' in source
    assert "value=bool(load_obs_password())" in source
    assert 'elem_classes="obs-password-heading"' in source
    assert 'elem_classes="obs-password-save"' in source
    assert "保存済みPasswordを削除" not in source
    assert "obs_clear_password_btn" not in source
