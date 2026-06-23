"""Google Drive upload module.

OAuth plumbing lives in _google_auth; this module is a thin Drive-specific
wrapper over it. Token + credentials live in the user's per-OS config
directory (see _google_auth.get_user_config_dir).
"""

from pathlib import Path

from googleapiclient.http import MediaFileUpload

import _google_auth

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# Migrate legacy files from the project root (one-shot, idempotent).
_google_auth.migrate_legacy_file("token.json")
_google_auth.migrate_legacy_file("credentials.json")

TOKEN_PATH = _google_auth.get_user_config_dir() / "token.json"
CREDENTIALS_PATH = _google_auth.get_user_config_dir() / "credentials.json"


def get_drive_service():
    """Authenticate and return Google Drive service."""
    return _google_auth.build_authenticated_service(
        "drive", "v3", SCOPES, TOKEN_PATH, CREDENTIALS_PATH,
    )


def create_folder(service, folder_name: str, parent_id: str = None) -> str:
    """Create a folder in Google Drive and return its ID."""
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_file(service, file_path: Path, folder_id: str) -> dict:
    """Upload a single file to Google Drive."""
    mime_types = {
        ".mp4": "video/mp4",
        ".srt": "application/x-subrip",
        ".xml": "application/xml",
        ".txt": "text/plain",
        ".json": "application/json",
    }

    mime_type = mime_types.get(file_path.suffix.lower(), "application/octet-stream")
    metadata = {
        "name": file_path.name,
        "parents": [folder_id],
    }

    media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=True)
    file = service.files().create(
        body=metadata, media_body=media, fields="id, name, webViewLink"
    ).execute()

    return file


def upload_output_directory(output_dir: Path, drive_folder_name: str = None) -> dict:
    """Upload entire output directory to Google Drive."""
    service = get_drive_service()

    if not drive_folder_name:
        drive_folder_name = output_dir.name

    # Create main folder
    main_folder_id = create_folder(service, drive_folder_name)
    uploaded = {"folder_name": drive_folder_name, "files": []}

    # Cache subfolder ids keyed by their relative tuple-path so we don't
    # create the same folder (e.g. "clips") once per file inside it.
    folder_cache: dict[tuple, str] = {(): main_folder_id}

    # Upload all files recursively
    for file_path in sorted(output_dir.rglob("*")):
        if file_path.is_file():
            rel_path = file_path.relative_to(output_dir)
            parts = rel_path.parts[:-1]  # directory parts only

            # Walk/create folders, caching each intermediate id
            parent_id = main_folder_id
            for depth in range(1, len(parts) + 1):
                key = parts[:depth]
                if key not in folder_cache:
                    folder_cache[key] = create_folder(service, key[-1], parent_id)
                parent_id = folder_cache[key]

            result = upload_file(service, file_path, parent_id)
            uploaded["files"].append({
                "name": str(rel_path),
                "link": result.get("webViewLink", ""),
            })
            print(f"  Uploaded: {rel_path}")

    # Get folder link
    folder_meta = service.files().get(
        fileId=main_folder_id, fields="webViewLink"
    ).execute()
    uploaded["folder_link"] = folder_meta.get("webViewLink", "")

    return uploaded


def is_configured() -> bool:
    """Check if Google Drive credentials are configured."""
    return CREDENTIALS_PATH.exists()


def check_auth_status(
    token_path: Path | None = None,
    credentials_path: Path | None = None,
) -> dict:
    """Inspect the on-disk auth state without prompting the user.

    Thin wrapper over _google_auth.check_auth_status that defaults the paths
    to this module's constants (so UIs / tests that monkey-patch
    drive_upload.TOKEN_PATH / CREDENTIALS_PATH keep working).
    """
    token_path = token_path or TOKEN_PATH
    credentials_path = credentials_path or CREDENTIALS_PATH
    return _google_auth.check_auth_status(token_path, credentials_path, SCOPES)


def ensure_authenticated(force_reauth: bool = False) -> bool:
    """Guarantee a usable token exists; runs the OAuth browser flow if needed.

    Returns False when credentials.json is missing (nothing we can do
    without it). Raises on genuine auth failures so callers can surface
    the error.
    """
    if force_reauth:
        revoke_auth()
    if not CREDENTIALS_PATH.exists():
        return False
    get_drive_service()
    return True


def revoke_auth() -> bool:
    """Delete token.json if present. Returns whether a file was removed."""
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
    return "未認証: Settings タブまたは --drive-setup で認証してください"


def install_credentials_from_file(src_path) -> str:
    """Validate and copy an uploaded credentials.json into CREDENTIALS_PATH.

    Delegates to _google_auth; reads the module-level CREDENTIALS_PATH so
    tests that monkey-patch it keep working.
    """
    return _google_auth.install_credentials_from_file(src_path, CREDENTIALS_PATH)
