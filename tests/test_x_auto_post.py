"""OBS stream-start behavior for automatic X announcements."""

import ast
import json
import os
import queue
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import web_app
import x_post
from x_post import XCredentials, XPostError, XPostResult


@pytest.fixture(autouse=True)
def _join_x_workers():
    web_app._reset_obs_x_post_stream_guard()
    web_app._apply_obs_x_post_runtime_settings(False, False)
    yield
    web_app._join_obs_workers(timeout=5)
    web_app._reset_obs_x_post_stream_guard()


def _disable_youtube_lookup(monkeypatch):
    monkeypatch.setattr(
        web_app.youtube_api,
        "check_auth_status",
        lambda: {"authenticated": False},
    )


_MISSING_DURATION = object()


def _probe_active_stream(on_stream_started, output_duration_ms=_MISSING_DURATION):
    """Run the real watcher probe path with a fake active OBS response."""
    import obs_integration

    response_data = {"output_active": True}
    if output_duration_ms is not _MISSING_DURATION:
        response_data["output_duration"] = output_duration_ms

    class RequestClient:
        def __init__(self, **_kwargs):
            pass

        def get_stream_status(self):
            return types.SimpleNamespace(**response_data)

        def disconnect(self):
            pass

    watcher = obs_integration.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        lambda _path: None,
        stop_event="stream",
        on_stream_started=on_stream_started,
    )
    watcher._probe_current_stream_status(
        types.SimpleNamespace(ReqClient=RequestClient)
    )
    return watcher


def _install_x_http_post_mock(monkeypatch):
    created_with = []
    posts = []

    class FakeResponse:
        status_code = 201

        @staticmethod
        def json():
            return {"data": {"id": "1234567890123456789"}}

    class FakeOAuth1Session:
        def __init__(self, **kwargs):
            created_with.append(kwargs)

        def post(self, url, *, json, timeout):
            posts.append((url, json, timeout))
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "requests_oauthlib",
        types.SimpleNamespace(OAuth1Session=FakeOAuth1Session),
    )
    return created_with, posts


def _install_archive_capture_recorder(monkeypatch, *, block_service=False):
    service_calls = []
    active_started_after = []
    completed_cutoffs = []
    service_entered = threading.Event()
    release_service = threading.Event()
    if not block_service:
        release_service.set()

    def fake_get_service():
        service_calls.append(object())
        service_entered.set()
        if not release_service.wait(timeout=5):
            raise RuntimeError("test did not release YouTube service lookup")
        return object()

    def fake_find_active(_service, *, started_after=None):
        active_started_after.append(started_after)
        return None

    def fake_list_completed(_service, *, completed_before=None):
        completed_cutoffs.append(completed_before)
        return set()

    monkeypatch.setattr(
        web_app.youtube_api,
        "get_youtube_service",
        fake_get_service,
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "find_active_broadcast",
        fake_find_active,
    )
    monkeypatch.setattr(
        web_app.youtube_api,
        "list_completed_broadcast_ids",
        fake_list_completed,
    )
    return {
        "service_calls": service_calls,
        "active_started_after": active_started_after,
        "completed_before": completed_cutoffs,
        "service_entered": service_entered,
        "release_service": release_service,
    }


def test_auto_post_publishes_once_for_duplicate_stream_start_events(monkeypatch):
    _disable_youtube_lookup(monkeypatch)
    statuses = []
    posted = []
    posted_event = threading.Event()
    credentials = XCredentials("key", "key-secret", "token", "token-secret")

    def fake_post(text, received_credentials):
        posted.append((text, received_credentials))
        posted_event.set()
        return XPostResult(
            post_id="1234567890123456789",
            status_url="https://x.com/i/web/status/1234567890123456789",
        )

    monkeypatch.setattr(web_app, "post_x_post", fake_post)
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("success must not open the composer"),
    )
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)

    started, _finished = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_auto": True,
            "obs_x_post_template": "配信開始！\n{links}",
            "obs_x_post_destinations": "Twitch|https://twitch.tv/example",
        },
        credentials=credentials,
    )

    started()
    started()

    assert posted_event.wait(timeout=5), "X API post was not attempted"
    web_app._join_obs_workers(timeout=5)
    assert posted == [(
        "配信開始！\nTwitch: https://twitch.tv/example",
        credentials,
    )]
    assert any(
        "Xへ自動投稿しました" in line
        and "https://x.com/i/web/status/1234567890123456789" in line
        for line in statuses
    )


def test_unconfirmed_proactive_start_never_calls_x_api(monkeypatch):
    _disable_youtube_lookup(monkeypatch)
    posted = []
    credentials = XCredentials("key", "key-secret", "token", "token-secret")

    def fake_post(*_args, **_kwargs):
        posted.append("called")
        return XPostResult("1", "https://x.com/i/web/status/1")

    monkeypatch.setattr(web_app, "post_x_post", fake_post)
    monkeypatch.setattr(web_app, "open_x_post_composer", lambda _text: None)

    started, _finished = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_auto": True,
            "obs_x_post_template": "配信開始！",
            "obs_x_post_destinations": "",
        },
        credentials=credentials,
    )

    started(proactive=True)
    web_app._join_obs_workers(timeout=5)

    assert posted == []


