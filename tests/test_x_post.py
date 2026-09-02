import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import x_post


def test_parse_destination_links_accepts_named_and_plain_urls():
    links = x_post.parse_destination_links(
        "Twitch|https://www.twitch.tv/example\nhttps://kick.com/example\n"
        "# comment\nnot-a-url"
    )

    assert links == (
        x_post.DestinationLink("Twitch", "https://www.twitch.tv/example"),
        x_post.DestinationLink("kick.com", "https://kick.com/example"),
    )


def test_render_x_post_text_includes_dynamic_youtube_and_simulcast_links():
    rendered = x_post.render_x_post_text(
        "配信開始！\n{links}\n{title}",
        youtube_url="https://www.youtube.com/watch?v=abc123",
        title="夜のゲーム配信",
        destinations="Twitch|https://twitch.tv/example",
    )

    assert rendered == (
        "配信開始！\n"
        "YouTube: https://www.youtube.com/watch?v=abc123\n"
        "Twitch: https://twitch.tv/example\n夜のゲーム配信"
    )


def test_render_x_post_text_keeps_body_usable_when_lookup_is_unavailable():
    rendered = x_post.render_x_post_text(
        x_post.DEFAULT_X_POST_TEMPLATE,
        destinations="Twitch|https://twitch.tv/example",
    )

    assert rendered == "配信開始しました！\nTwitch: https://twitch.tv/example"


def test_build_x_post_intent_url_encodes_a_fresh_post_body():
    intent_url = x_post.build_x_post_intent_url("配信開始！\nhttps://example.com/a?x=1")

    parsed = urlparse(intent_url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == x_post.X_POST_INTENT_URL
    assert parse_qs(parsed.query)["text"] == ["配信開始！\nhttps://example.com/a?x=1"]


def test_open_x_post_composer_uses_new_intent_url_without_publishing():
    opened = []

    def fake_open(url, *, new, autoraise):
        opened.append((url, new, autoraise))
        return True

    returned = x_post.open_x_post_composer("配信開始！", opener=fake_open)

    assert returned == opened[0][0]
    assert opened[0][1:] == (2, True)


def test_post_x_post_uses_oauth1_user_context_and_returns_status_url():
    created_with = []
    posted = []

    class FakeResponse:
        status_code = 201

        @staticmethod
        def json():
            return {"data": {"id": "1234567890123456789", "text": "配信開始！"}}

    class FakeSession:
        def post(self, url, *, json, timeout):
            posted.append((url, json, timeout))
            return FakeResponse()

        def close(self):
            pass

    def fake_session_factory(**kwargs):
        created_with.append(kwargs)
        return FakeSession()

    credentials = x_post.XCredentials(
        api_key="api-key",
        api_key_secret="api-key-secret",
        access_token="access-token",
        access_token_secret="access-token-secret",
    )

    result = x_post.post_x_post(
        "配信開始！",
        credentials,
        session_factory=fake_session_factory,
    )

    assert created_with == [{
        "client_key": "api-key",
        "client_secret": "api-key-secret",
        "resource_owner_key": "access-token",
        "resource_owner_secret": "access-token-secret",
    }]
    assert posted == [(
        x_post.X_API_POST_URL,
        {"text": "配信開始！"},
        x_post.X_API_TIMEOUT,
    )]
    assert result.post_id == "1234567890123456789"
    assert result.status_url == (
        "https://x.com/i/web/status/1234567890123456789"
    )


@pytest.mark.parametrize("status_code", [400, 401, 403, 429, 500])
def test_post_x_post_http_errors_never_expose_response_or_credentials(status_code):
    secret = "must-not-leak"

    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self.text = f"server echoed {secret}"

        def json(self):
            return {"detail": self.text}

    class FakeSession:
        def post(self, *_args, **_kwargs):
            return FakeResponse()

        def close(self):
            pass

    credentials = x_post.XCredentials(secret, secret, secret, secret)

    with pytest.raises(x_post.XPostError) as raised:
        x_post.post_x_post(
            "配信開始！",
            credentials,
            session_factory=lambda **_kwargs: FakeSession(),
        )

    message = str(raised.value)
    assert f"HTTP {status_code}" in message
    assert secret not in message


def test_post_x_post_network_error_is_sanitized():
    secret = "network-secret"

    class FakeSession:
        def post(self, *_args, **_kwargs):
            raise RuntimeError(secret)

        def close(self):
            pass

    credentials = x_post.XCredentials(secret, secret, secret, secret)

    with pytest.raises(x_post.XPostError) as raised:
        x_post.post_x_post(
            "配信開始！",
            credentials,
            session_factory=lambda **_kwargs: FakeSession(),
        )

    assert str(raised.value) == "X APIへの接続に失敗しました"
    assert secret not in str(raised.value)
