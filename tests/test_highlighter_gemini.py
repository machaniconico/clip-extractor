"""Contract tests for the supported Google Gen AI SDK integration."""

from __future__ import annotations

import sys
import json
from types import ModuleType, SimpleNamespace

import httpx
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


@pytest.fixture
def mock_genai_transport(monkeypatch):
    """Run the real google-genai client against an in-memory HTTP transport."""
    from google import genai
    from google.genai import types

    requests = []
    responses = []
    sleeps = []
    real_client = genai.Client

    def handler(request):
        requests.append(request)
        status, body = responses.pop(0)
        return httpx.Response(
            status,
            json=body,
            request=request,
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(handler)

    # Retries (and their backoff) run in highlighter._call_gemini, not the SDK.
    monkeypatch.setattr(highlighter.time, "sleep", sleeps.append)

    class TrackingClient:
        def __init__(self, client):
            self._client = client
            self.models = client.models
            self.close_count = 0

        def close(self):
            self.close_count += 1
            self._client.close()

    clients = []

    def make_client(*, api_key, http_options):
        http_client = httpx.Client(transport=transport)
        options = types.HttpOptions(
            timeout=http_options.timeout,
            retry_options=http_options.retry_options,
            httpx_client=http_client,
        )
        client = TrackingClient(real_client(api_key=api_key, http_options=options))
        clients.append(client)
        return client

    monkeypatch.setattr(genai, "Client", make_client)
    return SimpleNamespace(
        requests=requests,
        responses=responses,
        sleeps=sleeps,
        clients=clients,
    )


def _gemini_success(text='{"highlights":[]}'):
    return 200, {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
    }


def _gemini_error(code, status):
    return code, {"error": {"code": code, "status": status, "message": status}}


@pytest.mark.parametrize(
    "code,status",
    [
        (500, "INTERNAL"),
        (502, "BAD_GATEWAY"),
        (503, "UNAVAILABLE"),
        (504, "GATEWAY_TIMEOUT"),
    ],
)
def test_call_gemini_retries_transient_http_errors_with_real_sdk(
    mock_genai_transport, code, status
):
    mock_genai_transport.responses.extend([_gemini_error(code, status), _gemini_success()])

    result = highlighter._call_gemini("USER PROMPT", "secret-key", "gemini-test")

    assert result == '{"highlights":[]}'
    assert len(mock_genai_transport.requests) == 2
    assert 5.0 <= mock_genai_transport.sleeps[0] <= 6.0
    assert mock_genai_transport.clients[0].close_count == 1

    first = mock_genai_transport.requests[0]
    second = mock_genai_transport.requests[1]
    assert first.url == second.url
    assert "gemini-test" in str(first.url)
    assert first.content == second.content
    assert first.headers["x-goog-api-key"] == "secret-key"
    body = json.loads(first.content)
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "USER PROMPT"}]}
    ]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] == highlighter.HIGHLIGHTS_JSON_SCHEMA


def test_call_gemini_wraps_exhausted_transient_errors_after_four_attempts(
    mock_genai_transport,
):
    mock_genai_transport.responses.extend(
        [_gemini_error(503, "UNAVAILABLE")] * 4
    )

    with pytest.raises(highlighter.GeminiTemporaryError) as caught:
        highlighter._call_gemini("prompt", "key", "gemini-test")

    assert caught.value.status_code == 503
    assert "数分待ってから" in str(caught.value)
    assert len(mock_genai_transport.requests) == 4
    assert len(mock_genai_transport.sleeps) == 3
    assert 5.0 <= mock_genai_transport.sleeps[0] <= 6.0
    assert 10.0 <= mock_genai_transport.sleeps[1] <= 11.0
    assert 20.0 <= mock_genai_transport.sleeps[2] <= 21.0
    assert caught.value.__cause__.code == 503
    assert "UNAVAILABLE" not in str(caught.value)
    assert mock_genai_transport.clients[0].close_count == 1


@pytest.mark.parametrize(
    "code,status",
    [
        (400, "INVALID_ARGUMENT"),
        (403, "PERMISSION_DENIED"),
        (404, "NOT_FOUND"),
    ],
)
def test_call_gemini_does_not_retry_non_transient_http_errors(
    mock_genai_transport, code, status
):
    mock_genai_transport.responses.extend([_gemini_error(code, status), _gemini_success()])

    from google.genai import errors

    with pytest.raises(errors.APIError) as caught:
        highlighter._call_gemini("prompt", "key", "gemini-test")

    assert caught.value.code == code
    assert len(mock_genai_transport.requests) == 1
    assert mock_genai_transport.sleeps == []
    assert mock_genai_transport.clients[0].close_count == 1


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
    retry_options = fake_genai_sdk.client_http_options[0].retry_options
    # The SDK must not retry on its own: highlighter retries (and logs) each attempt.
    assert retry_options.attempts == 1
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
    assert {"start", "end", "title", "reason"} == set(item["required"])
    assert "se_cues" not in item["properties"]

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