def test_same_stream_is_not_reposted_after_watcher_restart(monkeypatch, tmp_path):
    import obs_integration

    _disable_youtube_lookup(monkeypatch)
    posted = []
    monkeypatch.setattr(web_app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(
        web_app,
        "X_CREDENTIALS_FILE",
        tmp_path / ".x_credentials.json",
    )

    def fake_post(text, _credentials):
        posted.append(text)
        return XPostResult("1", "https://x.com/i/web/status/1")

    monkeypatch.setattr(web_app, "post_x_post", fake_post)
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("successful API posting must not open composer"),
    )
    class ActiveWatcher:
        status = "connected"
        stream_status_checked = True
        stream_active = True

        def __init__(self, on_stream_started):
            self._on_stream_started = on_stream_started

        def start(self):
            self._on_stream_started(
                stream_start_context={
                    "source": "probe",
                    "output_duration_ms": 120_000,
                }
            )

        def stop(self):
            self.status = "stopped"

    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **kwargs: ActiveWatcher(kwargs["on_stream_started"]),
    )

    def start_watcher():
        return web_app.start_obs_watch(
            "websocket",
            "localhost",
            4455,
            "",
            False,
            "record",
            "",
            False,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
            obs_x_post_on_stream_start=True,
            obs_x_post_template="配信開始！",
            obs_x_post_auto=True,
            obs_x_api_key="key",
            obs_x_api_key_secret="key-secret",
            obs_x_access_token="token",
            obs_x_access_token_secret="token-secret",
        )

    try:
        assert start_watcher() == "connected"
        web_app._join_obs_workers(timeout=5)
        assert start_watcher() == "connected"
        web_app._join_obs_workers(timeout=5)

        assert posted == ["配信開始！"]
    finally:
        web_app.stop_obs_watch()


def test_new_stream_probe_posts_after_watcher_gap_without_finished_event(
    monkeypatch,
):
    _disable_youtube_lookup(monkeypatch)
    posted = []
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    settings = {
        "obs_x_post_on_stream_start": True,
        "obs_x_post_auto": True,
        "obs_x_post_template": "配信開始！",
        "obs_x_post_destinations": "",
    }
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult(str(len(posted)), f"https://x.com/i/web/status/{len(posted)}"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("successful API posting must not open composer"),
    )

    first_started, _ = web_app._obs_make_x_post_callbacks(
        settings,
        credentials=credentials,
    )
    first_watcher = _probe_active_stream(first_started, 120_000)
    web_app._join_obs_workers(timeout=5)
    first_watcher.stop()

    second_started, _ = web_app._obs_make_x_post_callbacks(
        settings,
        credentials=credentials,
    )
    second_watcher = _probe_active_stream(second_started, 5_000)
    web_app._join_obs_workers(timeout=5)

    assert posted == ["配信開始！", "配信開始！"]
    second_watcher.stop()


def test_same_stream_probe_duration_does_not_repost_after_watcher_restart(
    monkeypatch,
):
    _disable_youtube_lookup(monkeypatch)
    posted = []
    statuses = []
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    settings = {
        "obs_x_post_on_stream_start": True,
        "obs_x_post_auto": True,
        "obs_x_post_template": "配信開始！",
        "obs_x_post_destinations": "",
    }
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult("1", "https://x.com/i/web/status/1"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("same stream must not open composer again"),
    )
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)

    first_started, _ = web_app._obs_make_x_post_callbacks(
        settings,
        credentials=credentials,
    )
    first_watcher = _probe_active_stream(first_started, 120_000)
    web_app._join_obs_workers(timeout=5)
    first_watcher.stop()

    same_started, _ = web_app._obs_make_x_post_callbacks(
        settings,
        credentials=credentials,
    )
    same_watcher = _probe_active_stream(same_started, 120_500)
    same_watcher.stop()

    unknown_started, _ = web_app._obs_make_x_post_callbacks(
        settings,
        credentials=credentials,
    )
    unknown_watcher = _probe_active_stream(unknown_started)
    web_app._join_obs_workers(timeout=5)

    assert posted == ["配信開始！"]
    assert any("配信時間を取得できない" in line for line in statuses)
    unknown_watcher.stop()


def test_probe_without_duration_never_posts(monkeypatch):
    _disable_youtube_lookup(monkeypatch)
    posted = []
    statuses = []
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult("1", "https://x.com/i/web/status/1"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("a duration-less probe must not open composer"),
    )
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)
    started, _ = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_on_stream_start": True,
            "obs_x_post_auto": True,
            "obs_x_post_template": "配信開始！",
            "obs_x_post_destinations": "",
        },
        credentials=credentials,
    )

    watcher = _probe_active_stream(started)
    web_app._join_obs_workers(timeout=5)

    assert posted == []
    assert any("配信時間を取得できない" in line for line in statuses)
    watcher.stop()


def test_duration_probe_then_same_real_started_opens_composer_once(monkeypatch):
    import obs_integration

    _disable_youtube_lookup(monkeypatch)
    composer_texts = []
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda *_args: pytest.fail("auto=false must not call the X API"),
    )
    monkeypatch.setattr(web_app, "open_x_post_composer", composer_texts.append)
    started, _ = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_on_stream_start": True,
            "obs_x_post_auto": False,
            "obs_x_post_template": "配信開始！",
            "obs_x_post_destinations": "",
        }
    )

    watcher = _probe_active_stream(started, 0)
    try:
        web_app._join_obs_workers(timeout=5)
        watcher.on_stream_state_changed(
            types.SimpleNamespace(
                output_state=obs_integration.OBS_WEBSOCKET_OUTPUT_STARTED,
            )
        )
        web_app._join_obs_workers(timeout=5)

        assert composer_texts == ["配信開始！"]
    finally:
        watcher.stop()


