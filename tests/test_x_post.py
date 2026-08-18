import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
