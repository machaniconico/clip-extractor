"""Tests for onboarding UX, setup banner, safe API key save/delete, and connection tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import highlighter
import web_app
import x_post
from x_post import XCredentials


ROOT = Path(__file__).resolve().parents[1]


def test_save_api_key_empty_does_not_delete_existing_key(monkeypatch, tmp_path):
    key_file = tmp_path / ".gemini_key"
    key_file.write_text("existing-secret-key", encoding="utf-8")
    monkeypatch.setattr(web_app, "GEMINI_KEY_FILE", key_file)

    update = web_app.save_gemini_api_key("   ")

    assert key_file.exists()
    assert key_file.read_text(encoding="utf-8") == "existing-secret-key"
    assert update["value"] == ""
    assert "保存済み" in update["placeholder"]


def test_save_api_key_strips_quotes_and_explicit_delete_removes_file(monkeypatch, tmp_path):
    key_file = tmp_path / ".gemini_key"
    monkeypatch.setattr(web_app, "GEMINI_KEY_FILE", key_file)
    monkeypatch.setattr(web_app.secret_store, "is_encryption_available", lambda: False)

    web_app.save_gemini_api_key('  "my-clean-key"  ')
    assert key_file.read_text(encoding="utf-8") == "my-clean-key"

    update = web_app.delete_saved_api_key()
    assert not key_file.exists()
    assert update["value"] == ""
    assert "APIキーを入力" in update["placeholder"]


def test_setup_banner_reflects_missing_and_ready_state_without_leaking_key(monkeypatch, tmp_path):
    key_file = tmp_path / ".gemini_key"
    monkeypatch.setattr(web_app, "GEMINI_KEY_FILE", key_file)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(web_app.shutil, "which", lambda cmd: "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None)

    pending_html = web_app._build_setup_banner_html("gemini")
    assert "setup-banner--pending" in pending_html
    assert "未設定" in pending_html

    sentinel = "secret-key-must-not-appear-in-banner"
    key_file.write_text(sentinel, encoding="utf-8")
    ready_html = web_app._build_setup_banner_html("gemini")
    assert "setup-banner--ready" in ready_html
    assert "初期設定完了" in ready_html
    assert sentinel not in ready_html


def test_detect_phase_fails_fast_when_gemini_key_missing(monkeypatch, tmp_path):
    key_file = tmp_path / ".gemini_key"
    monkeypatch.setattr(web_app, "GEMINI_KEY_FILE", key_file)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        web_app,
        "download_video",
        lambda *_a, **_k: pytest.fail("Must fail before downloading when API key is missing"),
    )
    monkeypatch.setattr(
        web_app,
        "transcribe",
        lambda *_a, **_k: pytest.fail("Must fail before transcribing when API key is missing"),
    )

    session, status, panel = web_app.detect_phase(
        input_url="https://youtube.com/watch?v=abcdefghijk",
        input_file=None,
        enable_clips=True,
        clip_prompt="",
        enable_chapters=False,
        chapter_prompt="",
        num_clips=3,
        ai_provider="gemini",
        ai_model="gemini-3.5-flash-lite",
        api_key="",
        min_duration=30,
        max_duration=60,
        whisper_model="tiny",
        language="ja",
        audio_fusion=False,
        audio_alpha=0.35,
        output_base_dir=str(tmp_path),
        progress=lambda *_a, **_k: None,
    )

    assert session == {}
    assert "Gemini APIキーが未設定" in status
    assert "aistudio.google.com/apikey" in status
    assert panel["visible"] is False


def test_ai_connection_test_handles_empty_keys_and_missing_claude(monkeypatch):
    ok, msg = highlighter.test_ai_connection("gemini", "")
    assert ok is False
    assert "未設定" in msg

    ok, msg = highlighter.test_ai_connection("openai", "")
    assert ok is False
    assert "未設定" in msg

    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    ok, msg = highlighter.test_ai_connection("claude", "")
    assert ok is False
    assert "Claude CLI" in msg
    assert "gemini" in msg


def test_x_credentials_test_verifies_without_posting_or_leaking_secrets():
    called = {}

    class FakeSession:
        def __init__(self, **kwargs):
            called["init"] = kwargs

        def get(self, url, timeout):
            called["get_url"] = url
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"data": {"username": "streamer_test"}},
            )

        def post(self, *_args, **_kwargs):
            pytest.fail("Connection test must never POST a tweet")

        def close(self):
            called["closed"] = True

    creds = XCredentials("secret-k", "secret-ks", "secret-t", "secret-ts")
    ok, msg = x_post.test_x_credentials(creds, session_factory=FakeSession)
    assert ok is True
    assert "@streamer_test" in msg
    assert called["get_url"] == x_post.X_API_USERS_ME_URL
    assert called["closed"] is True
    for secret in ("secret-k", "secret-ks", "secret-t", "secret-ts"):
        assert secret not in msg


def test_x_credentials_test_masks_401_and_403_errors():
    def _make_factory(code):
        class FakeSession:
            def __init__(self, **_kwargs):
                pass

            def get(self, _url, timeout):
                return SimpleNamespace(status_code=code, text="private-raw-body")

            def close(self):
                pass

        return FakeSession

    creds = XCredentials("k", "ks", "t", "ts")
    ok_401, msg_401 = x_post.test_x_credentials(creds, session_factory=_make_factory(401))
    assert ok_401 is False
    assert "401" in msg_401
    assert "private-raw-body" not in msg_401

    ok_403, msg_403 = x_post.test_x_credentials(creds, session_factory=_make_factory(403))
    assert ok_403 is False
    assert "403" in msg_403
    assert "Read and write" in msg_403
    assert "private-raw-body" not in msg_403


def test_setup_guide_html_contains_all_parts_and_interactive_checklist():
    html_text = (ROOT / "SETUP_GUIDE.html").read_text(encoding="utf-8")
    for marker in (
        "Part 0：アプリの導入",
        "Part 1：Gemini APIキーをもらう",
        "Part 2：credentials.json をもらう",
        "Part 3：OBS 配信終了で自動切り抜き",
        "Part 4：配信開始時に X へ自動告知",
        "data-copy=",
        "goalYoutube",
        "接続テスト",
        "fig-oauth-audience",
        "fig-oauth-client-json",
        "fig-obs-setup",
        "fig-x-developer",
    ):
        assert marker in html_text

    for rel_img in (
        "assets/guide/onboarding_flow.jpg",
        "assets/guide/two_keys_visual.jpg",
        "assets/guide/obs_x_automation.jpg",
    ):
        assert rel_img in html_text
        img_path = ROOT / rel_img
        assert img_path.is_file() and img_path.stat().st_size > 10_000


def test_model_choices_order_and_paid_cost_warnings(monkeypatch, tmp_path):
    assert highlighter.GEMINI_DEFAULT_MODEL == "gemini-4-hydrogen"
    assert highlighter.GEMINI_MODEL_CHOICES[:4] == (
        "gemini-4-hydrogen",
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
    )

    level, msg = highlighter.classify_ai_cost_risk("openai", "gpt-4.1")
    assert level == "paid_only"
    assert "無料枠がありません" in msg

    level, msg = highlighter.classify_ai_cost_risk("gemini", "gemini-4-carbon")
    assert level == "paid_only"
    assert "有料専用" in msg

    level, msg = highlighter.classify_ai_cost_risk("gemini", "gemini-custom-99")
    assert level == "unknown_custom"
    assert "確認済み一覧に含まれていません" in msg

    level, msg = highlighter.classify_ai_cost_risk("gemini", "gemini-4-hydrogen")
    assert level == "billing_caveat"
    assert "無料枠あり" in msg
    assert "gemini-3.5-flash-lite" in msg

    level, msg = highlighter.classify_ai_cost_risk("gemini", "gemini-3.5-flash-lite")
    assert level == "free_low_cost"
    assert "最安" in msg

    warn_html = web_app._ai_model_cost_warning_html("gemini", "gemini-4-argon")
    assert "cost-warning--danger" in warn_html
    assert "有料課金の警告" in warn_html

    key_file = tmp_path / ".gemini_key"
    key_file.write_text("saved-key", encoding="utf-8")
    monkeypatch.setattr(web_app, "GEMINI_KEY_FILE", key_file)
    monkeypatch.setattr(web_app.shutil, "which", lambda cmd: "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None)

    banner_paid = web_app._build_setup_banner_html("openai", "gpt-4.1")
    assert "有料課金の可能性" in banner_paid
    assert "saved-key" not in banner_paid


def test_gemini_free_tier_auto_fallback_steps_down_on_quota_error(monkeypatch):
    tried = []

    def fake_call_gemini(user_prompt, api_key, model):
        tried.append(model)
        if model in {"gemini-4-hydrogen", "gemini-3.8-flash"}:
            raise highlighter.GeminiQuotaError(per_minute=False)
        return (
            '{"highlights":[{"start":"00:00:00","end":"00:00:35",'
            '"title":"Fallback OK","reason":"quota"}]}'
        )

    monkeypatch.setattr(highlighter, "_call_gemini", fake_call_gemini)

    result = highlighter.detect_highlights(
        "transcript",
        ai_provider="gemini",
        api_key="secret",
        ai_model="gemini-4-hydrogen",
    )

    assert tried == [
        "gemini-4-hydrogen",
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
    ]
    assert result[0]["title"] == "Fallback OK"


def test_gemini_paid_or_custom_model_does_not_silently_fallback(monkeypatch):
    tried = []

    def fake_call_gemini(user_prompt, api_key, model):
        tried.append(model)
        raise highlighter.GeminiQuotaError(per_minute=False)

    monkeypatch.setattr(highlighter, "_call_gemini", fake_call_gemini)

    with pytest.raises(highlighter.GeminiQuotaError):
        highlighter.detect_highlights(
            "transcript",
            ai_provider="gemini",
            api_key="secret",
            ai_model="gemini-4-carbon",
        )

    assert tried == ["gemini-4-carbon"]
