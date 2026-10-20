"""Highlight detection using Claude, cloud APIs, or opt-in local inference."""

import json
import math
import logging
import os
import random
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("clip-extractor")

SYSTEM_PROMPT = """あなたはYouTube動画の切り抜きエキスパートです。
配信アーカイブのトランスクリプト（タイムスタンプ付き）を分析し、
ショート動画として切り抜くべき見どころシーンを特定してください。

トランスクリプトは1行1発話で、各行は「[H:MM:SS] 発話内容」の形式です。
行頭の時刻はその発話の開始時刻（秒未満切り捨て）です。

以下のJSON形式で回答してください。他のテキストは含めないでください：
{
  "highlights": [
    {
      "start": "HH:MM:SS",
      "end": "HH:MM:SS",
      "title": "クリップのタイトル（短く、キャッチーに）",
      "reason": "このシーンを選んだ理由"
    }
  ]
}

選定基準：
- 面白い・感動的・印象的・情報価値が高いシーンを優先
- クリップ同士が重複しないように
- 会話の途中で切れないよう、自然な区切りを意識
"""

GEMINI_SYSTEM_PROMPT = """あなたはYouTube動画の切り抜きエキスパートです。
配信アーカイブのトランスクリプト（タイムスタンプ付き）を分析し、
ショート動画として切り抜くべき見どころシーンを特定してください。

トランスクリプトは1行1発話で、各行は「[H:MM:SS] 発話内容」の形式です。
行頭の時刻はその発話の開始時刻（秒未満切り捨て）です。

選定基準：
- 面白い・感動的・印象的・情報価値が高いシーンを優先
- クリップ同士が重複しないように
- 会話の途中で切れないよう、自然な区切りを意識
"""


GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"
GEMINI_REQUEST_TIMEOUT_MS = 300_000
GEMINI_REQUEST_ATTEMPTS = 4
GEMINI_RETRY_STATUS_CODES = (500, 502, 503, 504)
GEMINI_MODEL_CHOICES = (
    GEMINI_DEFAULT_MODEL,
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    # Keep the stable 2.5 models selectable for existing saved workflows.
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
)


