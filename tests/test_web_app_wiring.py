"""AST regression tests for web_app function/input ordering."""

import ast
import re
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


def _click_output_names(module: ast.Module, button_name: str) -> list[str]:
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "click"
            and isinstance(func.value, ast.Name)
            and func.value.id == button_name
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg != "outputs":
                continue
            if isinstance(keyword.value, ast.Name):
                return [keyword.value.id]
            assert isinstance(keyword.value, ast.List), ast.dump(keyword.value)
            names: list[str] = []
            for elt in keyword.value.elts:
                assert isinstance(elt, ast.Name), ast.dump(elt)
                names.append(elt.id)
            return names
    raise AssertionError(f"{button_name}.click(outputs=...) not found")


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
            if isinstance(keyword.value, ast.Name):
                return [keyword.value.id]
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
            if isinstance(keyword.value, ast.Name):
                return [keyword.value.id]
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


def _css_properties(css: str, selector: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector not in {item.strip() for item in selectors.split(",")}:
            continue
        for declaration in body.split(";"):
            if ":" not in declaration:
                continue
            name, value = declaration.split(":", 1)
            properties[name.strip()] = value.strip()
    return properties


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
    assert args[-4:] == [
        "audio_fusion",
        "audio_alpha",
        "output_base_dir",
        "generate_shorts",
    ]


def test_render_phase_signature_matches_render_inputs():
    module = _module()
    args = _function_args(module, "render_phase")
    args = [arg for arg in args if arg != "progress"]
    render_inputs = _event_input_names(module, "render_phase", "then")
    assert args[0] == "session"
    assert render_inputs[0] == "session_state"
    assert args[1:] == render_inputs[1:]
    assert args[-4:] == [
        "generate_thumbnails",
        "karaoke",
        "shorts_blur_strength",
        "shorts_title_position",
    ]
    assert "shorts_blur_strength" in args
    assert "shorts_title_position" in args


def test_save_defaults_signature_matches_save_button_inputs():
    module = _module()
    args = _function_args(module, "save_defaults")
    assert args == _click_input_names(module, "save_defaults_btn")
    assert args[-3:] == [
        "obs_launch_on_startup",
        "obs_executable_path",
        "obs_auto_connect_on_startup",
    ]
    assert "shorts_blur_strength" in args
    assert "shorts_title_position" in args


def test_shorts_visual_controls_are_available_for_input_and_obs():
    module = _module()
    source = WEB_APP.read_text(encoding="utf-8")

    assert source.count('label="背景のぼかし強度"') == 2
    assert source.count('label="タイトルの配置"') == 2
    assert source.count("fn=shorts_blur_visibility") == 2
    for event_name in ("render_phase", "maybe_render_phase"):
        event_inputs = _event_input_names(module, event_name, "then")
        assert "shorts_blur_strength" in event_inputs
        assert "shorts_title_position" in event_inputs
    for button in ("save_defaults_btn", "input_save_defaults_btn"):
        click_inputs = _click_input_names(module, button)
        assert "shorts_blur_strength" in click_inputs
        assert "shorts_title_position" in click_inputs
    for button in ("obs_save_processing_btn", "obs_start_btn"):
        click_inputs = _click_input_names(module, button)
        assert "obs_shorts_blur_strength" in click_inputs
        assert "obs_shorts_title_position" in click_inputs


def test_input_tab_exposes_default_save_button_with_same_settings():
    module = _module()
    source = WEB_APP.read_text(encoding="utf-8")

    input_tab = source.index('with gr.Tab("Input / 入力")')
    obs_tab = source.index('with gr.Tab("OBS連携 / OBS")')
    button = source.index("input_save_defaults_btn =")

    assert input_tab < button < obs_tab
    assert '"現在のInput設定をデフォルトに保存"' in source
    assert _click_input_names(
        module,
        "input_save_defaults_btn",
    ) == _click_input_names(module, "save_defaults_btn")
    assert _click_output_names(module, "input_save_defaults_btn") == [
        "input_save_defaults_msg",
    ]


def test_input_workspace_fills_desktop_width_without_reordering_mobile_flow():
    source = WEB_APP.read_text(encoding="utf-8")

    input_tab = source.index('with gr.Tab("Input / 入力")')
    obs_tab = source.index('with gr.Tab("OBS連携 / OBS")')
    source_row = source.index(
        'with gr.Row(elem_classes="input-source-row")',
        input_tab,
    )
    url_column = source.index(
        'elem_classes="input-url-column"',
        source_row,
    )
    file_column = source.index(
        'elem_classes="input-file-column"',
        url_column,
    )
    settings_grid = source.index(
        'with gr.Row(elem_classes="input-settings-grid")',
        file_column,
    )
    core_settings = source.index(
        'elem_classes="input-core-settings-column"',
        settings_grid,
    )
    shorts_settings = source.index(
        'elem_classes="input-shorts-settings-column"',
        core_settings,
    )
    actions_column = source.index(
        'with gr.Column(elem_classes="input-actions-column"):',
        shorts_settings,
    )
    review_panel = source.index("as review_panel:", actions_column)

    assert input_tab < source_row < url_column < file_column < settings_grid
    assert settings_grid < core_settings < shorts_settings < actions_column
    assert actions_column < review_panel < obs_tab

    url_controls = source[url_column:file_column]
    assert "input_url = gr.Textbox(" in url_controls
    assert "input_file = gr.File(" not in url_controls

    file_controls = source[file_column:settings_grid]
    assert "input_file = gr.File(" in file_controls
    assert "height=128" in file_controls

    core_controls = source[core_settings:shorts_settings]
    for control in (
        "num_clips = gr.Number(",
        "min_duration = gr.Number(",
        "max_duration = gr.Number(",
        "output_mode = gr.Radio(",
        "generate_thumbnails = gr.Checkbox(",
        "audio_fusion = gr.Checkbox(",
        "audio_alpha = gr.Slider(",
        "generate_zip = gr.Checkbox(",
        "upload_to_drive = gr.Checkbox(",
    ):
        assert control in core_controls

    shorts_controls = source[shorts_settings:actions_column]
    for control in (
        "generate_shorts = gr.Checkbox(",
        "shorts_mode = gr.Radio(",
        "shorts_blur_strength = gr.Slider(",
        "shorts_crop = gr.Radio(",
        "shorts_title = gr.Checkbox(",
        "shorts_title_position = gr.Radio(",
        "karaoke = gr.Checkbox(",
    ):
        assert control in shorts_controls

    action_controls = source[actions_column:review_panel]
    for control in (
        "detect_btn = gr.Button(",
        "render_btn = gr.Button(",
        "auto_run_both = gr.Checkbox(",
        "input_save_defaults_btn = gr.Button(",
        "input_save_defaults_msg = gr.Textbox(",
    ):
        assert control in action_controls

    assert 'gap: var(--input-workspace-gap);' in source
    assert "@media (max-width: 899px)" in source
    for responsive_class in (
        ".input-source-row",
        ".input-settings-grid",
        ".input-url-column",
        ".input-file-column",
        ".input-core-settings-column",
        ".input-shorts-settings-column",
    ):
        assert responsive_class in source


def test_input_workspace_separates_sources_and_hides_native_scroll_controls():
    css = _string_constant(_module(), "APP_CSS")

    root = _css_properties(css, ".gradio-container")
    assert root["--input-source-settings-gap"] == "1.25rem"
    assert "var(--block-background-fill)" in root["--input-source-tint"]
    assert "var(--primary-500)" in root["--input-source-tint"]
    assert "var(--border-color-primary)" in root["--input-source-border"]

    settings = _css_properties(css, ".input-settings-grid")
    assert settings["margin-top"] == "var(--input-source-settings-gap)"

    for selector in (
        ".input-core-settings-column > .input-settings-title",
        ".input-shorts-settings-column > .input-settings-title",
    ):
        settings_title = _css_properties(css, selector)
        assert settings_title["overflow"] == "visible !important"

    source_control = _css_properties(css, ".input-source-control")
    assert source_control["background"] == "var(--input-source-tint) !important"
    assert source_control["border-color"] == "var(--input-source-border) !important"
    assert WEB_APP.read_text(encoding="utf-8").count(
        'elem_classes="input-source-control"'
    ) == 2

    number_input = _css_properties(
        css,
        '.input-settings-grid input[type="number"]',
    )
    assert number_input["-moz-appearance"] == "textfield"
    for selector in (
        '.input-settings-grid input[type="number"]::-webkit-inner-spin-button',
        '.input-settings-grid input[type="number"]::-webkit-outer-spin-button',
    ):
        properties = _css_properties(css, selector)
        assert properties["-webkit-appearance"] == "none"
        assert properties["margin"] == "0"


def test_obs_connection_workspace_uses_left_space_without_reordering_mobile_flow():
    module = _module()
    source = WEB_APP.read_text(encoding="utf-8")

    obs_tab = source.index('with gr.Tab("OBS連携 / OBS")')
    settings_tab = source.index('with gr.Tab("Settings / 設定")')
    workspace = source.index(
        'with gr.Row(elem_classes="obs-connection-workspace")',
        obs_tab,
    )
    trigger_column = source.index(
        'elem_classes="obs-trigger-column"',
        workspace,
    )
    connection_column = source.index(
        'elem_classes="obs-connection-settings-column"',
        trigger_column,
    )
    actions_column = source.index(
        'elem_classes="obs-connection-actions-column"',
        connection_column,
    )
    processing_settings = source.index(
        '"OBS自動処理の生成設定"',
        actions_column,
    )

    assert obs_tab < workspace < trigger_column < connection_column
    assert connection_column < actions_column < processing_settings < settings_tab

    trigger_controls = source[trigger_column:connection_column]
    for control in (
        "obs_trigger_radio =",
        "obs_stop_event_radio =",
        "obs_auto_process =",
    ):
        assert control in trigger_controls

    connection_controls = source[connection_column:actions_column]
    for control in (
        "obs_host =",
        "obs_port =",
        "obs_save_password =",
        "obs_password =",
        "obs_watch_folder =",
        "obs_browse_folder_btn =",
    ):
        assert control in connection_controls

    action_controls = source[actions_column:processing_settings]
    for control in (
        "obs_start_btn =",
        "obs_stop_btn =",
        "obs_refresh_btn =",
        "obs_status_box =",
    ):
        assert control in action_controls
    assert "lines=8" in action_controls

    css = _string_constant(module, "APP_CSS")
    workspace_css = _css_properties(css, ".obs-connection-workspace")
    assert workspace_css["grid-template-areas"] == (
        '"trigger connection" "actions connection"'
    )
    assert workspace_css["grid-template-rows"] == "max-content 1fr"
    assert workspace_css["flex-direction"] == "column !important"
    assert _css_properties(css, ".obs-trigger-column")["grid-area"] == "trigger"
    assert (
        _css_properties(css, ".obs-connection-settings-column")["grid-area"]
        == "connection"
    )
    assert (
        _css_properties(css, ".obs-connection-actions-column")["grid-area"]
        == "actions"
    )

    assert _click_output_names(module, "obs_start_btn") == ["obs_status_box"]
    assert _click_output_names(module, "obs_stop_btn") == ["obs_status_box"]
    assert _click_output_names(module, "obs_refresh_btn") == ["obs_status_box"]


def test_clip_duration_controls_live_in_their_workflow_tabs():
    source = WEB_APP.read_text(encoding="utf-8")

    input_tab = source.index('with gr.Tab("Input / 入力")')
    obs_tab = source.index('with gr.Tab("OBS連携 / OBS")')
    settings_tab = source.index('with gr.Tab("Settings / 設定")')
    output_tab = source.index('with gr.Tab("Output / 出力")')

    input_min = source.index("min_duration = gr.Number(", input_tab)
    input_max = source.index("max_duration = gr.Number(", input_min)
    obs_min = source.index("obs_min_duration = gr.Number(", obs_tab)
    obs_max = source.index("obs_max_duration = gr.Number(", obs_min)

    assert input_tab < input_min < input_max < obs_tab
    assert obs_tab < obs_min < obs_max < settings_tab
    assert 'value=defaults["min_duration"]' in source[input_min:input_max]
    assert 'value=defaults["max_duration"]' in source[input_max:obs_tab]
    assert (
        'value=obs_processing_defaults["min_duration"]'
        in source[obs_min:obs_max]
    )
    assert (
        'value=obs_processing_defaults["max_duration"]'
        in source[obs_max:settings_tab]
    )
    settings_source = source[settings_tab:output_tab]
    assert "min_duration = gr.Number(" not in settings_source
    assert "max_duration = gr.Number(" not in settings_source


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
    assert "OBSが後から起動した場合も待機を続け" in source
    assert "配信開始を検知" in source


def test_obs_recording_folder_picker_updates_the_watch_folder_textbox():
    module = _module()
    source = WEB_APP.read_text(encoding="utf-8")

    assert '"📁 録画出力フォルダを選択…"' in source
    assert _event_input_names(
        module,
        "pick_obs_watch_folder_dialog",
        "click",
    ) == ["obs_watch_folder"]
    assert _event_output_names(
        module,
        "pick_obs_watch_folder_dialog",
        "click",
    ) == ["obs_watch_folder"]


def test_obs_generation_checkboxes_are_independent_and_profile_backed():
    source = WEB_APP.read_text(encoding="utf-8")

    assert 'label="切り抜き動画を生成（OBSでは固定ON）"' not in source
    assert 'label="タイムスタンプ(概要欄)を生成（OBSでは固定ON）"' not in source
    assert 'value=obs_processing_defaults["enable_clips"]' in source
    assert 'value=obs_processing_defaults["enable_chapters"]' in source
    assert 'value=obs_processing_defaults["generate_shorts"]' in source
    assert 'info="OBS録画から切り抜きを生成します"' in source
    assert 'info="配信終了後にYouTube概要欄へ反映します"' in source
    assert 'label="ショート動画 (9:16) を生成"' in source
    assert "通常の切り抜きがOFFでも生成できます" in source
    assert "3つのうち少なくとも1つはONにしてください" in source
    assert source.count(
        "切り抜き動画とショート動画が両方無効のときだけ使われます"
    ) == 2


def test_input_shorts_are_an_independent_generation_output():
    source = WEB_APP.read_text(encoding="utf-8")

    assert 'label="ショート動画 (9:16) も生成"' not in source
    assert 'label="ショート動画 (9:16) を生成"' in source
    assert "切り抜き動画・ショート動画・タイムスタンプ" in source
    assert "通常の切り抜きがOFFでも生成できます" in source
    assert "OBS自動処理の生成設定" in source


def test_obs_prompt_confirmation_is_wired_to_saved_setting_and_actions():
    module = _module()
    source = WEB_APP.read_text(encoding="utf-8")

    assert 'label="プロンプトの入力を確認しないで自動で生成開始"' in source
    assert "confirm_before_auto_process" in source
    assert "_obs_confirmation_poll" in source
    assert "_obs_confirm_generation" in source
    assert "_obs_confirm_generation_with_prompt" in source
    assert "_obs_skip_generation" in source
    assert '"そのまま生成開始"' in source
    assert '"プロンプトを入力して開始"' in source
    assert '"今回は生成しない"' in source
    assert 'label="今回の生成プロンプト"' in source
    assert "未入力ならLLMにお任せします" in source
    assert _event_input_names(module, "_obs_confirmation_poll", "tick") == [
        "obs_confirmation_request_token"
    ]
    assert _event_output_names(module, "_obs_confirmation_poll", "tick") == [
        "obs_confirmation_group",
        "obs_confirmation_message",
        "obs_confirmation_prompt",
        "obs_confirmation_request_token",
    ]
    assert _event_input_names(
        module,
        "_obs_confirm_generation_with_prompt",
        "click",
    ) == ["obs_confirmation_prompt"]


def test_input_accepts_youtube_and_twitch_urls():
    source = WEB_APP.read_text(encoding="utf-8")

    assert 'label="動画URL（YouTube / Twitch）"' in source
    assert "https://twitch.tv/videos/..." in source
    assert "Twitch入力ではタイムスタンプを生成しません" in source


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


def test_obs_start_signature_matches_inputs_and_passes_obs_profile():
    module = _module()
    args = _function_args(module, "start_obs_watch")
    assert args == [
        "method", "host", "port", "password", "save_password", "stop_event",
        "watch_folder", "auto_process", "auto_append_youtube", "num_clips",
        "output_mode", "generate_shorts", "ai_provider", "whisper_model",
        "output_base_dir", "obs_enable_clips", "obs_clip_prompt",
        "obs_enable_chapters", "obs_chapter_prompt", "obs_min_duration",
        "obs_max_duration", "obs_shorts_mode", "obs_shorts_crop",
        "obs_shorts_title", "obs_generate_thumbnails", "obs_audio_fusion",
        "obs_audio_alpha", "obs_karaoke",
        "obs_auto_start_without_prompt_confirmation",
        "obs_shorts_blur_strength", "obs_shorts_title_position",
    ]
    assert _click_input_names(module, "obs_start_btn") == [
        "obs_trigger_radio", "obs_host", "obs_port", "obs_password",
        "obs_save_password", "obs_stop_event_radio", "obs_watch_folder",
        "obs_auto_process", "obs_auto_append_youtube", "obs_num_clips", "obs_output_mode",
        "obs_generate_shorts", "ai_provider", "whisper_model", "output_base_dir",
        "obs_enable_clips", "obs_clip_prompt", "obs_enable_chapters",
        "obs_chapter_prompt", "obs_min_duration", "obs_max_duration",
        "obs_shorts_mode", "obs_shorts_crop", "obs_shorts_title",
        "obs_generate_thumbnails", "obs_audio_fusion", "obs_audio_alpha",
        "obs_karaoke", "obs_auto_start_without_prompt_confirmation",
        "obs_shorts_blur_strength", "obs_shorts_title_position",
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
    assert "プロンプトの入力を確認しないで自動で生成開始" in source


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
