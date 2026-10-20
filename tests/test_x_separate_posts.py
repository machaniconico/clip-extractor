"""Per-destination announcements, using local settings and mocked external writes."""

import json
import threading

import pytest

import web_app
import x_post
from x_post import XCredentials, XPostError, XPostResult


YT = "https://www.youtube.com/watch?v=abcdefghijk"
TWITCH = "https://www.twitch.tv/example"
NICO = "https://live.nicovideo.jp/watch/lv123456789"
DESTINATIONS = f"{TWITCH}\n{NICO}"
CREDENTIALS = XCredentials("key", "secret", "token", "token-secret")


@pytest.fixture(autouse=True)
def isolated_announcements(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(web_app, "_obs_append_status", lambda _message: None)
    monkeypatch.setattr(
        web_app.youtube_api, "check_auth_status", lambda: {"authenticated": False}
    )
    web_app._reset_obs_x_post_stream_guard()
    web_app._apply_obs_x_post_runtime_settings(True, True)
    yield
    web_app._join_obs_workers(timeout=5)
    web_app._reset_obs_x_post_stream_guard()
    web_app._apply_obs_x_post_runtime_settings(False, False)


@pytest.mark.parametrize("mode", [None, "combined", "invalid", "SEPARATE"])
def test_combined_mode_preserves_existing_body(mode):
    kwargs = dict(youtube_url=YT, title="今日の配信", destinations=DESTINATIONS)
    expected = x_post.render_x_post_text("{title}\n{links}", **kwargs)
    assert x_post.build_x_post_texts("{title}\n{links}", mode=mode, **kwargs) == (expected,)


def test_separate_posts_have_common_title_and_only_their_own_link():
    texts = x_post.build_x_post_texts(
        "{title}\n{links}", youtube_url=YT, title="今日の配信",
        destinations=f"{DESTINATIONS}\n{TWITCH}\n{YT}", mode="separate",
    )
    assert texts == (
        f"今日の配信\nYouTube: {YT}",
        f"今日の配信\nTwitch: {TWITCH}",
        f"今日の配信\nニコ生: {NICO}",
    )


def test_youtube_aliases_are_one_target_but_distinct_videos_remain():
    texts = x_post.build_x_post_texts(
        "{links}", youtube_url=YT, mode="separate",
        destinations=(
            "https://youtu.be/abcdefghijk?t=1\n"
            "https://youtube.com/live/abcdefghijk?feature=share\n"
            "https://www.youtube.com/shorts/abcdefghijk\n"
            "https://www.youtube.com/watch?v=lmnopqrstuv"
        ),
    )
    assert texts == (
        f"YouTube: {YT}", "YouTube: https://www.youtube.com/watch?v=lmnopqrstuv"
    )


def test_malformed_urls_are_skipped_and_lookalikes_are_not_youtube():
    texts = x_post.build_x_post_texts(
        "{youtube_url}\n{links}", mode="separate",
        destinations="https://[broken\nhttps://youtube.com.evil.test/watch?v=abcdefghijk",
    )
    assert texts == (
        "youtube.com.evil.test: https://youtube.com.evil.test/watch?v=abcdefghijk",
    )
    assert x_post.build_x_post_texts("開始！", destinations="invalid", mode="separate") == ()


@pytest.mark.parametrize("template", ["開始！", "YouTube: {youtube_url}", "{title}"])
def test_separate_custom_template_always_contains_destination_url(template):
    texts = x_post.build_x_post_texts(
        template, title="配信", destinations=DESTINATIONS, mode="separate"
    )
    assert len(texts) == 2
    assert TWITCH in texts[0] and NICO not in texts[0]
    assert NICO in texts[1] and TWITCH not in texts[1]


def test_substitution_does_not_expand_tokens_inside_titles_or_labels():
    title = "{links} と {url} の使い方"
    texts = x_post.build_x_post_texts(
        "{platform}: {title}\n{url}", title=title,
        destinations=f"Label {{url}}|{TWITCH}", mode="separate",
    )
    assert texts == (f"Label {{url}}: {title}\n{TWITCH}",)
    assert x_post.render_x_post_text("{title}\n{links}", title=title, destinations=TWITCH) == (
        f"{title}\nTwitch: {TWITCH}"
    )


def callbacks(*, auto=True, destinations=DESTINATIONS, generation=None):
    web_app._apply_obs_x_post_runtime_settings(True, auto)
    return web_app._obs_make_x_post_callbacks(
        {"obs_x_post_on_stream_start": True, "obs_x_post_auto": auto,
         "obs_x_post_mode": "separate", "obs_x_post_template": "{links}",
         "obs_x_post_destinations": destinations},
        generation=generation, credentials=CREDENTIALS, use_runtime_settings=True,
    )


def run_started(started):
    started()
    started()
    web_app._join_obs_workers(timeout=5)
    started()
    web_app._join_obs_workers(timeout=5)


def test_batch_posts_each_destination_once_even_on_duplicate_events(monkeypatch):
    posted = []
    monkeypatch.setattr(web_app.youtube_api, "check_auth_status", lambda: {"authenticated": True})
    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", object)
    monkeypatch.setattr(web_app.youtube_api, "find_active_broadcast", lambda *_a, **_k: {
        "video_id": "abcdefghijk", "title": "配信タイトル",
    })
    monkeypatch.setattr(web_app, "post_x_post", lambda text, _creds: posted.append(text) or XPostResult("1", "https://x.com/i/web/status/1"))
    monkeypatch.setattr(web_app, "open_x_post_composer", lambda _text: pytest.fail("Unexpected composer"))
    started, _ = callbacks(destinations=f"{DESTINATIONS}\nhttps://youtu.be/abcdefghijk")
    run_started(started)
    assert posted == [f"YouTube: {YT}", f"Twitch: {TWITCH}", f"ニコ生: {NICO}"]


def test_partial_api_failure_falls_back_only_for_failed_and_remaining_targets(monkeypatch):
    posted, composers, statuses = [], [], []

    def post(text, _credentials):
        posted.append(text)
        if len(posted) == 2:
            raise XPostError("通信エラー")
        return XPostResult("1", "https://x.com/i/web/status/1")

    monkeypatch.setattr(web_app, "post_x_post", post)
    monkeypatch.setattr(web_app, "open_x_post_composer", composers.append)
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)
    started, _ = callbacks(destinations=f"{YT}\n{DESTINATIONS}")
    run_started(started)
    assert posted == [f"YouTube: {YT}", f"Twitch: {TWITCH}"]
    assert composers == [f"Twitch: {TWITCH}", f"ニコ生: {NICO}"]
    assert any("確認" in status and "X" in status for status in statuses)