def test_auto_api_duration_probe_then_same_real_started_posts_once(monkeypatch):
    import obs_integration

    _disable_youtube_lookup(monkeypatch)
    created_with, posts = _install_x_http_post_mock(monkeypatch)
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("successful auto posting must not open composer"),
    )
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    started, _ = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_on_stream_start": True,
            "obs_x_post_auto": True,
            "obs_x_post_template": "配信開始！",
            "obs_x_post_destinations": "",
        },
        credentials=credentials,
    )

    watcher = _probe_active_stream(started, 0)
    try:
        watcher._join_workers(timeout=5)
        web_app._join_obs_workers(timeout=5)
        watcher.on_stream_state_changed(
            types.SimpleNamespace(
                output_state=obs_integration.OBS_WEBSOCKET_OUTPUT_STARTED,
            )
        )
        watcher._join_workers(timeout=5)
        web_app._join_obs_workers(timeout=5)

        assert created_with == [{
            "client_key": "key",
            "client_secret": "key-secret",
            "resource_owner_key": "token",
            "resource_owner_secret": "token-secret",
        }]
        assert posts == [(
            x_post.X_API_POST_URL,
            {"text": "配信開始！"},
            x_post.X_API_TIMEOUT,
        )]
    finally:
        watcher.stop()


def test_auto_api_durationless_probe_then_real_started_posts_once(monkeypatch):
    import obs_integration

    _disable_youtube_lookup(monkeypatch)
    created_with, posts = _install_x_http_post_mock(monkeypatch)
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("successful auto posting must not open composer"),
    )
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    started, _ = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_on_stream_start": True,
            "obs_x_post_auto": True,
            "obs_x_post_template": "配信開始！",
            "obs_x_post_destinations": "",
        },
        credentials=credentials,
    )

    watcher = _probe_active_stream(started)
    try:
        watcher._join_workers(timeout=5)
        real_started = types.SimpleNamespace(
            output_state=obs_integration.OBS_WEBSOCKET_OUTPUT_STARTED,
        )
        watcher.on_stream_state_changed(real_started)
        watcher.on_stream_state_changed(real_started)
        watcher._join_workers(timeout=5)
        web_app._join_obs_workers(timeout=5)

        assert created_with == [{
            "client_key": "key",
            "client_secret": "key-secret",
            "resource_owner_key": "token",
            "resource_owner_secret": "token-secret",
        }]
        assert posts == [(
            x_post.X_API_POST_URL,
            {"text": "配信開始！"},
            x_post.X_API_TIMEOUT,
        )]
    finally:
        watcher.stop()


def test_durationless_probe_then_real_started_posts_exactly_once(monkeypatch):
    import obs_integration

    _disable_youtube_lookup(monkeypatch)
    posted = []
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult("1", "https://x.com/i/web/status/1"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("the real STARTED event must use the API once"),
    )
    started, _ = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_on_stream_start": True,
            "obs_x_post_auto": True,
            "obs_x_post_template": "配信開始！",
            "obs_x_post_destinations": "",
        },
        credentials=credentials,
    )

    watcher = _probe_active_stream(started)
    real_started = types.SimpleNamespace(
        output_state=obs_integration.OBS_WEBSOCKET_OUTPUT_STARTED,
    )
    watcher.on_stream_state_changed(real_started)
    watcher.on_stream_state_changed(real_started)
    web_app._join_obs_workers(timeout=5)

    assert posted == ["配信開始！"]
    watcher.stop()


def test_real_started_replaces_stale_same_owner_probe_guard(monkeypatch):
    _disable_youtube_lookup(monkeypatch)
    posted = []
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult("1", "https://x.com/i/web/status/1"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("the real STARTED event must use the API"),
    )
    started, _ = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_on_stream_start": True,
            "obs_x_post_auto": True,
            "obs_x_post_template": "配信開始！",
            "obs_x_post_destinations": "",
        },
        credentials=credentials,
    )

    started(
        stream_start_context={
            "source": "probe",
            "output_duration_ms": None,
        }
    )
    probe_epoch = web_app._obs_x_stream_state["epoch"]
    probe_owner = web_app._obs_x_stream_state["owner"]
    assert web_app._obs_x_stream_state["action_claimed"] is True
    assert web_app._obs_x_stream_state["started_at"] is None
    stale_started_at = (
        time.time() - web_app._OBS_X_STREAM_START_TOLERANCE_SECONDS - 1
    )
    with web_app._obs_x_post_lock:
        web_app._obs_x_stream_state["started_at"] = stale_started_at
    assert (
        time.time() - stale_started_at
        > web_app._OBS_X_STREAM_START_TOLERANCE_SECONDS
    )

    started()
    web_app._join_obs_workers(timeout=5)

    assert posted == ["配信開始！"]
    assert web_app._obs_x_stream_state["epoch"] == probe_epoch + 1
    assert web_app._obs_x_stream_state["owner"] is probe_owner
    assert web_app._obs_x_stream_state["started_at"] is not None


def test_real_started_event_replaces_stale_stream_guard(monkeypatch):
    import obs_integration

    _disable_youtube_lookup(monkeypatch)
    posted = []
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    settings = {
        "obs_x_post_on_stream_start": True,
        "obs_x_post_auto": True,
        "obs_x_post_template": "配信開始！",
        "obs_x_post_destinations": "",
    }
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult(str(len(posted)), f"https://x.com/i/web/status/{len(posted)}"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("successful API posting must not open composer"),
    )

    first_started, _ = web_app._obs_make_x_post_callbacks(
        settings,
        credentials=credentials,
    )
    first_watcher = _probe_active_stream(first_started, 120_000)
    web_app._join_obs_workers(timeout=5)
    first_watcher.stop()

    next_started, _ = web_app._obs_make_x_post_callbacks(
        settings,
        credentials=credentials,
    )
    next_watcher = obs_integration.ObsWebsocketWatcher(
        "localhost",
        4455,
        "pw",
        lambda _path: None,
        stop_event="stream",
        on_stream_started=next_started,
    )
    next_watcher.on_stream_state_changed(
        types.SimpleNamespace(
            output_state=obs_integration.OBS_WEBSOCKET_OUTPUT_STARTED,
        )
    )
    web_app._join_obs_workers(timeout=5)

    assert posted == ["配信開始！", "配信開始！"]
    next_watcher.stop()


