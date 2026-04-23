"""Chapter and moment generation from transcripts using the selected AI provider.

Two modes:
- generate_chapters: whole-video YouTube-style chapter list (for the description box).
- search_moments: free-text prompt search that returns every matching interval.

Both reuse the provider pattern established in highlighter.py.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("clip-extractor")

CHAPTER_SYSTEM_PROMPT = """あなたはYouTube配信アーカイブのチャプターエディターです。
タイムスタンプ付きトランスクリプトを分析し、視聴者が目的の箇所を見つけやすい章立てを作ってください。

以下のJSON形式で返答してください。他のテキストは含めないでください：
{
  "chapters": [
    {"start": "HH:MM:SS", "title": "短く具体的な章タイトル"}
  ]
}

ルール:
- 最初の章は必ず 00:00:00 から始める
- 最低3章、できれば5〜15章程度
- 各章は10秒以上
- タイトルは30文字以内、配信で起きた出来事・話題・名場面の要約
- 時系列順。章同士が重複しないこと"""

SEARCH_SYSTEM_PROMPT = """あなたはYouTube配信アーカイブの検索エンジンです。
配信の文字起こし（タイムスタンプ付き）からユーザーの指定した条件に当てはまる区間を全て抜き出してください。

以下のJSON形式で返答してください。該当なしなら moments を空配列にしてください：
{
  "moments": [
    {
      "start": "HH:MM:SS",
      "end": "HH:MM:SS",
      "title": "このシーンの短い要約",
      "excerpt": "該当セリフ（30字以内、無ければ空文字）"
    }
  ]
}

