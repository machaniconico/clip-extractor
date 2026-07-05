"""Shared OAuth2 helpers for Google APIs (Drive, YouTube).

Centralises what drive_upload.py and youtube_api.py used to duplicate:

1. Resolve a per-user, writable config directory (outside the project tree
   so OneDrive sync / accidental git add never touches OAuth secrets).
2. One-shot migration of legacy token/credentials files from the project
   root into that config directory (so existing users keep working after
   the upgrade without re-doing OAuth).
3. A single build_authenticated_service() that runs the OAuth flow,
   refreshes cached tokens, and returns a googleapiclient resource.
4. Validation + install helpers for credentials.json drops from the UI.

Callers still expose module-level TOKEN_PATH / CREDENTIALS_PATH constants
so existing monkey-patch-based tests keep working.

## Surprising behaviours by design

- **Import-time migration.** drive_upload.py and youtube_api.py call
  migrate_legacy_file() at module load so their module-level path
  constants resolve to the canonical (user config dir) location even on
  the first import after the upgrade. The side effect is idempotent —
  if the destination already exists the legacy file is left untouched,
  and failures downgrade to copy-then-warn rather than raising.
- **check_auth_status() may write to disk.** When a cached token is
  expired but still refreshable, this function silently refreshes it
  and rewrites the file so subsequent callers see a valid token. The UI
  treats the function as a pure read, but for persistence across
  process restarts the write-through is load-bearing.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# The google-auth / google-api-python-client stack is the heaviest import in
# this project's startup path but is only needed once an OAuth flow actually
# runs. Resolve the five names lazily via PEP 562 module __getattr__ instead
# of importing them at module load time.
#
# This must stay __getattr__-based rather than moving the imports inside each
# function: tests/test_google_auth.py does
# `mock.patch.object(_google_auth.Credentials, "from_authorized_user_file", ...)`
# and `mock.patch.object(_google_auth, "build", ...)`, i.e. it patches these
# as module attributes. __getattr__ resolves the real object into globals()
# on first touch (including the attribute access patch.object itself does),
# so the patch lands on the same object the functions below use.
_LAZY_GOOGLE = {
    "RefreshError": ("google.auth.exceptions", "RefreshError"),
    "Request": ("google.auth.transport.requests", "Request"),
    "Credentials": ("google.oauth2.credentials", "Credentials"),
    "InstalledAppFlow": ("google_auth_oauthlib.flow", "InstalledAppFlow"),
    "build": ("googleapiclient.discovery", "build"),
}


def __getattr__(name):
    if name in _LAZY_GOOGLE:
        import importlib
        module_name, attr = _LAZY_GOOGLE[name]
        value = getattr(importlib.import_module(module_name), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _ensure_google_imports():
    """Force-resolve all lazy names before a function that uses them runs.

    Never overwrites a name already in globals() — that would clobber a
    test's mock.patch.object() replacement with the real import.
    """
    for name in _LAZY_GOOGLE:
        if name not in globals():
            __getattr__(name)

APP_DIR_NAME = "clip-extractor"

# Legacy location (next to this file) — used only as a source during the
# one-shot migration. All new reads/writes go to get_user_config_dir().
LEGACY_PROJECT_DIR = Path(__file__).parent


def get_user_config_dir() -> Path:
    """Return the per-OS directory where app secrets should live.

    Windows: %APPDATA%/clip-extractor           (e.g. C:/Users/NAME/AppData/Roaming/clip-extractor)
    macOS:   ~/Library/Application Support/clip-extractor
    Linux:   $XDG_CONFIG_HOME/clip-extractor or ~/.config/clip-extractor
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def ensure_user_config_dir() -> Path:
    """mkdir -p the user config dir and return it."""
    d = get_user_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def migrate_legacy_file(filename: str) -> Path:
    """Move project-root/filename into user_config_dir/filename if present.

    Idempotent. The new location is returned regardless of whether a move
    happened. Rules:

    - If the new location already has the file, do nothing (don't even touch
      the legacy copy — the user might still reference it manually).
    - Else, if the legacy copy exists, move it to the new location.
    - Else, just return the (non-existent) destination Path.

    A shutil.move failure falls back to copy so we never block startup on a
    filesystem error — the caller may still succeed via a fresh OAuth flow.
    """
    dest = get_user_config_dir() / filename
    legacy = LEGACY_PROJECT_DIR / filename
    if dest.exists():
        return dest
    if legacy.exists():
        try:
            ensure_user_config_dir()
            shutil.move(str(legacy), str(dest))
            logger.info("Migrated %s -> %s", legacy, dest)
        except Exception as e:
            logger.warning("Migration move of %s failed (%s); attempting copy", legacy, e)
            try:
                shutil.copy2(str(legacy), str(dest))
                logger.info("Migrated (copy) %s -> %s", legacy, dest)
            except Exception as e2:
                logger.error("Legacy file migration failed: %s", e2)
    return dest


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    fd_owned = True  # we must close fd ourselves until os.fdopen takes ownership
    try:
        fchmod = getattr(os, "fchmod", None)
        if fchmod is not None:
            try:
                fchmod(fd, 0o600)
            except OSError:
                pass
        fh = os.fdopen(fd, "w", encoding="utf-8")
        fd_owned = False  # fh now owns the descriptor and will close it
        with fh:
            fh.write(text)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except BaseException:
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_authenticated_service(
    service_name: str,
    version: str,
    scopes: list[str],
    token_path: Path,
    credentials_path: Path,
):
    """Return an authenticated googleapiclient resource.

    Reuses the cached token when still valid, silently refreshes if possible,
    otherwise triggers the OAuth desktop flow (opens a browser). Raises
    FileNotFoundError when credentials.json is missing and a fresh flow is
    required.
    """
    _ensure_google_imports()
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                creds = None
        if not refreshed:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"credentials.json が見つかりません: {credentials_path}\n"
                    "Google Cloud Console で OAuth 2.0 クライアント (デスクトップアプリ) を作成し、"
                    "Settings タブで JSON をアップロードしてください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0)

        _write_secret(token_path, creds.to_json())

    return build(service_name, version, credentials=creds)


