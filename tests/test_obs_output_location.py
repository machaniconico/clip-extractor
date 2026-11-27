"""OBS録画から生成したクリップの保存先(obs_output_location)。"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import web_app


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(web_app, "_resolve_provider_api_key", lambda *a, **k: "")


@pytest.fixture
def created_dirs(monkeypatch):
    """Run the real output-dir creation and stop before transcription."""
    created: list[Path] = []

    def fake_detect_phase(*args, **kwargs):
        created.append(
            web_app._create_output_dir(web_app.resolve_output_base(args[16]))
        )
        return (None, "stopped by test", {})

    monkeypatch.setattr(web_app, "detect_phase", fake_detect_phase)
    return created


def _recording(tmp_path: Path) -> Path:
    rec_dir = tmp_path / "obs_recordings"
    rec_dir.mkdir()
    rec = rec_dir / "2026-11-27 20-00-00.mkv"
    rec.write_bytes(b"video")
    return rec


def _settings(tmp_path: Path, **overrides) -> dict:
    settings = {
        "enable_clips": True,
        "enable_chapters": False,
        "output_base_dir": str(tmp_path / "custom_out"),
    }
    settings.update(overrides)
    return settings


def test_default_writes_output_next_to_recording(tmp_path, created_dirs):
    rec = _recording(tmp_path)

    web_app._run_obs_auto_pipeline_outcome(str(rec), _settings(tmp_path))

    assert len(created_dirs) == 1
    assert created_dirs[0].parent == rec.parent.resolve()
    assert created_dirs[0].name.startswith("output_")
    assert not (tmp_path / "custom_out").exists()


def test_custom_writes_output_under_output_base_dir(tmp_path, created_dirs):
    rec = _recording(tmp_path)

    web_app._run_obs_auto_pipeline_outcome(
        str(rec), _settings(tmp_path, obs_output_location="custom")
    )

    assert len(created_dirs) == 1
    assert created_dirs[0].parent == tmp_path / "custom_out"
    assert list(rec.parent.glob("output_*")) == []


def test_unwritable_recording_dir_falls_back_to_output_base_dir(
    tmp_path, created_dirs, monkeypatch
):
    rec = _recording(tmp_path)
    original_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        if Path(kwargs.get("dir", "")).resolve() == rec.parent.resolve():
            raise PermissionError("read-only recording folder")
        return original_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(web_app.tempfile, "mkdtemp", mkdtemp)
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()

    web_app._run_obs_auto_pipeline_outcome(str(rec), _settings(tmp_path))

    assert len(created_dirs) == 1
    assert created_dirs[0].parent == tmp_path / "custom_out"
    with web_app._obs_status_lock:
        status = "\n".join(web_app._obs_status_lines)
    assert "録画フォルダに書き込めない" in status


def test_input_settings_are_not_mutated(tmp_path, created_dirs):
    rec = _recording(tmp_path)
    settings = _settings(tmp_path)

    web_app._run_obs_auto_pipeline_outcome(str(rec), settings)

    assert settings["output_base_dir"] == str(tmp_path / "custom_out")
    assert not list(rec.parent.glob(".clip_extractor_probe_*"))


def test_archive_fallback_keeps_output_base_dir(tmp_path, created_dirs):
    web_app._run_obs_youtube_pipeline_outcome(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        _settings(tmp_path, obs_output_location="recording"),
    )

    assert len(created_dirs) == 1
    assert created_dirs[0].parent == tmp_path / "custom_out"


@pytest.mark.parametrize(
    ("saved", "expected"),
    [
        (None, "recording"),
        ("recording", "recording"),
        ("custom", "custom"),
        ("CUSTOM", "custom"),
        ("bogus", "recording"),
        ("", "recording"),
        (1, "recording"),
    ],
)
def test_saved_output_location_is_parsed_strictly(tmp_path, saved, expected):
    data = {} if saved is None else {"obs_output_location": saved}
    web_app.SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")

    assert web_app.load_defaults()["obs_output_location"] == expected


def test_default_settings_json_ships_recording_default():
    shipped = json.loads(
        (Path(web_app.__file__).parent / "default_settings.json").read_text(
            encoding="utf-8"
        )
    )
    assert shipped["obs_output_location"] == "recording"
    assert web_app.OBS_CONNECTION_DEFAULTS["obs_output_location"] == "recording"


def test_general_save_defaults_preserves_output_location(tmp_path):
    web_app.SETTINGS_FILE.write_text(
        json.dumps({"obs_output_location": "custom"}), encoding="utf-8"
    )
    defaults = web_app.load_defaults()

    web_app.save_defaults(
        defaults["ai_provider"], defaults["ai_model"],
        True, True, "", "", False,
        5, "combined", False, "pad", "center", True,
        60, 90, "large-v3", "ja",
        "Noto Sans JP", 96, "#FFFFFF",
        str(tmp_path / "out"),
    )

    saved = json.loads(web_app.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert saved["obs_output_location"] == "custom"


def test_manual_start_persists_selected_output_location(tmp_path, monkeypatch):
    captured = {}

    def fake_impl(**kwargs):
        captured.update(kwargs)
        return "started"

    monkeypatch.setattr(web_app, "_start_obs_watch_impl", fake_impl)

    web_app.start_obs_watch(
        "websocket", "localhost", 4455, "", False, "record", "", True,
        False, 5, "combined", False, "gemini", "large-v3", "",
        obs_output_location="custom",
    )

    assert captured["obs_output_location"] == "custom"


def test_recording_dir_allowing_files_but_not_subdirs_falls_back(
    tmp_path, created_dirs, monkeypatch
):
    """The probe must test folder creation, which is what the output needs."""
    rec = _recording(tmp_path)
    original_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        if Path(kwargs.get("dir", "")).resolve() == rec.parent.resolve():
            raise PermissionError("cannot create folders here")
        return original_mkdtemp(*args, **kwargs)

    # Creating plain files in the recording folder still works.
    monkeypatch.setattr(web_app.tempfile, "mkdtemp", mkdtemp)

    web_app._run_obs_auto_pipeline_outcome(str(rec), _settings(tmp_path))

    assert created_dirs[0].parent == tmp_path / "custom_out"
