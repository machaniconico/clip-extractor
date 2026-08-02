"""Regression tests for the yt-dlp download configuration."""

import sys
from pathlib import Path
from types import SimpleNamespace

from downloader import download_video, get_url_source, is_twitch_url, is_youtube_url


def test_supported_url_detection_includes_twitch_vods_and_channels():
    assert is_youtube_url("https://www.youtube.com/watch?v=test")
    assert is_twitch_url("https://www.twitch.tv/videos/123456789")
    assert is_twitch_url("https://twitch.tv/example_channel")
    assert not is_twitch_url("https://clips.twitch.tv/ExampleClip")
    assert get_url_source("https://www.twitch.tv/videos/123456789") == "twitch"
    assert get_url_source("C:/videos/stream.mp4") is None


def test_download_video_enables_node_javascript_runtime(monkeypatch, tmp_path):
    downloaded = tmp_path / "downloaded.mp4"
    downloaded.write_bytes(b"video")
    captured_options = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured_options.update(options)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def extract_info(self, url, download):
            assert url == "https://www.youtube.com/watch?v=test"
            assert download is True
            return {"id": "test"}

        def prepare_filename(self, info):
            assert info == {"id": "test"}
            return str(downloaded)

    monkeypatch.setitem(
        sys.modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )

    result = download_video(
        "https://www.youtube.com/watch?v=test",
        tmp_path,
    )

    assert result == downloaded
    assert captured_options["js_runtimes"] == {
        "deno": {"path": None},
        "node": {"path": None},
    }


def test_requirements_install_ytdlp_default_dependencies():
    lines = {
        line.strip()
        for line in (Path(__file__).parent.parent / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "yt-dlp[default]>=2026.7.4" in lines