def check_auth_status(
    token_path: Path,
    credentials_path: Path,
    scopes: list[str],
) -> dict:
    """Inspect on-disk auth state silently.

    Never prompts, never opens a browser. Performs a silent token refresh
    when possible. Returns a status dict UIs can render as-is.
    """
    _ensure_google_imports()
    status = {
        "configured": credentials_path.exists(),
        "token_exists": token_path.exists(),
        "authenticated": False,
        "expired": False,
        "error": None,
    }
    if not status["token_exists"]:
        return status

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    except Exception as e:
        status["error"] = f"token 読込失敗: {e}"
        return status

    if creds.valid:
        status["authenticated"] = True
        return status

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _write_secret(token_path, creds.to_json())
            status["authenticated"] = True
            return status
        except Exception as e:
            status["expired"] = True
            status["error"] = f"refresh 失敗: {e}"
            return status

    status["expired"] = True
    return status


def revoke_token(token_path: Path) -> bool:
    """Delete the token file if present. Returns True iff a file was removed."""
    if token_path.exists():
        token_path.unlink()
        return True
    return False


def validate_credentials_json(path: Path | str) -> tuple[bool, str]:
    """Peek at a JSON file and tell whether it looks like an OAuth secrets file.

    Returns (ok, message). On ok=False the message explains why; on ok=True
    it summarises what was found (client type / project) for confirmation.
    """
    path = Path(path)
    if not path.exists():
        return False, f"ファイルが見つかりません: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return False, f"JSON として読めません: {e}"
    except Exception as e:
        return False, f"読込失敗: {e}"

    # Google OAuth client secrets have exactly one of these top-level keys.
    # 'installed' = desktop / CLI app (the shape we need for run_local_server).
    # 'web' = hosted web app; won't work with our local-server redirect flow.
    if "installed" in data:
        section = data["installed"]
        client_type = "installed (desktop)"
    elif "web" in data:
        return False, (
            "このクレデンシャルは Web アプリ用です。OAuth クライアント作成時に "
            "『デスクトップアプリ』タイプを選び直してください。"
        )
    else:
        return False, "OAuth クライアント構造ではありません (installed キーが見当たらない)"

    for required in ("client_id", "client_secret"):
        if required not in section:
            return False, f"必須フィールド {required!r} がありません"

    project_id = section.get("project_id", "(project_id なし)")
    return True, f"有効な credentials.json — type: {client_type}, project: {project_id}"


def install_credentials_from_file(src_path: Path | str | None, credentials_path: Path) -> str:
    """Validate and copy an uploaded credentials.json into credentials_path.

    Returns a human-readable status string. Never raises — UI handlers render
    the string directly.
    """
    if src_path is None:
        return "ファイルが選択されていません"
    ok, detail = validate_credentials_json(src_path)
    if not ok:
        return f"配置を中止: {detail}"
    try:
        credentials_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_path), str(credentials_path))
    except Exception as e:
        return f"ファイルコピー失敗: {e}"
    return f"配置完了: {detail}. 次は『認証する』ボタンを押してください。"
