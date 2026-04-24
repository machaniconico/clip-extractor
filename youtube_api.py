"""YouTube Data API v3 integration for auto-appending chapter text to videos.

The update flow targets a video the authenticated user owns. We call
videos.list to fetch the existing snippet (title, categoryId, description)
then videos.update to write the merged description back. snippet.title and
snippet.categoryId are mandatory on update — omitting them yields 400.

Requires credentials.json (OAuth client secrets, shared with drive_upload.py)
and stores the per-scope token in youtube_token.json.
"""

import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Write-capable scope — lets us update our own videos' metadata.
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

TOKEN_PATH = Path(__file__).parent / "youtube_token.json"
CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"


_VIDEO_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^&]+&)*v=|embed/|shorts/|v/))"
    r"([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str | None:
    """Extract the 11-char YouTube video ID from common URL shapes.

    Returns None on any unrecognised string. Query parameters, fragments, and
    additional path segments after the ID are ignored."""
    if not url:
        return None
    m = _VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def is_configured() -> bool:
    """True when credentials.json is on disk — auth can be attempted."""
    return CREDENTIALS_PATH.exists()


def get_youtube_service():
    """Authenticate (refreshing cached token when possible) and return the
    `youtube` resource built with Data API v3."""
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    "credentials.json が見つかりません。\n"
                    "Google Cloud Console で YouTube Data API v3 を有効化し、\n"
                    "OAuth 2.0 クライアント (デスクトップアプリ) を作成、\n"
                    f"credentials.json を {CREDENTIALS_PATH} に配置してください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def _merge_description(existing: str, chapters: str, position: str) -> str:
    """Return the new description body.

    prepend: chapters + blank line + existing (default; makes chapters the
             first thing viewers see, required for YouTube auto-chapter UI
             to pick them up).
    append:  existing + blank line + chapters.
    replace: just chapters.
    """
    existing = existing or ""
    chapters = chapters or ""
    # No chapters to inject → return existing description verbatim so an
    # accidental empty call doesn't clobber the user's body with blank lines.
    if not chapters:
        return existing
    if position == "replace":
        return chapters
    if position == "append":
        if not existing:
            return chapters
        return f"{existing.rstrip()}\n\n{chapters}"
    # default: prepend
    if not existing:
        return chapters
    return f"{chapters}\n\n{existing.lstrip()}"


def update_video_description(
    service,
    video_id: str,
    chapters_text: str,
    position: str = "prepend",
) -> dict:
    """Fetch the video's current snippet, merge chapters into the description,
    and write it back via videos.update.

    Raises:
        ValueError: video_id is missing or not found.
        HttpError: underlying API error (caller decides whether to swallow).
    """
    if not video_id:
        raise ValueError("video_id is required")
    if position not in ("prepend", "append", "replace"):
        raise ValueError(f"invalid position {position!r}")

    resp = service.videos().list(part="snippet", id=video_id).execute()
    items = resp.get("items", [])
    if not items:
        raise ValueError(f"video {video_id!r} not found or inaccessible")

    snippet = items[0]["snippet"]
    existing = snippet.get("description", "")
    new_desc = _merge_description(existing, chapters_text, position)

    body = {
        "id": video_id,
        "snippet": {
            # title + categoryId are required by videos.update — the API
            # rejects snippet updates that omit either with HTTP 400.
            "title": snippet["title"],
            "categoryId": snippet["categoryId"],
            "description": new_desc,
        },
    }
    if "tags" in snippet:
        body["snippet"]["tags"] = snippet["tags"]
    if "defaultLanguage" in snippet:
        body["snippet"]["defaultLanguage"] = snippet["defaultLanguage"]

    return service.videos().update(part="snippet", body=body).execute()


# ----- Auth lifecycle helpers (status / setup / revoke) -----

def check_auth_status(
    token_path: Path | None = None,
    credentials_path: Path | None = None,
) -> dict:
    """Inspect the on-disk auth state without prompting the user.

    Performs a silent token refresh when possible (so callers learn the
    token is still good after its original expiry). Never opens a browser
    and never raises — UI callers can render the result as-is.

    Returns:
        {
          "configured":   credentials.json 存在するか,
          "token_exists": youtube_token.json 存在するか,
          "authenticated": 今すぐ API に使える状態か (refresh 済含む),
          "expired":      token はあるが refresh にも失敗,
          "error":        例外メッセージ (あれば)
        }

    `token_path` / `credentials_path` are for tests; production calls
    pass nothing and the module constants are used.
    """
    token_path = token_path or TOKEN_PATH
    credentials_path = credentials_path or CREDENTIALS_PATH

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
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as e:
        status["error"] = f"token 読込失敗: {e}"
        return status

    if creds.valid:
        status["authenticated"] = True
        return status

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            status["authenticated"] = True
            return status
        except Exception as e:
            status["expired"] = True
            status["error"] = f"refresh 失敗: {e}"
            return status

    status["expired"] = True
    return status


def ensure_authenticated(force_reauth: bool = False) -> bool:
    """Guarantee a usable token exists; runs the OAuth browser flow if needed.

    Returns False when credentials.json is missing (nothing we can do
    without it). Raises on genuine auth failures so callers can surface
    the error — pre-validation UIs should catch broadly.
    """
    if force_reauth:
        revoke_auth()
    if not CREDENTIALS_PATH.exists():
        return False
    # get_youtube_service does the refresh-or-new-flow dance and writes
    # the token to disk; we only care about the side effect.
    get_youtube_service()
    return True


def revoke_auth() -> bool:
    """Delete youtube_token.json if present. Returns whether a file was removed."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        return True
    return False


def auth_status_summary() -> str:
    """One-line human-readable status string for the Settings UI / CLI."""
    s = check_auth_status()
    if not s["configured"]:
        return "未設定: credentials.json を clip-extractor/ に配置してください"
    if s["authenticated"]:
        return "認証済み (token 有効)"
    if s["expired"]:
        err = s.get("error")
        return f"期限切れ: 再認証が必要{(' (' + err + ')') if err else ''}"
    if s["token_exists"]:
        err = s.get("error") or "token 不正"
        return f"要再認証: {err}"
    return "未認証: Settings タブで『認証する』を押してください"
