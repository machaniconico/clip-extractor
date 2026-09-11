"""Regression tests for OBS connection settings persistence."""

import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("gradio")

import obs_integration
import secret_store
import web_app

WEB_APP = Path(__file__).parent.parent / "web_app.py"


OBS_UI_KEYS = (
    "obs_trigger_method", "obs_host", "obs_port", "obs_stop_event",
    "obs_watch_folder", "obs_auto_process",
    "enable_clips", "clip_prompt", "enable_chapters", "chapter_prompt",
    "auto_append_youtube", "num_clips", "min_duration", "max_duration",
    "output_mode", "generate_shorts", "shorts_mode", "shorts_crop",
    "shorts_title", "generate_thumbnails", "audio_fusion", "audio_alpha",
    "karaoke", "auto_start_without_prompt_confirmation",
    "shorts_blur_strength", "shorts_title_position",
)


def _save_obs_controls(**overrides):
    args = dict(
        enable_clips=False, clip_prompt="OBS切り抜き", enable_chapters=True,
        chapter_prompt="OBSチャプター", auto_append_youtube=True,
        num_clips=11, min_duration=55, max_duration=120,
        output_mode="individual", generate_shorts=True, shorts_mode="blur",
        shorts_crop="left", shorts_title=False, generate_thumbnails=True,
        audio_fusion=True, audio_alpha=0.8, karaoke=True,
        auto_start_without_prompt_confirmation=True,
        shorts_blur_strength=42, shorts_title_position="overlay",
        trigger_method="folder", host="obs-host", port=4456,
        stop_event="stream", watch_folder="C:/録画", auto_process=False,
    )
    args.update(overrides)
    return web_app.save_obs_processing_defaults(**args)


def _ui_values(updates):
    assert len(updates) == 26
    assert all(update["__type__"] == "update" for update in updates)
    return dict(zip(OBS_UI_KEYS, (update["value"] for update in updates)))


@pytest.mark.parametrize("watcher_running", [False, True])
def test_obs_settings_save_preserves_connection_and_secrets(
    monkeypatch, tmp_path, caplog, watcher_running,
):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    watcher = _FakeWatcher() if watcher_running else None
    monkeypatch.setattr(web_app, "_obs_watcher", watcher)
    x_settings = {
        "obs_x_post_on_stream_start": True,
        "obs_x_post_auto": True,
        "obs_x_post_template": "配信開始 {links}",
        "obs_x_post_destinations": "Twitch|https://twitch.tv/example",
    }
    settings_file.write_text(json.dumps(x_settings), encoding="utf-8")
    password_bytes = b"saved-password-must-not-change"
    credentials_bytes = b"saved-x-credentials-must-not-change"
    web_app.OBS_PASSWORD_FILE.write_bytes(password_bytes)
    web_app.X_CREDENTIALS_FILE.write_bytes(credentials_bytes)

    def unexpected_secret_access(*_args, **_kwargs):
        pytest.fail("Saving these OBS controls must not read or write secrets")

    for name in (
        "load_obs_password", "_save_obs_password",
        "load_x_credentials", "_save_x_credentials",
    ):
        monkeypatch.setattr(web_app, name, unexpected_secret_access)

    with caplog.at_level("INFO", logger=web_app.logger.name):
        message, *updates = _save_obs_controls()

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    expected_connection = {
        "obs_trigger_method": "folder", "obs_host": "obs-host",
        "obs_port": 4456, "obs_stop_event": "stream",
        "obs_watch_folder": "C:/録画", "obs_auto_process": False,
    }
    assert {key: saved[key] for key in expected_connection} == expected_connection
    assert {key: saved[key] for key in x_settings} == x_settings
    assert web_app.OBS_PASSWORD_FILE.read_bytes() == password_bytes
    assert web_app.X_CREDENTIALS_FILE.read_bytes() == credentials_bytes
    assert "obs_password" not in saved
    assert not web_app.X_SECRET_SETTING_KEYS.intersection(saved)
    expected = {
        **expected_connection, **saved["obs_processing"],
        "auto_start_without_prompt_confirmation": True,
    }
    expected.pop("confirm_before_auto_process")
    assert _ui_values(updates) == expected
    assert updates[24]["visible"] is True
    assert "検知方式: folder / 取得元: stream / 自動処理: OFF" in message
    assert "クリップ数: 11 / 最小・最大長: 55〜120秒 / ショート生成: ON" in message
    assert ("今すぐ反映するには『OBS連携 開始』" in message) is watcher_running
    assert web_app._obs_watcher is watcher
    assert "settings saved (obs_settings):" in caplog.text
    for text in (
        "obs_trigger_method='folder'", "obs_stop_event='stream'",
        "obs_auto_process=False", "obs_processing.num_clips=11",
        "obs_processing.min_duration=55", "obs_processing.max_duration=120",
        "obs_processing.generate_shorts=True",
    ):
        assert text in caplog.text
    exposed = json.dumps([saved, message, updates]) + caplog.text
    assert password_bytes.decode() not in exposed
    assert credentials_bytes.decode() not in exposed