class GeminiTemporaryError(RuntimeError):
    """A temporary Gemini outage persisted after the bounded SDK retries."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(
            f"Gemini側で一時的なエラー（HTTP {status_code}）が発生し、"
            f"初回を含め{GEMINI_REQUEST_ATTEMPTS}回試しましたが復旧しませんでした。"
            "数分待ってから再実行してください。"
        )


class GeminiQuotaError(GeminiTemporaryError):
    """Gemini free-tier/quota exhaustion (HTTP 429) that retrying cannot fix now."""

    def __init__(self, per_minute: bool, retry_delay: float | None = None):
        self.status_code = 429
        self.per_minute = per_minute
        self.retry_delay = retry_delay
        RuntimeError.__init__(
            self,
            "Gemini の利用枠（無料枠の上限）に達したため、ハイライト検出に失敗しました。"
            + (
                "1分あたりのトークン数の上限です。"
                "数分待ってから再実行してください。"
                if per_minute
                else "1日あたりなどの上限に達した可能性があります。"
                "翌日以降に再実行するか、"
            )
            + (
                "長時間の配信では1回の入力が大きく、再実行しても同じ上限に達することがあります。"
                if per_minute
                else ""
            )
            + "有料枠への切り替えや、別のAIプロバイダー（Claude / ChatGPT）の利用を検討してください。"
        )


# Free tier: 250,000 input tokens per model per minute; keep a 10% margin.
GEMINI_INPUT_TOKENS_PER_MINUTE = 250_000
GEMINI_INPUT_TOKEN_BUDGET = int(GEMINI_INPUT_TOKENS_PER_MINUTE * 0.9)
# Measured: 179,325 tokens / 210,860 chars = 0.85 (old timestamp format) and
# 79,993 / 100,979 = 0.79 (compact format). Use the larger, conservative ratio.
GEMINI_TOKENS_PER_CHAR = 0.85
GEMINI_TOKEN_WINDOW_SECONDS = 60.0
GEMINI_TOKEN_MAX_WAIT_SECONDS = 65.0

_gemini_token_lock = threading.Lock()
_gemini_token_log: dict[str, list[list[float]]] = {}
_gemini_clock = time.monotonic


def _gemini_reserve_tokens(model: str, estimate: int) -> list[float]:
    """Wait until ``estimate`` input tokens fit in the per-minute budget.

    Records the attempt (it counts server-side even when it fails) and returns
    its ``[timestamp, tokens]`` entry so the real count can replace the estimate.
    """
    while True:
        with _gemini_token_lock:
            now = _gemini_clock()
            entries = [
                e for e in _gemini_token_log.get(model, [])
                if now - e[0] < GEMINI_TOKEN_WINDOW_SECONDS
            ]
            _gemini_token_log[model] = entries
            used = sum(e[1] for e in entries)
            oversized = estimate > GEMINI_INPUT_TOKEN_BUDGET
            if (not entries) if oversized else (
                used + estimate <= GEMINI_INPUT_TOKEN_BUDGET
            ):
                if oversized:
                    logger.warning(
                        "[Gemini] 今回の推定入力トークン(%d)が1分あたりの上限目安(%d)を"
                        "超えるため、直近の送信が無くなるのを待ってから送信します。"
                        "無料枠の上限に達する可能性があります",
                        estimate, GEMINI_INPUT_TOKEN_BUDGET,
                    )
                entry = [now, float(estimate)]
                entries.append(entry)
                return entry
            remaining = used
            wait = GEMINI_TOKEN_MAX_WAIT_SECONDS
            if oversized:
                # An oversized request needs an empty window: wait for the
                # newest reservation to expire.
                wait = min(
                    wait,
                    max(e[0] for e in entries) + GEMINI_TOKEN_WINDOW_SECONDS - now,
                )
            else:
                for e in entries:
                    remaining -= e[1]
                    if remaining + estimate <= GEMINI_INPUT_TOKEN_BUDGET:
                        wait = min(wait, e[0] + GEMINI_TOKEN_WINDOW_SECONDS - now)
                        break
            wait = max(wait, 0.0) + 0.5
        logger.info(
            "[Gemini] 入力トークン上限回避のため %.1fs 待機 (直近60秒: %d tokens, 今回推定: %d)",
            wait, used, estimate,
        )
        time.sleep(wait)


GEMINI_QUOTA_MAX_WAIT_SECONDS = 120.0
GEMINI_QUOTA_EXTRA_ATTEMPTS = 2


def _parse_retry_seconds(pattern: str, text: str, flags) -> float | None:
    """Return a finite non-negative seconds value matched by ``pattern`` or None."""
    m = re.search(pattern, text, flags)
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _gemini_quota_info(exc) -> tuple[bool, float | None, str, str]:
    """Return (is_per_minute, retry_delay_seconds, quotaId, quotaValue) of a 429."""
    quota_id = ""
    quota_value = ""
    delay = None
    details = getattr(exc, "details", None)
    error = details.get("error") if isinstance(details, dict) else None
    items = error.get("details") if isinstance(error, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        for violation in item.get("violations") or []:
            if isinstance(violation, dict) and not quota_id:
                quota_id = str(violation.get("quotaId", ""))
                quota_value = str(violation.get("quotaValue", ""))
        raw_delay = item.get("retryDelay")
        if delay is None and isinstance(raw_delay, str):
            delay = _parse_retry_seconds(r"^\s*(\d+(?:\.\d+)?)s\s*$", raw_delay, 0)
    if delay is None:
        delay = _parse_retry_seconds(
            r"retry in (\d+(?:\.\d+)?)s",
            str(getattr(exc, "message", "") or ""),
            re.IGNORECASE,
        )
    return "PerMinute" in quota_id, delay, quota_id, quota_value


HIGHLIGHTS_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "クリップ開始時刻（HH:MM:SS）",
                    },
                    "end": {
                        "type": "string",
                        "description": "クリップ終了時刻（HH:MM:SS）",
                    },
                    "title": {
                        "type": "string",
                        "description": "短くキャッチーなクリップタイトル",
                    },
                    "reason": {
                        "type": "string",
                        "description": "このシーンを選んだ理由",
                    },
                },
                "required": ["start", "end", "title", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["highlights"],
    "additionalProperties": False,
}

LOCAL_LLM_ENABLE_ENV = "CLIP_EXTRACTOR_ENABLE_LOCAL_LLM"
LOCAL_LLM_MODEL_ENV = "CLIP_EXTRACTOR_LOCAL_LLM_MODEL"
LOCAL_LLM_BASE_URL_ENV = "CLIP_EXTRACTOR_LOCAL_LLM_BASE_URL"
LOCAL_LLM_DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
LOCAL_LLM_TIMEOUT_SECONDS = 600.0
_LOCAL_LLM_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})


def local_llm_enabled(environ=None) -> bool:
    """Return whether the dormant local provider was explicitly enabled."""
    source = os.environ if environ is None else environ
    return str(source.get(LOCAL_LLM_ENABLE_ENV, "")).strip().casefold() in (
        _LOCAL_LLM_TRUTHY_VALUES
    )


def local_llm_default_model(environ=None) -> str:
    """Return the configured local model ID without inventing a default."""
    source = os.environ if environ is None else environ
    return str(source.get(LOCAL_LLM_MODEL_ENV, "")).strip()


def validate_local_llm_base_url(value: str) -> str:
    """Allow only a loopback HTTP OpenAI-compatible ``/v1`` endpoint."""
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid local LLM URL: {exc}") from exc

    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("Local LLM URL must use http:// on a loopback host")
    if (
        "@" in parsed.netloc
        or "?" in raw
        or "#" in raw
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Local LLM URL cannot contain credentials, query, or fragment")

    host = parsed.hostname.casefold()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Local LLM URL host must be loopback only")
    if parsed.path.rstrip("/") != "/v1":
        raise ValueError("Local LLM URL path must be exactly /v1")
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("Invalid local LLM URL port")
    return raw


def _build_user_prompt(transcript, num_clips, min_duration, max_duration, custom_prompt):
    user_prompt = f"""以下の配信トランスクリプトから、最も魅力的な {num_clips} 個のシーンを選んでください。
