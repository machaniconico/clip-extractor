"""Unit tests for youtube_api.py extractor + description merge."""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import youtube_api
from youtube_api import extract_video_id, _merge_description


class _FakeListResource:
    def __init__(self, response):
        self.response = response
        self.list_kwargs = None

    def list(self, **kwargs):
        self.list_kwargs = kwargs
        return self

    def execute(self):
        return self.response


class _FakeYouTubeService:
    def __init__(self, broadcasts=None, videos=None):
        self.broadcast_resource = _FakeListResource({"items": broadcasts or []})
        self.video_resource = _FakeListResource({"items": videos or []})

    def liveBroadcasts(self):
        return self.broadcast_resource

    def videos(self):
        return self.video_resource


def test_extract_id_standard_watch_url():
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_id_short_url():
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=42") == "dQw4w9WgXcQ"


def test_extract_id_with_extra_query_params():
    assert extract_video_id(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s&feature=youtu.be"
    ) == "dQw4w9WgXcQ"
    assert extract_video_id(
        "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ"
    ) == "dQw4w9WgXcQ"


def test_extract_id_shorts_and_embed():
    assert extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/embed/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_extract_id_returns_none_on_invalid():
    assert extract_video_id("") is None
    assert extract_video_id("not a url") is None
    assert extract_video_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    assert extract_video_id("https://youtube.com/") is None
    # 10-char id (too short) must not match
    assert extract_video_id("https://youtu.be/dQw4w9WgXc") is None


def test_merge_prepend_default():
    out = _merge_description("existing body", "0:00 イントロ\n1:23 A", "prepend")
    assert out == "0:00 イントロ\n1:23 A\n\nexisting body"


def test_merge_prepend_empty_existing():
    out = _merge_description("", "0:00 イントロ", "prepend")
    assert out == "0:00 イントロ"


def test_merge_append():
    out = _merge_description("existing", "0:00 A", "append")
    assert out == "existing\n\n0:00 A"


def test_merge_append_empty_existing():
    out = _merge_description("", "0:00 A", "append")
    assert out == "0:00 A"


def test_merge_replace():
    out = _merge_description("existing long body", "0:00 A", "replace")
    assert out == "0:00 A"


def test_merge_prepend_strips_existing_leading_blank():
    # Existing body with leading blank line should not double up after prepend
    out = _merge_description("\n\nbody", "0:00 A", "prepend")
    assert out == "0:00 A\n\nbody"


def test_merge_none_and_empty_inputs():
    assert _merge_description(None, None, "prepend") == ""
    assert _merge_description(None, "0:00 A", "prepend") == "0:00 A"
    assert _merge_description("existing", None, "prepend") == "existing"


def test_check_auth_status_no_credentials():
    """credentials.json も token もない → configured=False, authenticated=False."""
    import tempfile
    from youtube_api import check_auth_status
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "credentials.json"  # 存在しない
        tok = Path(td) / "youtube_token.json"  # 存在しない
        s = check_auth_status(token_path=tok, credentials_path=creds)
        assert s["configured"] is False
        assert s["token_exists"] is False
        assert s["authenticated"] is False
        assert s["expired"] is False
        assert s["error"] is None


def test_check_auth_status_credentials_only():
    """credentials.json はあるが token がない状態。"""
    import tempfile
    from youtube_api import check_auth_status
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "credentials.json"
        creds.write_text("{}", encoding="utf-8")
        tok = Path(td) / "youtube_token.json"
        s = check_auth_status(token_path=tok, credentials_path=creds)
        assert s["configured"] is True
        assert s["token_exists"] is False
        assert s["authenticated"] is False


def test_check_auth_status_invalid_token_json():
    """token ファイルが不正 JSON → error が詰まり raise しない."""
    import tempfile
    from youtube_api import check_auth_status
    with tempfile.TemporaryDirectory() as td:
        creds = Path(td) / "credentials.json"
        creds.write_text("{}", encoding="utf-8")
        tok = Path(td) / "youtube_token.json"
        tok.write_text("not a json", encoding="utf-8")
        s = check_auth_status(token_path=tok, credentials_path=creds)
        assert s["configured"] is True
        assert s["token_exists"] is True
        assert s["authenticated"] is False
        assert s["error"] is not None, "invalid json must surface as error string"


