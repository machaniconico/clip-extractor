"""Documentation pointers are present without rendering the Gradio UI."""

from pathlib import Path


def test_terms_and_privacy_pointers_in_ui_source():
    source = (Path(__file__).resolve().parents[1] / "web_app.py").read_text(
        encoding="utf-8"
    )
    x_description = source.split('with gr.Accordion("X 配信開始ポスト"', 1)[1].split(
        "obs_x_post_on_stream_start =", 1
    )[0]
    settings_description = source.split('with gr.Tab("Settings / 設定")', 1)[1].split(
        "save_api_key_btn =", 1
    )[0]
    for description in (x_description, settings_description):
        assert "`docs/TERMS.md`" in description
        assert "`docs/PRIVACY.md`" in description
    assert "Gemini の従量課金・無料枠のデータ利用" in settings_description