各クリップは必ず {min_duration}〜{max_duration} 秒にしてください。

"""
    if custom_prompt:
        user_prompt += f"追加の指示: {custom_prompt}\n\n"
    user_prompt += f"トランスクリプト:\n{transcript}"
    return user_prompt


def _call_claude(user_prompt):
    """Call Claude via Claude Code CLI."""
    full_prompt = f"{SYSTEM_PROMPT}\n\n---\n\n{user_prompt}"
    print("Analyzing transcript with Claude (CLI)...")
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=full_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "claude CLI が見つかりません。\n"
            "インストール: npm install -g @anthropic-ai/claude-code"
        )
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI error: {result.stderr}")
    return result.stdout


def _call_openai(user_prompt, api_key, model="gpt-4.1"):
    """Call OpenAI ChatGPT API.

    `response_format={"type": "json_object"}` forces JSON output — this
    makes the response guaranteed-parseable by `json.loads` without
    relying on regex cleanup.
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai パッケージが必要です: pip install openai")

    print(f"Analyzing transcript with OpenAI ({model})...")
    client = OpenAI(api_key=api_key)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
    except Exception:
        # Some older chat models (or custom-compatible endpoints) don't
        # support response_format; retry without it and fall back on the
        # downstream JSON extractor.
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
    return response.choices[0].message.content


