"""Deterministic automatic sound-effect assignment for generated clips.

The planner deliberately stays independent from FFmpeg.  It turns a scanned
SE folder into one optional assignment per highlight, so the delivery layer can
reuse the existing single-SE mix path for each clip while keeping output
reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence


SE_USAGE_MIN = 0.0
SE_USAGE_MAX = 100.0
DEFAULT_SE_USAGE_PERCENT = 40.0


@dataclass(frozen=True)
class SeClipAssignment:
    """One deterministic SE decision for one highlight."""

    asset: Any | None = None
    cue_seconds: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.asset is not None


def normalise_se_usage(value: Any, default: float = DEFAULT_SE_USAGE_PERCENT) -> float:
    """Clamp a UI/JSON value to the supported 0-100 SE usage range."""

    try:
        usage = float(value)
    except (TypeError, ValueError):
        usage = float(default)
    if not math.isfinite(usage):
        usage = float(default)
    return min(SE_USAGE_MAX, max(SE_USAGE_MIN, usage))


def plan_se_assignments(
    assets: Sequence[Any],
    highlights: Sequence[Mapping[str, Any]],
    usage_percent: Any,
    *,
    cue_seconds: Any = 0.0,
) -> tuple[SeClipAssignment, ...]:
    """Build a reproducible one-SE-per-clip plan.

    ``usage_percent`` controls how many clips receive an SE, not its loudness.
    The selected clip indexes are ranked from a stable hash of highlight data,
    which avoids always decorating the first clips when the slider changes.
    Assets are assigned in sorted-folder order and cycle when there are more
    selected clips than files.
    """

    count = len(highlights)
    if count == 0 or not assets:
        return tuple(SeClipAssignment() for _ in highlights)

    usage = normalise_se_usage(usage_percent, default=0.0)
    if usage <= SE_USAGE_MIN:
        return tuple(SeClipAssignment() for _ in highlights)

    try:
        cue = float(cue_seconds)
    except (TypeError, ValueError):
        cue = 0.0
    if not math.isfinite(cue):
        cue = 0.0
    cue = max(0.0, cue)

    target_count = max(1, min(count, int(math.ceil(count * usage / 100.0))))
    ranked_indexes = sorted(
        range(count),
        key=lambda index: _highlight_seed(highlights[index], index),
    )
    selected_indexes = set(ranked_indexes[:target_count])

    return tuple(
        SeClipAssignment(
            asset=assets[index % len(assets)] if index in selected_indexes else None,
            cue_seconds=cue,
        )
        for index in range(count)
    )


def _highlight_seed(highlight: Mapping[str, Any], index: int) -> str:
    """Return a stable sort key without depending on mapping insertion order."""

    payload = {
        "index": index,
        "title": str(highlight.get("title") or ""),
        "start_sec": _stable_number(highlight.get("start_sec")),
        "end_sec": _stable_number(highlight.get("end_sec")),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_number(value: Any) -> float | str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    return number if math.isfinite(number) else ""
