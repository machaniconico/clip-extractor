"""X weighted-length (twitter-text v3) limits for announcement posts."""

import pytest

import web_app
import x_post
from x_post import XPostTooLongError, x_weighted_length

URL1 = "https://www.youtube.com/watch?v=abcdefghijk"
URL2 = "https://www.twitch.tv/some_channel_with_a_long_name"
URL3 = "https://live.nicovideo.jp/watch/lv123456789012345"
DEST = f"{URL2}\n{URL3}"


def test_ascii_counts_one_each_and_japanese_two():
    assert x_weighted_length("a" * 280) == 280
    assert x_weighted_length("あ" * 140) == 280
    assert x_weighted_length("あ" * 141) == 282


def test_urls_count_23_regardless_of_length():
    assert x_weighted_length(URL1) == 23
    assert x_weighted_length(f"{URL1} {URL2}") == 23 + 1 + 23


def test_emoji_sequences_count_two():
    assert x_weighted_length("😀") == 2
    assert x_weighted_length("👨‍👩‍👧") == 2
    assert x_weighted_length("👍\U0001F3FD") == 2
    assert x_weighted_length("🇯🇵") == 2
    assert x_weighted_length("é…") == 3  # U+2026 is outside the weight-100 ranges


def test_ascii_boundary_280_ok_and_281_shortened():
    base = x_weighted_length("\n配信開始しました！\nYouTube: " + URL1)
    ok_title = "a" * (280 - base)
    text = x_post.render_x_post_text(
        "{title}\n配信開始しました！\n{links}", youtube_url=URL1, title=ok_title
    )
    assert x_weighted_length(text) == 280
    assert ok_title in text and "…" not in text
    over = x_post.render_x_post_text(
        "{title}\n配信開始しました！\n{links}", youtube_url=URL1, title=ok_title + "a"
    )
    assert x_weighted_length(over) <= 280
    assert "…" in over and URL1 in over


@pytest.mark.parametrize("mode", ["combined", "separate"])
def test_long_japanese_title_truncated_and_urls_kept(mode):
    texts = x_post.build_x_post_texts(
        x_post.DEFAULT_X_POST_TEMPLATE,
        youtube_url=URL1,
        title="あ" * 130,
        destinations=DEST,
        mode=mode,
    )
    assert texts
    for text in texts:
        assert x_weighted_length(text) <= 280
        assert "…" in text and "配信開始しました！" in text
    joined = "\n".join(texts)
    for url in (URL1, URL2, URL3):
        assert url in joined


def test_template_only_overflow_raises_japanese_error():
    with pytest.raises(XPostTooLongError, match="280"):
        x_post.render_x_post_text("あ" * 150 + "{title}", title="t")
    with pytest.raises(XPostTooLongError):
        x_post.build_x_post_texts(
            "あ" * 150 + "{links}", destinations=DEST, mode="separate"
        )


def test_preview_shows_count_and_error():
    preview = web_app.preview_x_post_text("開始 {links}", DEST)
    assert f"/{x_post.X_MAX_WEIGHTED_LENGTH}" in preview and "文字数" in preview
    separate = web_app.preview_x_post_text("開始 {links}", DEST, "separate")
    assert "1/2（" in separate and "/280）" in separate
    error = web_app.preview_x_post_text("あ" * 150 + "{links}", DEST)
    assert "280" in error and "投稿しません" in error


def test_over_limit_stream_start_never_posts_or_opens_composer(monkeypatch):
    statuses = []
    monkeypatch.setattr(web_app, "post_x_post", lambda *_a: pytest.fail("posted"))
    monkeypatch.setattr(web_app, "open_x_post_composer", lambda _t: pytest.fail("opened"))
    monkeypatch.setattr(web_app, "_obs_append_status", statuses.append)
    web_app._apply_obs_x_post_runtime_settings(True, True)
    started, _ = web_app._obs_make_x_post_callbacks(
        {"obs_x_post_on_stream_start": True, "obs_x_post_auto": True,
         "obs_x_post_mode": "combined", "obs_x_post_template": "あ" * 150 + "{links}",
         "obs_x_post_destinations": DEST},
        use_runtime_settings=True,
        credentials=web_app.XCredentials("a", "b", "c", "d"),
    )
    started()
    web_app._join_obs_workers(timeout=5)
    assert any("投稿を中止" in s and "280" in s for s in statuses)