def _call_local_openai_compatible(user_prompt, model=""):
    """Call an explicitly enabled loopback OpenAI-compatible local server."""
    if not local_llm_enabled():
        raise RuntimeError(
            f"ローカルLLMは実験機能です。{LOCAL_LLM_ENABLE_ENV}=1 を設定してください。"
        )

    resolved_model = str(model or local_llm_default_model()).strip()
    if not resolved_model:
        raise ValueError(
            "ローカルモデルIDが必要です。モデル欄または "
            f"{LOCAL_LLM_MODEL_ENV} を設定してください。"
        )
    base_url = validate_local_llm_base_url(
        os.environ.get(LOCAL_LLM_BASE_URL_ENV, LOCAL_LLM_DEFAULT_BASE_URL)
    )

    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai パッケージが必要です: pip install openai")

    print(f"Analyzing transcript with local AI ({resolved_model})...")
    timeout = httpx.Timeout(
        LOCAL_LLM_TIMEOUT_SECONDS,
        connect=3.0,
        write=30.0,
        read=LOCAL_LLM_TIMEOUT_SECONDS,
        pool=5.0,
    )
    http_client = httpx.Client(
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )
    try:
        client = OpenAI(
            base_url=base_url,
            api_key="lm-studio",
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )
    except Exception:
        http_client.close()
        raise
    try:
        response = client.chat.completions.create(
            model=resolved_model,
            messages=[
                {"role": "system", "content": GEMINI_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "clip_extractor_highlights",
                    "strict": True,
                    "schema": HIGHLIGHTS_JSON_SCHEMA,
                },
            },
        )
    finally:
        client.close()

    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Local AI returned an empty response")
    return content


def _call_gemini(user_prompt, api_key, model=GEMINI_DEFAULT_MODEL):
    """Call Google Gemini API.

    The supported stable models all implement structured output. A schema
    keeps the response contract explicit, while _extract_json_object remains
    as a defensive parser for previously saved or externally supplied text.
    """
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError:
        raise RuntimeError(
            "google-genai パッケージが必要です: pip install google-genai"
        )

    print(f"Analyzing transcript with Gemini ({model})...")
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=GEMINI_REQUEST_TIMEOUT_MS,
            # Retry only transient server failures. Quota/auth/schema errors
            # need user action, and must not trigger repeated paid requests.
            # Keep retries in the SDK so attempts cannot multiply across layers.
            # Retries are done in the loop below so every attempt is logged;
            # the SDK must not resend the (large) prompt behind our back.
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    server_attempt = 0
    quota_retries = 0
    attempt = 0
    try:
        while True:
            attempt += 1
            server_attempt += 1
            token_entry = _gemini_reserve_tokens(
                model,
                math.ceil(
                    (len(user_prompt) + len(GEMINI_SYSTEM_PROMPT))
                    * GEMINI_TOKENS_PER_CHAR
                ),
            )
            logger.info(
                "[Gemini] request: model=%s attempt=%d (5xx %d/%d, quota-retry %d/%d) "
                "prompt_chars=%d",
                model, attempt, server_attempt, GEMINI_REQUEST_ATTEMPTS,
                quota_retries, GEMINI_QUOTA_EXTRA_ATTEMPTS, len(user_prompt),
            )
            started = time.monotonic()
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=GEMINI_SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        response_json_schema=HIGHLIGHTS_JSON_SCHEMA,
                    ),
                )
            except errors.APIError as exc:
                elapsed = time.monotonic() - started
                status = str(getattr(exc, "status", "") or "")
                if exc.code in GEMINI_RETRY_STATUS_CODES:
                    if server_attempt >= GEMINI_REQUEST_ATTEMPTS:
                        logger.warning(
                            "[Gemini] failed after %.1fs: http=%s status=%s -> give up",
                            elapsed, exc.code, status,
                        )
                        raise GeminiTemporaryError(exc.code) from exc
                    wait = min(
                        30.0, 5.0 * 2.0 ** (server_attempt - 1)
                    ) + random.uniform(0.0, 1.0)  # nosec B311 - backoff jitter
                    logger.warning(
                        "[Gemini] failed after %.1fs: http=%s status=%s -> "
                        "sleep %.1fs and retry",
                        elapsed, exc.code, status, wait,
                    )
                    time.sleep(wait)
                    continue
                if exc.code != 429:
                    logger.warning(
                        "[Gemini] failed after %.1fs: http=%s status=%s -> give up",
                        elapsed, exc.code, status,
                    )
                    raise
                per_minute, delay, quota_id, quota_value = _gemini_quota_info(exc)
                retry = (
                    per_minute
                    and delay is not None
                    and delay <= GEMINI_QUOTA_MAX_WAIT_SECONDS
                    and quota_retries < GEMINI_QUOTA_EXTRA_ATTEMPTS
                )
                logger.warning(
                    "[Gemini] failed after %.1fs: http=429 status=%s quotaId=%s "
                    "quotaValue=%s retryDelay=%s -> %s",
                    elapsed, status, quota_id, quota_value, delay,
                    f"sleep {delay + 2.0:.1f}s and retry" if retry else "give up",
                )
                if not retry:
                    raise GeminiQuotaError(per_minute, delay) from exc
                quota_retries += 1
                server_attempt = 0
                print(
                    f"Gemini 1分あたりの上限に達しました。{delay + 2:.0f}秒待って再試行します"
                    f"（{quota_retries}/{GEMINI_QUOTA_EXTRA_ATTEMPTS}）"
                )
                time.sleep(delay + 2.0)
                continue
            usage = getattr(response, "usage_metadata", None)
            candidates = getattr(response, "candidates", None) or []
            finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
            real_tokens = getattr(usage, "prompt_token_count", None)
            if isinstance(real_tokens, int):
                with _gemini_token_lock:
                    token_entry[1] = float(real_tokens)
            logger.info(
                "[Gemini] success in %.1fs: prompt_tokens=%s candidates_tokens=%s "
                "total_tokens=%s finish_reason=%s",
                time.monotonic() - started,
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "candidates_token_count", None),
                getattr(usage, "total_token_count", None),
                finish_reason,
            )
            break
    finally:
        client.close()
    return response.text


