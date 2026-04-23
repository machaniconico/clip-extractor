"""Transcript cache keyed by YouTube video ID.

Avoids re-running faster-whisper when the same video is processed multiple times
(e.g. first for timestamp generation, then for clip extraction).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from transcriber import Segment, transcribe

logger = logging.getLogger("clip-extractor")

_YOUTUBE_ID_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtube\.com/live/|youtube\.com/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})"),
    re.compile(r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})"),
]


def get_cache_dir() -> Path:
    """Return the transcript cache directory, creating it if needed."""
    cache_dir = Path.home() / ".clip-extractor" / "transcript-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def extract_video_id(url: str) -> Optional[str]:
    """Extract the 11-character YouTube video ID from a URL. Returns None otherwise."""
    if not url:
        return None
    for pattern in _YOUTUBE_ID_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def cache_key_for_url(url: str) -> str:
    """Return a stable cache key for a URL.

    For YouTube URLs, the 11-char video ID is used directly so the same clip
    resolves across different URL shapes (watch, youtu.be, shorts, live).
    For other URLs, a short SHA256 hash is used.
    """
    video_id = extract_video_id(url or "")
    if video_id:
        return video_id
    digest = hashlib.sha256((url or "").encode("utf-8")).hexdigest()
    return f"url_{digest[:16]}"


def _cache_file(key: str, model: str, language: str) -> Path:
    safe_model = re.sub(r"[^A-Za-z0-9._-]", "_", model)
    safe_lang = re.sub(r"[^A-Za-z0-9._-]", "_", language)
    return get_cache_dir() / f"{key}__{safe_model}__{safe_lang}.json"


def load_cached(key: str, model: str, language: str) -> tuple[list[Segment], bool]:
    """Load cached segments. Returns (segments, hit)."""
    path = _cache_file(key, model, language)
    if not path.exists():
        return [], False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        segments = [
            Segment(start=float(s["start"]), end=float(s["end"]), text=str(s["text"]))
            for s in raw.get("segments", [])
        ]
        logger.info(f"[TranscriptCache] Hit: {path.name} ({len(segments)} segments)")
        return segments, True
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        logger.warning(f"[TranscriptCache] Corrupt cache file {path.name}: {e}; ignoring")
        return [], False


def save_cached(key: str, model: str, language: str, segments: list[Segment]) -> Path:
    """Persist segments to cache. Returns the cache file path."""
    path = _cache_file(key, model, language)
    payload = {
        "key": key,
        "model": model,
        "language": language,
        "segments": [asdict(s) for s in segments],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    logger.info(f"[TranscriptCache] Saved: {path.name} ({len(segments)} segments)")
    return path


def transcribe_with_cache(
    video_path: Path,
    model_size: str = "large-v3",
    language: str = "ja",
    video_url: str = "",
) -> list[Segment]:
    """Transcribe, using the cache when a video URL is provided and hit."""
    key = cache_key_for_url(video_url) if video_url else None

    if key:
        cached, hit = load_cached(key, model_size, language)
        if hit:
            return cached

    segments = transcribe(video_path, model_size, language)

    if key:
        save_cached(key, model_size, language, segments)

    return segments


def clear_cache() -> int:
    """Delete all cache entries. Returns the number of files removed."""
    removed = 0
    for p in get_cache_dir().glob("*.json"):
        p.unlink()
        removed += 1
    return removed
