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


def _event_output_names(
    module: ast.Module,
    function_name: str,
    event_method: str = "click",
) -> list[str]:
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == event_method
        ):
            continue
        callback_name = None
        for keyword in node.keywords:
            if keyword.arg == "fn" and isinstance(keyword.value, ast.Name):
                callback_name = keyword.value.id
        if callback_name != function_name:
            continue
        for keyword in node.keywords:
            if keyword.arg != "outputs":
                continue
            assert isinstance(keyword.value, ast.List), ast.dump(keyword.value)
            names = []
            for elt in keyword.value.elts:
                assert isinstance(elt, ast.Name), ast.dump(elt)
                names.append(elt.id)
            return names
    raise AssertionError(
        f"{event_method}(fn={function_name}, outputs=[...]) not found"
    )


def _event_input_names(
    module: ast.Module,
    function_name: str,
    event_method: str,
) -> list[str]:
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == event_method
        ):
            continue
        callback_name = None
        for keyword in node.keywords:
            if keyword.arg == "fn" and isinstance(keyword.value, ast.Name):
                callback_name = keyword.value.id
        if callback_name != function_name:
            continue
        for keyword in node.keywords:
            if keyword.arg != "inputs":
                continue
            assert isinstance(keyword.value, ast.List), ast.dump(keyword.value)
            names = []
            for elt in keyword.value.elts:
                assert isinstance(elt, ast.Name), ast.dump(elt)
                names.append(elt.id)
            return names
    raise AssertionError(
        f"{event_method}(fn={function_name}, inputs=[...]) not found"
    )


def _string_constant(module: ast.Module, name: str) -> str:
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            assert isinstance(node.value, ast.Constant), ast.dump(node.value)
            assert isinstance(node.value.value, str)
            return node.value.value
    raise AssertionError(f"String constant not found: {name}")


def _top_level_tab_labels(module: ast.Module) -> list[str]:
    for node in module.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "create_ui":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.With):
                continue
            if not any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and isinstance(item.context_expr.func.value, ast.Name)
                and item.context_expr.func.value.id == "gr"
                and item.context_expr.func.attr == "Tabs"
                for item in child.items
            ):
                continue

            labels: list[str] = []
            for statement in child.body:
                if not isinstance(statement, ast.With):
                    continue
                for item in statement.items:
                    context = item.context_expr
                    if not (
                        isinstance(context, ast.Call)
                        and isinstance(context.func, ast.Attribute)
                        and isinstance(context.func.value, ast.Name)
                        and context.func.value.id == "gr"
                        and context.func.attr == "Tab"
                        and context.args
                        and isinstance(context.args[0], ast.Constant)
                        and isinstance(context.args[0].value, str)
                    ):
                        continue
                    labels.append(context.args[0].value)
            return labels
    raise AssertionError("create_ui gr.Tabs() block not found")


def test_detect_phase_signature_matches_detect_inputs():
    module = _module()
    args = _function_args(module, "detect_phase")
    args = [arg for arg in args if arg != "progress"]
    assert args == _event_input_names(module, "detect_phase", "then")
    assert args[-3:] == ["audio_fusion", "audio_alpha", "output_base_dir"]


def test_render_phase_signature_matches_render_inputs():
    module = _module()
    args = _function_args(module, "render_phase")
    args = [arg for arg in args if arg != "progress"]
    render_inputs = _event_input_names(module, "render_phase", "then")
    assert args[0] == "session"
    assert render_inputs[0] == "session_state"
    assert args[1:] == render_inputs[1:]
    assert args[-2:] == ["generate_thumbnails", "karaoke"]


def test_save_defaults_signature_matches_save_button_inputs():
    module = _module()
    args = _function_args(module, "save_defaults")
    assert args == _click_input_names(module, "save_defaults_btn")
    assert args[-3:] == [
        "obs_launch_on_startup",
        "obs_executable_path",
        "obs_auto_connect_on_startup",
    ]


def test_settings_exposes_obs_startup_checkbox_and_executable_path():
    source = WEB_APP.read_text(encoding="utf-8")

    assert 'label="Clip Extractor起動時にOBS Studioも起動"' in source
    assert 'label="起動時にOBS連携も自動開始"' in source
    assert 'label="OBS実行ファイルのパス"' in source
    assert "obs_launch_on_startup" in _click_input_names(_module(), "save_defaults_btn")
    assert "obs_auto_connect_on_startup" in _click_input_names(
        _module(),
        "save_defaults_btn",
    )
    assert "obs_executable_path" in _click_input_names(_module(), "save_defaults_btn")