def test_revoke_auth_with_existing_token():
    """revoke_auth should delete youtube_token.json when present."""
    import tempfile
    import youtube_api
    with tempfile.TemporaryDirectory() as td:
        fake_token = Path(td) / "youtube_token.json"
        fake_token.write_text("{}", encoding="utf-8")
        original = youtube_api.TOKEN_PATH
        youtube_api.TOKEN_PATH = fake_token
        try:
            assert fake_token.exists()
            removed = youtube_api.revoke_auth()
            assert removed is True
            assert not fake_token.exists(), "token file must be gone"
            removed_again = youtube_api.revoke_auth()
            assert removed_again is False, "second revoke must report no-op"
        finally:
            youtube_api.TOKEN_PATH = original


def test_auth_status_summary_not_configured():
    """credentials.json なしで summary が 未設定 メッセージ。"""
    import youtube_api
    orig_creds = youtube_api.CREDENTIALS_PATH
    orig_tok = youtube_api.TOKEN_PATH
    try:
        youtube_api.CREDENTIALS_PATH = Path("/nonexistent/credentials.json")
        youtube_api.TOKEN_PATH = Path("/nonexistent/youtube_token.json")
        s = youtube_api.auth_status_summary()
        assert "未設定" in s, f"expected 未設定 in {s!r}"
    finally:
        youtube_api.CREDENTIALS_PATH = orig_creds
        youtube_api.TOKEN_PATH = orig_tok


def test_auth_status_summary_unauthenticated():
    """credentials.json あるが token なし → 未認証 メッセージ."""
    import tempfile
    import youtube_api
    orig_creds = youtube_api.CREDENTIALS_PATH
    orig_tok = youtube_api.TOKEN_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            creds = Path(td) / "credentials.json"
            creds.write_text("{}", encoding="utf-8")
            tok = Path(td) / "youtube_token.json"
            youtube_api.CREDENTIALS_PATH = creds
            youtube_api.TOKEN_PATH = tok
            s = youtube_api.auth_status_summary()
            assert "未認証" in s, f"expected 未認証 in {s!r}"
    finally:
        youtube_api.CREDENTIALS_PATH = orig_creds
        youtube_api.TOKEN_PATH = orig_tok


def test_validate_credentials_json_missing_file():
    import tempfile
    from youtube_api import validate_credentials_json
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "nope.json"
        ok, msg = validate_credentials_json(missing)
        assert ok is False
        assert "見つかりません" in msg


def test_validate_credentials_json_malformed():
    import tempfile
    from youtube_api import validate_credentials_json
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.json"
        bad.write_text("not json at all", encoding="utf-8")
        ok, msg = validate_credentials_json(bad)
        assert ok is False
        assert "JSON" in msg


def test_validate_credentials_json_web_client_rejected():
    import tempfile
    from youtube_api import validate_credentials_json
    with tempfile.TemporaryDirectory() as td:
        web = Path(td) / "web.json"
        web.write_text(
            '{"web": {"client_id": "x", "client_secret": "y"}}',
            encoding="utf-8",
        )
        ok, msg = validate_credentials_json(web)
        assert ok is False
        assert "Web" in msg or "デスクトップ" in msg


def test_validate_credentials_json_desktop_happy():
    import tempfile
    from youtube_api import validate_credentials_json
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.json"
        good.write_text(
            '{"installed": {"client_id": "id", "client_secret": "sec", "project_id": "p-1"}}',
            encoding="utf-8",
        )
        ok, msg = validate_credentials_json(good)
        assert ok is True
        assert "installed" in msg
        assert "p-1" in msg


def test_validate_credentials_json_missing_client_secret():
    import tempfile
    from youtube_api import validate_credentials_json
    with tempfile.TemporaryDirectory() as td:
        partial = Path(td) / "part.json"
        partial.write_text(
            '{"installed": {"client_id": "id"}}',
            encoding="utf-8",
        )
        ok, msg = validate_credentials_json(partial)
        assert ok is False
        assert "client_secret" in msg