@pytest.mark.parametrize("changed_flags", [(False, True), (True, False), (False, False)])
def test_opt_out_after_first_post_stops_remaining_batch(monkeypatch, changed_flags):
    posted = []

    def post(text, _credentials):
        posted.append(text)
        web_app._apply_obs_x_post_runtime_settings(*changed_flags)
        return XPostResult("1", "https://x.com/i/web/status/1")

    monkeypatch.setattr(web_app, "post_x_post", post)
    monkeypatch.setattr(web_app, "open_x_post_composer", lambda _text: pytest.fail("Opt-out must stop fallback too"))
    started, _ = callbacks()
    run_started(started)
    assert posted == [f"Twitch: {TWITCH}"]


def test_manual_to_automatic_change_does_not_send_remaining_targets(monkeypatch):
    composers = []

    def compose(text):
        composers.append(text)
        web_app._apply_obs_x_post_runtime_settings(True, True)

    monkeypatch.setattr(web_app, "post_x_post", lambda *_a: pytest.fail("A manual batch must not become automatic"))
    monkeypatch.setattr(web_app, "open_x_post_composer", compose)
    started, _ = callbacks(auto=False)
    run_started(started)
    assert composers == [f"Twitch: {TWITCH}"]


def test_new_generation_cancels_remaining_targets(monkeypatch):
    posted = []
    generation = web_app._obs_generation

    def post(text, _credentials):
        posted.append(text)
        monkeypatch.setattr(web_app, "_obs_generation", generation + 1)
        return XPostResult("1", "https://x.com/i/web/status/1")

    monkeypatch.setattr(web_app, "post_x_post", post)
    monkeypatch.setattr(web_app, "open_x_post_composer", lambda _text: pytest.fail("Stale batch"))
    started, _ = callbacks(generation=generation)
    run_started(started)
    assert posted == [f"Twitch: {TWITCH}"]


def test_empty_batch_is_claimed_without_external_actions(monkeypatch):
    monkeypatch.setattr(web_app, "post_x_post", lambda *_a: pytest.fail("No destination"))
    monkeypatch.setattr(web_app, "open_x_post_composer", lambda _text: pytest.fail("No destination"))
    started, _ = callbacks(destinations="")
    run_started(started)
    assert web_app._obs_x_stream_state["action_claimed"] is True


