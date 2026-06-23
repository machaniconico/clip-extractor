"""Regression tests for clipper._shorts_crop_filter.

clipper.py keeps subprocess/ffmpeg calls inside functions, so the module
imports cleanly without ffmpeg present. Guard with importorskip anyway in
case an import-time dependency is ever introduced.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

clipper = pytest.importorskip("clipper")
_shorts_crop_filter = clipper._shorts_crop_filter
_shorts_base_vf = clipper._shorts_base_vf


def test_center_default_crop_and_scale():
    f = _shorts_crop_filter()
    assert "(iw-ih*9/16)/2" in f, f"center x missing: {f}"
    assert "scale=1080:1920" in f, f"scale missing: {f}"


def test_center_explicit_matches_default():
    assert _shorts_crop_filter("center") == _shorts_crop_filter(), (
        "explicit 'center' should match the default"
    )
    assert _shorts_base_vf("crop", "center") == _shorts_crop_filter("center")
    assert (
        _shorts_base_vf("crop", "center")
        == "crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920"
    )


def test_left_crop_x():
    f = _shorts_crop_filter("left")
    assert ":0:0" in f, f"left x missing: {f}"
    assert "scale=1080:1920" in f, f"scale missing: {f}"


def test_right_crop_x():
    f = _shorts_crop_filter("right")
    assert "iw-ih*9/16:0" in f, f"right x missing: {f}"
    assert "scale=1080:1920" in f, f"scale missing: {f}"


@pytest.mark.parametrize("crop_x", ["center", "left", "right"])
def test_all_patterns_start_with_crop(crop_x):
    f = _shorts_crop_filter(crop_x)
    assert f.startswith("crop="), f"{crop_x!r} filter must start with crop=: {f}"


def test_shorts_base_vf_pad():
    assert _shorts_base_vf("pad") == (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2"
    )


def test_shorts_base_vf_blur():
    assert _shorts_base_vf("blur") == (
        "split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=20[bgblur];"
        "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgscaled];"
        "[bgblur][fgscaled]overlay=(W-w)/2:(H-h)/2"
    )


def _leading_filter_labels(chain: str) -> list[str]:
    labels: list[str] = []
    while chain.startswith("["):
        end = chain.index("]")
        labels.append(chain[1:end])
        chain = chain[end + 1 :]
    return labels


def _trailing_filter_labels(chain: str) -> list[str]:
    labels: list[str] = []
    while chain.endswith("]"):
        start = chain.rindex("[")
        labels.append(chain[start + 1 : -1])
        chain = chain[:start]
    return labels


def test_shorts_blur_filter_does_not_redefine_output_labels():
    output_labels: list[str] = []
    for chain in clipper._SHORTS_BLUR_FILTER.split(";"):
        input_labels = _leading_filter_labels(chain)
        chain_output_labels = _trailing_filter_labels(chain)
        assert set(input_labels).isdisjoint(chain_output_labels)
        output_labels.extend(chain_output_labels)

    assert len(output_labels) == len(set(output_labels))
    assert set(output_labels) == {"bg", "fg", "bgblur", "fgscaled"}


def test_title_drawtext_escapes_specials(monkeypatch):
    monkeypatch.setattr(clipper, "_resolve_title_fontfile", lambda font_name: None)
    font_config = type("FontConfig", (), {"font_name": "Noto Sans JP"})()

    f = clipper._build_title_drawtext("A:B's 50% C\\D", font_config)

    assert f.startswith("drawtext=")
    assert "font='Noto Sans JP'" in f
    assert "text='A\\:B\\'s 50\\% C\\\\D'" in f
    assert "fontsize=80" in f
    assert "fontcolor=white" in f
    assert "box=1" in f
    assert "boxcolor=black@0.5" in f
    assert "boxborderw=24" in f
    assert "x=(w-text_w)/2" in f
    assert "y=140" in f
    assert "enable='lt(t\\,4)'" in f


def test_title_wraps_long_japanese_with_real_newline(monkeypatch):
    monkeypatch.setattr(clipper, "_resolve_title_fontfile", lambda font_name: None)
    font_config = type("FontConfig", (), {"font_name": "Noto Sans JP"})()
    title = "あいうえおかきくけこさしすせそたちつてと"

    wrapped = clipper._wrap_title_text(title)
    assert "\n" in wrapped
    for line in wrapped.splitlines():
        assert sum(clipper._title_char_width(ch) for ch in line) <= 28

    f = clipper._build_title_drawtext(title, font_config)
    # drawtext breaks lines on an actual newline (0x0A). The literal sequence
    # "\n" would render a stray "n" instead of wrapping, so it must NOT appear.
    assert "\n" in f
    assert r"\n" not in f


def test_title_cluster_width_handles_zero_advance_components():
    assert clipper._title_cluster_width("e\u0301") == 1
    assert clipper._title_cluster_width("1\ufe0e") == 1
    assert clipper._title_cluster_width("\U0001F44D\U0001F3FD") == 2
    assert clipper._title_cluster_width("あ") == 2
    assert clipper._title_cluster_width("\U0001F1EF\U0001F1F5") == 2
    assert list(clipper._title_grapheme_clusters("\U0001F1EF\U0001F1F5")) == [
        "\U0001F1EF\U0001F1F5"
    ]


@pytest.mark.parametrize(
    "cluster",
    [
        "👨\u200d👩\u200d👧\u200d👦",
        "🇯🇵",
        "👍🏽",
        "✌️",
        "e\u0301",
    ],
)
def test_title_wrap_preserves_grapheme_clusters(cluster):
    wrapped = clipper._wrap_title_text(f"あ{cluster}い", fullwidth_chars=1)

    assert cluster in wrapped
    assert any(cluster in line for line in wrapped.splitlines())


def test_title_drawtext_uses_fontfile_fallback(monkeypatch):
    monkeypatch.setattr(clipper, "_resolve_title_fontfile", lambda font_name: "/tmp/NotoSansJP.ttf")
    font_config = type("FontConfig", (), {"font_name": "Missing Japanese Font"})()

    f = clipper._build_title_drawtext("タイトル", font_config)

    assert "font='Missing Japanese Font'" in f
    assert "fontfile='/tmp/NotoSansJP.ttf'" in f
