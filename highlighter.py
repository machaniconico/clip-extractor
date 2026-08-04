"""Highlight detection using Claude, cloud APIs, or opt-in local inference."""

import json
import os
import re
import subprocess
from urllib.parse import urlsplit

import httpx


SYSTEM_PROMPT = """あなたはYouTube動画の切り抜きエキスパートです。
配信アーカイブのトランスクリプト（タイムスタンプ付き）を分析し、
ショート動画として切り抜くべき見どころシーンを特定してください。

以下のJSON形式で回答してください。他のテキストは含めないでください：
{
  "highlights": [
    {
      "start": "HH:MM:SS.mmm",
      "end": "HH:MM:SS.mmm",
      "title": "クリップのタイトル（短く、キャッチーに）",
      "reason": "このシーンを選んだ理由"
    }
  ]
}

選定基準：
- 各クリップは30〜90秒程度
- 面白い・感動的・印象的・情報価値が高いシーンを優先
- クリップ同士が重複しないように
- 会話の途中で切れないよう、自然な区切りを意識
"""

GEMINI_SYSTEM_PROMPT = """あなたはYouTube動画の切り抜きエキスパートです。
配信アーカイブのトランスクリプト（タイムスタンプ付き）を分析し、
ショート動画として切り抜くべき見どころシーンを特定してください。

選定基準：
- 各クリップは30〜90秒程度
- 面白い・感動的・印象的・情報価値が高いシーンを優先
- クリップ同士が重複しないように
- 会話の途中で切れないよう、自然な区切りを意識
"""


GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"
GEMINI_REQUEST_TIMEOUT_MS = 300_000
GEMINI_MODEL_CHOICES = (
    GEMINI_DEFAULT_MODEL,
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    # Keep the stable 2.5 models selectable for existing saved workflows.
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
)

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
                        "description": "クリップ開始時刻（HH:MM:SS.mmm）",
                    },
                    "end": {
                        "type": "string",
                        "description": "クリップ終了時刻（HH:MM:SS.mmm）",
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
各クリップは {min_duration}〜{max_duration} 秒程度にしてください。

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
        from google.genai import types
    except ImportError:
        raise RuntimeError(
            "google-genai パッケージが必要です: pip install google-genai"
        )

    print(f"Analyzing transcript with Gemini ({model})...")
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=GEMINI_REQUEST_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
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
) -> list[dict]:
    """Detect highlight moments in the transcript using the selected AI provider."""
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
            h["start_sec"] = _parse_timestamp(h["start"])
            h["end_sec"] = _parse_timestamp(h["end"])
            h["duration"] = h["end_sec"] - h["start_sec"]
        except (ValueError, TypeError, AttributeError) as e:
            print(f"[Warn] skipping highlight with bad timestamp ({e}): {h!r}")
            continue
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
