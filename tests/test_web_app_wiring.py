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
