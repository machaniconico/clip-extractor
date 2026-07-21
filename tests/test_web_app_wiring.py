"""AST regression tests for web_app function/input ordering."""

import ast
from pathlib import Path


WEB_APP = Path(__file__).parent.parent / "web_app.py"


def _module() -> ast.Module:
    return ast.parse(WEB_APP.read_text(encoding="utf-8"))


def _function_args(module: ast.Module, name: str) -> list[str]:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return [arg.arg for arg in node.args.args]
    raise AssertionError(f"Function not found: {name}")


def _click_input_names(module: ast.Module, button_name: str) -> list[str]:
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "click"
            and isinstance(func.value, ast.Name)
            and func.value.id == button_name
        ):
            for keyword in node.keywords:
                if keyword.arg == "inputs":
                    assert isinstance(keyword.value, ast.List)
                    names: list[str] = []
                    for elt in keyword.value.elts:
                        assert isinstance(elt, ast.Name), ast.dump(elt)
                        names.append(elt.id)
                    return names
    raise AssertionError(f"{button_name}.click(inputs=[...]) not found")


def _string_constant(module: ast.Module, name: str) -> str:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            assert isinstance(node.value, ast.Constant), ast.dump(node.value)
            assert isinstance(node.value.value, str)
            return node.value.value
    raise AssertionError(f"String constant not found: {name}")


def test_detect_phase_signature_matches_detect_inputs():
    module = _module()
    args = _function_args(module, "detect_phase")
    args = [arg for arg in args if arg != "progress"]
    assert args == _click_input_names(module, "detect_btn")
    assert args[-3:] == ["audio_fusion", "audio_alpha", "output_base_dir"]


def test_render_phase_signature_matches_render_inputs():
    module = _module()
    args = _function_args(module, "render_phase")
    args = [arg for arg in args if arg != "progress"]
    click_inputs = _click_input_names(module, "render_btn")
    assert args[0] == "session"
    assert click_inputs[0] == "session_state"
    assert args[1:] == click_inputs[1:]
    assert args[-2:] == ["generate_thumbnails", "karaoke"]


def test_save_defaults_signature_matches_save_button_inputs():
    module = _module()
    args = _function_args(module, "save_defaults")
    assert args == _click_input_names(module, "save_defaults_btn")
    assert args[-4:] == ["generate_thumbnails", "audio_fusion", "audio_alpha", "karaoke"]


def test_obs_start_signature_matches_inputs_and_passes_auto_append():
    module = _module()
    args = _function_args(module, "start_obs_watch")
    assert args == [
        "method", "host", "port", "password", "stop_event", "watch_folder",
        "auto_process", "auto_append_youtube", "num_clips", "output_mode",
        "generate_shorts", "ai_provider", "whisper_model", "output_base_dir",
    ]
    assert _click_input_names(module, "obs_start_btn") == [
        "obs_trigger_radio", "obs_host", "obs_port", "obs_password",
        "obs_stop_event_radio", "obs_watch_folder", "obs_auto_process",
        "auto_append_youtube", "num_clips", "output_mode", "generate_shorts",
        "ai_provider", "whisper_model", "output_base_dir",
    ]


def test_obs_help_explains_archive_clips_and_timestamps_without_recording():
    source = WEB_APP.read_text(encoding="utf-8")

    assert "録画は不要" in source
    assert "切り抜きとタイムスタンプを両方生成" in source
    assert "公開または限定公開" in source
    assert "概要欄に自動追加" in source


def test_google_unverified_app_guide_is_actionable_and_rendered():
    module = _module()
    guide_name = "GOOGLE_OAUTH_UNVERIFIED_GUIDE_MD"
    guide = _string_constant(module, guide_name)

    assert "このアプリは Google で確認されていません" in guide
    assert "https://console.cloud.google.com/auth/audience" in guide
    assert "正しいプロジェクト" in guide
    assert "テストユーザー" in guide
    assert "ユーザーを追加" in guide
    assert "保存" in guide
    assert "詳細" in guide
    assert "認証する" in guide

    rendered = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gr"
        and node.func.attr == "Markdown"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == guide_name
        for node in ast.walk(module)
    )
    assert rendered, "Google OAuth warning guide must be rendered in the Gradio UI"


def test_api_key_and_credentials_guides_are_short_and_actionable():
    module = _module()
    source = WEB_APP.read_text(encoding="utf-8")
    expected = {
        "GEMINI_API_KEY_GUIDE_MD": (
            18,
            [
                "https://aistudio.google.com/apikey",
                "APIキーを作成",
                "コピー",
                "APIキー",
                "このキーを保存",
                "credentials.json",
            ],
        ),
        "GOOGLE_CREDENTIALS_SETUP_GUIDE_MD": (
            36,
            [
                "https://console.cloud.google.com/",
                "YouTube Data API v3",
                "Google Drive API",
                "https://console.cloud.google.com/auth/audience",
                "テストユーザー",
                "デスクトップ アプリ",
                "JSON をダウンロード",
                "認証する",
            ],
        ),
    }

    for name, (max_lines, required_phrases) in expected.items():
        guide = _string_constant(module, name)
        assert len(guide.strip().splitlines()) <= max_lines
        for phrase in required_phrases:
            assert phrase in guide

        rendered = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "gr"
            and node.func.attr == "Markdown"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == name
            for node in ast.walk(module)
        )
        assert rendered, f"{name} must be rendered in the Gradio UI"

    assert "細かく分けた 19 step" not in source
