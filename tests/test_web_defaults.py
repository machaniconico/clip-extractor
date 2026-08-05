"""Round-trip regression tests for web_app save_defaults / load_defaults.

web_app imports gradio (heavy). Skip the whole module when gradio is not
installed. save_defaults writes SETTINGS_FILE, so we monkeypatch it onto a
tmp_path file to avoid clobbering the real default_settings.json.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip("gradio")

import web_app
from config import AppConfig


def _save_with(monkeypatch, tmp_path, **overrides):
    """Call save_defaults against a temp SETTINGS_FILE and return load_defaults()."""
    settings_file = tmp_path / "default_settings.json"
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(
        web_app,
        "OBS_PASSWORD_FILE",
        tmp_path / ".obs_password",
    )

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
        shorts_blur_strength=20,
        shorts_title_position="top",
        premiere_executable_path="",
        obs_launch_on_startup=False,
        obs_auto_connect_on_startup=False,
        obs_executable_path="",
        audio_delivery_mode="both",
        bgm_asset_id="",
        se_asset_id="",
        bgm_gain_db=-18.0,
        se_gain_db=-8.0,
        se_cue_seconds=0.0,
        bgm_user_folder="",
        se_user_folder="",
        vfx_user_folder="",
        vfx_asset_id="",
        effect_preset="none",
        vfx_automatic=False,
        vfx_cue_seconds=0.0,
        vfx_duration_seconds=1.0,
        vfx_anchor="center",
        vfx_scale_percent=100.0,
        vfx_opacity_percent=100.0,
        vfx_target="both",
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
        args["shorts_blur_strength"],
        args["shorts_title_position"],
        args["premiere_executable_path"],
        args["obs_launch_on_startup"],
        args["obs_executable_path"],
        args["obs_auto_connect_on_startup"],
        args["audio_delivery_mode"],
        args["bgm_asset_id"],
        args["se_asset_id"],
        args["bgm_gain_db"],
        args["se_gain_db"],
        args["se_cue_seconds"],
        args["bgm_user_folder"],
        args["se_user_folder"],
        args["vfx_user_folder"],
        args["vfx_asset_id"],
        args["effect_preset"],
        args["vfx_automatic"],
        args["vfx_cue_seconds"],
        args["vfx_duration_seconds"],
        args["vfx_anchor"],
        args["vfx_scale_percent"],
        args["vfx_opacity_percent"],
        args["vfx_target"],
    )
    assert settings_file.exists(), "save_defaults should write SETTINGS_FILE"
    return web_app.load_defaults()


def test_roundtrip_shorts_fields(monkeypatch, tmp_path):
    loaded = _save_with(
        monkeypatch, tmp_path,
        generate_shorts=True, output_mode="individual",
        shorts_mode="blur", shorts_crop="left", shorts_title=False,
        shorts_blur_strength=37,
        shorts_title_position="bottom",
    )
    assert loaded["generate_shorts"] is True, loaded
    assert loaded["output_mode"] == "individual", loaded
    assert loaded["shorts_mode"] == "blur", loaded
    assert loaded["shorts_crop"] == "left", loaded
    assert loaded["shorts_title"] is False, loaded
    assert loaded["shorts_blur_strength"] == 37, loaded
    assert loaded["shorts_title_position"] == "bottom", loaded


def test_roundtrip_audio_delivery_fields(monkeypatch, tmp_path):
    loaded = _save_with(
        monkeypatch,
        tmp_path,
        audio_delivery_mode="separate",
        bgm_asset_id="bgm-brand-new-wisdom",
        se_asset_id="se-interface-confirmation",
        bgm_gain_db=-21,
        se_gain_db=-7,
        se_cue_seconds=1.25,
    )

    assert loaded["audio_delivery_mode"] == "separate"
    assert loaded["bgm_asset_id"] == "bgm-brand-new-wisdom"
    assert loaded["se_asset_id"] == "se-interface-confirmation"
    assert loaded["bgm_gain_db"] == -21
    assert loaded["se_gain_db"] == -7
    assert loaded["se_cue_seconds"] == 1.25


def test_roundtrip_user_media_and_vfx_fields(monkeypatch, tmp_path):
    loaded = _save_with(
        monkeypatch,
        tmp_path,
        bgm_user_folder="D:/Media/BGM",
        se_user_folder="D:/Media/SE",
        vfx_user_folder="D:/Media/VFX",
        vfx_asset_id="user:vfx:" + ("a" * 64),
        effect_preset="punch",
        vfx_automatic=True,
        vfx_cue_seconds=1.25,
        vfx_duration_seconds=2.5,
        vfx_anchor="bottom-right",
        vfx_scale_percent=65,
        vfx_opacity_percent=80,
        vfx_target="shorts",
    )

    assert loaded["bgm_user_folder"] == "D:/Media/BGM"
    assert loaded["se_user_folder"] == "D:/Media/SE"
    assert loaded["vfx_user_folder"] == "D:/Media/VFX"
    assert loaded["vfx_asset_id"] == "user:vfx:" + ("a" * 64)
    assert loaded["effect_preset"] == "punch"
    assert loaded["vfx_automatic"] is True
    assert loaded["vfx_cue_seconds"] == 1.25
    assert loaded["vfx_duration_seconds"] == 2.5
    assert loaded["vfx_anchor"] == "bottom-right"
    assert loaded["vfx_scale_percent"] == 65
    assert loaded["vfx_opacity_percent"] == 80
    assert loaded["vfx_target"] == "shorts"


def test_obs_processing_profile_is_separate_from_archive_defaults(
    monkeypatch, tmp_path
):
    loaded = _save_with(
        monkeypatch,
        tmp_path,
        num_clips=3,
        min_duration=20,
        max_duration=45,
        generate_shorts=False,
    )
    assert loaded["num_clips"] == 3
    assert "obs_processing" not in loaded

    result = web_app.save_obs_processing_defaults(
        False,
        "OBS only",
        True,
        "OBS chapters",
        True,
        11,
        55,
        120,
        "individual",
        True,
        "blur",
        "left",
        False,
        True,
        True,
        0.8,
        True,
        auto_start_without_prompt_confirmation=True,
        shorts_blur_strength=42,
        shorts_title_position="overlay",
    )

    assert "OBS" in result
    separated = web_app.load_defaults()
    assert separated["num_clips"] == 3
    assert separated["min_duration"] == 20
    assert separated["max_duration"] == 45
    assert separated["generate_shorts"] is False
    assert separated["obs_processing"] == {
        "enable_clips": False,
        "clip_prompt": "OBS only",
        "enable_chapters": True,
        "chapter_prompt": "OBS chapters",
        "auto_append_youtube": True,
        "confirm_before_auto_process": False,
        "num_clips": 11,
        "min_duration": 55,
        "max_duration": 120,
        "output_mode": "individual",
        "generate_shorts": True,
        "shorts_mode": "blur",
        "shorts_crop": "left",
        "shorts_title": False,
        "shorts_blur_strength": 42,
        "shorts_title_position": "overlay",
        "generate_thumbnails": True,
        "audio_fusion": True,
        "audio_alpha": 0.8,
        "karaoke": True,
    }


def test_roundtrip_preserves_defaults(monkeypatch, tmp_path):
    loaded = _save_with(monkeypatch, tmp_path)
    assert loaded["generate_shorts"] is False, loaded
    assert loaded["output_mode"] == "combined", loaded
    assert loaded["shorts_mode"] == "crop", loaded
    assert loaded["shorts_crop"] == "center", loaded
    assert loaded["shorts_title"] is True, loaded
    assert loaded["shorts_blur_strength"] == 20, loaded
    assert loaded["shorts_title_position"] == "top", loaded
    assert loaded["audio_fusion"] is False, loaded
    assert loaded["audio_alpha"] == 0.35, loaded
    assert loaded["karaoke"] is False, loaded
    assert web_app._obs_processing_settings_from_defaults(loaded)[
        "confirm_before_auto_process"
    ] is True


def test_obs_recording_is_the_default_source(monkeypatch, tmp_path):
    monkeypatch.setattr(
        web_app,
        "SETTINGS_FILE",
        tmp_path / "missing-settings.json",
    )
    monkeypatch.setattr(
        web_app,
        "OBS_PASSWORD_FILE",
        tmp_path / ".obs_password",
    )

    assert web_app.load_defaults()["obs_stop_event"] == "record"
    assert web_app.load_defaults()["obs_auto_connect_on_startup"] is True
    assert web_app.load_defaults()["shorts_mode"] == "pad"
    assert web_app._obs_processing_settings_from_defaults()["shorts_mode"] == "pad"
    assert web_app.load_defaults()["shorts_blur_strength"] == 20
    assert web_app.load_defaults()["shorts_title_position"] == "top"
    assert web_app.load_defaults()["font_name"] == "Noto Sans JP"
    assert web_app.load_defaults()["ai_model"] == "gemini-3.5-flash-lite"
    assert web_app._obs_processing_settings_from_defaults()["shorts_blur_strength"] == 20
    assert web_app._obs_processing_settings_from_defaults()["shorts_title_position"] == "top"
    assert AppConfig().obs_stop_event == "record"
    assert AppConfig().shorts_mode == "pad"
    assert AppConfig().shorts_blur_strength == 20
    assert AppConfig().shorts_title_position == "top"


def test_blank_saved_gemini_model_migrates_to_current_default(monkeypatch, tmp_path):
    settings_file = tmp_path / "default_settings.json"
    settings_file.write_text(
        '{"ai_provider": "gemini", "ai_model": ""}',
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)

    defaults = web_app.load_defaults()

    assert defaults["ai_model"] == "gemini-3.5-flash-lite"
    assert web_app._ai_model_choices("gemini")[:3] == [
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
    ]
    assert "gemini-2.5-flash" in web_app._ai_model_choices("gemini")


def test_local_provider_is_hidden_until_explicitly_enabled(monkeypatch):
    monkeypatch.delenv(web_app.LOCAL_LLM_ENABLE_ENV, raising=False)

    assert "local" not in web_app._available_ai_providers()
    assert web_app._default_ai_model("gemini") == "gemini-3.5-flash-lite"


def test_local_provider_uses_environment_model_when_enabled(monkeypatch):
    monkeypatch.setenv(web_app.LOCAL_LLM_ENABLE_ENV, "1")
    monkeypatch.setenv(web_app.LOCAL_LLM_MODEL_ENV, "gemma-4-31b-it")

    assert "local" in web_app._available_ai_providers()
    assert web_app._ai_model_choices("local") == ["gemma-4-31b-it"]
    assert web_app._default_ai_model("local") == "gemma-4-31b-it"


def test_saved_local_provider_falls_back_in_memory_when_feature_is_off(
    monkeypatch, tmp_path
):
    settings_file = tmp_path / "default_settings.json"
    original = '{"ai_provider":"local","ai_model":"local-model"}'
    settings_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.delenv(web_app.LOCAL_LLM_ENABLE_ENV, raising=False)

    defaults = web_app.load_defaults()

    assert defaults["ai_provider"] == "gemini"
    assert defaults["ai_model"] == "gemini-3.5-flash-lite"
    assert settings_file.read_text(encoding="utf-8") == original


def test_local_provider_never_resolves_saved_cloud_key(monkeypatch):
    monkeypatch.setattr(
        web_app,
        "load_gemini_api_key",
        lambda: pytest.fail("local provider must not load a cloud API key"),
    )

    assert web_app._resolve_provider_api_key("local", "browser-value") == ""


@pytest.mark.parametrize(
    ("enabled", "expected_local"),
    [(False, False), (True, True)],
)
def test_local_provider_visibility_reaches_gradio_config(
    monkeypatch, tmp_path, enabled, expected_local
):
    monkeypatch.setattr(
        web_app,
        "SETTINGS_FILE",
        tmp_path / "missing-default-settings.json",
    )
    if enabled:
        monkeypatch.setenv(web_app.LOCAL_LLM_ENABLE_ENV, "1")
    else:
        monkeypatch.delenv(web_app.LOCAL_LLM_ENABLE_ENV, raising=False)

    config = web_app.create_ui().get_config_file()
    provider_components = [
        component
        for component in config.get("components", [])
        if component.get("props", {}).get("label") == "AIプロバイダー"
    ]
    assert len(provider_components) == 1
    values = {
        choice[1]
        for choice in provider_components[0]["props"]["choices"]
    }
    assert ("local" in values) is expected_local
    serialized = json.dumps(config, ensure_ascii=False, default=str)
    assert (web_app.LOCAL_LLM_ENABLE_ENV in serialized) is expected_local


def test_saved_api_key_is_not_embedded_in_browser_config(monkeypatch, tmp_path):
    sentinel = "sentinel-secret-must-stay-server-side"
    key_file = tmp_path / ".gemini_key"
    key_file.write_text(sentinel, encoding="utf-8")
    monkeypatch.setattr(web_app, "GEMINI_KEY_FILE", key_file)
    monkeypatch.setattr(
        web_app,
        "SETTINGS_FILE",
        tmp_path / "missing-default-settings.json",
    )

    app = web_app.create_ui()
    client_config = json.dumps(app.get_config_file(), ensure_ascii=False, default=str)

    assert sentinel not in client_config


def test_obs_processing_normalises_shorts_visual_settings():
    settings = web_app._normalise_obs_processing_settings(
        {
            "shorts_blur_strength": 999,
            "shorts_title_position": "invalid",
        },
        defaults={},
    )

    assert settings["shorts_blur_strength"] == 50
    assert settings["shorts_title_position"] == "top"


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
        obs_auto_connect_on_startup=True,
        obs_executable_path="  C:/Portable OBS/obs64.exe  ",
    )

    assert loaded["obs_launch_on_startup"] is True, loaded
    assert loaded["obs_auto_connect_on_startup"] is True, loaded
    assert loaded["obs_executable_path"] == "C:/Portable OBS/obs64.exe", loaded


def test_roundtrip_premiere_executable_path(monkeypatch, tmp_path):
    loaded = _save_with(
        monkeypatch,
        tmp_path,
        premiere_executable_path=(
            "  C:/Program Files/Adobe/Adobe Premiere Pro 2026/"
            "Adobe Premiere Pro.exe  "
        ),
    )

    assert loaded["premiere_executable_path"] == (
        "C:/Program Files/Adobe/Adobe Premiere Pro 2026/"
        "Adobe Premiere Pro.exe"
    )


def test_does_not_touch_real_settings(monkeypatch, tmp_path):
    real = web_app.SETTINGS_FILE
    before = real.read_text(encoding="utf-8") if real.exists() else None
    _save_with(monkeypatch, tmp_path, shorts_crop="right")
    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert before == after, "real default_settings.json must be untouched"
