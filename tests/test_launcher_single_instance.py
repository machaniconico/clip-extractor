"""Regression tests for reusing an already-running Clip Extractor UI."""

from types import SimpleNamespace
import urllib.request

import pytest

import launcher


class _FakeHttpResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"<title>Clip Extractor</title>", True),
        (b"<title>Another local app</title>", False),
    ],
)
def test_page_probe_only_accepts_clip_extractor(monkeypatch, body, expected):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _FakeHttpResponse(body),
    )

    assert launcher._is_clip_extractor_page_available() is expected


def test_existing_instance_opens_its_browser_page(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(
        launcher,
        "_is_clip_extractor_page_available",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

    assert launcher.open_existing_instance_if_running() is True
    assert opened == [launcher.SERVER_URL]
    assert "既に起動しています" in capsys.readouterr().out


def test_unrelated_or_unready_page_is_not_reused(monkeypatch):
    opened = []
    monkeypatch.setattr(
        launcher,
        "_is_clip_extractor_page_available",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)

    assert launcher.open_existing_instance_if_running() is False
    assert opened == []


def test_main_stops_before_obs_and_ui_when_instance_exists(monkeypatch):
    monkeypatch.setattr(
        launcher,
        "open_existing_instance_if_running",
        lambda: True,
    )
    monkeypatch.setattr(
        launcher,
        "launch_obs_if_requested",
        lambda *_args, **_kwargs: pytest.fail("OBS must not launch twice"),
    )

    assert launcher.main([]) == 0


def test_late_port_conflict_reuses_instance_started_at_the_same_time(monkeypatch):
    app = SimpleNamespace(
        launch=lambda **_kwargs: (_ for _ in ()).throw(
            OSError("Cannot find empty port in range: 7860-7860")
        )
    )
    monkeypatch.setattr(
        launcher,
        "open_existing_instance_if_running",
        lambda **_kwargs: True,
    )

    assert launcher._launch_with_port_reuse(app, {"server_port": 7860}) == 0


def test_unrelated_launch_oserror_is_not_hidden(monkeypatch):
    app = SimpleNamespace(
        launch=lambda **_kwargs: (_ for _ in ()).throw(OSError("disk failure"))
    )
    monkeypatch.setattr(
        launcher,
        "open_existing_instance_if_running",
        lambda **_kwargs: pytest.fail("not a bind error"),
    )

    with pytest.raises(OSError, match="disk failure"):
        launcher._launch_with_port_reuse(app, {"server_port": 7860})