@pytest.mark.parametrize("entry", ["manual_explicit", "manual_saved", "startup"])
def test_start_paths_preserve_and_use_separate_mode(monkeypatch, entry):
    import obs_integration

    web_app.SETTINGS_FILE.write_text(json.dumps({
        "obs_auto_connect_on_startup": True,
        "obs_auto_process": False,
        "obs_x_post_on_stream_start": True,
        "obs_x_post_auto": True,
        "obs_x_post_template": "{links}",
        "obs_x_post_destinations": DESTINATIONS,
        "obs_x_post_mode": "combined" if entry == "manual_explicit" else "separate",
    }), encoding="utf-8")
    posted = []

    class Watcher:
        status = "connected"
        stream_status_checked = True
        stream_active = False

        def __init__(self, started):
            self.started = started

        def start(self):
            self.stream_active = True
            self.started()

        def stop(self):
            pass

    monkeypatch.setattr(web_app, "_obs_watcher", None)
    monkeypatch.setattr(web_app, "_obs_auto_connect_cancel", threading.Event())
    monkeypatch.setattr(web_app, "_wait_for_obs_websocket", lambda *_a, **_k: True)
    monkeypatch.setattr(web_app, "load_x_credentials", lambda: CREDENTIALS)
    monkeypatch.setattr(obs_integration, "create_watcher", lambda *_a, **kwargs: Watcher(kwargs["on_stream_started"]))
    monkeypatch.setattr(web_app, "post_x_post", lambda text, _creds: posted.append(text) or XPostResult("1", "https://x.com/i/web/status/1"))
    monkeypatch.setattr(web_app, "open_x_post_composer", lambda _text: pytest.fail("Unexpected composer"))
    try:
        if entry == "startup":
            status = web_app.start_obs_watch_from_defaults()
        else:
            kwargs = {"obs_x_post_mode": "separate"} if entry == "manual_explicit" else {}
            status = web_app.start_obs_watch(
                "websocket", "localhost", 4455, "", False, "record", "",
                False, False, 5, "combined", False, "gemini", "large-v3", "", **kwargs,
            )
        assert status == "connected"
        web_app._join_obs_workers(timeout=5)
        assert posted == [f"Twitch: {TWITCH}", f"ニコ生: {NICO}"]
        assert web_app.load_defaults()["obs_x_post_mode"] == "separate"
    finally:
        web_app.stop_obs_watch()


def test_preview_uses_the_same_numbered_separate_bodies():
    preview = web_app.preview_x_post_text("開始！\n{links}", DESTINATIONS, "separate")
    expected = x_post.build_x_post_texts("開始！\n{links}", destinations=DESTINATIONS, mode="separate")
    assert all(text in preview for text in expected)
    assert "1/2" in preview and "2/2" in preview


def test_saved_mode_survives_omitted_arguments_and_invalid_modes_fall_back():
    web_app.save_x_post_settings("{links}", DESTINATIONS, "", "", "", "", "separate")
    assert web_app.load_defaults()["obs_x_post_mode"] == "separate"
    web_app.save_x_post_settings("更新 {links}", DESTINATIONS, "", "", "", "")
    web_app._save_obs_connection_defaults("websocket", "localhost", 4455, "", False, "record", "", False)
    assert web_app.load_defaults()["obs_x_post_mode"] == "separate"
    web_app.save_x_post_settings("{links}", DESTINATIONS, "", "", "", "", "invalid")
    assert web_app.load_defaults()["obs_x_post_mode"] == "combined"
    persisted = json.loads(web_app.SETTINGS_FILE.read_text(encoding="utf-8"))
    persisted["obs_x_post_mode"] = {"invalid": "value"}
    web_app.SETTINGS_FILE.write_text(json.dumps(persisted), encoding="utf-8")
    assert web_app.load_defaults()["obs_x_post_mode"] == "combined"


