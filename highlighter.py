"""Highlight detection using Claude Code CLI, OpenAI, or Gemini."""

import json
import re
import subprocess
import sys


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
    """Call OpenAI ChatGPT API."""
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("openai パッケージが必要です: pip install openai")

    print(f"Analyzing transcript with OpenAI ({model})...")
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content


def _call_gemini(user_prompt, api_key, model="gemini-3-flash-preview"):
    """Call Google Gemini API."""
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError("google-generativeai パッケージが必要です: pip install google-generativeai")

    print(f"Analyzing transcript with Gemini ({model})...")
    genai.configure(api_key=api_key)
    gmodel = genai.GenerativeModel(
        model_name=model,
        system_instruction=SYSTEM_PROMPT,
    )
    response = gmodel.generate_content(user_prompt)
    return response.text


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
        model = ai_model or "gemini-3-flash-preview"
        response_text = _call_gemini(user_prompt, api_key, model)
    else:
        response_text = _call_claude(user_prompt)

    # Extract JSON from response
    json_match = re.search(r'\{[\s\S]*\}', response_text)
    if not json_match:
        raise ValueError("AI did not return valid JSON")

    data = json.loads(json_match.group())
    highlights = data.get("highlights", [])

    # Parse timestamps to seconds
    for h in highlights:
        h["start_sec"] = _parse_timestamp(h["start"])
        h["end_sec"] = _parse_timestamp(h["end"])
        h["duration"] = h["end_sec"] - h["start_sec"]

    print(f"Found {len(highlights)} highlights:")
    for i, h in enumerate(highlights, 1):
        print(f"  {i}. [{h['start']} -> {h['end']}] {h['title']} ({h['duration']:.0f}s)")

    return highlights


def _parse_timestamp(ts: str) -> float:
    """Parse HH:MM:SS.mmm, HH:MM:SS,mmm, or MM:SS.mmm to seconds."""
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
