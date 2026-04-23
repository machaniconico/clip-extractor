"""YouTube Data API v3 wrapper for updating video descriptions.

Separate from drive_upload.py because the OAuth scopes and credential files differ.
Credentials live under ~/.clip-extractor/ so the upgrade story stays clean.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from transcript_cache import extract_video_id as _extract_video_id_impl

logger = logging.getLogger("clip-extractor")

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

CONFIG_DIR = Path.home() / ".clip-extractor"
CLIENT_SECRET_PATH = CONFIG_DIR / "client_secret.json"
TOKEN_PATH = CONFIG_DIR / "youtube-oauth.json"


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _restrict_permissions(path: Path) -> None:
    """Restrict file permissions to owner-only (POSIX; no-op on Windows).

    On Windows, Path.chmod() silently accepts POSIX mode bits without enforcing
    them. ACLs should protect the file since it lives in %USERPROFILE%.
    """
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError) as e:
        logger.debug(f"[YouTubeAPI] chmod 0600 not applied on {path}: {e}")


def is_configured() -> bool:
    """Whether a client_secret.json has been uploaded."""
    return CLIENT_SECRET_PATH.exists()


def save_client_secret(uploaded_path: Path | str) -> Path:
    """Copy an uploaded client_secret.json into the config dir.

    Validates that the file parses as JSON and has the expected OAuth shape
    (either 'installed' or 'web' key) before persisting. File is stored with
    owner-only permissions on POSIX.
    """
    _ensure_config_dir()
    src = Path(uploaded_path)
    if not src.exists():
        raise FileNotFoundError(f"client_secret.json not found: {src}")
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"client_secret.json が有効なJSONではありません: {e}") from e
    if not isinstance(data, dict) or not ("installed" in data or "web" in data):
        raise ValueError(
            "client_secret.json の構造が不正です ('installed' または 'web' キーが必要)"
        )
    shutil.copyfile(src, CLIENT_SECRET_PATH)
    _restrict_permissions(CLIENT_SECRET_PATH)
    logger.info(f"[YouTubeAPI] client_secret.json saved -> {CLIENT_SECRET_PATH}")
    return CLIENT_SECRET_PATH


def is_authenticated() -> bool:
    """Whether a usable token already exists (validity checked by _load_credentials)."""
    if not TOKEN_PATH.exists():
        return False
    creds = _load_credentials()
    return creds is not None and (creds.valid or bool(getattr(creds, "refresh_token", None)))


def _load_credentials():
    """Load credentials, refreshing if needed. Returns None on failure."""
    if not TOKEN_PATH.exists():
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise RuntimeError("google-auth パッケージが必要です") from e
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            _restrict_permissions(TOKEN_PATH)
        return creds
    except Exception as e:
        logger.warning(f"[YouTubeAPI] Failed to load token: {e}")
        return None


def authenticate() -> dict:
    """Run OAuth 2.0 flow. Opens a browser for user consent.

    Returns info dict: {ok: bool, channel: Optional[str], error: Optional[str]}.
    """
    if not is_configured():
        return {"ok": False, "error": "client_secret.json が未設定です"}
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:
        return {"ok": False, "error": f"google-auth-oauthlib が必要です: {e}"}

    _ensure_config_dir()
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        _restrict_permissions(TOKEN_PATH)
    except Exception as e:
        logger.error(f"[YouTubeAPI] OAuth failed: {e}")
        return {"ok": False, "error": f"認証に失敗: {e}"}

    channel = get_authenticated_user_info()
    return {"ok": True, "channel": channel, "error": None}


def get_authenticated_user_info() -> Optional[str]:
    """Return the authenticated channel's display name, or None."""
    creds = _load_credentials()
    if creds is None or not (creds.valid or getattr(creds, "refresh_token", None)):
        return None
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise RuntimeError("google-api-python-client が必要です") from e
    try:
        service = build("youtube", "v3", credentials=creds)
        response = service.channels().list(part="snippet", mine=True).execute()
        items = response.get("items", [])
        if not items:
            return None
        return items[0].get("snippet", {}).get("title")
    except Exception as e:
        logger.warning(f"[YouTubeAPI] channels.list failed: {e}")
        return None


def extract_video_id(url: str) -> Optional[str]:
    """Extract a YouTube video ID from a URL, or None.

    Delegates to transcript_cache.extract_video_id to keep the URL regexes
    in one place.
    """
    return _extract_video_id_impl(url)


def update_description(video_id: str, new_description: str) -> dict:
    """Replace the description of `video_id` with `new_description`.

    Returns {ok: bool, error: Optional[str]}.
    Requires the authenticated user to own the video.
    """
    if not video_id:
        return {"ok": False, "error": "video_idが空です"}
    if not is_authenticated():
        return {"ok": False, "error": "YouTube API 未認証"}
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        return {"ok": False, "error": f"google-api-python-client が必要: {e}"}

    creds = _load_credentials()
    if creds is None:
        return {"ok": False, "error": "トークン読込失敗"}

    try:
        service = build("youtube", "v3", credentials=creds)
        list_resp = service.videos().list(part="snippet,status", id=video_id).execute()
        items = list_resp.get("items", [])
        if not items:
            return {"ok": False, "error": f"動画が見つかりません (id={video_id})"}

        snippet = items[0].get("snippet", {})
        snippet["description"] = new_description or ""

        body = {"id": video_id, "snippet": snippet}
        service.videos().update(part="snippet", body=body).execute()
        logger.info(f"[YouTubeAPI] description updated for {video_id}")
        return {"ok": True, "error": None}
    except Exception as e:
        logger.error(f"[YouTubeAPI] videos.update failed for {video_id}: {e}")
        return {"ok": False, "error": f"update失敗: {e}"}
