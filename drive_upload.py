"""Google Drive upload module."""

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_PATH = Path(__file__).parent / "token.json"
CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"


def get_drive_service():
    """Authenticate and return Google Drive service."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    "credentials.json が見つかりません。\n"
                    "Google Cloud Console から OAuth 2.0 クライアントIDの認証情報をダウンロードし、\n"
                    f"{CREDENTIALS_PATH} に配置してください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


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

    # Upload all files recursively
    for file_path in sorted(output_dir.rglob("*")):
        if file_path.is_file():
            # Determine parent folder
            rel_path = file_path.relative_to(output_dir)
            parent_id = main_folder_id

            # Create subfolders if needed
            if len(rel_path.parts) > 1:
                for part in rel_path.parts[:-1]:
                    parent_id = create_folder(service, part, parent_id)

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