def test_archive_duration_probe_then_same_real_started_promotes_without_recapture(
    monkeypatch,
):
    import obs_integration

    capture = _install_archive_capture_recorder(
        monkeypatch,
        block_service=True,
    )
    archive_started, _ = web_app._obs_make_archive_callbacks(True, {})
    composed_started = web_app._obs_compose_stream_started_callbacks(
        archive_started,
        lambda proactive=False: None,
    )
    watcher = _probe_active_stream(composed_started, 0)
    try:
        assert capture["service_entered"].wait(timeout=5)
        before_real_start = datetime.now(timezone.utc)
        watcher.on_stream_state_changed(
            types.SimpleNamespace(
                output_state=obs_integration.OBS_WEBSOCKET_OUTPUT_STARTED,
            )
        )
        watcher._join_workers(timeout=5)
        after_real_start = datetime.now(timezone.utc)
        capture["release_service"].set()
        web_app._join_obs_workers(timeout=5)

        assert len(capture["service_calls"]) == 1
        assert len(capture["active_started_after"]) == 1
        assert len(capture["completed_before"]) == 1
        promoted_started_at = capture["completed_before"][0]
        assert before_real_start <= promoted_started_at <= after_real_start
        assert capture["active_started_after"] == [
            promoted_started_at - timedelta(seconds=30)
        ]
    finally:
        capture["release_service"].set()
        watcher.stop()


def test_archive_probe_without_real_started_still_captures_once(monkeypatch):
    capture = _install_archive_capture_recorder(monkeypatch)
    archive_started, _ = web_app._obs_make_archive_callbacks(True, {})
    composed_started = web_app._obs_compose_stream_started_callbacks(
        archive_started,
        lambda proactive=False: None,
    )

    watcher = _probe_active_stream(composed_started, 120_000)
    try:
        watcher._join_workers(timeout=5)
        web_app._join_obs_workers(timeout=5)

        assert len(capture["service_calls"]) == 1
        assert capture["active_started_after"] == [None]
        assert len(capture["completed_before"]) == 1
    finally:
        watcher.stop()


def test_archive_duration_probe_then_distinct_real_started_recaptures(monkeypatch):
    import obs_integration

    capture = _install_archive_capture_recorder(monkeypatch)
    archive_started, _ = web_app._obs_make_archive_callbacks(True, {})
    composed_started = web_app._obs_compose_stream_started_callbacks(
        archive_started,
        lambda proactive=False: None,
    )
    watcher = _probe_active_stream(composed_started, 120_000)
    try:
        watcher._join_workers(timeout=5)
        web_app._join_obs_workers(timeout=5)
        watcher.on_stream_state_changed(
            types.SimpleNamespace(
                output_state=obs_integration.OBS_WEBSOCKET_OUTPUT_STARTED,
            )
        )
        watcher._join_workers(timeout=5)
        web_app._join_obs_workers(timeout=5)

        assert len(capture["service_calls"]) == 2
        assert capture["active_started_after"][0] is None
        assert capture["active_started_after"][1] is not None
        assert len(capture["completed_before"]) == 2
    finally:
        watcher.stop()


def test_generation_race_cannot_post_twice(monkeypatch):
    _disable_youtube_lookup(monkeypatch)
    credentials = XCredentials("key", "key-secret", "token", "token-secret")
    posted = []
    first_render_started = threading.Event()
    release_first_render = threading.Event()
    original_render = web_app.render_x_post_text
    render_count = 0
    render_lock = threading.Lock()

    def blocking_render(*args, **kwargs):
        nonlocal render_count
        with render_lock:
            render_count += 1
            current = render_count
        if current == 1:
            first_render_started.set()
            assert release_first_render.wait(timeout=5)
        return original_render(*args, **kwargs)

    def fake_post(text, _credentials):
        posted.append(text)
        return XPostResult("1", "https://x.com/i/web/status/1")

    monkeypatch.setattr(web_app, "render_x_post_text", blocking_render)
    monkeypatch.setattr(web_app, "post_x_post", fake_post)
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("successful API posting must not open composer"),
    )
    settings = {
        "obs_x_post_auto": True,
        "obs_x_post_template": "配信開始！",
        "obs_x_post_destinations": "",
    }

    with web_app._obs_watcher_lock:
        original_generation = web_app._obs_generation
        web_app._obs_generation = original_generation + 1
        first_generation = web_app._obs_generation
    try:
        first_started, _first_finished = web_app._obs_make_x_post_callbacks(
            settings,
            first_generation,
            credentials=credentials,
        )
        first_started()
        assert first_render_started.wait(timeout=5)

        with web_app._obs_watcher_lock:
            web_app._obs_generation += 1
            second_generation = web_app._obs_generation
        second_started, second_finished = web_app._obs_make_x_post_callbacks(
            settings,
            second_generation,
            credentials=credentials,
        )
        second_started()
        release_first_render.set()
        web_app._join_obs_workers(timeout=5)

        assert posted == ["配信開始！"]
        second_finished()
    finally:
        release_first_render.set()
        web_app._join_obs_workers(timeout=5)
        with web_app._obs_watcher_lock:
            web_app._obs_generation = original_generation


