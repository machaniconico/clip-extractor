"""Regression tests for the yt-dlp download configuration."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import downloader
from downloader import download_post_live_video, download_video


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


def test_post_live_download_enables_incomplete_fragments(
    monkeypatch,
    tmp_path,
):
    downloaded = tmp_path / "post-live.mp4"
    downloaded.write_bytes(b"video")
    captured_options = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured_options.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download):
            assert download is True
            return {"id": "post-live"}

        def prepare_filename(self, _info):
            return str(downloaded)

    monkeypatch.setitem(
        sys.modules,
        "yt_dlp",
        SimpleNamespace(YoutubeDL=FakeYoutubeDL),
    )
    monkeypatch.setattr(downloader, "_probe_duration", lambda _path: 98.0)

    result = download_post_live_video(
        "https://www.youtube.com/watch?v=test",
        tmp_path,
        expected_duration_seconds=100,
    )

    assert result == downloaded
    assert captured_options["extractor_args"] == {
        "youtube": {"formats": ["incomplete"]}
    }
    assert captured_options["fragment_retries"] == 20


def test_post_live_download_rejects_incomplete_duration(monkeypatch, tmp_path):
    downloaded = tmp_path / "short.mp4"
    downloaded.write_bytes(b"video")
    monkeypatch.setattr(
        downloader,
        "_download_video",
        lambda *_args, **_kwargs: downloaded,
    )
    monkeypatch.setattr(downloader, "_probe_duration", lambda _path: 80.0)

    with pytest.raises(RuntimeError, match="取得尺が不足"):
        download_post_live_video(
            "https://www.youtube.com/watch?v=test",
            tmp_path,
            expected_duration_seconds=100,
        )