def _extract_json_object(text: str | None) -> dict | None:
    """Pull the first complete JSON object out of arbitrary LLM text.

    Strategies (in order):
      1. ```json ... ``` / ``` ... ``` code fence (common with chat models
         that ignore instructions to "output JSON only")
      2. Balanced-brace scan from each '{' — tolerates strings containing
         braces, escaped quotes, and multiple JSON objects in one reply
         (picks the first that parses)

    Returns None when no parseable object is found. Never raises."""
    if not text:
        return None

    # Try code fences first — LLMs often wrap JSON in fences even when asked not to
    for pattern in (r"```json\s*\n?(.*?)\n?```", r"```\s*\n?(.*?)\n?```"):
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            candidate = m.group(1).strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass  # fall through

    # Balanced-brace scan — handles nested objects, strings with `{`/`}`,
    # and multiple JSON objects (picks the first valid one).
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if in_string:
                if c == "\\":
                    escape_next = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # this `{` block was malformed; try the next one
        start = text.find("{", start + 1)

    return None


def detect_highlights(
    transcript: str,
    num_clips: int = 5,
    min_duration: int = 30,
    max_duration: int = 90,
    custom_prompt: str = "",
    ai_provider: str = "claude",
    api_key: str = "",
    ai_model: str = "",
    video_duration: float | None = None,
) -> list[dict]:
    """Detect highlight moments in the transcript using the selected AI provider."""
    min_duration, max_duration, video_duration = _validate_duration_bounds(
        min_duration,
        max_duration,
        video_duration,
    )
    user_prompt = _build_user_prompt(transcript, num_clips, min_duration, max_duration, custom_prompt)

    if ai_provider == "openai":
        model = ai_model or "gpt-4.1"
        response_text = _call_openai(user_prompt, api_key, model)
    elif ai_provider == "gemini":
        model = ai_model or GEMINI_DEFAULT_MODEL
        response_text = _call_gemini(user_prompt, api_key, model)
    elif ai_provider == "local":
        response_text = _call_local_openai_compatible(user_prompt, ai_model)
    elif ai_provider == "claude":
        response_text = _call_claude(user_prompt)
    else:
        raise ValueError(f"Unsupported AI provider: {ai_provider!r}")

    data = _extract_json_object(response_text)
    if data is None:
        snippet = (response_text or "")[:300]
        raise ValueError(
            f"AI did not return parseable JSON. Response snippet: {snippet!r}"
        )

    raw_highlights = data.get("highlights", [])
    valid_highlights: list[dict] = []
    for h in raw_highlights:
        if not isinstance(h, dict):
            print(f"[Warn] skipping non-dict highlight: {h!r}")
            continue
        if "start" not in h or "end" not in h:
            print(f"[Warn] skipping highlight missing start/end keys: {h!r}")
            continue
        try:
            start_sec, end_sec = normalize_highlight_range(
                _parse_timestamp(h["start"]),
                _parse_timestamp(h["end"]),
                min_duration=min_duration,
                max_duration=max_duration,
                video_duration=video_duration,
            )
        except (ValueError, TypeError, AttributeError) as e:
            print(f"[Warn] skipping highlight with bad timestamp ({e}): {h!r}")
            continue
        h["start_sec"] = start_sec
        h["end_sec"] = end_sec
        h["duration"] = end_sec - start_sec
        h["start"] = _format_timestamp(start_sec)
        h["end"] = _format_timestamp(end_sec)
        h.setdefault("title", "")
        h.setdefault("reason", "")
        valid_highlights.append(h)

    if not valid_highlights:
        raise ValueError(
            f"AI returned JSON but no valid highlights (keys: {list(data.keys())})"
        )

    print(f"Found {len(valid_highlights)} highlights:")
    for i, h in enumerate(valid_highlights, 1):
        print(f"  {i}. [{h['start']} -> {h['end']}] {h['title']} ({h['duration']:.0f}s)")

    return valid_highlights