@pytest.mark.parametrize(
    ("enabled_after", "auto_after", "expected_composer"),
    [
        pytest.param(False, False, False, id="announcement-off"),
        pytest.param(True, False, True, id="automatic-post-off"),
    ],
)
def test_running_watcher_uses_latest_x_opt_out_settings(
    monkeypatch,
    tmp_path,
    enabled_after,
    auto_after,
    expected_composer,
):
    import obs_integration

    _disable_youtube_lookup(monkeypatch)
    monkeypatch.setattr(web_app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(
        web_app,
        "X_CREDENTIALS_FILE",
        tmp_path / ".x_credentials.json",
    )
    posted = []
    opened = []

    class RunningWatcher:
        status = "connected"
        stream_status_checked = True
        stream_active = False

        def __init__(self, on_stream_started):
            self._on_stream_started = on_stream_started

        def start(self):
            pass

        def emit_stream_start(self):
            self.stream_active = True
            self._on_stream_started()

        def stop(self):
            self.status = "stopped"

    watchers = []

    def fake_create_watcher(*_args, **kwargs):
        watcher = RunningWatcher(kwargs["on_stream_started"])
        watchers.append(watcher)
        return watcher

    monkeypatch.setattr(obs_integration, "create_watcher", fake_create_watcher)
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult("1", "https://x.com/i/web/status/1"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda text: opened.append(text),
    )

    try:
        status = web_app.start_obs_watch(
            "websocket",
            "localhost",
            4455,
            "",
            False,
            "record",
            "",
            False,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
            obs_x_post_on_stream_start=True,
            obs_x_post_template="配信開始！",
            obs_x_post_auto=True,
            obs_x_api_key="key",
            obs_x_api_key_secret="key-secret",
            obs_x_access_token="token",
            obs_x_access_token_secret="token-secret",
        )
        assert status == "connected"

        web_app._set_obs_x_post_runtime_settings(enabled_after, auto_after)
        watchers[0].emit_stream_start()
        web_app._join_obs_workers(timeout=5)

        assert posted == []
        assert opened == (["配信開始！"] if expected_composer else [])
    finally:
        web_app.stop_obs_watch()


def test_x_opt_out_controls_update_runtime_state_without_queue():
    module = ast.parse(Path(web_app.__file__).read_text(encoding="utf-8"))
    wired_controls = set()

    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "change" or not isinstance(node.func.value, ast.Name):
            continue
        control = node.func.value.id
        if control not in {"obs_x_post_on_stream_start", "obs_x_post_auto"}:
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        assert isinstance(keywords.get("fn"), ast.Name)
        assert keywords["fn"].id == "_set_obs_x_post_runtime_settings"
        assert isinstance(keywords.get("queue"), ast.Constant)
        assert keywords["queue"].value is False
        wired_controls.add(control)

    assert wired_controls == {"obs_x_post_on_stream_start", "obs_x_post_auto"}


def test_running_opt_out_persists_and_blocks_auto_post_after_restart(
    monkeypatch,
    tmp_path,
):
    import obs_integration

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"obs_auto_connect_on_startup": True}),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(
        web_app,
        "X_CREDENTIALS_FILE",
        tmp_path / ".x_credentials.json",
    )
    monkeypatch.setattr(web_app, "_wait_for_obs_websocket", lambda *_a, **_k: True)
    _disable_youtube_lookup(monkeypatch)
    posted = []

    class StartEventWatcher:
        status = "connected"
        stream_status_checked = True

        def __init__(self, on_stream_started):
            self._on_stream_started = on_stream_started
            self.stream_active = False

        def start(self):
            if self._on_stream_started is not None:
                self.stream_active = True
                self._on_stream_started()

        def stop(self):
            self.status = "stopped"

    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **kwargs: StartEventWatcher(
            kwargs.get("on_stream_started")
        ),
    )
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult(str(len(posted)), f"https://x.com/i/web/status/{len(posted)}"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("auto=true with credentials must use the API"),
    )
    cancel_was_set = web_app._obs_auto_connect_cancel.is_set()

    try:
        assert web_app.start_obs_watch(
            "websocket",
            "localhost",
            4455,
            "",
            False,
            "record",
            "",
            False,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
            obs_x_post_on_stream_start=True,
            obs_x_post_template="配信開始！",
            obs_x_post_auto=True,
            obs_x_api_key="key",
            obs_x_api_key_secret="key-secret",
            obs_x_access_token="token",
            obs_x_access_token_secret="token-secret",
        ) == "connected"
        web_app._join_obs_workers(timeout=5)
        assert posted == ["配信開始！"]

        web_app._set_obs_x_post_runtime_settings(False, False)
        saved = json.loads(settings_file.read_text(encoding="utf-8"))
        assert saved["obs_x_post_on_stream_start"] is False
        assert saved["obs_x_post_auto"] is False

        web_app.stop_obs_watch()
        web_app._obs_auto_connect_cancel.clear()
        assert web_app.start_obs_watch_from_defaults() == "connected"
        web_app._join_obs_workers(timeout=5)
        assert posted == ["配信開始！"]
    finally:
        web_app.stop_obs_watch()
        if cancel_was_set:
            web_app._obs_auto_connect_cancel.set()
        else:
            web_app._obs_auto_connect_cancel.clear()


