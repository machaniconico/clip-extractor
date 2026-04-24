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
    test_gemini_key_env_var_wins_over_file()
    test_gemini_key_falls_back_to_file()
    test_gemini_key_empty_env_falls_back_to_file()
    test_gemini_key_returns_empty_when_nothing_available()
    test_gemini_key_strips_trailing_newline_from_file()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
