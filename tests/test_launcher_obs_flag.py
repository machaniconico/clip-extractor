"""Regression tests for launcher.py's opt-in --with-obs switch."""

import json
from types import SimpleNamespace

import launcher
import obs_launcher


def test_launch_obs_if_requested_is_opt_in(tmp_path, monkeypatch):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps({"obs_launch_on_startup": False}), encoding="utf-8"
    )
    calls = []
    monkeypatch.setattr(
        obs_launcher,
        "launch_obs",
        lambda executable=None: calls.append(executable),
    )

    assert launcher.launch_obs_if_requested([], settings_path=settings_file) is None
    assert calls == []


def test_launch_obs_if_requested_returns_nonfatal_result(monkeypatch, capsys):
    expected = SimpleNamespace(ok=False, status="not_found", message="OBS not found")
    monkeypatch.setattr(
        obs_launcher, "launch_obs_from_settings", lambda *_args, **_kwargs: expected
    )

    result = launcher.launch_obs_if_requested(["--with-obs"])

    assert result is expected
    assert "OBS not found" in capsys.readouterr().out


def test_saved_setting_launches_configured_obs_without_cli_flag(tmp_path, monkeypatch):
    configured = tmp_path / "Portable OBS" / "obs64.exe"
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_launch_on_startup": True,
                "obs_executable_path": str(configured),
            }
        ),
        encoding="utf-8",
    )
    calls = []
    expected = SimpleNamespace(ok=True, status="started", message="started")

    def fake_launch(path, *, force=False):
        calls.append((path, force))
        return expected

    monkeypatch.setattr(obs_launcher, "launch_obs_from_settings", fake_launch)

    result = launcher.launch_obs_if_requested([], settings_path=settings_file)

    assert result is expected
    assert calls == [(settings_file, False)]


def test_cli_flag_forces_launch_even_when_saved_setting_is_off(tmp_path, monkeypatch):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps({"obs_launch_on_startup": False}), encoding="utf-8"
    )
    calls = []
    expected = SimpleNamespace(ok=True, status="started", message="started")

    def fake_launch(path, *, force=False):
        calls.append((path, force))
        return expected

    monkeypatch.setattr(obs_launcher, "launch_obs_from_settings", fake_launch)

    result = launcher.launch_obs_if_requested(
        ["--with-obs"], settings_path=settings_file
    )

    assert result is expected
    assert calls == [(settings_file, True)]


def test_default_settings_file_is_used_for_normal_launcher_start(tmp_path, monkeypatch):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_launch_on_startup": True,
                "obs_executable_path": "C:/OBS/obs64.exe",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "SETTINGS_FILE", settings_file)
    calls = []
    expected = SimpleNamespace(ok=True, status="started", message="started")

    def fake_launch(path, *, force=False):
        calls.append((path, force))
        return expected

    monkeypatch.setattr(obs_launcher, "launch_obs_from_settings", fake_launch)

    result = launcher.launch_obs_if_requested([])

    assert result is expected
    assert calls == [(settings_file, False)]
