"""Content-aware sound-effect event detection and matching.

The first pass intentionally uses signals already available in the clipping
pipeline: timestamped transcript words, the clip's audio-energy peaks, and
descriptive filenames in the user's SE folder.  It stays deterministic so a
render can be reproduced without an external model call.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping, Sequence

from se_auto import normalise_se_usage


MIN_EVENT_GAP_SECONDS = 1.25
DEFAULT_MAX_EVENTS_PER_CLIP = 3
MAX_EVENTS_PER_CLIP = 8


@dataclass(frozen=True)
class SeContentEvent:
    """One content event expressed on the clip-relative timeline."""

    event_id: str
    category: str
    cue_seconds: float
    duration_seconds: float
    confidence: float
    intensity: float
    evidence: str
    source: str
    text: str = ""

    def to_manifest(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "category": self.category,
            "cue_seconds": self.cue_seconds,
            "confidence": self.confidence,
            "intensity": self.intensity,
            "evidence": self.evidence,
            "source": self.source,
            "text": self.text,
        }


@dataclass(frozen=True)
class PlannedSeCue:
    """One matched SE asset and its clip-relative playback position."""

    asset: Any
    event: SeContentEvent
    cue_seconds: float

    def to_manifest(self) -> dict[str, Any]:
        return {
            **self.event.to_manifest(),
            "cue_seconds": self.cue_seconds,
            "asset_id": str(getattr(self.asset, "id", "")),
            "asset_filename": str(
                getattr(
                    self.asset,
                    "filename",
                    Path(str(getattr(self.asset, "path", ""))).name,
                )
            ),
        }


_EVENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "surprise",
        (
            "うわ",
            "えっ",
            "ええっ",
            "えーっ",
            "まじ",
            "本当",
            "びっくり",
            "驚",
            "やば",
            "ヤバ",
            "なんと",
        ),
    ),
    (
        "laugh",
        (
            "笑",
            "www",
            "ww",
            "ワロ",
            "おもしろ",
            "面白",
            "爆笑",
            "草",
        ),
    ),
    (
        "success",
        (
            "やった",
            "成功",
            "勝った",
            "勝利",
            "おめでとう",
            "おめでと",
            "すごい",
            "すげえ",
        ),
    ),
    (
        "warning",
        (
            "危ない",
            "危ね",
            "注意",
            "警告",
            "やめて",
            "ダメ",
            "まずい",
            "失敗",
        ),
    ),
)

_ASSET_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "impact",
        (
            "バーン",
            "爆発",
            "衝撃",
            "インパクト",
            "ドン",
            "パンチ",
            "ビシッ",
            "斬る",
            "打ち合う",
            "impact",
            "hit",
            "punch",
            "bang",
        ),
    ),
    (
        "surprise",
        (
            "悲鳴",
            "驚",
            "うわ",
            "恐怖",
            "ホラー",
            "目が点",
            "ショック",
            "scream",
            "horror",
        ),
    ),
    (
        "laugh",
        (
            "間抜け",
            "パフ",
            "ズコー",
            "寒いギャグ",
            "コミカル",
            "comedy",
            "boing",
        ),
    ),
    (
        "success",
        (
            "決定",
            "レベルアップ",
            "ファンファーレ",
            "歓声",
            "拍手",
            "ゴング",
            "success",
            "level",
            "fanfare",
        ),
    ),
    (
        "warning",
        (
            "警報",
            "クラクション",
            "ブザー",
            "ピー音",
            "チャイム",
            "warning",
            "alarm",
        ),
    ),
    (
        "movement",
        (
            "高速",
            "ダッシュ",
            "移動",
            "逃げる",
            "縮む",
            "伸びる",
            "風",
            "ピューン",
            "whoosh",
            "swoosh",
        ),
    ),
)

_CATEGORY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "surprise": ("surprise", "impact", "laugh", "general"),
    "laugh": ("laugh", "surprise", "impact", "general"),
    "success": ("success", "impact", "general"),
    "warning": ("warning", "impact", "surprise", "general"),
    "impact": ("impact", "movement", "general"),
    "movement": ("movement", "impact", "general"),
    "general": ("general",),
}


def normalise_max_events(value: Any, default: int = DEFAULT_MAX_EVENTS_PER_CLIP) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = int(default)
    return max(1, min(MAX_EVENTS_PER_CLIP, count))


def build_se_content_events(
    highlight: Mapping[str, Any],
    transcript_segments: Sequence[Any] | None = None,
    *,
    clip_path: str | Path | None = None,
    max_events: int = DEFAULT_MAX_EVENTS_PER_CLIP * 2,
) -> tuple[SeContentEvent, ...]:
    """Detect transcript and audio events within one highlight."""

    start = _finite_float(highlight.get("start_sec"), 0.0)
    end = _finite_float(highlight.get("end_sec"), start)
    duration = max(0.001, end - start)
    events: list[SeContentEvent] = []

    for word_start, word_end, text in _iter_timed_text(transcript_segments or ()):
        if word_end <= start or word_start >= end:
            continue
        category = _match_event_category(text)
        if category is None:
            continue
        cue = _clamp(word_start - start, 0.0, max(0.0, duration - 0.05))
        intensity = 0.72 if _contains_emphasis(text) else 0.58
        confidence = 0.84 if word_end > word_start else 0.70
        event_id = _event_id(category, cue, text, "transcript")
        events.append(
            SeContentEvent(
                event_id=event_id,
                category=category,
                cue_seconds=cue,
                duration_seconds=duration,
                confidence=confidence,
                intensity=intensity,
                evidence=f"transcript_keyword:{text}",
                source="transcript",
                text=str(text),
            )
        )

    title_reason = " ".join(
        str(highlight.get(key) or "") for key in ("title", "reason")
    ).strip()
    category = _match_event_category(title_reason)
    if category is not None and not events:
        cue = _clamp(duration * 0.35, 0.0, max(0.0, duration - 0.05))
        events.append(
            SeContentEvent(
                event_id=_event_id(category, cue, title_reason, "highlight"),
                category=category,
                cue_seconds=cue,
                duration_seconds=duration,
                confidence=0.58,
                intensity=0.52,
                evidence=f"highlight_text:{title_reason}",
                source="highlight",
                text=title_reason,
            )
        )

    events.extend(_audio_peak_events(clip_path, duration, max_events=max_events))
    return _deduplicate_events(events, max_events=normalise_max_events(max_events, default=6))


def plan_se_cues(
    assets: Sequence[Any],
    events: Sequence[SeContentEvent],
    usage_percent: Any,
    *,
    max_events_per_clip: int = DEFAULT_MAX_EVENTS_PER_CLIP,
    cue_offset_seconds: float = 0.0,
) -> tuple[PlannedSeCue, ...]:
    """Select event density and match each selected event to an SE asset."""

    if not assets or not events:
        return ()
    usage = normalise_se_usage(usage_percent, default=0.0)
    if usage <= 0:
        return ()

    limit = normalise_max_events(max_events_per_clip)
    target = max(1, int(math.ceil(len(events) * usage / 100.0)))
    target = min(limit, len(events), target)
    ranked = sorted(
        events,
        key=lambda event: (-event.confidence, _stable_key(event.event_id)),
    )[:target]

    used_assets: set[str] = set()
    plans: list[PlannedSeCue] = []
    for event in sorted(ranked, key=lambda item: item.cue_seconds):
        asset = _match_asset(assets, event.category, event.event_id, used_assets)
        if asset is None:
            continue
        asset_id = str(getattr(asset, "id", getattr(asset, "path", "")))
        used_assets.add(asset_id)
        cue = _clamp(
            event.cue_seconds + max(0.0, _finite_float(cue_offset_seconds, 0.0)),
            0.0,
            max(0.0, event.duration_seconds - 0.05),
        )
        plans.append(PlannedSeCue(asset=asset, event=event, cue_seconds=cue))
    return tuple(plans)


def _iter_timed_text(segments: Sequence[Any]):
    for segment in segments:
        segment_start = _finite_float(_field(segment, "start"), 0.0)
        segment_end = _finite_float(_field(segment, "end"), segment_start)
        words = _field(segment, "words", ()) or ()
        emitted = False
        for word in words:
            text = str(_field(word, "text", "") or "").strip()
            if not text:
                continue
            emitted = True
            yield (
                _finite_float(_field(word, "start"), segment_start),
                _finite_float(_field(word, "end"), segment_end),
                text,
            )
        if not emitted:
            text = str(_field(segment, "text", "") or "").strip()
            if text:
                yield segment_start, segment_end, text


def _audio_peak_events(
    clip_path: str | Path | None,
    duration: float,
    *,
    max_events: int,
) -> tuple[SeContentEvent, ...]:
    if not clip_path:
        return ()
    try:
        from audio_energy import compute_energy_curve, excitement_scores

        curve = compute_energy_curve(Path(clip_path))
        if curve is None:
            return ()
        scores = excitement_scores(curve)
        candidates: list[tuple[float, float]] = []
        for index, score in enumerate(scores):
            value = float(score)
            if value < 0.72:
                continue
            previous = float(scores[index - 1]) if index else -1.0
            following = float(scores[index + 1]) if index + 1 < len(scores) else -1.0
            if value < previous or value < following:
                continue
            cue = _clamp(float(curve.times[index]), 0.0, max(0.0, duration - 0.05))
            candidates.append((value, cue))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        events: list[SeContentEvent] = []
        for score, cue in candidates[: max(1, normalise_max_events(max_events, default=6))]:
            if any(abs(cue - event.cue_seconds) < MIN_EVENT_GAP_SECONDS for event in events):
                continue
            events.append(
                SeContentEvent(
                    event_id=_event_id("impact", cue, f"{score:.3f}", "audio"),
                    category="impact",
                    cue_seconds=cue,
                    duration_seconds=duration,
                    confidence=_clamp(0.52 + score * 0.35, 0.0, 1.0),
                    intensity=_clamp(score, 0.0, 1.0),
                    evidence=f"audio_peak:{score:.3f}",
                    source="audio_peak",
                )
            )
        return tuple(events)
    except Exception:
        return ()


def _deduplicate_events(
    events: Sequence[SeContentEvent],
    *,
    max_events: int,
) -> tuple[SeContentEvent, ...]:
    chosen: list[SeContentEvent] = []
    ranked = sorted(events, key=lambda event: (-event.confidence, event.cue_seconds))
    for event in ranked:
        existing_index = next(
            (
                index
                for index, current in enumerate(chosen)
                if abs(current.cue_seconds - event.cue_seconds) < MIN_EVENT_GAP_SECONDS
            ),
            None,
        )
        if existing_index is None:
            chosen.append(event)
        elif event.confidence > chosen[existing_index].confidence:
            chosen[existing_index] = event
    return tuple(sorted(chosen, key=lambda event: event.cue_seconds)[:max_events])


def _match_asset(
    assets: Sequence[Any],
    category: str,
    event_id: str,
    used_assets: set[str],
) -> Any | None:
    tagged: dict[str, list[Any]] = {}
    for asset in assets:
        tagged.setdefault(_asset_category(asset), []).append(asset)
    candidates: list[Any] = []
    for fallback in _CATEGORY_FALLBACKS.get(category, (category, "general")):
        candidates = tagged.get(fallback, [])
        if candidates:
            break
    if not candidates:
        candidates = list(assets)
    unused = [
        asset
        for asset in candidates
        if str(getattr(asset, "id", getattr(asset, "path", ""))) not in used_assets
    ]
    candidates = unused or candidates
    if not candidates:
        return None
    index = int(_stable_key(f"{event_id}:{category}"), 16) % len(candidates)
    return candidates[index]


def _asset_category(asset: Any) -> str:
    filename = str(
        getattr(asset, "filename", Path(str(getattr(asset, "path", ""))).name)
    )
    normalised = _normalise_text(filename)
    for category, keywords in _ASSET_KEYWORDS:
        if any(_normalise_text(keyword) in normalised for keyword in keywords):
            return category
    return "general"


def _match_event_category(text: Any) -> str | None:
    normalised = _normalise_text(text)
    if not normalised:
        return None
    for category, keywords in _EVENT_KEYWORDS:
        if any(_normalise_text(keyword) in normalised for keyword in keywords):
            return category
    return None


def _contains_emphasis(text: Any) -> bool:
    value = str(text or "")
    return any(mark in value for mark in ("!", "！", "?", "？"))


def _event_id(category: str, cue: float, text: str, source: str) -> str:
    return hashlib.sha256(
        f"{source}|{category}|{cue:.3f}|{text}".encode("utf-8")
    ).hexdigest()[:16]


def _stable_key(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _normalise_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", "", text)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _finite_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, float(value)))