def test_running_opt_out_persistence_preserves_other_obs_settings(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "settings.json"
    credentials_file = tmp_path / ".x_credentials.json"
    preserved = {
        "obs_host": "studio.local",
        "obs_port": 5566,
        "obs_stop_event": "stream",
        "obs_watch_folder": "D:/recordings",
        "obs_auto_process": True,
        "custom_setting": {"keep": [1, 2, 3]},
    }
    settings_file.write_text(
        json.dumps(
            {
                **preserved,
                "obs_x_post_on_stream_start": True,
                "obs_x_post_auto": True,
            }
        ),
        encoding="utf-8",
    )
    credentials_file.write_text("secret-sidecar", encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "X_CREDENTIALS_FILE", credentials_file)

    web_app._set_obs_x_post_runtime_settings(False, False)

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["obs_x_post_on_stream_start"] is False
    assert saved["obs_x_post_auto"] is False
    assert {key: saved[key] for key in preserved} == preserved
    assert credentials_file.read_text(encoding="utf-8") == "secret-sidecar"


def test_start_from_defaults_reloads_opt_out_saved_during_wait(
    monkeypatch,
    tmp_path,
):
    import obs_integration

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_auto_connect_on_startup": True,
                "obs_trigger_method": "websocket",
                "obs_host": "localhost",
                "obs_port": 4455,
                "obs_stop_event": "record",
                "obs_auto_process": False,
                "obs_x_post_on_stream_start": True,
                "obs_x_post_auto": True,
                "obs_x_post_template": "配信開始！",
                "obs_x_post_destinations": "",
            }
        ),
        encoding="utf-8",
    )
    credentials_file = tmp_path / ".x_credentials.json"
    credentials_file.write_text(
        json.dumps(
            {
                "api_key": "saved-key",
                "api_key_secret": "saved-key-secret",
                "access_token": "saved-token",
                "access_token_secret": "saved-token-secret",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(web_app, "X_CREDENTIALS_FILE", credentials_file)
    _disable_youtube_lookup(monkeypatch)
    posted = []
    saved_during_wait = []

    def wait_and_opt_out(*_args, **_kwargs):
        web_app._set_obs_x_post_runtime_settings(False, False)
        saved = json.loads(settings_file.read_text(encoding="utf-8"))
        saved_during_wait.append(
            (
                saved["obs_x_post_on_stream_start"],
                saved["obs_x_post_auto"],
            )
        )
        return True

    class StartEventWatcher:
        status = "connected"
        stream_status_checked = True

        def __init__(self, on_stream_started):
            self._on_stream_started = on_stream_started
            self.stream_active = False

        def start(self):
            self.stream_active = True
            if self._on_stream_started is not None:
                self._on_stream_started()

        def stop(self):
            self.status = "stopped"

    monkeypatch.setattr(web_app, "_wait_for_obs_websocket", wait_and_opt_out)
    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **kwargs: StartEventWatcher(
            kwargs.get("on_stream_started")
        ),
    )
    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda text, _credentials: posted.append(text)
        or XPostResult("1", "https://x.com/i/web/status/1"),
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("saved opt-out must suppress all X actions"),
    )
    cancel_was_set = web_app._obs_auto_connect_cancel.is_set()
    web_app._obs_auto_connect_cancel.clear()

    try:
        assert web_app.start_obs_watch_from_defaults() == "connected"
        web_app._join_obs_workers(timeout=5)

        saved_after_start = json.loads(settings_file.read_text(encoding="utf-8"))
        assert saved_during_wait == [(False, False)]
        assert posted == []
        assert saved_after_start["obs_x_post_on_stream_start"] is False
        assert saved_after_start["obs_x_post_auto"] is False
    finally:
        web_app.stop_obs_watch()
        if cancel_was_set:
            web_app._obs_auto_connect_cancel.set()
        else:
            web_app._obs_auto_connect_cancel.clear()


def test_concurrent_opt_out_wins_over_from_defaults_save(monkeypatch, tmp_path):
    import obs_integration

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_auto_connect_on_startup": True,
                "obs_trigger_method": "websocket",
                "obs_host": "localhost",
                "obs_port": 4455,
                "obs_auto_process": False,
                "obs_x_post_on_stream_start": True,
                "obs_x_post_auto": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(web_app, "_wait_for_obs_websocket", lambda *_a, **_k: True)

    class InactiveWatcher:
        status = "connected"
        stream_status_checked = True
        stream_active = False

        def start(self):
            pass

        def stop(self):
            self.status = "stopped"

    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **_kwargs: InactiveWatcher(),
    )
    original_save = web_app._save_obs_connection_defaults
    opt_out_finished = threading.Event()
    opt_out_threads = []
    finished_before_stale_save = []

    def opt_out():
        web_app._set_obs_x_post_runtime_settings(False, False)
        opt_out_finished.set()

    def coordinated_save(*args, **kwargs):
        thread = threading.Thread(target=opt_out)
        opt_out_threads.append(thread)
        thread.start()
        finished_before_stale_save.append(opt_out_finished.wait(timeout=0.2))
        original_save(*args, **kwargs)

    monkeypatch.setattr(web_app, "_save_obs_connection_defaults", coordinated_save)
    cancel_was_set = web_app._obs_auto_connect_cancel.is_set()
    web_app._obs_auto_connect_cancel.clear()

    try:
        assert web_app.start_obs_watch_from_defaults() == "connected"
        for thread in opt_out_threads:
            thread.join(timeout=5)

        saved = json.loads(settings_file.read_text(encoding="utf-8"))
        assert finished_before_stale_save == [False]
        assert opt_out_finished.is_set()
        assert saved["obs_x_post_on_stream_start"] is False
        assert saved["obs_x_post_auto"] is False
    finally:
        web_app.stop_obs_watch()
        if cancel_was_set:
            web_app._obs_auto_connect_cancel.set()
        else:
            web_app._obs_auto_connect_cancel.clear()