def _validate_duration_bounds(min_duration, max_duration, video_duration=None):
    """Return finite clip bounds and reject impossible source durations."""
    min_value = float(min_duration)
    max_value = float(max_duration)
    if not math.isfinite(min_value) or min_value <= 0:
        raise ValueError("Minimum clip duration must be a positive finite number")
    if not math.isfinite(max_value) or max_value < min_value:
        raise ValueError("Maximum clip duration must be at least the minimum")

    source_value = None
    if video_duration is not None:
        source_value = float(video_duration)
        if not math.isfinite(source_value) or source_value <= 0:
            raise ValueError("Video duration must be a positive finite number")
        if source_value + 1e-9 < min_value:
            raise ValueError(
                f"Video duration ({source_value:.3f}s) is shorter than the "
                f"minimum clip duration ({min_value:.3f}s)"
            )
    return min_value, max_value, source_value


def normalize_highlight_range(
    start_sec,
    end_sec,
    *,
    min_duration,
    max_duration,
    video_duration=None,
) -> tuple[float, float]:
    """Clamp one AI-selected range to the configured duration and source bounds."""
    min_value, max_value, source_value = _validate_duration_bounds(
        min_duration,
        max_duration,
        video_duration,
    )
    start = float(start_sec)
    end = float(end_sec)
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError("Highlight timestamps must be finite numbers")
    if end <= start:
        raise ValueError("Highlight end must be after its start")

    start = max(0.0, start)
    if source_value is not None:
        start = min(start, source_value)
        end = min(max(0.0, end), source_value)
        if end <= start:
            raise ValueError("Highlight is outside the source video")

    current_duration = end - start
    target_duration = min(max(current_duration, min_value), max_value)
    center = (start + end) / 2.0
    normalized_start = center - target_duration / 2.0
    normalized_end = normalized_start + target_duration

    if normalized_start < 0.0:
        normalized_start = 0.0
        normalized_end = target_duration
    if source_value is not None and normalized_end > source_value:
        normalized_end = source_value
        normalized_start = source_value - target_duration

    normalized_start = max(0.0, normalized_start)
    normalized_end = normalized_start + target_duration
    if source_value is not None:
        normalized_end = min(source_value, normalized_end)
        normalized_start = normalized_end - target_duration
    return float(normalized_start), float(normalized_end)


def _format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def _parse_timestamp(ts: str | int | float) -> float:
    """Parse HH:MM:SS.mmm, HH:MM:SS,mmm, or MM:SS.mmm to seconds."""
    if isinstance(ts, (int, float)):
        return float(ts)
    ts = ts.strip().replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    else:
        return float(ts)
