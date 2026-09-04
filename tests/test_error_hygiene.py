"""Credential-bearing exceptions stay in local logs, not UI status text."""

import logging

import web_app


def _reset_x_state():
    web_app._join_obs_workers(timeout=5)
    web_app._reset_obs_x_post_stream_guard()
    with web_app._obs_status_lock:
        web_app._obs_status_lines.clear()


def test_x_composer_error_is_masked_in_ui_and_logged(monkeypatch, caplog):
    secret = "composer-url-with-rendered-secret"
    _reset_x_state()
    monkeypatch.setattr(
        web_app.youtube_api,
        "check_auth_status",
        lambda: {"authenticated": False},
    )
    monkeypatch.setattr(
        web_app,
        "open_x_post_composer",
        lambda _text: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    started, _finished = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_on_stream_start": True,
            "obs_x_post_auto": False,
            "obs_x_post_template": "stream announcement",
            "obs_x_post_destinations": "",
        }
    )

    with caplog.at_level(logging.WARNING, logger="clip-extractor"):
        started()
        web_app._join_obs_workers(timeout=5)

    assert secret not in web_app._obs_status_text()
    assert secret in caplog.text
    _reset_x_state()


def test_x_youtube_auth_error_is_masked_in_ui_and_logged(monkeypatch, caplog):
    secret = "token-endpoint-response-secret"
    _reset_x_state()
    monkeypatch.setattr(
        web_app.youtube_api,
        "check_auth_status",
        lambda: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    monkeypatch.setattr(web_app, "open_x_post_composer", lambda _text: None)
    started, _finished = web_app._obs_make_x_post_callbacks(
        {
            "obs_x_post_on_stream_start": True,
            "obs_x_post_auto": False,
            "obs_x_post_template": "stream announcement",
            "obs_x_post_destinations": "",
        }
    )

    with caplog.at_level(logging.WARNING, logger="clip-extractor"):
        started()
        web_app._join_obs_workers(timeout=5)

    assert secret not in web_app._obs_status_text()
    assert secret in caplog.text
    _reset_x_state()


def test_obs_start_youtube_auth_error_is_masked_in_ui_and_logged(
    monkeypatch,
    caplog,
):
    secret = "refresh-response-with-token"
    monkeypatch.setattr(
        web_app.youtube_api,
        "check_auth_status",
        lambda: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    with caplog.at_level(logging.ERROR, logger="clip-extractor"):
        status = web_app.start_obs_watch(
            "websocket",
            "localhost",
            4455,
            "",
            False,
            "stream",
            "",
            True,
            False,
            5,
            "combined",
            False,
            "gemini",
            "large-v3",
            "",
        )

    assert secret not in status
    assert secret in caplog.text
