"""Round-trip regression tests for web_app save_defaults / load_defaults.

web_app imports gradio (heavy). Skip the whole module when gradio is not
installed. save_defaults writes SETTINGS_FILE, so we monkeypatch it onto a
tmp_path file to avoid clobbering the real default_settings.json.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("gradio")

import web_app


def _save_with(monkeypatch, tmp_path, **overrides):
    """Call save_defaults against a temp SETTINGS_FILE and return load_defaults()."""
    settings_file = tmp_path / "default_settings.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)

    # Baseline args matching the current save_defaults positional signature.
    args = dict(
        ai_provider="gemini", ai_model="gemini-2.5-flash",
        enable_clips=True, enable_chapters=True,
        clip_prompt="", chapter_prompt="",
        auto_append_youtube=False,
        num_clips=5, output_mode="combined", generate_shorts=False,
        shorts_mode="crop", shorts_crop="center", shorts_title=True,
        min_duration=30, max_duration=90,
        whisper_model="large-v3", language="ja",
        font_name="Noto Sans JP", font_size=96, font_color="#FFFFFF",
        output_base_dir="",
        generate_thumbnails=False,
        audio_fusion=False, audio_alpha=0.35,
        karaoke=False,
        obs_launch_on_startup=False,
        obs_executable_path="",
    )
    args.update(overrides)

    web_app.save_defaults(
        args["ai_provider"], args["ai_model"],
        args["enable_clips"], args["enable_chapters"],
        args["clip_prompt"], args["chapter_prompt"],
        args["auto_append_youtube"],
        args["num_clips"], args["output_mode"], args["generate_shorts"],
        args["shorts_mode"], args["shorts_crop"], args["shorts_title"],
        args["min_duration"], args["max_duration"],
        args["whisper_model"], args["language"],
        args["font_name"], args["font_size"], args["font_color"],
        args["output_base_dir"],
        args["generate_thumbnails"],
        args["audio_fusion"], args["audio_alpha"],
        args["karaoke"],
        args["obs_launch_on_startup"],
        args["obs_executable_path"],
    )
    assert settings_file.exists(), "save_defaults should write SETTINGS_FILE"
    return web_app.load_defaults()


def test_roundtrip_shorts_fields(monkeypatch, tmp_path):
    loaded = _save_with(
        monkeypatch, tmp_path,
        generate_shorts=True, output_mode="individual",
        shorts_mode="blur", shorts_crop="left", shorts_title=False,
    )
    assert loaded["generate_shorts"] is True, loaded
    assert loaded["output_mode"] == "individual", loaded
    assert loaded["shorts_mode"] == "blur", loaded
    assert loaded["shorts_crop"] == "left", loaded
    assert loaded["shorts_title"] is False, loaded


def test_roundtrip_preserves_defaults(monkeypatch, tmp_path):
    loaded = _save_with(monkeypatch, tmp_path)
    assert loaded["generate_shorts"] is False, loaded
    assert loaded["output_mode"] == "combined", loaded
    assert loaded["shorts_mode"] == "crop", loaded
    assert loaded["shorts_crop"] == "center", loaded
    assert loaded["shorts_title"] is True, loaded
    assert loaded["audio_fusion"] is False, loaded
    assert loaded["audio_alpha"] == 0.35, loaded
    assert loaded["karaoke"] is False, loaded


def test_roundtrip_audio_fusion_fields(monkeypatch, tmp_path):
    loaded = _save_with(
        monkeypatch, tmp_path,
        generate_thumbnails=True,
        audio_fusion=True,
        audio_alpha=0.65,
        karaoke=True,
    )
    assert loaded["generate_thumbnails"] is True, loaded
    assert loaded["audio_fusion"] is True, loaded
    assert loaded["audio_alpha"] == 0.65, loaded
    assert loaded["karaoke"] is True, loaded


def test_roundtrip_obs_launch_fields(monkeypatch, tmp_path):
    loaded = _save_with(
        monkeypatch,
        tmp_path,
        obs_launch_on_startup=True,
        obs_executable_path="  C:/Portable OBS/obs64.exe  ",
    )

    assert loaded["obs_launch_on_startup"] is True, loaded
    assert loaded["obs_executable_path"] == "C:/Portable OBS/obs64.exe", loaded


def test_does_not_touch_real_settings(monkeypatch, tmp_path):
    real = web_app.SETTINGS_FILE
    before = real.read_text(encoding="utf-8") if real.exists() else None
    _save_with(monkeypatch, tmp_path, shorts_crop="right")
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert before == after, "real default_settings.json must be untouched"