def test_install_credentials_from_file_copies_and_reports():
    import tempfile
    import youtube_api
    orig_creds = youtube_api.CREDENTIALS_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "user_creds.json"
            src.write_text(
                '{"installed": {"client_id": "id", "client_secret": "sec"}}',
                encoding="utf-8",
            )
            dest = Path(td) / "credentials.json"
            youtube_api.CREDENTIALS_PATH = dest
            msg = youtube_api.install_credentials_from_file(src)
            assert "配置完了" in msg, f"unexpected message: {msg!r}"
            assert dest.exists(), "destination file must be written"
            assert dest.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
    finally:
        youtube_api.CREDENTIALS_PATH = orig_creds


def test_install_credentials_from_file_rejects_none():
    from youtube_api import install_credentials_from_file
    msg = install_credentials_from_file(None)
    assert "選択されていません" in msg


def test_install_credentials_from_file_rejects_web_client():
    import tempfile
    import youtube_api
    orig_creds = youtube_api.CREDENTIALS_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "web.json"
            src.write_text(
                '{"web": {"client_id": "id", "client_secret": "sec"}}',
                encoding="utf-8",
            )
            dest = Path(td) / "credentials.json"
            youtube_api.CREDENTIALS_PATH = dest
            msg = youtube_api.install_credentials_from_file(src)
            assert "中止" in msg
            assert not dest.exists(), "destination must not be touched on rejection"
    finally:
        youtube_api.CREDENTIALS_PATH = orig_creds


def test_find_active_broadcast_selects_latest_actual_start():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "old-active",
            "snippet": {
                "title": "old",
                "actualStartTime": "2026-07-22T09:00:00Z",
            },
            "status": {"lifeCycleStatus": "live", "privacyStatus": "public"},
        },
        {
            "id": "current-live",
            "snippet": {
                "title": "current",
                "actualStartTime": "2026-07-22T10:00:00Z",
            },
            "status": {"lifeCycleStatus": "live", "privacyStatus": "unlisted"},
        },
    ])

    result = youtube_api.find_active_broadcast(service)

    assert result == {
        "video_id": "current-live",
        "title": "current",
        "actual_start_time": "2026-07-22T10:00:00Z",
        "actual_end_time": "",
        "privacy_status": "unlisted",
    }
    assert service.broadcast_resource.list_kwargs == {
        "part": "id,snippet,status",
        "broadcastStatus": "active",
        "broadcastType": "all",
        "maxResults": 50,
    }


def test_find_active_broadcast_rejects_stream_started_after_target_stop():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "target-stream",
            "snippet": {"actualStartTime": "2026-07-22T10:00:00Z"},
            "status": {"lifeCycleStatus": "live"},
        },
        {
            "id": "next-stream",
            "snippet": {"actualStartTime": "2026-07-22T10:06:00Z"},
            "status": {"lifeCycleStatus": "live"},
        },
    ])

    result = youtube_api.find_active_broadcast(
        service,
        started_before=datetime(2026, 7, 22, 10, 5, tzinfo=timezone.utc),
    )

    assert result["video_id"] == "target-stream"


def test_find_active_broadcast_rejects_previous_stream_for_new_obs_start():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "previous-stream",
            "snippet": {"actualStartTime": "2026-07-22T09:40:00Z"},
            "status": {"lifeCycleStatus": "live"},
        },
        {
            "id": "current-stream",
            "snippet": {"actualStartTime": "2026-07-22T10:00:00Z"},
            "status": {"lifeCycleStatus": "live"},
        },
    ])

    result = youtube_api.find_active_broadcast(
        service,
        started_after=datetime(2026, 7, 22, 9, 59, 30, tzinfo=timezone.utc),
    )

    assert result["video_id"] == "current-stream"


