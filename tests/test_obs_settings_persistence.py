"""Regression tests for OBS connection settings persistence."""

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_obs_render_settings_reload_latest_media_and_clear_stale_in_memory(
    monkeypatch,
):
    bgm_id = "user:bgm:" + ("a" * 64)
    stale_vfx_id = "user:vfx:" + ("b" * 64)
    monkeypatch.setattr(
        web_app,
        "load_defaults",
        lambda: {
            "bgm_asset_id": bgm_id,
            "bgm_user_folder": "D:/media/bgm",
            "se_asset_id": "se-missing-pack",
            "vfx_asset_id": stale_vfx_id,
            "vfx_user_folder": "D:/media/vfx",
            "vfx_automatic": True,
            "vfx_anchor": "bottom-right",
        },
    )

    def resolve(_folder, _asset_id, kind):
        if kind == "bgm":
            return SimpleNamespace(kind="bgm")
        raise web_app.UserMediaError("missing")

    monkeypatch.setattr(web_app, "resolve_user_media_asset", resolve)
    monkeypatch.setattr(web_app, "scan_optional_user_media", lambda *_args: ())
    monkeypatch.setattr(
        web_app,
        "get_installed_asset",
        lambda _asset_id: (_ for _ in ()).throw(
            web_app.AudioAssetError("pack missing")
        ),
    )
    statuses = []
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)

    refreshed = web_app._obs_settings_for_render(
        {
            "num_clips": 12,
            "bgm_asset_id": "old",
            web_app._OBS_RELOAD_MEDIA_DEFAULTS_KEY: True,
        }
    )

    assert refreshed["num_clips"] == 12
    assert refreshed["bgm_asset_id"] == bgm_id
    assert refreshed["se_asset_id"] == ""
    assert refreshed["vfx_asset_id"] == ""
    assert refreshed["vfx_automatic"] is True
    assert refreshed["vfx_anchor"] == "bottom-right"
    assert web_app._OBS_RELOAD_MEDIA_DEFAULTS_KEY not in refreshed
    assert any("SE素材" in message for message in statuses)
    assert any("VFX素材" in message for message in statuses)


def test_obs_render_settings_prefer_saved_obs_media_profile(monkeypatch):
    monkeypatch.setattr(
        web_app,
        "load_defaults",
        lambda: {
            "audio_delivery_mode": "both",
            "bgm_asset_id": "input-bgm",
            "obs_media": {
                "audio_delivery_mode": "mixed",
                "bgm_asset_id": "obs-bgm",
                "se_asset_id": "obs-se",
                "bgm_user_folder": "D:/obs/bgm",
                "se_user_folder": "D:/obs/se",
                "vfx_user_folder": "",
                "vfx_asset_id": "",
                "effect_preset": "none",
                "vfx_automatic": False,
                "vfx_cue_seconds": 0.0,
                "vfx_duration_seconds": 1.0,
                "vfx_anchor": "center",
                "vfx_scale_percent": 100.0,
                "vfx_opacity_percent": 100.0,
                "vfx_target": "both",
            },
        },
    )
    monkeypatch.setattr(
        web_app,
        "get_installed_asset",
        lambda asset_id: SimpleNamespace(
            kind="bgm" if asset_id == "obs-bgm" else "se"
        ),
    )
    monkeypatch.setattr(
        web_app,
        "resolve_user_media_asset",
        lambda *_args: SimpleNamespace(kind="se"),
    )

    refreshed = web_app._obs_settings_for_render(
        {web_app._OBS_RELOAD_MEDIA_DEFAULTS_KEY: True}
    )

    assert refreshed["audio_delivery_mode"] == "mixed"
    assert refreshed["bgm_asset_id"] == "obs-bgm"
    assert refreshed["se_asset_id"] == "obs-se"
    assert refreshed["bgm_user_folder"] == "D:/obs/bgm"


