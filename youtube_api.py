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
from datetime import datetime, timezone
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


def _parse_api_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _broadcast_summary(item: dict) -> dict:
    snippet = item.get("snippet", {})
    status = item.get("status", {})
    return {
        "video_id": item.get("id", ""),
        "title": snippet.get("title", ""),
        "actual_start_time": snippet.get("actualStartTime", ""),
        "actual_end_time": snippet.get("actualEndTime", ""),
        "privacy_status": status.get("privacyStatus", ""),
    }


def find_active_broadcast(
    service,
    started_before: datetime | None = None,
    started_after: datetime | None = None,
) -> dict | None:
    """Return the most recently started active broadcast for this channel."""
    if started_before is not None:
        if started_before.tzinfo is None:
            started_before = started_before.replace(tzinfo=timezone.utc)
        else:
            started_before = started_before.astimezone(timezone.utc)
    if started_after is not None:
        if started_after.tzinfo is None:
            started_after = started_after.replace(tzinfo=timezone.utc)
        else:
            started_after = started_after.astimezone(timezone.utc)

    response = service.liveBroadcasts().list(
        part="id,snippet,status",
        broadcastStatus="active",
        broadcastType="all",
        maxResults=50,
    ).execute()
    candidates = []
    for item in response.get("items", []):
        if not item.get("id"):
            continue
        life_cycle = item.get("status", {}).get("lifeCycleStatus", "")
        if life_cycle not in {"live", "liveStarting", "testing"}:
            continue
        started = _parse_api_datetime(item.get("snippet", {}).get("actualStartTime"))
        if started_before is not None and (
            started is None or started > started_before
        ):
            continue
        if started_after is not None and (
            started is None or started < started_after
        ):
            continue
        candidates.append((started or datetime.min.replace(tzinfo=timezone.utc), item))
    if not candidates:
        return None
    return _broadcast_summary(max(candidates, key=lambda pair: pair[0])[1])


def list_completed_broadcast_ids(
    service,
    completed_before: datetime | None = None,
) -> set[str]:
    """Return IDs already completed when an OBS stream starts."""
    if completed_before is not None:
        if completed_before.tzinfo is None:
            completed_before = completed_before.replace(tzinfo=timezone.utc)
        else:
            completed_before = completed_before.astimezone(timezone.utc)

    response = service.liveBroadcasts().list(
        part="id,snippet",
        broadcastStatus="completed",
        broadcastType="all",
        maxResults=50,
    ).execute()
    completed_ids = set()
    for item in response.get("items", []):
        video_id = item.get("id")
        if not video_id:
            continue
        if completed_before is not None:
            ended = _parse_api_datetime(
                item.get("snippet", {}).get("actualEndTime")
            )
            if ended is None or ended > completed_before:
                continue
        completed_ids.add(str(video_id))
    return completed_ids


def find_recent_completed_broadcast(
    service,
    ended_after: datetime | None = None,
    exclude_video_ids: set[str] | None = None,
    started_before: datetime | None = None,
    started_after: datetime | None = None,
) -> dict | None:
    """Return the latest completed broadcast ending after ``ended_after``."""
    excluded = set(exclude_video_ids or ())
    if ended_after is not None:
        if ended_after.tzinfo is None:
            ended_after = ended_after.replace(tzinfo=timezone.utc)
        else:
            ended_after = ended_after.astimezone(timezone.utc)
    if started_before is not None:
        if started_before.tzinfo is None:
            started_before = started_before.replace(tzinfo=timezone.utc)
        else:
            started_before = started_before.astimezone(timezone.utc)
    if started_after is not None:
        if started_after.tzinfo is None:
            started_after = started_after.replace(tzinfo=timezone.utc)
        else:
            started_after = started_after.astimezone(timezone.utc)

    response = service.liveBroadcasts().list(
        part="id,snippet,status",
        broadcastStatus="completed",
        broadcastType="all",
        maxResults=50,
    ).execute()
    candidates = []
    for item in response.get("items", []):
        if not item.get("id") or item.get("id") in excluded:
            continue
        if item.get("status", {}).get("lifeCycleStatus") != "complete":
            continue
        ended = _parse_api_datetime(item.get("snippet", {}).get("actualEndTime"))
        if ended is None or (ended_after is not None and ended < ended_after):
            continue
        started = _parse_api_datetime(
            item.get("snippet", {}).get("actualStartTime")
        )
        if started_before is not None:
            if started is None or started > started_before:
                continue
        if started_after is not None and (
            started is None or started < started_after
        ):
            continue
        candidates.append((
            started or datetime.min.replace(tzinfo=timezone.utc),
            ended,
            item,
        ))
    if not candidates:
        return None
    latest = max(candidates, key=lambda entry: (entry[0], entry[1]))
    return _broadcast_summary(latest[2])


def get_broadcast_lifecycle_status(service, video_id: str) -> str:
    """Return the current lifecycle status for a YouTube live broadcast."""
    response = service.liveBroadcasts().list(
        part="status",
        id=video_id,
    ).execute()
    items = response.get("items", [])
    if not items:
        return "not_found"
    return items[0].get("status", {}).get("lifeCycleStatus", "unknown")


def get_archive_processing_state(service, video_id: str) -> dict:
    """Return whether a completed broadcast is downloadable by yt-dlp."""
    response = service.videos().list(
        part="status,processingDetails",
        id=video_id,
    ).execute()
    items = response.get("items", [])
    if not items:
        return {
            "ready": False,
            "failed": False,
            "processing_status": "not_found",
            "upload_status": "not_found",
            "privacy_status": "",
        }

    item = items[0]
    processing_status = item.get("processingDetails", {}).get("processingStatus", "")
    status = item.get("status", {})
    upload_status = status.get("uploadStatus", "")
    privacy_status = status.get("privacyStatus", "")
    failed = processing_status == "failed" or upload_status in {
        "failed", "rejected", "deleted",
    }
    processed = processing_status == "succeeded" or upload_status == "processed"
    return {
        "ready": bool(processed and privacy_status in {"public", "unlisted"}),
        "failed": bool(failed),
        "processing_status": processing_status,
        "upload_status": upload_status,
        "privacy_status": privacy_status,
    }


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


def auth_status_placeholder() -> str:
    """Cheap file-existence-only status for initial UI render.

    Never imports the google stack — the real check runs after page load
    (see web_app create_ui's app.load wiring).
    """
    if not CREDENTIALS_PATH.exists():
        return (
            "未設定: Settings タブの『credentials.json』欄にファイルをドロップしてください "
            f"(保存先: {CREDENTIALS_PATH})"
        )
    if not TOKEN_PATH.exists():
        return "未認証: Settings タブで『認証する』を押してください"
    return "確認中… (token の有効性を検証しています)"


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

# Re-export unchanged — the YouTube layer adds no logic beyond forwarding.
validate_credentials_json = _google_auth.validate_credentials_json


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