def test_concurrent_obs_settings_save_cannot_restore_x_opt_in(
    monkeypatch,
    tmp_path,
):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_x_post_on_stream_start": True,
                "obs_x_post_auto": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    original_load = web_app.load_defaults
    stale_save_loaded = threading.Event()
    release_stale_save = threading.Event()
    opt_out_paths = queue.Queue()

    class TrackedSettingsLock:
        def __init__(self):
            self._lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "x-opt-out":
                opt_out_paths.put("settings-lock")
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()

    def coordinated_load():
        data = original_load()
        thread_name = threading.current_thread().name
        if thread_name == "stale-obs-save":
            stale_save_loaded.set()
            assert release_stale_save.wait(timeout=5)
        elif thread_name == "x-opt-out":
            opt_out_paths.put("unlocked-load")
        return data

    def save_stale_obs_settings():
        web_app._save_obs_connection_defaults(
            "websocket",
            "localhost",
            4455,
            "",
            False,
            "record",
            "",
            False,
            x_post_on_stream_start=True,
            x_post_auto=True,
        )

    def opt_out():
        web_app._set_obs_x_post_runtime_settings(False, False)

    monkeypatch.setattr(web_app, "load_defaults", coordinated_load)
    monkeypatch.setattr(web_app, "_settings_file_lock", TrackedSettingsLock())
    stale_save_thread = threading.Thread(
        target=save_stale_obs_settings,
        name="stale-obs-save",
    )
    opt_out_thread = threading.Thread(target=opt_out, name="x-opt-out")
    opt_out_path = None
    try:
        stale_save_thread.start()
        assert stale_save_loaded.wait(timeout=5)
        opt_out_thread.start()
        opt_out_path = opt_out_paths.get(timeout=5)
    finally:
        release_stale_save.set()
        stale_save_thread.join(timeout=5)
        opt_out_thread.join(timeout=5)

    assert not stale_save_thread.is_alive()
    assert not opt_out_thread.is_alive()
    assert opt_out_path == "settings-lock"
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["obs_x_post_on_stream_start"] is False
    assert saved["obs_x_post_auto"] is False


def test_x_credentials_sidecar_is_atomically_created_owner_only(
    monkeypatch,
    tmp_path,
):
    credentials_file = tmp_path / ".x_credentials.json"
    monkeypatch.setattr(web_app, "X_CREDENTIALS_FILE", credentials_file)
    replacements = []
    real_replace = os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(web_app.os, "replace", recording_replace)

    web_app._save_x_credentials(
        XCredentials("key", "key-secret", "token", "token-secret")
    )

    assert json.loads(credentials_file.read_text(encoding="utf-8")) == {
        "api_key": "key",
        "api_key_secret": "key-secret",
        "access_token": "token",
        "access_token_secret": "token-secret",
    }
    assert len(replacements) == 1
    assert replacements[0][0].name.startswith(f"{credentials_file.name}.")
    assert replacements[0][1] == credentials_file
    if os.name == "posix":
        assert credentials_file.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


def test_compliance_doc_records_conflicting_limits_and_console_authority():
    document = (
        Path(web_app.__file__).parent / "docs" / "compliance" / "x-api.md"
    ).read_text(encoding="utf-8")

    assert "200 POST requests per user per 15 minutes" in document
    assert "100 requests per user per 15 minutes" in document
    assert "10,000 requests per app per 24 hours" in document
    assert "Developer Console" in document
    assert "stricter 100-per-user/15-minute" in document


def test_partial_credential_input_preserves_saved_sidecar(monkeypatch, tmp_path):
    import obs_integration

    settings_file = tmp_path / "settings.json"
    credentials_file = tmp_path / ".x_credentials.json"
    original_payload = json.dumps(
        {
            "api_key": "saved-key",
            "api_key_secret": "saved-key-secret",
            "access_token": "saved-token",
            "access_token_secret": "saved-token-secret",
        },
        ensure_ascii=False,
    )
    credentials_file.write_text(original_payload, encoding="utf-8")
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(web_app, "X_CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **_kwargs: pytest.fail(
            "partial credentials must fail before watcher creation"
        ),
    )

    status = web_app.start_obs_watch(
        "websocket",
        "localhost",
        4455,
        "",
        False,
        "record",
        "",
        False,
        False,
        5,
        "combined",
        False,
        "gemini",
        "large-v3",
        "",
        obs_x_post_on_stream_start=True,
        obs_x_post_auto=True,
        obs_x_api_key="replacement-key",
    )

    assert "4項目すべて" in status
    assert credentials_file.read_text(encoding="utf-8") == original_payload


def test_auto_post_failure_reports_error_and_opens_manual_composer(monkeypatch):
    _disable_youtube_lookup(monkeypatch)
    statuses = []
    opened = []
    opened_event = threading.Event()
    credentials = XCredentials("key", "key-secret", "token", "token-secret")

    def fail_post(_text, _credentials):
        raise XPostError("X APIの認証情報が拒否されました (HTTP 401)")

    def fake_open(text):
        opened.append(text)
        opened_event.set()
        return "https://x.com/intent/post"

    monkeypatch.setattr(web_app, "post_x_post", fail_post)
    monkeypatch.setattr(web_app, "open_x_post_composer", fake_open)
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)

    started, _finished = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_auto": True,
            "obs_x_post_template": "配信開始！\n{links}",
            "obs_x_post_destinations": "https://example.com/live",
        },
        credentials=credentials,
    )

    started()

    assert opened_event.wait(timeout=5), "manual fallback was not opened"
    web_app._join_obs_workers(timeout=5)
    assert opened == ["配信開始！\nexample.com: https://example.com/live"]
    assert any(
        "X自動投稿に失敗" in line and "HTTP 401" in line
        for line in statuses
    )


