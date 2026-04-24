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
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

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
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"credentials.json が見つかりません: {credentials_path}\n"
                    "Google Cloud Console で OAuth 2.0 クライアント (デスクトップアプリ) を作成し、"
                    "Settings タブで JSON をアップロードしてください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")

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
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")
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