def test_find_recent_completed_broadcast_uses_obs_stop_window():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "too-old",
            "snippet": {
                "title": "old",
                "actualEndTime": "2026-07-22T09:00:00Z",
            },
            "status": {"lifeCycleStatus": "complete", "privacyStatus": "public"},
        },
        {
            "id": "just-ended",
            "snippet": {
                "title": "archive",
                "actualStartTime": "2026-07-22T08:00:00Z",
                "actualEndTime": "2026-07-22T10:05:00Z",
            },
            "status": {"lifeCycleStatus": "complete", "privacyStatus": "public"},
        },
    ])

    result = youtube_api.find_recent_completed_broadcast(
        service,
        ended_after=datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
    )

    assert result["video_id"] == "just-ended"
    assert result["actual_end_time"] == "2026-07-22T10:05:00Z"
    assert service.broadcast_resource.list_kwargs["broadcastStatus"] == "completed"
    assert "mine" not in service.broadcast_resource.list_kwargs


def test_find_recent_completed_broadcast_excludes_preexisting_ids():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "previous-stream",
            "snippet": {
                "title": "previous",
                "actualEndTime": "2026-07-22T10:04:00Z",
            },
            "status": {"lifeCycleStatus": "complete", "privacyStatus": "public"},
        },
        {
            "id": "current-stream",
            "snippet": {
                "title": "current",
                "actualEndTime": "2026-07-22T10:05:00Z",
            },
            "status": {"lifeCycleStatus": "complete", "privacyStatus": "public"},
        },
    ])

    result = youtube_api.find_recent_completed_broadcast(
        service,
        ended_after=datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
        exclude_video_ids={"previous-stream"},
    )

    assert result["video_id"] == "current-stream"


def test_find_recent_completed_broadcast_rejects_next_stream():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "target-stream",
            "snippet": {
                "actualStartTime": "2026-07-22T10:00:00Z",
                "actualEndTime": "2026-07-22T10:05:10Z",
            },
            "status": {"lifeCycleStatus": "complete"},
        },
        {
            "id": "next-stream",
            "snippet": {
                "actualStartTime": "2026-07-22T10:05:30Z",
                "actualEndTime": "2026-07-22T10:06:00Z",
            },
            "status": {"lifeCycleStatus": "complete"},
        },
    ])

    result = youtube_api.find_recent_completed_broadcast(
        service,
        ended_after=datetime(2026, 7, 22, 10, 3, tzinfo=timezone.utc),
        started_before=datetime(2026, 7, 22, 10, 5, 20, tzinfo=timezone.utc),
    )

    assert result["video_id"] == "target-stream"


def test_find_recent_completed_broadcast_rejects_previous_late_completion():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "previous-stream",
            "snippet": {
                "actualStartTime": "2026-07-22T09:40:00Z",
                "actualEndTime": "2026-07-22T10:05:15Z",
            },
            "status": {"lifeCycleStatus": "complete"},
        },
        {
            "id": "current-stream",
            "snippet": {
                "actualStartTime": "2026-07-22T10:00:00Z",
                "actualEndTime": "2026-07-22T10:05:10Z",
            },
            "status": {"lifeCycleStatus": "complete"},
        },
    ])

    result = youtube_api.find_recent_completed_broadcast(
        service,
        ended_after=datetime(2026, 7, 22, 10, 3, tzinfo=timezone.utc),
        started_after=datetime(2026, 7, 22, 9, 59, 30, tzinfo=timezone.utc),
        started_before=datetime(2026, 7, 22, 10, 5, 20, tzinfo=timezone.utc),
    )

    assert result["video_id"] == "current-stream"


def test_find_recent_completed_broadcast_prefers_latest_start_over_late_old_end():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "previous-stream",
            "snippet": {
                "actualStartTime": "2026-07-22T09:40:00Z",
                "actualEndTime": "2026-07-22T10:05:15Z",
            },
            "status": {"lifeCycleStatus": "complete"},
        },
        {
            "id": "current-stream",
            "snippet": {
                "actualStartTime": "2026-07-22T10:00:00Z",
                "actualEndTime": "2026-07-22T10:05:10Z",
            },
            "status": {"lifeCycleStatus": "complete"},
        },
    ])

    result = youtube_api.find_recent_completed_broadcast(
        service,
        ended_after=datetime(2026, 7, 22, 10, 3, tzinfo=timezone.utc),
        started_before=datetime(2026, 7, 22, 10, 5, 20, tzinfo=timezone.utc),
    )

    assert result["video_id"] == "current-stream"