@pytest.mark.parametrize(
    ("auto_enabled", "credentials", "expected_warning"),
    [
        (
            False,
            XCredentials("key", "key-secret", "token", "token-secret"),
            None,
        ),
        (
            True,
            XCredentials("key", "", "token", ""),
            "認証情報未設定のため手動投稿",
        ),
    ],
)
def test_manual_modes_never_call_x_api(
    monkeypatch,
    auto_enabled,
    credentials,
    expected_warning,
):
    _disable_youtube_lookup(monkeypatch)
    statuses = []
    opened = []
    opened_event = threading.Event()

    monkeypatch.setattr(
        web_app,
        "post_x_post",
        lambda *_args, **_kwargs: pytest.fail("X API must not be called"),
    )

    def fake_open(text):
        opened.append(text)
        opened_event.set()
        return "https://x.com/intent/post"

    monkeypatch.setattr(web_app, "open_x_post_composer", fake_open)
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)

    started, _finished = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_auto": auto_enabled,
            "obs_x_post_template": "配信開始！",
            "obs_x_post_destinations": "",
        },
        credentials=credentials,
    )

    started()

    assert opened_event.wait(timeout=5), "manual composer was not opened"
    web_app._join_obs_workers(timeout=5)
    assert opened == ["配信開始！"]
    if expected_warning is None:
        assert all("認証情報未設定" not in line for line in statuses)
    else:
        assert any(expected_warning in line for line in statuses)


def test_saved_credentials_are_reused_server_side_after_restart(
    monkeypatch,
    tmp_path,
):
    import obs_integration

    credentials_file = tmp_path / ".x_credentials.json"
    credentials_file.write_text(
        json.dumps({
            "api_key": "saved-key",
            "api_key_secret": "saved-key-secret",
            "access_token": "saved-token",
            "access_token_secret": "saved-token-secret",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(web_app, "X_CREDENTIALS_FILE", credentials_file)
    captured = {}

    class FakeWatcher:
        status = "connected"
        stream_status_checked = True
        stream_active = False

        def start(self):
            pass

        def stop(self):
            self.status = "stopped"

    def fake_callbacks(
        _settings,
        _generation,
        *,
        credentials,
        use_runtime_settings,
    ):
        captured["credentials"] = credentials
        captured["use_runtime_settings"] = use_runtime_settings
        return (lambda **_kwargs: None), (lambda: None)

    monkeypatch.setattr(web_app, "_obs_make_x_post_callbacks", fake_callbacks)
    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **_kwargs: FakeWatcher(),
    )

    try:
        status = web_app.start_obs_watch(
            "websocket",
            "localhost",
            4455,
            "",
            False,
            "record",
            "",
            False,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
            obs_x_post_on_stream_start=True,
            obs_x_post_auto=True,
        )

        assert status == "connected"
        assert captured["credentials"].as_dict() == {
            "api_key": "saved-key",
            "api_key_secret": "saved-key-secret",
            "access_token": "saved-token",
            "access_token_secret": "saved-token-secret",
        }
        assert captured["use_runtime_settings"] is True
    finally:
        web_app.stop_obs_watch()


def test_start_from_defaults_restores_auto_post_and_publishes(
    monkeypatch,
    tmp_path,
):
    import obs_integration

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "obs_auto_connect_on_startup": True,
                "obs_trigger_method": "websocket",
                "obs_host": "localhost",
                "obs_port": 4455,
                "obs_stop_event": "record",
                "obs_watch_folder": "",
                "obs_auto_process": False,
                "auto_append_youtube": False,
                "obs_x_post_on_stream_start": True,
                "obs_x_post_auto": True,
                "obs_x_post_template": "再起動後の配信！",
                "obs_x_post_destinations": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    credentials_file = tmp_path / ".x_credentials.json"
    credentials_file.write_text(
        json.dumps(
            {
                "api_key": "saved-key",
                "api_key_secret": "saved-key-secret",
                "access_token": "saved-token",
                "access_token_secret": "saved-token-secret",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_app, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(web_app, "OBS_PASSWORD_FILE", tmp_path / ".obs_password")
    monkeypatch.setattr(web_app, "X_CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr(web_app, "_wait_for_obs_websocket", lambda *_a, **_k: True)
    _disable_youtube_lookup(monkeypatch)
    posted = []

    class ActiveWatcher:
        status = "connected"
        stream_status_checked = True
        stream_active = False

        def __init__(self, on_stream_started):
            self._on_stream_started = on_stream_started

        def start(self):
            self.stream_active = True
            self._on_stream_started()

        def stop(self):
            self.status = "stopped"

    monkeypatch.setattr(
        obs_integration,
        "create_watcher",
        lambda *_args, **kwargs: ActiveWatcher(kwargs["on_stream_started"]),
    )

    def fake_post(text, credentials):
        posted.append((text, credentials.as_dict()))
        return XPostResult("1", "https://x.com/i/web/status/1")

    monkeypatch.setattr(web_app, "post_x_post", fake_post)
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("restored auto=true must use the API"),
    )
    cancel_was_set = web_app._obs_auto_connect_cancel.is_set()
    web_app._obs_auto_connect_cancel.clear()

    try:
        assert web_app.start_obs_watch_from_defaults() == "connected"
        web_app._join_obs_workers(timeout=5)

        assert posted == [
            (
                "再起動後の配信！",
                {
                    "api_key": "saved-key",
                    "api_key_secret": "saved-key-secret",
                    "access_token": "saved-token",
                    "access_token_secret": "saved-token-secret",
                },
            )
        ]
    finally:
        web_app.stop_obs_watch()
        if cancel_was_set:
            web_app._obs_auto_connect_cancel.set()
        else:
            web_app._obs_auto_connect_cancel.clear()
