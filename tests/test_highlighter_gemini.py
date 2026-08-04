"""Contract tests for the supported Google Gen AI SDK integration."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

import highlighter


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for name, value in kwargs.items():
            setattr(self, name, value)


class _FakeHttpOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for name, value in kwargs.items():
            setattr(self, name, value)


class _FakeHttpRetryOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for name, value in kwargs.items():
            setattr(self, name, value)


class _FakeAPIError(Exception):
    def __init__(self, code: int, status: str, message: str):
        super().__init__(f"{code} {status}: {message}")
        self.code = code
        self.status = status
        self.message = message
        self.details = None
        self.response = None


class _FakeClientError(_FakeAPIError):
    pass


class _FakeServerError(_FakeAPIError):
    pass


@pytest.fixture
def fake_genai_sdk(monkeypatch):
    """Install a deterministic in-memory stand-in for ``google-genai``."""

    state = SimpleNamespace(
        client_api_keys=[],
        client_http_options=[],
        calls=[],
        outcomes=[],
        close_count=0,
    )

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            state.calls.append(
                {"model": model, "contents": contents, "config": config}
            )
            outcome = state.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    class FakeClient:
        def __init__(self, *, api_key, http_options):
            state.client_api_keys.append(api_key)
            state.client_http_options.append(http_options)
            self.models = FakeModels()

        def close(self):
            state.close_count += 1

    genai_module = ModuleType("google.genai")
    types_module = ModuleType("google.genai.types")
    errors_module = ModuleType("google.genai.errors")
    legacy_module = ModuleType("google.generativeai")

    genai_module.Client = FakeClient
    genai_module.types = types_module
    genai_module.errors = errors_module
    types_module.GenerateContentConfig = _FakeGenerateContentConfig
    types_module.HttpOptions = _FakeHttpOptions
    types_module.HttpRetryOptions = _FakeHttpRetryOptions
    errors_module.APIError = _FakeAPIError
    errors_module.ClientError = _FakeClientError
    errors_module.ServerError = _FakeServerError

    def fail_if_legacy_sdk_is_used(*_args, **_kwargs):
        raise AssertionError("highlighter must use google-genai, not google-generativeai")

    legacy_module.configure = fail_if_legacy_sdk_is_used
    legacy_module.GenerativeModel = fail_if_legacy_sdk_is_used

    google_module = sys.modules.get("google")
    if google_module is None:
        google_module = ModuleType("google")
        google_module.__path__ = []

    monkeypatch.setattr(google_module, "genai", genai_module, raising=False)
    monkeypatch.setattr(google_module, "generativeai", legacy_module, raising=False)
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_module)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_module)
    monkeypatch.setitem(sys.modules, "google.generativeai", legacy_module)

    state.response = lambda text: SimpleNamespace(text=text)
    state.client_error = _FakeClientError
    state.server_error = _FakeServerError
    return state


def _config_values(config) -> dict:
    if isinstance(config, dict):
        return config
    return config.kwargs


def test_call_gemini_uses_google_genai_with_structured_json_config(fake_genai_sdk):
    response_text = (
        '{"highlights":[{"start":"00:00:01.000","end":"00:00:31.000",'
        '"title":"A","reason":"B"}]}'
    )
    fake_genai_sdk.outcomes = [fake_genai_sdk.response(response_text)]

    result = highlighter._call_gemini("USER PROMPT", "secret-key")

    assert result == response_text
    assert fake_genai_sdk.client_api_keys == ["secret-key"]
    assert fake_genai_sdk.client_http_options[0].timeout == 300_000
    assert fake_genai_sdk.client_http_options[0].retry_options.attempts == 1
    assert fake_genai_sdk.close_count == 1
    assert len(fake_genai_sdk.calls) == 1
    call = fake_genai_sdk.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    assert call["contents"] == "USER PROMPT"

    config = _config_values(call["config"])
    assert config["system_instruction"] == highlighter.GEMINI_SYSTEM_PROMPT
    assert '"highlights"' not in config["system_instruction"]
    assert config["response_mime_type"] == "application/json"
    assert "response_schema" not in config
    assert "response_json_schema" in config
    assert not ({"temperature", "top_p", "top_k"} & config.keys())

    schema = config["response_json_schema"]
    assert schema["type"] == "object"
    assert "highlights" in schema["required"]
    highlights = schema["properties"]["highlights"]
    assert highlights["type"] == "array"
    item = highlights["items"]
    assert item["type"] == "object"
    assert {"start", "end", "title", "reason"} <= set(item["required"])

    # Structured output remains compatible with the defensive downstream parser.
    assert highlighter._extract_json_object(result) == {
        "highlights": [
            {
                "start": "00:00:01.000",
                "end": "00:00:31.000",
                "title": "A",
                "reason": "B",
            }
        ]
    }


def test_call_gemini_does_not_retry_schema_capability_errors(fake_genai_sdk):
    capability_error = fake_genai_sdk.client_error(
        400,
        "INVALID_ARGUMENT",
        "response_json_schema is not supported by this model",
    )
    fake_genai_sdk.outcomes = [
        capability_error,
        fake_genai_sdk.response('{"highlights": []}'),
    ]

    with pytest.raises(type(capability_error)) as caught:
        highlighter._call_gemini("prompt", "key", "gemini-3.6-flash")

    assert caught.value is capability_error
    assert len(fake_genai_sdk.calls) == 1


@pytest.mark.parametrize(
    ("code", "status", "message", "error_kind"),
    [
        (403, "PERMISSION_DENIED", "API key lacks permission", "client"),
        (404, "NOT_FOUND", "requested model was not found", "client"),
        (429, "RESOURCE_EXHAUSTED", "quota exceeded", "client"),
        (500, "INTERNAL", "backend failed", "server"),
        (400, "INVALID_ARGUMENT", "contents must not be empty", "client"),
    ],
)
def test_call_gemini_does_not_retry_non_schema_errors(
    fake_genai_sdk, code, status, message, error_kind
):
    error_type = (
        fake_genai_sdk.client_error
        if error_kind == "client"
        else fake_genai_sdk.server_error
    )
    error = error_type(code, status, message)
    fake_genai_sdk.outcomes = [
        error,
        fake_genai_sdk.response('{"highlights": []}'),
    ]

    with pytest.raises(type(error)) as caught:
        highlighter._call_gemini("prompt", "key", "gemini-3.6-flash")

    assert caught.value is error
    assert len(fake_genai_sdk.calls) == 1


def test_detect_highlights_uses_35_flash_lite_when_gemini_model_is_blank(monkeypatch):
    captured = {}

    def fake_call(user_prompt, api_key, model):
        captured.update(prompt=user_prompt, api_key=api_key, model=model)
        return (
            '{"highlights":[{"start":"00:00:00","end":"00:00:30",'
            '"title":"Default model","reason":"contract"}]}'
        )

    monkeypatch.setattr(highlighter, "_call_gemini", fake_call)

    result = highlighter.detect_highlights(
        "transcript", ai_provider="gemini", api_key="secret", ai_model=""
    )

    assert captured["model"] == "gemini-3.5-flash-lite"
    assert captured["api_key"] == "secret"
    assert result[0]["title"] == "Default model"
