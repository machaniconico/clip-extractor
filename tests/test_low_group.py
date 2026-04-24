"""Unit tests for LOW-priority improvements (US-003 from the teams review).

Covers:
- downloader.build_output_template — byte-limited title to keep Windows
  MAX_PATH safe with Japanese titles.
- web_app.load_gemini_api_key — env var beats the on-disk .gemini_key file.
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---- downloader.build_output_template ---------------------------------

def test_output_template_uses_byte_limit():
    from downloader import TITLE_BYTE_LIMIT, build_output_template

    tmpl = build_output_template(Path("/tmp/out"))
    assert "%(title)." in tmpl, f"missing title formatter: {tmpl}"
    assert f".{TITLE_BYTE_LIMIT}B" in tmpl, (
        f"template does not use byte limit {TITLE_BYTE_LIMIT}B: {tmpl}"
    )
    assert tmpl.endswith(".%(ext)s"), f"template must end with .%(ext)s: {tmpl}"


def test_output_template_default_byte_limit_is_100():
    from downloader import TITLE_BYTE_LIMIT

    assert TITLE_BYTE_LIMIT == 100


def test_output_template_respects_output_dir():
    from downloader import build_output_template

    out_dir = Path("/foo/bar/baz")
    tmpl = build_output_template(out_dir)
    # Normalise separators cross-platform — Windows uses "\" in paths.
    normalised = tmpl.replace("\\", "/")
    assert "/foo/bar/baz/" in normalised, (
        f"template must contain the output dir prefix: {tmpl}"
    )


def test_output_template_expanded_length_bounded_for_japanese_title():
    """A real 100-char Japanese title would blow up under %s; the byte
    limit keeps the expanded filename within Windows MAX_PATH after joining.
    We approximate by asserting the template contains the byte limiter
    and the output_dir prefix only (yt-dlp does the expansion at runtime)."""
    from downloader import build_output_template

    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td) / "really" / "long" / "onedrive" / "style" / "path"
        tmpl = build_output_template(out_dir)
        # Template itself is small (dir + formatter), but the expanded
        # filename would be: dir + title[:100 bytes] + .ext. With a 260-byte
        # MAX_PATH and our 100-byte title cap, even 150-byte dirs are safe.
        assert len(tmpl.encode("utf-8")) < 400, (
            f"template itself shouldn't be huge: {len(tmpl.encode('utf-8'))} bytes"
        )
        assert ".100B" in tmpl


# ---- web_app.load_gemini_api_key ---------------------------------------

def test_gemini_key_env_var_wins_over_file():
    import web_app

    with tempfile.TemporaryDirectory() as td:
        fake_file = Path(td) / ".gemini_key"
        fake_file.write_text("from-file", encoding="utf-8")
        with mock.patch.object(web_app, "GEMINI_KEY_FILE", fake_file):
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "from-env"}, clear=False):
                got = web_app.load_gemini_api_key()
        assert got == "from-env", f"expected env to win, got {got!r}"


def test_gemini_key_falls_back_to_file():
    import web_app

    with tempfile.TemporaryDirectory() as td:
        fake_file = Path(td) / ".gemini_key"
        fake_file.write_text("from-file", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with mock.patch.object(web_app, "GEMINI_KEY_FILE", fake_file):
            with mock.patch.dict(os.environ, env, clear=True):
                got = web_app.load_gemini_api_key()
        assert got == "from-file"


def test_gemini_key_empty_env_falls_back_to_file():
    """An empty-string env var should be treated as unset (fall through to file)."""
    import web_app

    with tempfile.TemporaryDirectory() as td:
        fake_file = Path(td) / ".gemini_key"
        fake_file.write_text("from-file", encoding="utf-8")
        with mock.patch.object(web_app, "GEMINI_KEY_FILE", fake_file):
            with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "   "}, clear=False):
                got = web_app.load_gemini_api_key()
        assert got == "from-file"


def test_gemini_key_returns_empty_when_nothing_available():
    import web_app

    with tempfile.TemporaryDirectory() as td:
        missing_file = Path(td) / ".gemini_key_absent"
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with mock.patch.object(web_app, "GEMINI_KEY_FILE", missing_file):
            with mock.patch.dict(os.environ, env, clear=True):
                got = web_app.load_gemini_api_key()
        assert got == ""


def test_gemini_key_strips_trailing_newline_from_file():
    import web_app

    with tempfile.TemporaryDirectory() as td:
        fake_file = Path(td) / ".gemini_key"
        fake_file.write_text("from-file-with-trailing-newline\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "GEMINI_API_KEY"}
        with mock.patch.object(web_app, "GEMINI_KEY_FILE", fake_file):
            with mock.patch.dict(os.environ, env, clear=True):
                got = web_app.load_gemini_api_key()
        assert got == "from-file-with-trailing-newline"


def run_all():
    test_output_template_uses_byte_limit()
    test_output_template_default_byte_limit_is_100()
    test_output_template_respects_output_dir()
    test_output_template_expanded_length_bounded_for_japanese_title()
    test_gemini_key_env_var_wins_over_file()
    test_gemini_key_falls_back_to_file()
    test_gemini_key_empty_env_falls_back_to_file()
    test_gemini_key_returns_empty_when_nothing_available()
    test_gemini_key_strips_trailing_newline_from_file()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
