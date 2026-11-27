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


GEMINI_DEFAULT_MODEL = "gemini-4-hydrogen"
GEMINI_REQUEST_TIMEOUT_MS = 300_000
GEMINI_REQUEST_ATTEMPTS = 4
GEMINI_RETRY_STATUS_CODES = (500, 502, 503, 504)
GEMINI_MODEL_CHOICES = (
    GEMINI_DEFAULT_MODEL,
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    # Keep legacy 2.5 models selectable for existing saved workflows
    # (Google routes 2.5-flash -> 3.5-flash-lite and 2.5-pro -> 3.8-flash from Dec 2026).
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
)
GEMINI_FREE_TIER_MODELS = frozenset(
    {
        "gemini-4-hydrogen",
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    }
)
GEMINI_FREE_FALLBACK_CHAIN = (
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
)
GEMINI_LOW_COST_MODELS = frozenset(
    {
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash-lite",
    }
)
GEMINI_PAID_ONLY_MARKERS = ("carbon", "argon", "ultra", "-pro")


def classify_ai_cost_risk(ai_provider: str, ai_model: str = "") -> tuple[str, str]:
    """Classify billing/cost risk for ``(ai_provider, ai_model)``.

    Returns ``(level, message)`` where ``level`` is one of:
      - ``"paid_only"``: always billed or no free tier available
      - ``"unknown_custom"``: unrecognized custom Gemini model ID (might be paid-only)
      - ``"billing_caveat"``: has a free tier, but billed at higher rate when Cloud Billing is linked
      - ``"free_low_cost"``: has a free tier and lowest paid unit price
      - ``"subscription"``: uses CLI / external subscription quota
      - ``"local_free"``: local loopback inference (no cloud billing)
    """
    provider = str(ai_provider or "gemini").strip().lower()
    model = str(ai_model or "").strip().lower()

    if provider == "openai":
        return (
            "paid_only",
            "OpenAI（ChatGPT API）には無料枠がありません。実行のたびに従量課金されます。"
            "無料で使う場合はプロバイダーを「gemini」に切り替えてください。",
        )
    if provider == "claude":
        return (
            "subscription",
            "Claude CLI はAPIキー不要ですが、契約中のClaudeプラン（またはAPI課金）の利用枠を消費します。",
        )
    if provider == "local":
        return (
            "local_free",
            "ローカルLLMはPC内で処理するため、クラウドAPI料金は発生しません。",
        )
    if provider != "gemini":
        return (
            "unknown_custom",
            f"プロバイダー「{provider}」の課金条件は公式料金表を確認してください。",
        )

    effective_model = model or GEMINI_DEFAULT_MODEL
    if any(marker in effective_model for marker in GEMINI_PAID_ONLY_MARKERS):
        return (
            "paid_only",
            f"「{effective_model}」は無料枠の対象外（有料専用）または非推奨Proモデルです。"
            "課金プロジェクトでは実行ごとに料金が発生し、無課金プロジェクトではエラーになる場合があります。"
            "無料枠で使う場合は gemini-4-hydrogen / gemini-3.8-flash / gemini-3.5-flash-lite を選んでください。",
        )
    if effective_model not in GEMINI_FREE_TIER_MODELS:
        return (
            "unknown_custom",
            f"カスタムモデル「{effective_model}」は無料枠の確認済み一覧に含まれていません。"
            "有料専用モデルの場合、課金設定があるプロジェクトでは料金が発生します（Google公式料金表を確認してください）。",
        )
    if effective_model in GEMINI_LOW_COST_MODELS:
        return (
            "free_low_cost",
            f"「{effective_model}」は無料枠あり・最安クラスです。"
            "クレカ未登録なら無料枠で動作し、課金プロジェクトでも低コスト（入力 $0.30 / 出力 $2.50 / 100万トークン）です。",
        )
    if effective_model.startswith("gemini-2.5-"):
        return (
            "billing_caveat",
            f"「{effective_model}」は旧世代（非推奨）です。2026年12月以降、Google側で後継モデルへ自動転送されます。"
            "新規利用は gemini-4-hydrogen / gemini-3.8-flash / gemini-3.5-flash-lite を推奨します。",
        )
    extra = (
        "（3.8 Flash は 2027/1/1 から有料単価が $0.75/$3.75 → $1.50/$7.50 へ改定予定）"
        if effective_model == "gemini-3.8-flash"
        else ""
    )
    return (
        "billing_caveat",
        f"「{effective_model}」は無料枠あり（クレカ未登録なら無料・目安: 1日20回前後）です。"
        f"ただし Google Cloud の課金（Billing）を紐づけたプロジェクトでは有料枠で従量課金されます{extra}。"
        "課金プロジェクトで費用を抑えたい場合は「gemini-3.5-flash-lite」（約1/5の単価）をおすすめします。",
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


def gemini_fallback_models(model: str) -> tuple[str, ...]:
    """Return strictly lower free-tier Gemini models to try if ``model`` hits 429."""
    current = str(model or GEMINI_DEFAULT_MODEL).strip() or GEMINI_DEFAULT_MODEL
    if current in GEMINI_FREE_FALLBACK_CHAIN:
        idx = GEMINI_FREE_FALLBACK_CHAIN.index(current)
        return GEMINI_FREE_FALLBACK_CHAIN[idx + 1 :]
    if current == "gemini-3.6-flash":
        return ("gemini-3.5-flash-lite",)
    return ()


def _call_gemini_with_fallback(
    user_prompt: str,
    api_key: str,
    model: str = GEMINI_DEFAULT_MODEL,
) -> str:
    """Call Gemini and automatically step down to lower free-tier models on 429 quota exhaustion."""
    candidates = (model, *gemini_fallback_models(model))
    last_quota_error: GeminiQuotaError | None = None
    for idx, candidate in enumerate(candidates):
        try:
            return _call_gemini(user_prompt, api_key, candidate)
        except GeminiQuotaError as exc:
            last_quota_error = exc
            if idx + 1 < len(candidates):
                next_model = candidates[idx + 1]
                logger.warning(
                    "[Gemini] %s の無料枠上限に達したため、下位モデル %s へ自動フォールバックします",
                    candidate,
                    next_model,
                )
                print(
                    f"[Gemini] {candidate} の無料枠上限に達したため、"
                    f"下位の無料枠モデル {next_model} に切り替えて再試行します..."
                )
                continue
            raise
    if last_quota_error is not None:  # pragma: no cover
        raise last_quota_error
    raise RuntimeError("Gemini highlight detection did not execute any model")  # pragma: no cover


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
        response_text = _call_gemini_with_fallback(user_prompt, api_key, model)
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


AI_CONNECTION_TEST_TIMEOUT_SECONDS = 10.0


def test_ai_connection(
    ai_provider: str,
    api_key: str = "",
    ai_model: str = "",
) -> tuple[bool, str]:
    """Verify AI provider readiness with a lightweight, non-generating probe.

    Returns ``(ok, message)`` with user-safe Japanese text that never echoes
    credentials or raw third-party response payloads.
    """
    provider = str(ai_provider or "").strip().lower()
    key = str(api_key or "").strip().strip('"').strip("'")

    if provider == "gemini":
        if not key:
            return (
                False,
                "❌ Gemini APIキーが未設定です。Google AI Studio（https://aistudio.google.com/apikey）で"
                "取得し、上の「APIキー」欄に貼り付けて保存してください。",
            )
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError:
            return (
                False,
                "❌ google-genai パッケージが見つかりません。setup.bat を再実行してください。",
            )
        model_name = str(ai_model or GEMINI_DEFAULT_MODEL).strip() or GEMINI_DEFAULT_MODEL
        client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=int(AI_CONNECTION_TEST_TIMEOUT_SECONDS * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        try:
            client.models.get(model=model_name)
        except errors.APIError as exc:
            code = getattr(exc, "code", None)
            logger.warning("[Gemini] connection test failed: http=%s", code)
            if code in (400, 401, 403):
                hint = ""
                if not key.startswith("AIza"):
                    hint = "（Gemini APIキーは通常「AIza」で始まります）"
                return (
                    False,
                    f"❌ Gemini APIキーが無効または拒否されました（HTTP {code}）。{hint}"
                    "コピー漏れ・余分な文字がないか確認するか、Google AI Studio で新しいキーを作成し直してください。",
                )
            if code == 404:
                return (
                    False,
                    f"⚠️ APIキーは認識されましたが、モデル「{model_name}」が見つかりません。"
                    f"既定の「{GEMINI_DEFAULT_MODEL}」などに切り替えてお試しください。",
                )
            if code == 429:
                return (
                    False,
                    "⚠️ APIキーは有効ですが、現在レート制限（HTTP 429）に達しています。"
                    "少し時間を置いてから再度お試しください。",
                )
            if isinstance(code, int) and code >= 500:
                return (
                    False,
                    f"⚠️ Google側で一時的な障害が発生しています（HTTP {code}）。数分待ってから再試行してください。",
                )
            return (
                False,
                f"❌ Gemini への接続テストに失敗しました（HTTP {code or '不明'}）。"
                "ネットワーク環境とAPIキーを確認してください。",
            )
        except Exception as exc:
            logger.warning("[Gemini] connection test error: %s", type(exc).__name__)
            return (
                False,
                "❌ Gemini へ接続できませんでした。インターネット接続やプロキシ設定を確認してください。",
            )
        finally:
            try:
                client.close()
            except Exception:
                pass
        return (
            True,
            f"✅ Gemini APIキーの接続テストに成功しました（モデル: {model_name} 利用可能）",
        )

    if provider == "openai":
        if not key:
            return (
                False,
                "❌ OpenAI APIキーが未設定です。OpenAI Platform でキーを取得し、"
                "上の「APIキー」欄に貼り付けて保存してください。",
            )
        try:
            import openai
            from openai import OpenAI
        except ImportError:
            return (
                False,
                "❌ openai パッケージが見つかりません。setup.bat を再実行してください。",
            )
        model_name = str(ai_model or "gpt-4.1").strip() or "gpt-4.1"
        client = OpenAI(
            api_key=key,
            timeout=AI_CONNECTION_TEST_TIMEOUT_SECONDS,
            max_retries=0,
        )
        try:
            client.models.retrieve(model_name)
        except openai.AuthenticationError:
            logger.warning("[OpenAI] connection test failed: authentication error")
            return (
                False,
                "❌ OpenAI APIキーが無効です（HTTP 401）。"
                "キーのコピー漏れや失効がないか確認してください。",
            )
        except openai.PermissionDeniedError:
            logger.warning("[OpenAI] connection test failed: permission denied")
            return (
                False,
                "❌ OpenAI APIキーに権限がありません（HTTP 403）。プロジェクト権限や課金設定を確認してください。",
            )
        except openai.NotFoundError:
            logger.warning("[OpenAI] connection test failed: model not found")
            return (
                False,
                f"⚠️ APIキーは有効ですが、モデル「{model_name}」にアクセスできません。"
                "別のモデルを選んでお試しください。",
            )
        except openai.RateLimitError:
            logger.warning("[OpenAI] connection test failed: rate limit")
            return (
                False,
                "⚠️ APIキーは有効ですが、利用上限（HTTP 429）に達しています。OpenAI の残高・上限を確認してください。",
            )
        except Exception as exc:
            logger.warning("[OpenAI] connection test error: %s", type(exc).__name__)
            return (
                False,
                "❌ OpenAI へ接続できませんでした。インターネット接続とAPIキーを確認してください。",
            )
        finally:
            try:
                client.close()
            except Exception:
                pass
        return (
            True,
            f"✅ OpenAI APIキーの接続テストに成功しました（モデル: {model_name} 利用可能）",
        )

    if provider == "claude":
        import shutil

        claude_path = shutil.which("claude")
        if not claude_path:
            return (
                False,
                "❌ Claude CLI が見つかりません。Claude を使うには `npm install -g @anthropic-ai/claude-code` が必要です。"
                "無料枠のある「gemini」に切り替えると、コマンド導入なしですぐ始められます。",
            )
        try:
            result = subprocess.run(
                [claude_path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
        except Exception as exc:
            logger.warning("[Claude] version probe failed: %s", type(exc).__name__)
            return (
                False,
                "⚠️ Claude CLI の実行確認に失敗しました。コマンドプロンプトで `claude --version` を確認してください。",
            )
        if result.returncode != 0:
            return (
                False,
                "⚠️ Claude CLI がエラーを返しました。先にログイン（`claude`）を済ませてください。",
            )
        version = (result.stdout or "").strip().splitlines()[0] if (result.stdout or "").strip() else "検出済み"
        return (
            True,
            f"✅ Claude CLI を利用できます（{version} / APIキー入力は不要です）",
        )

    if provider == "local":
        if not local_llm_enabled():
            return (
                False,
                f"❌ ローカルLLMは無効です。環境変数 {LOCAL_LLM_ENABLE_ENV}=1 を設定して起動してください。",
            )
        try:
            base_url = validate_local_llm_base_url(
                os.environ.get(LOCAL_LLM_BASE_URL_ENV, LOCAL_LLM_DEFAULT_BASE_URL)
            )
        except ValueError:
            return (
                False,
                "❌ ローカルLLMのURL設定が不正です（loopback の /v1 のみ許可）。",
            )
        try:
            response = httpx.get(
                f"{base_url}/models",
                timeout=5.0,
                trust_env=False,
                follow_redirects=False,
            )
        except Exception as exc:
            logger.warning("[Local] connection test failed: %s", type(exc).__name__)
            return (
                False,
                f"❌ ローカルサーバー（{base_url}）に接続できません。LM Studio の Local Server が起動しているか確認してください。",
            )
        if response.status_code != 200:
            return (
                False,
                f"❌ ローカルサーバーが HTTP {response.status_code} を返しました。LM Studio の状態を確認してください。",
            )
        return (
            True,
            f"✅ ローカルLLMサーバー（{base_url}）に接続できました（APIキー不要）",
        )

    return False, f"❌ 未対応のAIプロバイダーです: {provider}"
