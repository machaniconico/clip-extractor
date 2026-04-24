"""Unit tests for youtube_api.py extractor + description merge."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from youtube_api import extract_video_id, _merge_description


def test_extract_id_standard_watch_url():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_id_short_url():
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42") == "dQw4w9WgXcQ"


def test_extract_id_with_extra_query_params():
    assert extract_video_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s&feature=youtu.be"
    ) == "dQw4w9WgXcQ"
    assert extract_video_id(
        "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_extract_id_shorts_and_embed():
    assert extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_id_returns_none_on_invalid():
    assert extract_video_id("") is None
    assert extract_video_id("not a url") is None
    assert extract_video_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert extract_video_id("https://youtube.com/") is None
    # 10-char id (too short) must not match
    assert extract_video_id("https://youtu.be/dQw4w9WgXc") is None


def test_merge_prepend_default():
    out = _merge_description("existing body", "0:00 イントロ\n1:23 A", "prepend")
    assert out == "0:00 イントロ\n1:23 A\n\nexisting body"


def test_merge_prepend_empty_existing():
    out = _merge_description("", "0:00 イントロ", "prepend")
    assert out == "0:00 イントロ"


def test_merge_append():
    out = _merge_description("existing", "0:00 A", "append")
    assert out == "existing\n\n0:00 A"


def test_merge_append_empty_existing():
    out = _merge_description("", "0:00 A", "append")
    assert out == "0:00 A"


def test_merge_replace():
    out = _merge_description("existing long body", "0:00 A", "replace")
    assert out == "0:00 A"


def test_merge_prepend_strips_existing_leading_blank():
    # Existing body with leading blank line should not double up after prepend
    out = _merge_description("\n\nbody", "0:00 A", "prepend")
    assert out == "0:00 A\n\nbody"


def test_merge_none_and_empty_inputs():
    assert _merge_description(None, None, "prepend") == ""
    assert _merge_description(None, "0:00 A", "prepend") == "0:00 A"
    assert _merge_description("existing", None, "prepend") == "existing"


def test_check_auth_status_no_credentials(tmp_path=None):
    """credentials.json も token もない → configured=False, authenticated=False."""
    import tempfile
    from youtube_api import check_auth_status
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "credentials.json"  # 存在しない
        tok = Path(td) / "youtube_token.json"  # 存在しない
        s = check_auth_status(token_path=tok, credentials_path=creds)
        assert s["configured"] is False
        assert s["token_exists"] is False
        assert s["authenticated"] is False
        assert s["expired"] is False
        assert s["error"] is None


def test_check_auth_status_credentials_only():
    """credentials.json はあるが token がない状態。"""
    import tempfile
    from youtube_api import check_auth_status
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "credentials.json"
        creds.write_text("{}", encoding="utf-8")
        tok = Path(td) / "youtube_token.json"
        s = check_auth_status(token_path=tok, credentials_path=creds)
        assert s["configured"] is True
        assert s["token_exists"] is False
        assert s["authenticated"] is False


def test_check_auth_status_invalid_token_json():
    """token ファイルが不正 JSON → error が詰まり raise しない."""
    import tempfile
    from youtube_api import check_auth_status
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "credentials.json"
        creds.write_text("{}", encoding="utf-8")
        tok = Path(td) / "youtube_token.json"
        tok.write_text("not a json", encoding="utf-8")
        s = check_auth_status(token_path=tok, credentials_path=creds)
        assert s["configured"] is True
        assert s["token_exists"] is True
        assert s["authenticated"] is False
        assert s["error"] is not None, "invalid json must surface as error string"


def test_revoke_auth_with_existing_token(monkeypatch_target="youtube_api"):
    """revoke_auth should delete youtube_token.json when present."""
    import tempfile
    import youtube_api
    with tempfile.TemporaryDirectory() as td:
        fake_token = Path(td) / "youtube_token.json"
        fake_token.write_text("{}", encoding="utf-8")
        original = youtube_api.TOKEN_PATH
        youtube_api.TOKEN_PATH = fake_token
        try:
            assert fake_token.exists()
            removed = youtube_api.revoke_auth()
            assert removed is True
            assert not fake_token.exists(), "token file must be gone"
            removed_again = youtube_api.revoke_auth()
            assert removed_again is False, "second revoke must report no-op"
        finally:
            youtube_api.TOKEN_PATH = original


def test_auth_status_summary_not_configured():
    """credentials.json なしで summary が 未設定 メッセージ。"""
    import youtube_api
    orig_creds = youtube_api.CREDENTIALS_PATH
    orig_tok = youtube_api.TOKEN_PATH
    try:
        youtube_api.CREDENTIALS_PATH = Path("/nonexistent/credentials.json")
        youtube_api.TOKEN_PATH = Path("/nonexistent/youtube_token.json")
        s = youtube_api.auth_status_summary()
        assert "未設定" in s, f"expected 未設定 in {s!r}"
    finally:
        youtube_api.CREDENTIALS_PATH = orig_creds
        youtube_api.TOKEN_PATH = orig_tok


def test_auth_status_summary_unauthenticated():
    """credentials.json あるが token なし → 未認証 メッセージ."""
    import tempfile
    import youtube_api
    orig_creds = youtube_api.CREDENTIALS_PATH
    orig_tok = youtube_api.TOKEN_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            creds = Path(td) / "credentials.json"
            creds.write_text("{}", encoding="utf-8")
            tok = Path(td) / "youtube_token.json"
            youtube_api.CREDENTIALS_PATH = creds
            youtube_api.TOKEN_PATH = tok
            s = youtube_api.auth_status_summary()
            assert "未認証" in s, f"expected 未認証 in {s!r}"
    finally:
        youtube_api.CREDENTIALS_PATH = orig_creds
        youtube_api.TOKEN_PATH = orig_tok


def run_all():
    test_extract_id_standard_watch_url()
    test_extract_id_short_url()
    test_extract_id_with_extra_query_params()
    test_extract_id_shorts_and_embed()
    test_extract_id_returns_none_on_invalid()
    test_merge_prepend_default()
    test_merge_prepend_empty_existing()
    test_merge_append()
    test_merge_append_empty_existing()
    test_merge_replace()
    test_merge_prepend_strips_existing_leading_blank()
    test_merge_none_and_empty_inputs()
    test_check_auth_status_no_credentials()
    test_check_auth_status_credentials_only()
    test_check_auth_status_invalid_token_json()
    test_revoke_auth_with_existing_token()
    test_auth_status_summary_not_configured()
    test_auth_status_summary_unauthenticated()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