def _quota_error(quota_id, retry_delay=None, message="Quota exceeded"):
    details = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": quota_id, "quotaValue": "250000"}],
        }
    ]
    if retry_delay is not None:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": retry_delay,
            }
        )
    return 429, {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": message,
            "details": details,
        }
    }


PER_MINUTE_QUOTA = "GenerateContentInputTokensPerModelPerMinute-FreeTier"


def test_call_gemini_waits_and_retries_per_minute_quota(
    mock_genai_transport, monkeypatch
):
    slept = []
    monkeypatch.setattr(highlighter.time, "sleep", slept.append)
    mock_genai_transport.responses.extend(
        [_quota_error(PER_MINUTE_QUOTA, "52s"), _gemini_success()]
    )

    result = highlighter._call_gemini("prompt", "key", "gemini-test")

    assert result == '{"highlights":[]}'
    assert slept == [54.0]
    assert len(mock_genai_transport.requests) == 2


def test_call_gemini_reads_retry_delay_from_message(mock_genai_transport, monkeypatch):
    slept = []
    monkeypatch.setattr(highlighter.time, "sleep", slept.append)
    mock_genai_transport.responses.extend(
        [
            _quota_error(PER_MINUTE_QUOTA, None, "Please retry in 52.81s."),
            _gemini_success(),
        ]
    )

    highlighter._call_gemini("prompt", "key", "gemini-test")

    assert slept == [pytest.approx(54.81)]


def test_call_gemini_quota_exhausted_after_two_extra_attempts(
    mock_genai_transport, monkeypatch
):
    slept = []
    monkeypatch.setattr(highlighter.time, "sleep", slept.append)
    mock_genai_transport.responses.extend(
        [_quota_error(PER_MINUTE_QUOTA, "52s")] * 3
    )

    with pytest.raises(highlighter.GeminiQuotaError) as caught:
        highlighter._call_gemini("prompt", "secret-key", "gemini-test")

    assert len(mock_genai_transport.requests) == 3
    assert slept == [54.0, 54.0]
    message = str(caught.value)
    assert "利用枠" in message
    assert "1分あたり" in message
    assert "RESOURCE_EXHAUSTED" not in message
    assert "250000" not in message
    assert "secret-key" not in message
    assert isinstance(caught.value, highlighter.GeminiTemporaryError)
    assert mock_genai_transport.clients[0].close_count == 1


def test_call_gemini_daily_quota_fails_without_waiting(
    mock_genai_transport, monkeypatch
):
    slept = []
    monkeypatch.setattr(highlighter.time, "sleep", slept.append)
    mock_genai_transport.responses.extend(
        [
            _quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "52s"),
            _gemini_success(),
        ]
    )

    with pytest.raises(highlighter.GeminiQuotaError) as caught:
        highlighter._call_gemini("prompt", "key", "gemini-test")

    assert slept == []
    assert len(mock_genai_transport.requests) == 1
    assert "翌日" in str(caught.value)
    assert caught.value.per_minute is False


def test_call_gemini_long_per_minute_delay_fails_without_waiting(
    mock_genai_transport, monkeypatch
):
    slept = []
    monkeypatch.setattr(highlighter.time, "sleep", slept.append)
    mock_genai_transport.responses.append(_quota_error(PER_MINUTE_QUOTA, "300s"))

    with pytest.raises(highlighter.GeminiQuotaError):
        highlighter._call_gemini("prompt", "key", "gemini-test")

    assert slept == []


