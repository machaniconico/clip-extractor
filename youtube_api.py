"""YouTube Data API v3 integration for auto-appending chapter text to videos.

The update flow targets a video the authenticated user owns. We call
videos.list to fetch the existing snippet (title, categoryId, description)
then videos.update to write the merged description back. snippet.title and
snippet.categoryId are mandatory on update — omitting them yields 400.

OAuth plumbing lives in _google_auth; this module is a thin YouTube-specific
wrapper over it. Token + credentials now live in the user's per-OS config
directory (see _google_auth.get_user_config_dir).
"""

import re
from pathlib import Path

import _google_auth

# Write-capable scope — lets us update our own videos' metadata.
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Migrate legacy files from the project root into the user config dir
# (one-shot; harmless when there's nothing to move). Runs at import time
# so the module-level constants below always point at the canonical dir.
_google_auth.migrate_legacy_file("youtube_token.json")
_google_auth.migrate_legacy_file("credentials.json")

TOKEN_PATH = _google_auth.get_user_config_dir() / "youtube_token.json"
CREDENTIALS_PATH = _google_auth.get_user_config_dir() / "credentials.json"


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
    return _google_auth.build_authenticated_service(
        "youtube", "v3", SCOPES, TOKEN_PATH, CREDENTIALS_PATH,
    )


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
            # rejects snippet updates that omit either with HTTP 400. The
            # `get` fallback to "22" (People & Blogs) covers rare private-
            # video responses where YouTube omits categoryId in the snippet.
            "title": snippet.get("title", ""),
            "categoryId": snippet.get("categoryId", "22"),
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

    Thin wrapper over _google_auth.check_auth_status that defaults the paths
    to this module's constants (so UIs / tests that monkey-patch
    youtube_api.TOKEN_PATH / CREDENTIALS_PATH keep working).
    """
    token_path = token_path or TOKEN_PATH
    credentials_path = credentials_path or CREDENTIALS_PATH
    return _google_auth.check_auth_status(token_path, credentials_path, SCOPES)


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
    # build_authenticated_service does the refresh-or-new-flow dance and
    # writes the token to disk; we only care about the side effect.
    get_youtube_service()
    return True


def revoke_auth() -> bool:
    """Delete youtube_token.json if present. Returns whether a file was removed."""
    return _google_auth.revoke_token(TOKEN_PATH)


def auth_status_summary() -> str:
    """One-line human-readable status string for the Settings UI / CLI."""
    s = check_auth_status()
    if not s["configured"]:
        return (
            "未設定: Settings タブの『credentials.json』欄にファイルをドロップしてください "
            f"(保存先: {CREDENTIALS_PATH})"
        )
    if s["authenticated"]:
        return "認証済み (token 有効)"
    if s["expired"]:
        err = s.get("error")
        return f"期限切れ: 再認証が必要{(' (' + err + ')') if err else ''}"
    if s["token_exists"]:
        err = s.get("error") or "token 不正"
        return f"要再認証: {err}"
    return "未認証: Settings タブで『認証する』を押してください"


# ----- credentials.json install helpers (UI file-drop support) -----

def validate_credentials_json(path: Path | str) -> tuple[bool, str]:
    """Thin re-export of _google_auth.validate_credentials_json."""
    return _google_auth.validate_credentials_json(path)


def install_credentials_from_file(src_path: Path | str | None) -> str:
    """Validate and copy an uploaded credentials.json into CREDENTIALS_PATH.

    Delegates to _google_auth; reads the module-level CREDENTIALS_PATH so
    tests that monkey-patch it keep working.
    """
    return _google_auth.install_credentials_from_file(src_path, CREDENTIALS_PATH)


# Deep link to the YouTube Data API v3 library page so users only have to
# click "Enable" rather than hunt for the API in the console.
GOOGLE_CLOUD_CONSOLE_URL = (
    "https://console.cloud.google.com/apis/library/youtube.googleapis.com"
)