ルール:
- 条件に合う区間は全部列挙（複数ヒット対応）
- 各区間は最低10秒、前後の文脈が途切れないように
- 時系列順、重複NG
- 条件が抽象的な場合（例: 面白いシーン）も文脈から推定"""


@dataclass
class Chapter:
    start_sec: float
    title: str

    def timestamp(self) -> str:
        return _format_hms(self.start_sec, compact=True)


@dataclass
class Moment:
    start_sec: float
    end_sec: float
    title: str
    excerpt: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


def generate_chapters(
    transcript_text: str,
    video_duration: float,
    ai_provider: str = "gemini",
    api_key: str = "",
    ai_model: str = "",
    custom_instructions: str = "",
    llm_caller: Optional[Callable[[str, str], str]] = None,
) -> list[Chapter]:
    """Generate a YouTube-style chapter list covering the full video.

    llm_caller is injected for tests; when None, the real provider is used.
    """
    user_prompt = _build_chapter_user_prompt(transcript_text, video_duration, custom_instructions)
    response_text = _dispatch_llm(
        CHAPTER_SYSTEM_PROMPT, user_prompt, ai_provider, api_key, ai_model, llm_caller
    )
    data = _parse_json_response(response_text)
    raw_chapters = data.get("chapters", []) or []
    chapters = []
    for c in raw_chapters:
        try:
            chapters.append(Chapter(start_sec=_parse_timestamp(c["start"]), title=str(c["title"]).strip()))
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[ChapterGen] Skipping invalid chapter row {c}: {e}")
    chapters = enforce_youtube_chapter_rules(chapters, video_duration)
    logger.info(f"[ChapterGen] {len(chapters)} chapters")
    for c in chapters:
        logger.info(f"  {c.timestamp()} {c.title}")
    return chapters


def search_moments(
    transcript_text: str,
    prompt: str,
    ai_provider: str = "gemini",
    api_key: str = "",
    ai_model: str = "",
    llm_caller: Optional[Callable[[str, str], str]] = None,
) -> list[Moment]:
    """Return all intervals matching the user's prompt."""
    if not prompt or not prompt.strip():
        raise ValueError("検索プロンプトが空です")

    user_prompt = f"条件: {prompt.strip()}\n\nトランスクリプト:\n{transcript_text}"
    response_text = _dispatch_llm(
        SEARCH_SYSTEM_PROMPT, user_prompt, ai_provider, api_key, ai_model, llm_caller
    )
    data = _parse_json_response(response_text)
    raw_moments = data.get("moments", []) or []
    moments: list[Moment] = []
    for m in raw_moments:
        try:
            start = _parse_timestamp(m["start"])
            end = _parse_timestamp(m["end"])
            if end <= start:
                end = start + 10.0
            moments.append(
                Moment(
                    start_sec=start,
                    end_sec=end,
                    title=str(m.get("title", "")).strip() or "(no title)",
                    excerpt=str(m.get("excerpt", "")).strip(),
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"[ChapterGen] Skipping invalid moment row {m}: {e}")
    moments.sort(key=lambda x: x.start_sec)
    logger.info(f"[ChapterGen] {len(moments)} moments for prompt '{prompt[:40]}...'")
    return moments


def format_chapters_for_youtube(chapters: list[Chapter]) -> str:
    """Format chapter list as YouTube description lines (`MM:SS タイトル`)."""
    lines = []
    for c in chapters:
        lines.append(f"{_format_hms(c.start_sec, compact=True)} {c.title}")
    return "\n".join(lines)


def format_moments_for_display(moments: list[Moment]) -> str:
    """Format moments as multi-line human-readable text."""
    if not moments:
        return "(該当なし)"
    lines = []
    for m in moments:
        start = _format_hms(m.start_sec, compact=True)
        end = _format_hms(m.end_sec, compact=True)
        lines.append(f"[{start} - {end}] {m.title}")
        if m.excerpt:
            lines.append(f" └ {m.excerpt}")
    return "\n".join(lines)


def enforce_youtube_chapter_rules(chapters: list[Chapter], video_duration: float) -> list[Chapter]:
    """Ensure: first at 0:00, min 3 chapters, each >= 10s.

    Dedup + sort + trim to video duration.
    """
    if not chapters:
        chapters = [
            Chapter(0.0, "オープニング"),
            Chapter(max(10.0, min(60.0, video_duration * 0.33)), "中盤"),
            Chapter(max(20.0, min(120.0, video_duration * 0.66)), "終盤"),
        ]

    chapters = [c for c in chapters if c.start_sec < max(video_duration, 0.0) + 0.001]
    chapters.sort(key=lambda c: c.start_sec)

    # Force first chapter at 0:00
    if not chapters or chapters[0].start_sec > 0.5:
        chapters.insert(0, Chapter(0.0, "オープニング"))
    else:
        chapters[0] = Chapter(0.0, chapters[0].title)

    # Dedup close chapters (min 10s apart)
    deduped: list[Chapter] = []
    for c in chapters:
        if deduped and c.start_sec - deduped[-1].start_sec < 10.0:
            continue
        deduped.append(c)
    chapters = deduped

    # Guarantee at least 3 chapters
    while len(chapters) < 3:
        last = chapters[-1].start_sec if chapters else 0.0
        next_start = min(last + max(30.0, video_duration / 4.0), max(video_duration - 10.0, last + 10.0))
        if next_start <= last:
            next_start = last + 10.0
        chapters.append(Chapter(next_start, f"パート{len(chapters) + 1}"))

    return chapters


def _build_chapter_user_prompt(transcript_text: str, video_duration: float, extra: str) -> str:
    duration_hms = _format_hms(video_duration, compact=True)
    parts = [
        f"配信の総尺: {duration_hms}",
        "この配信全体を視聴者向けのチャプターに分割してください。",
    ]
    if extra and extra.strip():
        parts.append(f"追加の指示: {extra.strip()}")
    parts.append(f"トランスクリプト:\n{transcript_text}")
    return "\n\n".join(parts)


def _dispatch_llm(
    system_prompt: str,
    user_prompt: str,
    ai_provider: str,
    api_key: str,
    ai_model: str,
    llm_caller: Optional[Callable[[str, str], str]],
) -> str:
    if llm_caller is not None:
        return llm_caller(system_prompt, user_prompt)
    provider = (ai_provider or "gemini").lower()
    if provider == "gemini":
        return _call_gemini(system_prompt, user_prompt, api_key, ai_model or "gemini-3-flash-preview")
    if provider == "openai":
        return _call_openai(system_prompt, user_prompt, api_key, ai_model or "gpt-4.1")
    if provider == "claude":
        return _call_claude(system_prompt, user_prompt)
    raise ValueError(f"未対応のAIプロバイダー: {ai_provider}")


def _call_gemini(system_prompt: str, user_prompt: str, api_key: str, model: str) -> str:
    try:
        import google.generativeai as genai
    except ImportError as e:
        raise RuntimeError("google-generativeai パッケージが必要です: pip install google-generativeai") from e
    if not api_key:
        raise RuntimeError("Gemini APIキーが未設定です")
    genai.configure(api_key=api_key)
    gmodel = genai.GenerativeModel(model_name=model, system_instruction=system_prompt)
    response = gmodel.generate_content(user_prompt)
    return response.text


def _call_openai(system_prompt: str, user_prompt: str, api_key: str, model: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai パッケージが必要です: pip install openai") from e
    if not api_key:
        raise RuntimeError("OpenAI APIキーが未設定です")
    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content or ""


def _call_claude(system_prompt: str, user_prompt: str) -> str:
    full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=full_prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "claude CLI が見つかりません。npm install -g @anthropic-ai/claude-code"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI error: {result.stderr}")
    return result.stdout


def _parse_json_response(response_text: str) -> dict:
    if not response_text:
        raise ValueError("AIが空のレスポンスを返しました")
    match = re.search(r"\{[\s\S]*\}", response_text)
    if not match:
        raise ValueError("AIが有効なJSONを返しませんでした")
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        raise ValueError(f"AI応答のJSONパースに失敗: {e}") from e


def _parse_timestamp(ts: str) -> float:
    """Parse HH:MM:SS(.mmm) / MM:SS(.mmm) / seconds float."""
    if isinstance(ts, (int, float)):
        return float(ts)
    ts = str(ts).strip()
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(ts)


def _format_hms(seconds: float, compact: bool = False) -> str:
    """Format seconds. compact=True collapses leading 00:."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if compact:
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
    return f"{h:02d}:{m:02d}:{s:02d}"
