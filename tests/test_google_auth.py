"""Unit tests for _google_auth.py (shared OAuth helpers).

Covers user config dir resolution (per-OS), legacy migration idempotence,
credentials.json validation and install, and token revocation. The OAuth
flow + service build path requires Google servers and is only smoke-tested
via import in test_google_auth_imports.
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import _google_auth


def test_user_config_dir_windows():
    with mock.patch.object(_google_auth.sys, "platform", "win32"):
        with mock.patch.dict(_google_auth.os.environ, {"APPDATA": r"C:\Users\test\AppData\Roaming"}, clear=False):
            d = _google_auth.get_user_config_dir()
    assert d.name == "clip-extractor", d
    # Accept Windows or POSIX normalisation depending on the runtime.
    assert "Roaming" in str(d).replace("\\", "/"), d


def test_user_config_dir_windows_fallback_no_appdata():
    env = {k: v for k, v in _google_auth.os.environ.items() if k != "APPDATA"}
    with mock.patch.object(_google_auth.sys, "platform", "win32"):
        with mock.patch.dict(_google_auth.os.environ, env, clear=True):
            d = _google_auth.get_user_config_dir()
    assert d.name == "clip-extractor"
    assert "Roaming" in str(d).replace("\\", "/")


def test_user_config_dir_macos():
    with mock.patch.object(_google_auth.sys, "platform", "darwin"):
        d = _google_auth.get_user_config_dir()
    assert d.name == "clip-extractor"
    assert "Library/Application Support" in str(d).replace("\\", "/")


def test_user_config_dir_linux_xdg():
    with mock.patch.object(_google_auth.sys, "platform", "linux"):
        with mock.patch.dict(_google_auth.os.environ, {"XDG_CONFIG_HOME": "/tmp/fake-xdg"}, clear=False):
            d = _google_auth.get_user_config_dir()
    assert d == Path("/tmp/fake-xdg") / "clip-extractor"


def test_user_config_dir_linux_default():
    env = {k: v for k, v in _google_auth.os.environ.items() if k != "XDG_CONFIG_HOME"}
    with mock.patch.object(_google_auth.sys, "platform", "linux"):
        with mock.patch.dict(_google_auth.os.environ, env, clear=True):
            d = _google_auth.get_user_config_dir()
    assert d.name == "clip-extractor"
    assert ".config" in str(d).replace("\\", "/")


def test_migrate_legacy_no_source():
    """No legacy file → dest returned (may or may not exist)."""
    with tempfile.TemporaryDirectory() as td:
        fake_cfg = Path(td) / "cfg"
        with mock.patch.object(_google_auth, "get_user_config_dir", return_value=fake_cfg):
            with mock.patch.object(_google_auth, "LEGACY_PROJECT_DIR", Path(td) / "project"):
                dest = _google_auth.migrate_legacy_file("ghost.json")
        assert dest == fake_cfg / "ghost.json"
        assert not dest.exists()


def test_migrate_legacy_source_exists_moves():
    with tempfile.TemporaryDirectory() as td:
        legacy_dir = Path(td) / "project"
        legacy_dir.mkdir()
        legacy_file = legacy_dir / "creds.json"
        legacy_file.write_text('{"installed": {}}', encoding="utf-8")

        fake_cfg = Path(td) / "cfg"
        with mock.patch.object(_google_auth, "get_user_config_dir", return_value=fake_cfg):
            with mock.patch.object(_google_auth, "LEGACY_PROJECT_DIR", legacy_dir):
                dest = _google_auth.migrate_legacy_file("creds.json")
        assert dest.exists(), "destination should have the migrated content"
        assert not legacy_file.exists(), "legacy copy should be moved"
        assert dest.read_text(encoding="utf-8") == '{"installed": {}}'


def test_migrate_legacy_dest_already_exists_is_noop():
    """If the new location already has the file, legacy is left untouched."""
    with tempfile.TemporaryDirectory() as td:
        legacy_dir = Path(td) / "project"
        legacy_dir.mkdir()
        legacy_file = legacy_dir / "creds.json"
        legacy_file.write_text("LEGACY", encoding="utf-8")

        fake_cfg = Path(td) / "cfg"
        fake_cfg.mkdir()
        canonical_file = fake_cfg / "creds.json"
        canonical_file.write_text("CANONICAL", encoding="utf-8")

        with mock.patch.object(_google_auth, "get_user_config_dir", return_value=fake_cfg):
            with mock.patch.object(_google_auth, "LEGACY_PROJECT_DIR", legacy_dir):
                dest = _google_auth.migrate_legacy_file("creds.json")
        assert dest.read_text(encoding="utf-8") == "CANONICAL", "must not overwrite"
        assert legacy_file.exists(), "legacy must be left alone when dest existed"
        assert legacy_file.read_text(encoding="utf-8") == "LEGACY"


def test_validate_credentials_installed_happy():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "good.json"
        p.write_text('{"installed": {"client_id": "id", "client_secret": "sec", "project_id": "p-1"}}', encoding="utf-8")
        ok, msg = _google_auth.validate_credentials_json(p)
        assert ok is True
        assert "installed" in msg
        assert "p-1" in msg


def test_validate_credentials_web_rejected():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "web.json"
        p.write_text('{"web": {"client_id": "id", "client_secret": "sec"}}', encoding="utf-8")
        ok, msg = _google_auth.validate_credentials_json(p)
        assert ok is False
        assert "Web" in msg or "デスクトップ" in msg


def test_validate_credentials_missing_file():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "absent.json"
        ok, msg = _google_auth.validate_credentials_json(p)
        assert ok is False
        assert "見つかりません" in msg


def test_validate_credentials_malformed_json():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.json"
        p.write_text("not json", encoding="utf-8")
        ok, msg = _google_auth.validate_credentials_json(p)
        assert ok is False
        assert "JSON" in msg


def test_validate_credentials_missing_client_secret():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "part.json"
        p.write_text('{"installed": {"client_id": "id"}}', encoding="utf-8")
        ok, msg = _google_auth.validate_credentials_json(p)
        assert ok is False
        assert "client_secret" in msg


def test_install_credentials_happy():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.json"
        src.write_text('{"installed": {"client_id": "id", "client_secret": "sec"}}', encoding="utf-8")
        dest = Path(td) / "deep" / "nested" / "credentials.json"
        msg = _google_auth.install_credentials_from_file(src, dest)
        assert "配置完了" in msg, msg
        assert dest.exists(), "nested dest dir must be created"
        assert dest.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")


def test_install_credentials_rejects_none():
    msg = _google_auth.install_credentials_from_file(None, Path("/tmp/x"))
    assert "選択されていません" in msg


def test_install_credentials_rejects_web():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "web.json"
        src.write_text('{"web": {"client_id": "id", "client_secret": "sec"}}', encoding="utf-8")
        dest = Path(td) / "credentials.json"
        msg = _google_auth.install_credentials_from_file(src, dest)
        assert "中止" in msg
        assert not dest.exists(), "bad source must not be copied"


def test_revoke_token_exists():
    with tempfile.TemporaryDirectory() as td:
        tok = Path(td) / "tok.json"
        tok.write_text("{}", encoding="utf-8")
        assert _google_auth.revoke_token(tok) is True
        assert not tok.exists()


def test_revoke_token_missing():
    with tempfile.TemporaryDirectory() as td:
        tok = Path(td) / "absent.json"
        assert _google_auth.revoke_token(tok) is False


def test_check_auth_status_no_files():
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "creds.json"
        tok = Path(td) / "tok.json"
        s = _google_auth.check_auth_status(tok, creds, ["dummy"])
        assert s["configured"] is False
        assert s["token_exists"] is False
        assert s["authenticated"] is False
        assert s["error"] is None


def test_check_auth_status_token_invalid_json():
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "creds.json"
        creds.write_text("{}", encoding="utf-8")
        tok = Path(td) / "tok.json"
        tok.write_text("NOT JSON", encoding="utf-8")
        s = _google_auth.check_auth_status(tok, creds, ["dummy"])
        assert s["configured"] is True
        assert s["token_exists"] is True
        assert s["authenticated"] is False
        assert s["error"] is not None


def test_ensure_user_config_dir_creates():
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "cfg" / "nested"
        with mock.patch.object(_google_auth, "get_user_config_dir", return_value=target):
            out = _google_auth.ensure_user_config_dir()
        assert out == target
        assert target.exists()
        assert target.is_dir()


def run_all():
    test_user_config_dir_windows()
    test_user_config_dir_windows_fallback_no_appdata()
    test_user_config_dir_macos()
    test_user_config_dir_linux_xdg()
    test_user_config_dir_linux_default()
    test_migrate_legacy_no_source()
    test_migrate_legacy_source_exists_moves()
    test_migrate_legacy_dest_already_exists_is_noop()
    test_validate_credentials_installed_happy()
    test_validate_credentials_web_rejected()
    test_validate_credentials_missing_file()
    test_validate_credentials_malformed_json()
    test_validate_credentials_missing_client_secret()
    test_install_credentials_happy()
    test_install_credentials_rejects_none()
    test_install_credentials_rejects_web()
    test_revoke_token_exists()
    test_revoke_token_missing()
    test_check_auth_status_no_files()
    test_check_auth_status_token_invalid_json()
    test_ensure_user_config_dir_creates()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
