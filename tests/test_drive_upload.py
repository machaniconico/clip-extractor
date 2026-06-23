"""Unit tests for drive_upload.py auth surface (CLI parity with youtube_api).

Covers only the Drive-specific wrapper behaviours — the underlying OAuth
plumbing is exercised by test_google_auth.py.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_is_configured_false_without_credentials():
    """is_configured reflects presence of the module-level CREDENTIALS_PATH."""
    import drive_upload
    orig = drive_upload.CREDENTIALS_PATH
    try:
        drive_upload.CREDENTIALS_PATH = Path("/nonexistent/drive-creds.json")
        assert drive_upload.is_configured() is False
    finally:
        drive_upload.CREDENTIALS_PATH = orig


def test_check_auth_status_no_credentials():
    """No credentials, no token → configured=False, authenticated=False."""
    from drive_upload import check_auth_status
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "credentials.json"
        tok = Path(td) / "token.json"
        s = check_auth_status(token_path=tok, credentials_path=creds)
        assert s["configured"] is False
        assert s["token_exists"] is False
        assert s["authenticated"] is False
        assert s["expired"] is False
        assert s["error"] is None


def test_revoke_auth_deletes_drive_token():
    """revoke_auth must remove drive_upload.TOKEN_PATH (token.json, not youtube_token.json)."""
    import drive_upload
    with tempfile.TemporaryDirectory() as td:
        fake_token = Path(td) / "token.json"
        fake_token.write_text("{}", encoding="utf-8")
        original = drive_upload.TOKEN_PATH
        drive_upload.TOKEN_PATH = fake_token
        try:
            assert fake_token.exists()
            assert drive_upload.revoke_auth() is True
            assert not fake_token.exists()
            assert drive_upload.revoke_auth() is False, "second revoke is a no-op"
        finally:
            drive_upload.TOKEN_PATH = original


def test_auth_status_summary_not_configured_mentions_drive_setup():
    """summary steers users to --drive-setup or the Settings tab when unconfigured."""
    import drive_upload
    orig_creds = drive_upload.CREDENTIALS_PATH
    orig_tok = drive_upload.TOKEN_PATH
    try:
        drive_upload.CREDENTIALS_PATH = Path("/nonexistent/credentials.json")
        drive_upload.TOKEN_PATH = Path("/nonexistent/token.json")
        s = drive_upload.auth_status_summary()
        assert "未設定" in s
    finally:
        drive_upload.CREDENTIALS_PATH = orig_creds
        drive_upload.TOKEN_PATH = orig_tok


def test_ensure_authenticated_returns_false_without_credentials():
    """ensure_authenticated short-circuits to False when credentials.json is missing."""
    import drive_upload
    orig = drive_upload.CREDENTIALS_PATH
    try:
        drive_upload.CREDENTIALS_PATH = Path("/nonexistent/credentials.json")
        assert drive_upload.ensure_authenticated(force_reauth=False) is False
    finally:
        drive_upload.CREDENTIALS_PATH = orig


def test_install_credentials_from_file_rejects_none():
    """Delegated to _google_auth; surface must not raise on None input."""
    import drive_upload
    msg = drive_upload.install_credentials_from_file(None)
    assert isinstance(msg, str) and len(msg) > 0


def test_scopes_is_drive_file_only():
    """Guardrail: Drive OAuth scope must stay drive.file (not full drive).

    Broader scopes would trigger Google's sensitive-scope review.
    """
    import drive_upload
    assert drive_upload.SCOPES == ["https://www.googleapis.com/auth/drive.file"]