def test_page_reload_reads_saved_mode_and_text_fields_without_secrets(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Reload must not read saved credentials")

    monkeypatch.setattr(web_app, "_read_secret_for_ui", lambda _loader, fallback: fallback)
    monkeypatch.setattr(web_app, "load_x_credentials", forbidden)
    app = web_app.create_ui()
    handler = next(fn for fn in app.fns.values() if fn.fn is web_app.load_x_post_defaults_for_ui)
    assert [component.label for component in handler.outputs] == [
        "X投稿文テンプレート", "同時配信先URL（1行1件）", "X投稿のまとめ方",
    ]
    assert not handler.inputs
    assert any(target[1] == "load" for target in app.get_config_file()["dependencies"][handler._id]["targets"])
    for mode in ("separate", "combined"):
        web_app.save_x_post_settings("保存した配信 {links}", DESTINATIONS, "", "", "", "", mode)
        assert handler.fn() == ("保存した配信 {links}", DESTINATIONS, mode)


@pytest.mark.parametrize(
    "cancel_action",
    ["opt_out", "opt_out_then_in", "finished", "stop", "new_generation"],
)
def test_concurrent_lifecycle_change_cancels_remaining_batch(
    monkeypatch, cancel_action
):
    posted = []
    first_post_started = threading.Event()
    release_first_post = threading.Event()
    action_finished = threading.Event()
    action_errors = []
    original_generation = web_app._obs_generation
    monkeypatch.setattr(web_app, "_obs_auto_connect_cancel", threading.Event())
    monkeypatch.setattr(web_app, "_obs_watcher", None)

    def post(text, _credentials):
        posted.append(text)
        if len(posted) == 1:
            first_post_started.set()
            assert release_first_post.wait(timeout=5)
        return XPostResult("1", "https://x.com/i/web/status/1")

    monkeypatch.setattr(web_app, "post_x_post", post)
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("Cancellation must not open a composer"),
    )
    started, finished = callbacks(
        destinations=f"{TWITCH}\n{NICO}\nhttps://example.com/live",
        generation=original_generation,
    )

    def cancel_from_another_thread():
        try:
            if cancel_action in {"opt_out", "opt_out_then_in"}:
                web_app._set_obs_x_post_runtime_settings(False, False)
                if cancel_action == "opt_out_then_in":
                    web_app._set_obs_x_post_runtime_settings(True, True)
            elif cancel_action == "finished":
                finished()
            elif cancel_action == "stop":
                web_app.stop_obs_watch()
            else:
                with web_app._obs_watcher_lock:
                    web_app._obs_generation += 1
        except BaseException as exc:
            action_errors.append(exc)
        finally:
            action_finished.set()

    try:
        started()
        assert first_post_started.wait(timeout=5)
        # A duplicate STARTED event while the first reserved post is in flight
        # must not create a second batch.
        started()
        action_thread = threading.Thread(target=cancel_from_another_thread)
        action_thread.start()
        assert action_finished.wait(timeout=5), (
            "The lifecycle change must not wait for the external X request"
        )
        assert action_errors == []
        if cancel_action == "opt_out":
            assert web_app._obs_x_runtime_settings == {
                "enabled": False,
                "auto": False,
            }
        elif cancel_action == "opt_out_then_in":
            # The values match the batch's original latch, but the intervening
            # opt-out revision must still cancel its remaining posts.
            assert web_app._obs_x_runtime_settings == {
                "enabled": True,
                "auto": True,
            }
        elif cancel_action == "finished":
            assert web_app._obs_x_stream_state["active"] is False
        else:
            assert web_app._obs_generation != original_generation
        release_first_post.set()
        action_thread.join(timeout=5)
        web_app._join_obs_workers(timeout=5)
        assert posted == [f"Twitch: {TWITCH}"]
    finally:
        release_first_post.set()
        web_app._join_obs_workers(timeout=5)
        with web_app._obs_watcher_lock:
            web_app._obs_generation = original_generation


def test_concurrent_opt_out_after_api_failure_skips_manual_fallback(monkeypatch):
    attempted = []
    composers = []
    api_started = threading.Event()
    release_api = threading.Event()
    opt_out_finished = threading.Event()
    opt_out_errors = []
    original_generation = web_app._obs_generation

    def fail_post(text, _credentials):
        attempted.append(text)
        api_started.set()
        assert release_api.wait(timeout=5)
        raise XPostError("通信エラー")

    def opt_out():
        try:
            web_app._set_obs_x_post_runtime_settings(False, False)
        except BaseException as exc:
            opt_out_errors.append(exc)
        finally:
            opt_out_finished.set()

    monkeypatch.setattr(web_app, "post_x_post", fail_post)
    monkeypatch.setattr(web_app, "open_x_post_composer", composers.append)
    started, _finished = callbacks(
        destinations=f"{TWITCH}\n{NICO}",
        generation=original_generation,
    )

    try:
        started()
        assert api_started.wait(timeout=5)
        # Confirm duplicate suppression while the first API request is in flight.
        started()
        opt_out_thread = threading.Thread(target=opt_out)
        opt_out_thread.start()
        assert opt_out_finished.wait(timeout=5), (
            "Opt-out must be applied before the external X request completes"
        )
        assert opt_out_errors == []
        assert web_app._obs_x_runtime_settings == {
            "enabled": False,
            "auto": False,
        }
        release_api.set()
        opt_out_thread.join(timeout=5)
        web_app._join_obs_workers(timeout=5)
        assert attempted == [f"Twitch: {TWITCH}"]
        assert composers == []
    finally:
        release_api.set()
        web_app._join_obs_workers(timeout=5)
