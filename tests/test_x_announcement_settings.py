"""Announcement setup and one-post simulcast behavior, without external writes."""

import json

import pytest

import web_app
from x_post import XCredentials, XPostResult, parse_destination_links


@pytest.fixture(autouse=True)
def isolated_announcements(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "SETTINGS_FILE", tmp_path / "settings.json")
    web_app._reset_obs_x_post_stream_guard()
    web_app._apply_obs_x_post_runtime_settings(False, False)
    yield
    web_app._join_obs_workers(timeout=5)
    web_app._reset_obs_x_post_stream_guard()
    web_app._apply_obs_x_post_runtime_settings(False, False)


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("https://www.twitch.tv/example", "Twitch"),
        ("https://youtube.com/watch?v=abcdefghijk", "YouTube"),
        ("https://youtu.be/abcdefghijk", "YouTube"),
        ("https://live.nicovideo.jp/watch/lv123456789", "ニコ生"),
        ("https://twitch.tv.example.com/live", "twitch.tv.example.com"),
        ("https://youtube.com.example.com/live", "youtube.com.example.com"),
        ("https://live.nicovideo.jp.example.com/live", "live.nicovideo.jp.example.com"),
    ],
)
def test_plain_urls_have_service_labels_without_matching_lookalikes(url, label):
    assert parse_destination_links(url)[0].label == label
    assert parse_destination_links(f"My stream|{url}")[0].label == "My stream"


@pytest.mark.parametrize(
    ("template", "announcement"),
    [
        (None, "今日の配信\n配信開始しました！"),
        ("{title} 開始！\n{links}", "今日の配信 開始！"),
    ],
)
def test_simulcast_links_make_one_x_post_on_duplicate_obs_events(
    monkeypatch, template, announcement
):
    youtube_url = "https://www.youtube.com/watch?v=abcdefghijk"
    posted = []
    monkeypatch.setattr(
        web_app.youtube_api, "check_auth_status", lambda: {"authenticated": True}
    )
    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", object)
    monkeypatch.setattr(
        web_app.youtube_api,
        "find_active_broadcast",
        lambda *_a, **_k: {"video_id": "abcdefghijk", "title": "今日の配信"},
    )
    monkeypatch.setattr(web_app, "_obs_append_status", lambda _message: None)
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: pytest.fail("An accepted API post must not open a composer"),
    )

    def post(text, credentials):
        posted.append(text)
        return XPostResult("12345", "https://x.com/i/web/status/12345")

    monkeypatch.setattr(web_app, "post_x_post", post)
    settings = {
        "obs_x_post_on_stream_start": True,
        "obs_x_post_auto": True,
        "obs_x_post_destinations": (
            "https://www.twitch.tv/example\n"
            "https://live.nicovideo.jp/watch/lv123456789\n"
            f"{youtube_url}"
        ),
    }
    if template is not None:
        settings["obs_x_post_template"] = template
    started, _finished = web_app._obs_make_x_post_callbacks(
        settings,
        credentials=XCredentials("key", "key-secret", "token", "token-secret"),
    )
    started()
    started()
    web_app._join_obs_workers(timeout=5)

    assert posted == [
        f"{announcement}\n"
        f"YouTube: {youtube_url}\n"
        "Twitch: https://www.twitch.tv/example\n"
        "ニコ生: https://live.nicovideo.jp/watch/lv123456789"
    ]