def test_call_gemini_logs_success_with_token_counts(
    mock_genai_transport, caplog
):
    import logging

    caplog.set_level(logging.INFO, logger="clip-extractor")
    mock_genai_transport.responses.append(
        (
            200,
            {
                "candidates": [
                    {"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": 179325,
                    "candidatesTokenCount": 42,
                    "totalTokenCount": 179367,
                },
            },
        )
    )

    highlighter._call_gemini("SECRET PROMPT TEXT", "secret-key", "gemini-test")

    text = caplog.text
    assert "model=gemini-test attempt=1" in text
    assert "prompt_chars=18" in text
    assert "prompt_tokens=179325" in text
    assert "candidates_tokens=42" in text
    assert "total_tokens=179367" in text
    assert "STOP" in text
    assert "SECRET PROMPT TEXT" not in text
    assert "secret-key" not in text


def test_call_gemini_logs_each_5xx_attempt(mock_genai_transport, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="clip-extractor")
    mock_genai_transport.responses.extend(
        [_gemini_error(503, "UNAVAILABLE"), _gemini_success()]
    )

    highlighter._call_gemini("SECRET PROMPT TEXT", "secret-key", "gemini-test")

    text = caplog.text
    assert text.count("[Gemini] request:") == 2
    assert "attempt=2" in text
    assert "http=503 status=UNAVAILABLE" in text
    assert "sleep" in text
    assert 5.0 <= mock_genai_transport.sleeps[0] <= 6.0
    assert "SECRET PROMPT TEXT" not in text
    assert "secret-key" not in text


def test_call_gemini_logs_quota_fields(mock_genai_transport, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="clip-extractor")
    mock_genai_transport.responses.extend(
        [_quota_error(PER_MINUTE_QUOTA, "52s"), _gemini_success()]
    )

    highlighter._call_gemini("SECRET PROMPT TEXT", "secret-key", "gemini-test")

    text = caplog.text
    assert f"quotaId={PER_MINUTE_QUOTA}" in text
    assert "quotaValue=250000" in text
    assert "retryDelay=52.0" in text
    assert "sleep 54.0s and retry" in text
    assert "SECRET PROMPT TEXT" not in text
    assert "secret-key" not in text


@pytest.fixture(autouse=True)
def _reset_gemini_token_limiter():
    highlighter._gemini_token_log.clear()
    yield
    highlighter._gemini_token_log.clear()


class _FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(highlighter, "_gemini_clock", clock)
    monkeypatch.setattr(highlighter.time, "sleep", clock.sleep)
    return clock


def _chars_for_tokens(tokens):
    return int(tokens / highlighter.GEMINI_TOKENS_PER_CHAR) - len(
        highlighter.GEMINI_SYSTEM_PROMPT
    )


def test_limiter_waits_for_first_request_to_expire(fake_clock):
    first = highlighter._gemini_reserve_tokens("m", 150_000)
    assert fake_clock.sleeps == []
    fake_clock.now += 10
    highlighter._gemini_reserve_tokens("m", 150_000)
    # first entry expires 60s after its timestamp: 50s from now (+0.5 margin)
    assert fake_clock.sleeps == [pytest.approx(50.5)]
    assert first[0] == 1000.0


def test_limiter_does_not_wait_for_small_requests_or_other_models(fake_clock):
    for _ in range(5):
        highlighter._gemini_reserve_tokens("m", 10_000)
    highlighter._gemini_reserve_tokens("other", 200_000)
    highlighter._gemini_reserve_tokens("m", 100_000)
    assert fake_clock.sleeps == []


def test_limiter_sends_oversized_request_with_warning(fake_clock, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="clip-extractor")
    highlighter._gemini_reserve_tokens("m", 300_000)
    assert fake_clock.sleeps == []
    assert "送信します" in caplog.text


def test_limiter_oversized_request_waits_for_empty_window(fake_clock):
    highlighter._gemini_reserve_tokens("m", 10_000)
    fake_clock.now += 5
    highlighter._gemini_reserve_tokens("m", 226_000 * 2)
    # newest prior entry (5s old) must expire first: 55s (+0.5 margin)
    assert fake_clock.sleeps == [pytest.approx(55.5)]
    assert len(highlighter._gemini_token_log["m"]) == 1


def test_quota_info_rejects_malformed_retry_delays():
    class Exc:
        def __init__(self, delay, message=""):
            self.message = message
            self.details = {"error": {"details": [{"retryDelay": delay}]}}

    for bad in ("1..5s", ".s", "-3s", "nans", "infs", "5"):
        assert highlighter._gemini_quota_info(Exc(bad))[1] is None
    assert highlighter._gemini_quota_info(Exc("1.5s"))[1] == 1.5
    for msg in ("retry in 1..5s", "retry in .s", "retry in -2s"):
        assert highlighter._gemini_quota_info(Exc(None, msg))[1] is None
    assert highlighter._gemini_quota_info(Exc(None, "Please Retry in 2.5s."))[1] == 2.5


def test_limiter_logs_wait_and_caps_it(fake_clock, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="clip-extractor")
    highlighter._gemini_reserve_tokens("m", 200_000)
    highlighter._gemini_reserve_tokens("m", 100_000)
    assert "入力トークン上限回避のため" in caplog.text
    assert max(fake_clock.sleeps) <= highlighter.GEMINI_TOKEN_MAX_WAIT_SECONDS + 0.5


def test_call_gemini_uses_real_token_count_for_limiter(
    mock_genai_transport, fake_clock
):
    prompt = "x" * _chars_for_tokens(150_000)
    mock_genai_transport.responses.append(
        (
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                "usageMetadata": {"promptTokenCount": 10_000},
            },
        )
    )
    highlighter._call_gemini(prompt, "key", "gemini-test")
    assert highlighter._gemini_token_log["gemini-test"][0][1] == 10_000.0

    # With the real count recorded, a second 150k request fits without waiting.
    mock_genai_transport.responses.append(_gemini_success())
    highlighter._call_gemini(prompt, "key", "gemini-test")
    assert fake_clock.sleeps == []


def test_call_gemini_waits_before_second_large_request(
    mock_genai_transport, fake_clock
):
    prompt = "x" * _chars_for_tokens(150_000)
    mock_genai_transport.responses.extend([_gemini_success(), _gemini_success()])
    highlighter._call_gemini(prompt, "key", "gemini-test")
    highlighter._call_gemini(prompt, "key", "gemini-test")
    assert len(fake_clock.sleeps) == 1
    assert fake_clock.sleeps[0] == pytest.approx(60.5)
