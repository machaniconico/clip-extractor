"""Contract tests for the dormant OpenAI-compatible local provider."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

import highlighter


@pytest.fixture
def fake_openai_sdk(monkeypatch):
    state = SimpleNamespace(
        client_kwargs=[],
        calls=[],
        outcomes=[],
        close_count=0,
    )

    class FakeCompletions:
        def create(self, **kwargs):
            state.calls.append(kwargs)
            outcome = state.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    class FakeClient:
        def __init__(self, **kwargs):
            state.client_kwargs.append(kwargs)
            self._http_client = kwargs.get("http_client")
            self.chat = SimpleNamespace(completions=FakeCompletions())

        def close(self):
            state.close_count += 1
            if self._http_client is not None:
                self._http_client.close()

    openai_module = ModuleType("openai")
    openai_module.OpenAI = FakeClient
    monkeypatch.setitem(sys.modules, "openai", openai_module)
    state.response = lambda text: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )
    return state


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_local_llm_feature_flag_truthy_values(value):
    assert highlighter.local_llm_enabled(
        {highlighter.LOCAL_LLM_ENABLE_ENV: value}
    ) is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "unexpected"])
def test_local_llm_feature_flag_is_disabled_by_default(value):
    environ = ({highlighter.LOCAL_LLM_ENABLE_ENV: value} if value else {})
    assert highlighter.local_llm_enabled(environ) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1234/v1",
        "http://localhost:1234/v1/",
        "http://[::1]:1234/v1",
    ],
)
def test_local_base_url_accepts_loopback_v1_only(url):
    assert highlighter.validate_local_llm_base_url(url).endswith("/v1")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1234/v1",
        "http://0.0.0.0:1234/v1",
        "http://127.0.0.2:1234/v1",
        "http://192.168.1.50:1234/v1",
        "http://example.com/v1",
        "http://localhost:1234/api/v1",
        "http://user:pass@localhost:1234/v1",
        "http://@localhost:1234/v1",
        "http://localhost:1234/v1?",
        "http://localhost:1234/v1#",
        "http://localhost:1234/v1?target=remote",
        "not-a-url",
    ],
)
def test_local_base_url_rejects_nonlocal_or_ambiguous_targets(url):
    with pytest.raises(ValueError, match="loopback|/v1|URL"):
        highlighter.validate_local_llm_base_url(url)


def test_call_local_uses_single_structured_request(fake_openai_sdk, monkeypatch):
    monkeypatch.setenv(highlighter.LOCAL_LLM_ENABLE_ENV, "1")
    monkeypatch.setenv(
        highlighter.LOCAL_LLM_BASE_URL_ENV,
        "http://127.0.0.1:1234/v1",
    )
    monkeypatch.setenv(highlighter.LOCAL_LLM_MODEL_ENV, "gemma-4-31b-it")
    response_text = (
        '{"highlights":[{"start":"00:00:01.000","end":"00:00:31.000",'
        '"title":"Local","reason":"Visual candidate"}]}'
    )
    fake_openai_sdk.outcomes = [fake_openai_sdk.response(response_text)]

    result = highlighter._call_local_openai_compatible("USER PROMPT")

    assert result == response_text
    assert len(fake_openai_sdk.client_kwargs) == 1
    client_kwargs = fake_openai_sdk.client_kwargs[0]
    assert client_kwargs["base_url"] == "http://127.0.0.1:1234/v1"
    assert client_kwargs["api_key"] == "lm-studio"
    assert client_kwargs["max_retries"] == 0
    assert client_kwargs["timeout"].connect == 3.0
    assert client_kwargs["timeout"].read == 600.0
    assert client_kwargs["timeout"].write == 30.0
    assert client_kwargs["timeout"].pool == 5.0
    assert client_kwargs["http_client"].trust_env is False
    assert client_kwargs["http_client"].follow_redirects is False
    assert fake_openai_sdk.close_count == 1
    assert len(fake_openai_sdk.calls) == 1
    call = fake_openai_sdk.calls[0]
    assert call["model"] == "gemma-4-31b-it"
    assert call["messages"] == [
        {"role": "system", "content": highlighter.GEMINI_SYSTEM_PROMPT},
        {"role": "user", "content": "USER PROMPT"},
    ]
    assert call["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "clip_extractor_highlights",
            "strict": True,
            "schema": highlighter.HIGHLIGHTS_JSON_SCHEMA,
        },
    }


def test_call_local_ignores_proxy_environment(fake_openai_sdk, monkeypatch):
    monkeypatch.setenv(highlighter.LOCAL_LLM_ENABLE_ENV, "1")
    monkeypatch.setenv(highlighter.LOCAL_LLM_MODEL_ENV, "local-model")
    monkeypatch.setenv("HTTP_PROXY", "http://external-proxy.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://external-proxy.example:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)
    fake_openai_sdk.outcomes = [fake_openai_sdk.response('{"highlights":[]}')]

    highlighter._call_local_openai_compatible("private transcript")

    http_client = fake_openai_sdk.client_kwargs[0]["http_client"]
    assert http_client.trust_env is False
    assert http_client.follow_redirects is False


def test_call_local_explicit_model_overrides_environment(fake_openai_sdk, monkeypatch):
    monkeypatch.setenv(highlighter.LOCAL_LLM_ENABLE_ENV, "1")
    monkeypatch.setenv(highlighter.LOCAL_LLM_MODEL_ENV, "environment-model")
    fake_openai_sdk.outcomes = [fake_openai_sdk.response('{"highlights":[]}')]

    highlighter._call_local_openai_compatible("prompt", model="ui-model")

    assert fake_openai_sdk.calls[0]["model"] == "ui-model"


def test_call_local_requires_feature_flag_before_importing_sdk(monkeypatch):
    monkeypatch.delenv(highlighter.LOCAL_LLM_ENABLE_ENV, raising=False)
    monkeypatch.delitem(sys.modules, "openai", raising=False)

    with pytest.raises(RuntimeError, match=highlighter.LOCAL_LLM_ENABLE_ENV):
        highlighter._call_local_openai_compatible("prompt", model="model")


def test_call_local_requires_an_explicit_or_environment_model(monkeypatch):
    monkeypatch.setenv(highlighter.LOCAL_LLM_ENABLE_ENV, "1")
    monkeypatch.delenv(highlighter.LOCAL_LLM_MODEL_ENV, raising=False)

    with pytest.raises(ValueError, match=highlighter.LOCAL_LLM_MODEL_ENV):
        highlighter._call_local_openai_compatible("prompt")


def test_call_local_rejects_remote_url_before_creating_client(
    fake_openai_sdk, monkeypatch
):
    monkeypatch.setenv(highlighter.LOCAL_LLM_ENABLE_ENV, "1")
    monkeypatch.setenv(highlighter.LOCAL_LLM_MODEL_ENV, "local-model")
    monkeypatch.setenv(
        highlighter.LOCAL_LLM_BASE_URL_ENV,
        "http://example.com/v1",
    )

    with pytest.raises(ValueError, match="loopback"):
        highlighter._call_local_openai_compatible("private transcript")

    assert fake_openai_sdk.client_kwargs == []
    assert fake_openai_sdk.calls == []


def test_call_local_does_not_retry_and_always_closes(fake_openai_sdk, monkeypatch):
    monkeypatch.setenv(highlighter.LOCAL_LLM_ENABLE_ENV, "1")
    error = RuntimeError("local inference failed")
    fake_openai_sdk.outcomes = [error, fake_openai_sdk.response('{"highlights":[]}')]

    with pytest.raises(RuntimeError, match="local inference failed"):
        highlighter._call_local_openai_compatible("prompt", model="local-model")

    assert len(fake_openai_sdk.calls) == 1
    assert fake_openai_sdk.close_count == 1


def test_detect_highlights_routes_local_without_cloud_fallback(monkeypatch):
    captured = {}

    def fake_local(user_prompt, model):
        captured.update(prompt=user_prompt, model=model)
        return (
            '{"highlights":[{"start":"00:00:00","end":"00:00:30",'
            '"title":"Local","reason":"offline"}]}'
        )

    monkeypatch.setattr(highlighter, "_call_local_openai_compatible", fake_local)
    monkeypatch.setattr(
        highlighter,
        "_call_claude",
        lambda *_args, **_kwargs: pytest.fail("local must not fall back to Claude"),
    )

    result = highlighter.detect_highlights(
        "transcript",
        ai_provider="local",
        api_key="must-not-be-forwarded",
        ai_model="local-model",
    )

    assert captured["model"] == "local-model"
    assert result[0]["title"] == "Local"


def test_unknown_provider_fails_closed(monkeypatch):
    monkeypatch.setattr(
        highlighter,
        "_call_claude",
        lambda *_args, **_kwargs: pytest.fail("unknown provider must not use Claude"),
    )

    with pytest.raises(ValueError, match="Unsupported AI provider"):
        highlighter.detect_highlights("transcript", ai_provider="typo-provider")
