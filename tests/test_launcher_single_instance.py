"""Regression tests for restarting an already-running Clip Extractor UI."""

from types import SimpleNamespace
import urllib.request
from pathlib import Path

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


def test_existing_instance_is_killed_before_restart(monkeypatch, capsys):
    class FakeProcess:
        pid = 1234

        def __init__(self):
            self.killed = False
            self.wait_timeout = None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            self.wait_timeout = timeout

    process = FakeProcess()
    monkeypatch.setattr(
        launcher,
        "_is_clip_extractor_page_available",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        launcher,
        "_find_listening_process",
        lambda _port=launcher.SERVER_PORT: process,
    )
    monkeypatch.setattr(
        launcher,
        "_is_owned_clip_extractor_process",
        lambda _process: True,
    )
    monkeypatch.setattr(
        launcher,
        "_wait_for_port_release",
        lambda **_kwargs: True,
    )

    assert launcher.stop_existing_instance_if_running() is True
    assert process.killed is True
    assert process.wait_timeout == launcher.PROCESS_EXIT_TIMEOUT
    assert "終了して再起動します" in capsys.readouterr().out


def test_matching_page_cannot_authorize_killing_an_unknown_process(
    monkeypatch,
    capsys,
):
    process = SimpleNamespace(pid=4321)
    monkeypatch.setattr(
        launcher,
        "_is_clip_extractor_page_available",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        launcher,
        "_find_listening_process",
        lambda _port=launcher.SERVER_PORT: process,
    )
    monkeypatch.setattr(
        launcher,
        "_is_owned_clip_extractor_process",
        lambda _process: False,
    )

    assert launcher.stop_existing_instance_if_running() is False
    assert "安全確認" in capsys.readouterr().out


def test_unrelated_or_unready_page_is_never_killed(monkeypatch):
    monkeypatch.setattr(
        launcher,
        "_is_clip_extractor_page_available",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        launcher,
        "_find_listening_process",
        lambda _port=launcher.SERVER_PORT: pytest.fail(
            "an unrelated listener must not be inspected or killed"
        ),
    )

    assert launcher.stop_existing_instance_if_running() is True


def test_main_stops_before_obs_when_existing_instance_cannot_be_stopped(monkeypatch):
    monkeypatch.setattr(
        launcher,
        "stop_existing_instance_if_running",
        lambda: False,
    )
    monkeypatch.setattr(
        launcher,
        "launch_obs_if_requested",
        lambda *_args, **_kwargs: pytest.fail("OBS must not launch twice"),
    )

    assert launcher.main([]) == 1


def test_windows_browser_launch_requests_a_new_chrome_window(monkeypatch):
    browser = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    launched = []
    monkeypatch.setattr(
        launcher,
        "_find_windows_default_browser_executable",
        lambda: browser,
    )
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda args, **kwargs: launched.append((args, kwargs)),
    )

    assert launcher.open_app_page(platform="win32") is True
    assert launched == [
        (
            [str(browser), "--new-window", launcher.SERVER_URL],
            {"close_fds": True},
        )
    ]


def test_late_port_conflict_restarts_instance_started_at_the_same_time(monkeypatch):
    launch_count = 0

    def launch(**_kwargs):
        nonlocal launch_count
        launch_count += 1
        if launch_count == 1:
            raise OSError("Cannot find empty port in range: 7860-7860")

    app = SimpleNamespace(launch=launch)
    monkeypatch.setattr(
        launcher,
        "stop_existing_instance_if_running",
        lambda **_kwargs: True,
    )

    assert launcher._launch_with_port_reuse(app, {"server_port": 7860}) == 0
    assert launch_count == 2


def test_unrelated_launch_oserror_is_not_hidden(monkeypatch):
    app = SimpleNamespace(
        launch=lambda **_kwargs: (_ for _ in ()).throw(OSError("disk failure"))
    )
    monkeypatch.setattr(
        launcher,
        "stop_existing_instance_if_running",
        lambda **_kwargs: pytest.fail("not a bind error"),
    )

    with pytest.raises(OSError, match="disk failure"):
        launcher._launch_with_port_reuse(app, {"server_port": 7860})