def test_obs_render_settings_auto_vfx_missing_folder_uses_builtin_effects(
    monkeypatch,
):
    monkeypatch.setattr(
        web_app,
        "load_defaults",
        lambda: {
            "vfx_user_folder": "D:/removed/vfx",
            "vfx_asset_id": "",
            "vfx_automatic": True,
            "effect_preset": "none",
        },
    )
    monkeypatch.setattr(
        web_app,
        "scan_optional_user_media",
        lambda *_args: (_ for _ in ()).throw(
            web_app.UserMediaError("folder was removed")
        ),
    )
    statuses = []
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)

    refreshed = web_app._obs_settings_for_render(
        {web_app._OBS_RELOAD_MEDIA_DEFAULTS_KEY: True}
    )

    assert refreshed["vfx_automatic"] is True
    assert refreshed["vfx_user_folder"] == ""
    assert refreshed["vfx_asset_id"] == ""
    assert any("内蔵エフェクトのみ" in message for message in statuses)


def test_obs_render_settings_without_reload_marker_preserve_direct_call(
    monkeypatch,
):
    monkeypatch.setattr(
        web_app,
        "load_defaults",
        lambda: pytest.fail("direct compatibility calls must not reload defaults"),
    )
    original = {"bgm_asset_id": "direct", "num_clips": 3}

    assert web_app._obs_settings_for_render(original) == original


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


def test_start_obs_watch_persists_media_controls_for_live_pipeline(
    monkeypatch, tmp_path
):
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
            "folder",
            "localhost",
            4455,
            "",
            False,
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
            obs_audio_delivery_mode="mixed",
            obs_bgm_asset_id="bgm-obs",
            obs_se_asset_id="se-obs",
            obs_bgm_gain_db=-20,
            obs_se_gain_db=-5,
            obs_se_cue_seconds=0.75,
            obs_bgm_user_folder="C:/OBS/BGM",
            obs_se_user_folder="C:/OBS/SE",
            obs_vfx_user_folder="C:/OBS/VFX",
            obs_vfx_asset_id="",
            obs_effect_preset="punch",
            obs_vfx_automatic=False,
            obs_vfx_cue_seconds=0.25,
            obs_vfx_duration_seconds=1.25,
            obs_vfx_anchor="bottom",
            obs_vfx_scale_percent=90,
            obs_vfx_opacity_percent=85,
            obs_vfx_target="both",
        )

        assert status == "connected"
        assert web_app.load_defaults()["obs_media"] == {
            "audio_delivery_mode": "mixed",
            "bgm_asset_id": "bgm-obs",
            "se_asset_id": "se-obs",
            "bgm_user_folder": "C:/OBS/BGM",
            "se_user_folder": "C:/OBS/SE",
            "bgm_gain_db": -20,
            "se_gain_db": -5,
            "se_cue_seconds": 0.75,
            "vfx_user_folder": "C:/OBS/VFX",
            "vfx_asset_id": "",
            "effect_preset": "punch",
            "vfx_automatic": False,
            "vfx_cue_seconds": 0.25,
            "vfx_duration_seconds": 1.25,
            "vfx_anchor": "bottom",
            "vfx_scale_percent": 90,
            "vfx_opacity_percent": 85,
            "vfx_target": "both",
        }
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


def test_obs_password_ui_copy_makes_saved_state_and_save_timing_clear():
    saved_placeholder, saved_info = web_app._obs_password_ui_copy(True)
    empty_placeholder, empty_info = web_app._obs_password_ui_copy(False)

    assert "保存済み" in saved_placeholder
    assert "保存済み" in saved_info
    assert "空欄のまま再利用" in saved_info
    assert "OBS連携 開始" in saved_info

    assert "入力" in empty_placeholder
    assert "未保存" in empty_info
    assert "OBS連携 開始" in empty_info
    assert "チェックだけでは保存されません" in empty_info


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
        assert isinstance(keywords["placeholder"], ast.Name)
        assert keywords["placeholder"].id == "obs_password_placeholder"
        assert isinstance(keywords["info"], ast.Name)
        assert keywords["info"].id == "obs_password_info"
        break
    else:
        raise AssertionError("obs_password Textbox assignment not found")

    source = WEB_APP.read_text(encoding="utf-8")
    assert 'label="Passwordを保存"' in source
    assert "value=True" in source
    assert "value=bool(load_obs_password())" not in source
    assert "placeholder=obs_password_placeholder" in source
    assert "info=obs_password_info" in source
    assert 'elem_classes="obs-password-heading"' in source
    assert 'elem_classes="obs-password-save"' in source
    assert "保存済みPasswordを削除" not in source
    assert "obs_clear_password_btn" not in source
