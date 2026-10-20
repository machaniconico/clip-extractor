import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("gradio")

import web_app


@pytest.mark.parametrize(
    "value, default, expected",
    [
        (True, False, True),
        (False, True, False),
        ("true", False, True),
        ("TRUE", False, True),
        ("false", True, False),
        ("FALSE", True, False),
        ("0", True, False),
        ("1", False, True),
        ("yes", False, True),
        ("no", True, False),
        ("on", False, True),
        ("off", True, False),
        (" Off ", True, False),
        ("", True, False),
        (None, True, True),
        (None, False, False),
        (0, True, False),
        (1, False, True),
        (2, False, True),
        (0.0, True, False),
        (1.5, False, True),
        ("garbage", True, True),
        ("garbage", False, False),
        (["x"], True, True),
    ],
)
def test_setting_bool(value, default, expected):
    assert web_app._setting_bool(value, default) is expected


def test_setting_bool_warning_names_key_not_value(caplog):
    with caplog.at_level("WARNING", logger="clip-extractor"):
        web_app._setting_bool("s3cr3t-garbage", True, "obs_x_post_auto")
    assert "obs_x_post_auto" in caplog.text
    assert "s3cr3t-garbage" not in caplog.text


import json

import obs_integration
import obs_launcher


def _use_settings(monkeypatch, tmp_path, data):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    return settings_file


def test_load_defaults_returns_real_bools(monkeypatch, tmp_path):
    _use_settings(
        monkeypatch,
        tmp_path,
        {
            "generate_shorts": "false",
            "auto_append_youtube": "off",
            "enable_clips": "true",
            "enable_chapters": None,
            "obs_launch_on_startup": "yes",
            "obs_auto_connect_on_startup": "false",
            "obs_x_post_auto": 2,
            "obs_processing": {
                "auto_append_youtube": "off",
                "generate_shorts": "1",
                "confirm_before_auto_process": 2,
                "karaoke": None,
            },
        },
    )

    d = web_app.load_defaults()

    assert d["generate_shorts"] is False
    assert d["auto_append_youtube"] is False
    assert d["enable_clips"] is True
    assert d["enable_chapters"] is True  # explicit None -> default
    assert d["obs_launch_on_startup"] is True
    assert d["obs_auto_connect_on_startup"] is False
    assert d["obs_x_post_auto"] is True
    assert d["obs_processing"]["auto_append_youtube"] is False
    assert d["obs_processing"]["generate_shorts"] is True
    assert d["obs_processing"]["confirm_before_auto_process"] is True
    assert d["obs_processing"]["karaoke"] is False


def test_ui_initial_values_use_real_bools(monkeypatch, tmp_path):
    _use_settings(
        monkeypatch,
        tmp_path,
        {
            "obs_processing": {
                "generate_shorts": "false",
                "auto_append_youtube": "off",
                "confirm_before_auto_process": "false",
            }
        },
    )

    updates = web_app.load_obs_defaults_for_ui()
    values = [u["value"] for u in updates if isinstance(u, dict) and "value" in u]

    # generate_shorts / auto_append_youtube / "skip confirmation" inversion
    assert "false" not in values and "off" not in values
    processing = web_app._obs_processing_settings_from_defaults()
    assert processing["generate_shorts"] is False
    assert processing["auto_append_youtube"] is False
    assert processing["confirm_before_auto_process"] is False


def test_confirm_before_auto_process_int_is_consistent(monkeypatch, tmp_path):
    _use_settings(
        monkeypatch, tmp_path, {"obs_processing": {"confirm_before_auto_process": 2}}
    )
    assert (
        web_app._obs_processing_settings_from_defaults()[
            "confirm_before_auto_process"
        ]
        is True
    )
    assert (
        web_app._normalise_obs_processing_settings(None)[
            "confirm_before_auto_process"
        ]
        is True
    )


@pytest.mark.parametrize("raw, expected", [("false", False), ("true", True)])
def test_auto_connect_on_startup_strings(monkeypatch, tmp_path, raw, expected):
    _use_settings(monkeypatch, tmp_path, {"obs_auto_connect_on_startup": raw})
    assert web_app.load_defaults()["obs_auto_connect_on_startup"] is expected


@pytest.mark.parametrize("raw, expected", [("true", True), ("false", False), ("off", False)])
def test_obs_launcher_reads_string_bools(tmp_path, raw, expected):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"obs_launch_on_startup": raw}), encoding="utf-8")
    assert obs_launcher.load_obs_launch_preferences(path).enabled is expected


def test_obs_launcher_launches_for_string_true(monkeypatch, tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"obs_launch_on_startup": "true"}), encoding="utf-8")
    launched = []
    monkeypatch.setattr(
        obs_launcher, "launch_obs", lambda exe=None: launched.append(exe) or "ok"
    )
    assert obs_launcher.launch_obs_from_settings(path) == "ok"
    assert launched == [None]


class _Watcher:
    status = "connected"

    def start(self):
        pass

    def stop(self):
        self.status = "stopped"


def test_start_obs_watch_impl_saved_off_does_not_enable_auto_append(
    monkeypatch, tmp_path
):
    _use_settings(
        monkeypatch,
        tmp_path,
        {"obs_processing": {"auto_append_youtube": "off", "enable_chapters": True}},
    )
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(
        obs_integration, "create_watcher", lambda *_a, **_k: _Watcher()
    )
    seen = []
    real = web_app._obs_make_callback
    monkeypatch.setattr(
        web_app,
        "_obs_make_callback",
        lambda auto, settings, gen=None: seen.append(dict(settings))
        or real(auto, settings, gen),
    )
    try:
        web_app.start_obs_watch(
            "websocket", "h", 4455, "", False, "stream", "", False, False, 5,
            "combined", False, "gemini", "large-v3", "",
        )
    finally:
        web_app.stop_obs_watch()
    assert seen and seen[0]["auto_append_youtube"] is False
