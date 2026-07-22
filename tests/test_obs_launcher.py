"""Unit tests for the optional OBS + Clip Extractor launch path."""

import json
from pathlib import Path
import subprocess

import obs_launcher


def test_find_obs_executable_prefers_explicit_environment_path(tmp_path, monkeypatch):
    explicit = tmp_path / "custom" / "obs64.exe"
    explicit.parent.mkdir()
    explicit.write_bytes(b"")
    path_copy = tmp_path / "path" / "obs64.exe"
    path_copy.parent.mkdir()
    path_copy.write_bytes(b"")

    monkeypatch.setenv("OBS_EXECUTABLE", str(explicit))
    monkeypatch.setattr(obs_launcher.shutil, "which", lambda _name: str(path_copy))

    assert obs_launcher.find_obs_executable() == explicit


def test_find_obs_executable_uses_standard_windows_install(tmp_path, monkeypatch):
    program_files = tmp_path / "Program Files"
    installed = program_files / "obs-studio" / "bin" / "64bit" / "obs64.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"")

    monkeypatch.delenv("OBS_EXECUTABLE", raising=False)
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(obs_launcher.shutil, "which", lambda _name: None)

    assert obs_launcher.find_obs_executable(platform="win32") == installed


def test_invalid_explicit_environment_path_does_not_launch_another_install(
    tmp_path, monkeypatch
):
    path_copy = tmp_path / "path" / "obs64.exe"
    path_copy.parent.mkdir()
    path_copy.write_bytes(b"")
    monkeypatch.setenv("OBS_EXECUTABLE", str(tmp_path / "missing" / "obs64.exe"))
    monkeypatch.setattr(obs_launcher.shutil, "which", lambda _name: str(path_copy))

    assert obs_launcher.find_obs_executable(platform="win32") is None


def test_launch_obs_does_not_start_a_second_instance(monkeypatch):
    launched = []
    monkeypatch.setattr(obs_launcher, "is_obs_running", lambda **_kwargs: True)
    monkeypatch.setattr(obs_launcher.subprocess, "Popen", launched.append)

    result = obs_launcher.launch_obs()

    assert result.status == "already_running"
    assert result.ok is True
    assert launched == []


def test_launch_obs_uses_argument_list_and_executable_directory(tmp_path, monkeypatch):
    executable = tmp_path / "OBS Studio" / "bin" / "64bit" / "obs64.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    calls = []

    monkeypatch.setattr(obs_launcher, "is_obs_running", lambda **_kwargs: False)

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(obs_launcher.subprocess, "Popen", fake_popen)

    result = obs_launcher.launch_obs(executable)

    assert result.status == "started"
    assert result.ok is True
    assert calls == [([str(executable)], {"cwd": str(executable.parent)})]


def test_launch_obs_missing_install_is_nonfatal(monkeypatch):
    monkeypatch.setattr(obs_launcher, "is_obs_running", lambda **_kwargs: False)
    monkeypatch.setattr(obs_launcher, "find_obs_executable", lambda **_kwargs: None)

    result = obs_launcher.launch_obs()

    assert result.status == "not_found"
    assert result.ok is False
    assert "OBS_EXECUTABLE" in result.message


def test_launch_obs_process_error_is_nonfatal(tmp_path, monkeypatch):
    executable = tmp_path / "obs64.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(obs_launcher, "is_obs_running", lambda **_kwargs: False)

    def fail_to_start(*_args, **_kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(obs_launcher.subprocess, "Popen", fail_to_start)

    result = obs_launcher.launch_obs(executable)

    assert result.status == "error"
    assert result.ok is False
    assert "blocked" in result.message


def test_tasklist_detection_matches_obs_process_name(monkeypatch):
    monkeypatch.setattr(
        obs_launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="obs64.exe  123 Console", stderr=""
        ),
    )

    assert obs_launcher.is_obs_running(platform="win32") is True


def test_load_obs_launch_preferences_reads_checkbox_and_path(tmp_path):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_launch_on_startup": True,
                "obs_executable_path": "  C:/Portable OBS/obs64.exe  ",
            }
        ),
        encoding="utf-8",
    )

    preferences = obs_launcher.load_obs_launch_preferences(settings_file)

    assert preferences.enabled is True
    assert preferences.executable_path == "C:/Portable OBS/obs64.exe"


def test_corrupt_settings_disable_automatic_obs_launch(tmp_path, monkeypatch):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text("{not-json", encoding="utf-8")
    calls = []
    monkeypatch.setattr(obs_launcher, "launch_obs", calls.append)

    result = obs_launcher.launch_obs_from_settings(settings_file)

    assert result is None
    assert calls == []


def test_enabled_setting_passes_saved_executable_to_launcher(tmp_path, monkeypatch):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_launch_on_startup": True,
                "obs_executable_path": "C:/Portable OBS/obs64.exe",
            }
        ),
        encoding="utf-8",
    )
    calls = []
    expected = obs_launcher.ObsLaunchResult("started", True, "started")

    def fake_launch(executable=None):
        calls.append(executable)
        return expected

    monkeypatch.setattr(obs_launcher, "launch_obs", fake_launch)

    result = obs_launcher.launch_obs_from_settings(settings_file)

    assert result is expected
    assert calls == ["C:/Portable OBS/obs64.exe"]


def test_force_launch_survives_corrupt_settings(tmp_path, monkeypatch):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text("{not-json", encoding="utf-8")
    calls = []
    expected = obs_launcher.ObsLaunchResult("started", True, "started")

    def fake_launch(executable=None):
        calls.append(executable)
        return expected

    monkeypatch.setattr(obs_launcher, "launch_obs", fake_launch)

    result = obs_launcher.launch_obs_from_settings(settings_file, force=True)

    assert result is expected
    assert calls == [None]


def test_force_launch_uses_saved_path_even_when_checkbox_is_off(tmp_path, monkeypatch):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_launch_on_startup": False,
                "obs_executable_path": "C:/Portable OBS/obs64.exe",
            }
        ),
        encoding="utf-8",
    )
    calls = []
    expected = obs_launcher.ObsLaunchResult("started", True, "started")

    def fake_launch(executable=None):
        calls.append(executable)
        return expected

    monkeypatch.setattr(obs_launcher, "launch_obs", fake_launch)

    result = obs_launcher.launch_obs_from_settings(settings_file, force=True)

    assert result is expected
    assert calls == ["C:/Portable OBS/obs64.exe"]


def test_unexpected_launch_error_is_converted_to_nonfatal_result(tmp_path, monkeypatch):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        json.dumps({"obs_launch_on_startup": True}), encoding="utf-8"
    )

    def unexpected_failure(_executable=None):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(obs_launcher, "launch_obs", unexpected_failure)

    result = obs_launcher.launch_obs_from_settings(settings_file)

    assert result is not None
    assert result.status == "error"
    assert result.ok is False
    assert "unexpected" in result.message


def test_unexpected_settings_read_error_is_converted_to_nonfatal_result(
    tmp_path, monkeypatch
):
    def unexpected_failure(_settings_path):
        raise ValueError("pathological JSON")

    monkeypatch.setattr(
        obs_launcher,
        "load_obs_launch_preferences",
        unexpected_failure,
    )

    result = obs_launcher.launch_obs_from_settings(
        tmp_path / "default_settings.json"
    )

    assert result is not None
    assert result.status == "error"
    assert result.ok is False
    assert "pathological JSON" in result.message