def test_obs_save_response_rereads_persisted_values(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    original_write = web_app._write_settings_file

    def write_different_values(data, *, reason):
        # Simulate storage returning values different from the submitted tab.
        persisted = {**data, "obs_stop_event": "record", "obs_host": "saved-host"}
        persisted["obs_processing"] = {
            **data["obs_processing"], "num_clips": 19,
            "min_duration": 75, "max_duration": 150,
            "generate_shorts": False, "confirm_before_auto_process": True,
        }
        original_write(persisted, reason=reason)

    monkeypatch.setattr(web_app, "_write_settings_file", write_different_values)

    message, *updates = _save_obs_controls(num_clips=3)

    values = _ui_values(updates)
    assert values["obs_host"] == "saved-host"
    assert values["obs_stop_event"] == "record"
    assert values["num_clips"] == 19
    assert values["min_duration"] == 75
    assert values["max_duration"] == 150
    assert values["generate_shorts"] is False
    assert values["auto_start_without_prompt_confirmation"] is False
    assert "取得元: record" in message
    assert "クリップ数: 19 / 最小・最大長: 75〜150秒 / ショート生成: OFF" in message


def test_obs_page_load_refreshes_current_file_and_wires_all_controls(
    monkeypatch, tmp_path, caplog,
):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    _save_obs_controls()
    with caplog.at_level("INFO", logger=web_app.logger.name):
        app = web_app.create_ui()
    assert "settings loaded: obs_trigger_method='folder'" in caplog.text
    load_fn = next(
        fn for fn in app.fns.values() if fn.fn is web_app.load_obs_defaults_for_ui
    )
    save_fn = next(
        fn for fn in app.fns.values() if fn.fn is web_app.save_obs_processing_defaults
    )
    assert not load_fn.inputs
    assert len(save_fn.inputs) == 26
    assert len(load_fn.outputs) == 26
    assert save_fn.outputs[1:] == load_fn.outputs
    # Save inputs retain their original processing-first order.
    assert save_fn.inputs == load_fn.outputs[6:] + load_fn.outputs[:6]
    assert all(getattr(component, "type", None) != "password" for component in load_fn.outputs)
    load_dependency = app.get_config_file()["dependencies"][load_fn._id]
    assert any(target[1] == "load" for target in load_dependency["targets"])

    # The same Blocks object must return new values for each tab/reload.
    for source, num_clips, confirm in (("record", 7, True), ("stream", 9, False)):
        persisted = {
            "obs_trigger_method": "websocket", "obs_host": "new-host",
            "obs_port": 4460, "obs_stop_event": source,
            "obs_watch_folder": "", "obs_auto_process": True,
            "obs_password": "legacy-secret-must-stay-hidden",
            "obs_x_api_key": "legacy-x-secret-must-stay-hidden",
            "obs_processing": {
                "num_clips": num_clips, "confirm_before_auto_process": confirm,
                "shorts_mode": "pad", "shorts_blur_strength": 15,
            },
        }
        settings_file.write_text(json.dumps(persisted), encoding="utf-8")
        updates = load_fn.fn()
        values = _ui_values(updates)
        expected = {
            **{key: persisted[key] for key in OBS_UI_KEYS[:6]},
            **web_app.OBS_PROCESSING_DEFAULTS,
            **persisted["obs_processing"],
            "auto_start_without_prompt_confirmation": not confirm,
        }
        expected.pop("confirm_before_auto_process")
        assert values == expected
        assert updates[24]["visible"] is False
        assert "legacy-secret" not in json.dumps(updates)
        assert "legacy-x-secret" not in json.dumps(updates)


def test_corrupt_settings_returns_defaults_and_warns(monkeypatch, tmp_path, caplog):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    expected = web_app.load_defaults()
    settings_file.write_text('{"obs_stop_event": broken', encoding="utf-8")

    with caplog.at_level("WARNING", logger=web_app.logger.name):
        assert web_app.load_defaults() == expected

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "設定ファイルの読込に失敗したため既定値を使用します" in warnings[0].message
    assert str(settings_file) in warnings[0].message
    assert "Expecting value" in warnings[0].message


@pytest.mark.parametrize("existing", [False, True])
def test_settings_write_atomically_replaces_json(monkeypatch, tmp_path, existing):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    old = '{"obs_stop_event": "record"}'
    if existing:
        settings_file.write_text(old, encoding="utf-8")
    data = {"obs_stop_event": "stream", "obs_watch_folder": "C:/録画"}
    original_replace = web_app.os.replace
    replacements = []

    def inspect_replace(source, destination):
        source, destination = Path(source), Path(destination)
        assert source != destination == settings_file
        assert source.parent == settings_file.parent
        assert settings_file.exists() is existing
        if existing:
            assert settings_file.read_text(encoding="utf-8") == old
        expected_text = json.dumps(data, ensure_ascii=False, indent=2)
        assert source.read_text(encoding="utf-8") == expected_text
        assert json.loads(source.read_text(encoding="utf-8")) == data
        replacements.append(source)
        original_replace(source, destination)

    monkeypatch.setattr(web_app.os, "replace", inspect_replace)
    web_app._write_settings_file(data, reason="test_atomic")

    assert len(replacements) == 1
    assert not replacements[0].exists()
    assert json.loads(settings_file.read_text(encoding="utf-8")) == data
    assert set(tmp_path.iterdir()) == {settings_file}


def test_settings_replace_failure_preserves_original(monkeypatch, tmp_path, caplog):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    original = b'{"obs_stop_event": "record"}'
    settings_file.write_bytes(original)

    def fail_replace(*_args):
        raise OSError("replace failed")

    monkeypatch.setattr(web_app.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        web_app._write_settings_file({"obs_stop_event": "stream"}, reason="failure")

    assert settings_file.read_bytes() == original
    assert set(tmp_path.iterdir()) == {settings_file}
    assert "settings saved" not in caplog.text


class _FakeWatcher:
    status = "connected"

    def start(self):
        pass

    def stop(self):
        self.status = "stopped"


def test_obs_render_settings_preserve_direct_call():
    original = {"output_mode": "combined", "num_clips": 3}

    prepared = web_app._obs_settings_for_render(original)

    assert prepared == original
    assert prepared is not original


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
    caplog,
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

    with caplog.at_level("ERROR", logger=web_app.logger.name):
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
    assert "access denied" not in status
    assert "OBS connection settings or secrets" in caplog.text
    assert "access denied" in caplog.text
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


def test_unavailable_saved_secret_uses_fixed_safe_ui_message(monkeypatch):
    warnings = []
    monkeypatch.setattr(web_app.gr, "Warning", warnings.append)

    def unavailable_loader():
        raise secret_store.SecretUnavailableError(
            "unsafe exception detail C:/private/.obs_password"
        )

    fallback = object()

    assert web_app._read_secret_for_ui(unavailable_loader, fallback) is fallback
    assert warnings == [web_app.SECRET_UNAVAILABLE_UI_MESSAGE]
    assert "unsafe exception detail" not in warnings[0]
    assert ".obs_password" not in warnings[0]


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


def test_start_obs_watch_persists_x_credentials_outside_settings_json(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "default_settings.json"
    password_file = tmp_path / ".obs_password"
    credentials_file = tmp_path / ".x_credentials.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", password_file)
    monkeypatch.setattr(web_app, "X_CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **_kwargs: _FakeWatcher(),
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
            obs_x_post_auto=True,
            obs_x_api_key="api-key",
            obs_x_api_key_secret="api-key-secret",
            obs_x_access_token="access-token",
            obs_x_access_token_secret="access-token-secret",
        )

        assert status == "connected"
        saved_settings = json.loads(settings_file.read_text(encoding="utf-8"))
        assert saved_settings["obs_x_post_auto"] is True
        assert all(
            secret not in settings_file.read_text(encoding="utf-8")
            for secret in (
                "api-key",
                "api-key-secret",
                "access-token",
                "access-token-secret",
            )
        )
        assert not {
            "obs_x_api_key",
            "obs_x_api_key_secret",
            "obs_x_access_token",
            "obs_x_access_token_secret",
        }.intersection(web_app.load_defaults())
        assert web_app.load_x_credentials().as_dict() == {
            "api_key": "api-key",
            "api_key_secret": "api-key-secret",
            "access_token": "access-token",
            "access_token_secret": "access-token-secret",
        }
    finally:
        web_app.stop_obs_watch()


def test_x_credentials_are_masked_and_never_used_as_textbox_initial_values():
    module = ast.parse(WEB_APP.read_text(encoding="utf-8"))
    expected = {
        "obs_x_api_key",
        "obs_x_api_key_secret",
        "obs_x_access_token",
        "obs_x_access_token_secret",
    }
    found = set()

    for node in ast.walk(module):
        if not isinstance(node, ast.Assign):
            continue
        names = {
            target.id
            for target in node.targets
            if isinstance(target, ast.Name) and target.id in expected
        }
        if not names:
            continue
        assert isinstance(node.value, ast.Call), ast.dump(node.value)
        keywords = {keyword.arg: keyword.value for keyword in node.value.keywords}
        assert isinstance(keywords["value"], ast.Constant)
        assert keywords["value"].value == ""
        assert isinstance(keywords["type"], ast.Constant)
        assert keywords["type"].value == "password"
        found.update(names)

    assert found == expected