def test_premiere_button_and_render_state_are_wired():
    module = _module()
    source = WEB_APP.read_text(encoding="utf-8")

    assert 'label="Premiere Pro実行ファイルのパス"' in source
    assert '"Premiere Proで編集"' in source
    assert _click_input_names(module, "premiere_edit_btn") == [
        "premiere_job_state",
        "premiere_include_shorts",
        "premiere_executable_path",
    ]
    assert _event_output_names(
        module,
        "render_phase",
        "then",
    )[-1] == "premiere_job_state"
    assert source.count("fn=clear_premiere_job_state") == 2
    assert "premiere_executable_path" in _click_input_names(
        module,
        "save_defaults_btn",
    )


def test_direct_web_entry_applies_saved_obs_launch_setting():
    source = WEB_APP.read_text(encoding="utf-8")

    assert "launch_obs_from_settings(SETTINGS_FILE)" in source
    assert "schedule_obs_auto_connect()" in source


def test_launcher_schedules_saved_obs_auto_connect_setting():
    source = (WEB_APP.parent / "launcher.py").read_text(encoding="utf-8")

    assert "schedule_obs_auto_connect" in source
    assert "schedule_obs_auto_connect()" in source


def test_obs_start_signature_matches_inputs_and_passes_auto_append():
    module = _module()
    args = _function_args(module, "start_obs_watch")
    assert args == [
        "method", "host", "port", "password", "save_password", "stop_event",
        "watch_folder", "auto_process", "auto_append_youtube", "num_clips",
        "output_mode", "generate_shorts", "ai_provider", "whisper_model",
        "output_base_dir",
    ]
    assert _click_input_names(module, "obs_start_btn") == [
        "obs_trigger_radio", "obs_host", "obs_port", "obs_password",
        "obs_save_password", "obs_stop_event_radio", "obs_watch_folder",
        "obs_auto_process", "auto_append_youtube", "num_clips", "output_mode",
        "generate_shorts", "ai_provider", "whisper_model", "output_base_dir",
    ]


def test_obs_help_explains_recording_primary_setup_and_archive_fallback():
    source = WEB_APP.read_text(encoding="utf-8")

    assert "**OBS録画を既定の素材**" in source
    assert "OBS録画優先：失敗時のみ完成アーカイブ" in source
    assert "YouTube完成アーカイブのみ：再エンコード後" in source
    assert "設定 → 出力" in source
    assert "出力モードを「詳細」" in source
    assert "録画出力先" in source
    assert "MKV（推奨）" in source
    assert "配信エンコーダーを使用" in source
    assert "設定 → 一般 → 出力" in source
    assert "配信時に自動的に録画する" in source
    assert "配信開始と同時に録画タイマーも動き" in source
    assert "https://obsproject.com/kb/standard-recording-output-guide" in source
    assert "https://obsproject.com/kb/obs-studio-overview" in source
    assert "録画処理が失敗した時だけ" in source
    assert "アーカイブをDLし直さず" in source
    assert "タイムスタンプだけをYouTube概要欄へ自動反映" in source
    assert "再エンコード完了" in source
    assert "完成アーカイブ待機は最大6時間" in source
    assert "完成アーカイブURLを貼って生成できます" in source
    assert "公開または限定公開" in source
    assert "post-live DVR" in source
    assert "アーカイブへの" in source
    assert "フォールバックとYouTube概要欄への自動反映は行いません" in source
    assert 'defaults.get("obs_stop_event", "record")' in source


def test_obs_tab_is_second_in_top_navigation():
    assert _top_level_tab_labels(_module()) == [
        "Input / 入力",
        "OBS連携 / OBS",
        "Settings / 設定",
        "Output / 出力",
    ]


def test_obs_help_is_collapsed_in_accordion_by_default():
    module = _module()
    expected_label = "配信終了で自動切り抜き — 設定手順・動作説明"

    for node in ast.walk(module):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            context = item.context_expr
            if not (
                isinstance(context, ast.Call)
                and isinstance(context.func, ast.Attribute)
                and isinstance(context.func.value, ast.Name)
                and context.func.value.id == "gr"
                and context.func.attr == "Accordion"
                and context.args
                and isinstance(context.args[0], ast.Constant)
                and context.args[0].value == expected_label
            ):
                continue

            open_keyword = next(
                (keyword for keyword in context.keywords if keyword.arg == "open"),
                None,
            )
            assert open_keyword is not None
            assert isinstance(open_keyword.value, ast.Constant)
            assert open_keyword.value.value is False
            assert any(
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "gr"
                and child.func.attr == "Markdown"
                for child in ast.walk(node)
            )
            return

    raise AssertionError("OBS help must be rendered in a collapsed accordion")


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