def test_preview_does_not_read_secrets_save_settings_or_contact_services(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Preview must not perform external or persistent actions")

    for name in ["load_x_credentials", "post_x_post", "open_x_post_composer", "_write_settings_file"]:
        monkeypatch.setattr(web_app, name, forbidden)
    monkeypatch.setattr(web_app.youtube_api, "check_auth_status", forbidden)
    monkeypatch.setattr(web_app.youtube_api, "get_youtube_service", forbidden)
    assert web_app.preview_x_post_text(
        "配信開始！\n{links}",
        "https://twitch.tv/example\nhttps://live.nicovideo.jp/watch/lv123456789",
    ) == (
        "配信開始！\nTwitch: https://twitch.tv/example\n"
        "ニコ生: https://live.nicovideo.jp/watch/lv123456789"
        "\n\n文字数: 74/280"
    )
    assert "プレビューできる内容がありません" in web_app.preview_x_post_text("{links}", "")


def test_save_preserves_blank_credentials_other_settings_and_opt_out(monkeypatch):
    saved = {
        "obs_host": "custom-host",
        "obs_processing": {"num_clips": 7},
        "custom_setting": "keep",
        "obs_x_post_on_stream_start": False,
        "obs_x_post_auto": False,
    }
    web_app.SETTINGS_FILE.write_text(json.dumps(saved), encoding="utf-8")
    original_secret = b"CLIPSEC1:unreadable-on-this-device"
    web_app.X_CREDENTIALS_FILE.write_bytes(original_secret)
    monkeypatch.setattr(
        web_app, "load_x_credentials", lambda: pytest.fail("Blank fields must not read secrets")
    )
    message, *updates = web_app.save_x_post_settings_for_ui(
        "配信！ {links}", "https://twitch.tv/example", "", "", "", ""
    )
    after = json.loads(web_app.SETTINGS_FILE.read_text(encoding="utf-8"))
    assert message.startswith("X告知設定を保存しました")
    assert all(after[key] == value for key, value in saved.items())
    assert after["obs_x_post_template"] == "配信！ {links}"
    assert after["obs_x_post_destinations"] == "https://twitch.tv/example"
    assert web_app.X_CREDENTIALS_FILE.read_bytes() == original_secret
    assert web_app._obs_x_runtime_settings == {"enabled": False, "auto": False}
    assert all(update["value"] == "" for update in updates)


def test_partial_credentials_do_not_modify_settings_and_keep_input():
    web_app.SETTINGS_FILE.write_text('{"custom_setting": 42}', encoding="utf-8")
    before = web_app.SETTINGS_FILE.read_bytes()
    message, *updates = web_app.save_x_post_settings_for_ui(
        "changed", "", "only-one-secret", "", "", ""
    )
    assert "4項目すべて" in message
    assert "only-one-secret" not in message
    assert web_app.SETTINGS_FILE.read_bytes() == before
    assert not web_app.X_CREDENTIALS_FILE.exists()
    assert all("value" not in update for update in updates)


def test_complete_credentials_use_secret_store_and_clear_ui_fields(monkeypatch):
    captured = []
    monkeypatch.setattr(web_app, "_save_x_credentials", captured.append)
    result = web_app.save_x_post_settings_for_ui(
        "配信 {links}", "https://twitch.tv/example", "key", "key-secret", "token", "token-secret"
    )
    assert captured[0].as_dict() == XCredentials("key", "key-secret", "token", "token-secret").as_dict()
    assert all(update["value"] == "" for update in result[1:])
    assert all("保存済み" in update["placeholder"] for update in result[1:])
    persisted = web_app.SETTINGS_FILE.read_text(encoding="utf-8")
    assert "key-secret" not in persisted
    assert "token-secret" not in persisted
    assert "token-secret" not in str(result)


def test_whitespace_credentials_do_not_claim_to_be_saved():
    result = web_app.save_x_post_settings_for_ui("配信", "", " ", " ", " ", " ")
    assert result[0].startswith("X告知設定を保存しました")
    assert not web_app.X_CREDENTIALS_FILE.exists()
    assert all("placeholder" not in update for update in result[1:])


def test_save_error_does_not_expose_exception_and_keeps_credential_input(monkeypatch, caplog):
    def fail(*_args, **_kwargs):
        raise OSError("private-request-detail")

    monkeypatch.setattr(web_app, "_write_settings_file", fail)
    message, *updates = web_app.save_x_post_settings_for_ui(
        "配信", "", "key", "key-secret", "token", "token-secret"
    )
    assert message == web_app.X_POST_SETTINGS_SAVE_ERROR_MESSAGE
    assert "private-request-detail" not in message + caplog.text
    assert all("value" not in update for update in updates)