def test_list_completed_broadcast_ids_returns_baseline_set():
    service = _FakeYouTubeService(broadcasts=[
        {"id": "first"},
        {"id": "second"},
        {"id": ""},
    ])

    result = youtube_api.list_completed_broadcast_ids(service)

    assert result == {"first", "second"}
    assert service.broadcast_resource.list_kwargs == {
        "part": "id,snippet",
        "broadcastStatus": "completed",
        "broadcastType": "all",
        "maxResults": 50,
    }


def test_completed_baseline_uses_obs_start_time_not_api_response_time():
    service = _FakeYouTubeService(broadcasts=[
        {
            "id": "previous-stream",
            "snippet": {"actualEndTime": "2026-07-22T09:59:00Z"},
        },
        {
            "id": "current-stream",
            "snippet": {"actualEndTime": "2026-07-22T10:01:00Z"},
        },
    ])

    result = youtube_api.list_completed_broadcast_ids(
        service,
        completed_before=datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
    )

    assert result == {"previous-stream"}


def test_get_archive_processing_state_requires_processed_and_non_private():
    service = _FakeYouTubeService(videos=[{
        "id": "archive-id",
        "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
        "processingDetails": {"processingStatus": "succeeded"},
    }])

    state = youtube_api.get_archive_processing_state(service, "archive-id")

    assert state == {
        "ready": True,
        "failed": False,
        "processing_status": "succeeded",
        "upload_status": "processed",
        "privacy_status": "unlisted",
    }
    assert service.video_resource.list_kwargs == {
        "part": "status,processingDetails",
        "id": "archive-id",
    }


def test_get_archive_processing_state_allows_terminated_metadata_when_uploaded():
    service = _FakeYouTubeService(videos=[{
        "id": "archive-id",
        "status": {"uploadStatus": "processed", "privacyStatus": "public"},
        "processingDetails": {"processingStatus": "terminated"},
    }])

    state = youtube_api.get_archive_processing_state(service, "archive-id")

    assert state["ready"] is True
    assert state["failed"] is False


def test_get_broadcast_lifecycle_status_by_id():
    service = _FakeYouTubeService(broadcasts=[{
        "id": "archive-id",
        "status": {"lifeCycleStatus": "complete"},
    }])

    status = youtube_api.get_broadcast_lifecycle_status(service, "archive-id")

    assert status == "complete"
    assert service.broadcast_resource.list_kwargs == {
        "part": "status",
        "id": "archive-id",
    }


def run_all():
    test_extract_id_standard_watch_url()
    test_extract_id_short_url()
    test_extract_id_with_extra_query_params()
    test_extract_id_shorts_and_embed()
    test_extract_id_returns_none_on_invalid()
    test_merge_prepend_default()
    test_merge_prepend_empty_existing()
    test_merge_append()
    test_merge_append_empty_existing()
    test_merge_replace()
    test_merge_prepend_strips_existing_leading_blank()
    test_merge_none_and_empty_inputs()
    test_check_auth_status_no_credentials()
    test_check_auth_status_credentials_only()
    test_check_auth_status_invalid_token_json()
    test_revoke_auth_with_existing_token()
    test_auth_status_summary_not_configured()
    test_auth_status_summary_unauthenticated()
    test_validate_credentials_json_missing_file()
    test_validate_credentials_json_malformed()
    test_validate_credentials_json_web_client_rejected()
    test_validate_credentials_json_desktop_happy()
    test_validate_credentials_json_missing_client_secret()
    test_install_credentials_from_file_copies_and_reports()
    test_install_credentials_from_file_rejects_none()
    test_install_credentials_from_file_rejects_web_client()
    print("All tests passed")


if __name__ == "__main__":
    run_all()
