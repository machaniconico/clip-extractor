"""Folder-picker behavior used by output and OBS settings."""

from types import SimpleNamespace

import web_app


def test_canceling_folder_picker_preserves_a_blank_value(monkeypatch, tmp_path):
    calls = []

    monkeypatch.setattr(web_app, "resolve_output_base", lambda _value: tmp_path)
    monkeypatch.setattr(web_app.os, "name", "nt")

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(web_app.subprocess, "run", fake_run)

    assert web_app.pick_folder_dialog("") == ""
    assert calls
    assert calls[0][0][:4] == [
        "powershell",
        "-NoProfile",
        "-Sta",
        "-Command",
    ]


def test_windows_folder_picker_returns_a_unicode_path(monkeypatch, tmp_path):
    selected = r"C:\配信 録画\OBS"
    captured = {}

    monkeypatch.setattr(web_app, "resolve_output_base", lambda _value: tmp_path)
    monkeypatch.setattr(web_app.os, "name", "nt")

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(stdout=f"{selected}\n")

    monkeypatch.setattr(web_app.subprocess, "run", fake_run)

    assert web_app.pick_folder_dialog("", title="録画先を選択") == selected
    assert "[Console]::OutputEncoding" in captured["args"][-1]
    assert "$d.Description = '録画先を選択'" in captured["args"][-1]
    assert captured["kwargs"]["encoding"] == "utf-8"


def test_obs_folder_picker_uses_recording_specific_dialog_title(monkeypatch):
    calls = []

    def fake_picker(current_value, title="保存先フォルダを選択"):
        calls.append((current_value, title))
        return "C:/OBS/recordings"

    monkeypatch.setattr(web_app, "pick_folder_dialog", fake_picker)

    assert (
        web_app.pick_obs_watch_folder_dialog("C:/old")
        == "C:/OBS/recordings"
    )
    assert calls == [("C:/old", "録画出力フォルダを選択")]
