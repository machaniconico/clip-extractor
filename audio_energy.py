"""Audio loudness/excitement scoring for highlight selection."""

import logging
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger("clip-extractor")

LEVEL_WEIGHT = 0.7
SPIKE_WEIGHT = 0.3
_AUDIO_SAMPLE_RATE = "16000"
_PCM_FULL_SCALE = 32768.0


@dataclass
class EnergyCurve:
    times: np.ndarray
    db: np.ndarray
    hop_sec: float


def compute_energy_curve(
    video_path: Path,
    win_sec: float = 0.5,
    hop_sec: float = 0.5,
) -> EnergyCurve | None:
    """Extract mono PCM audio and compute a per-window dBFS curve."""
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", _AUDIO_SAMPLE_RATE, "-ac", "1",
            str(tmp_path),
        ]
        subprocess.run(cmd, capture_output=True, encoding="utf-8", check=True)

        with wave.open(str(tmp_path), "rb") as wav:
            sample_rate = wav.getframerate()
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            raw = wav.readframes(wav.getnframes())

        if sample_width != 2:
            raise ValueError(f"expected 16-bit PCM WAV, got {sample_width * 8}-bit")

        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)

        win_samples = max(1, int(round(float(win_sec) * sample_rate)))
        hop_samples = max(1, int(round(float(hop_sec) * sample_rate)))
        if samples.size < win_samples:
            return EnergyCurve(
                times=np.array([], dtype=float),
                db=np.array([], dtype=float),
                hop_sec=float(hop_sec),
            )

        starts = np.arange(0, samples.size - win_samples + 1, hop_samples, dtype=int)
        rms = np.empty(starts.size, dtype=float)
        for i, start in enumerate(starts):
            window = samples[start:start + win_samples]
            rms[i] = float(np.sqrt(np.mean(window * window)))

        rms = np.maximum(rms, np.finfo(float).tiny)
        db = 20.0 * np.log10(rms / _PCM_FULL_SCALE)
        times = starts.astype(float) / float(sample_rate)
        return EnergyCurve(times=times, db=db.astype(float), hop_sec=float(hop_sec))
    except Exception:
        return None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def excitement_scores(
    curve: EnergyCurve,
    level_weight: float = LEVEL_WEIGHT,
    spike_weight: float = SPIKE_WEIGHT,
) -> np.ndarray:
    """Return 0..1 excitement scores from loudness level and sudden onsets."""
    db = np.asarray(curve.db, dtype=float)
    if db.size == 0:
        return np.array([], dtype=float)

    level_norm = _percentile_norm(db)

    lookback = max(1, int(round(3.0 / max(float(curve.hop_sec), 1e-9))))
    positive_delta = np.zeros_like(db, dtype=float)
    for i in range(1, db.size):
        start = max(0, i - lookback)
        baseline = float(np.median(db[start:i]))
        positive_delta[i] = max(0.0, float(db[i] - baseline))

    spike_norm = _percentile_norm(positive_delta)
    score = float(level_weight) * level_norm + float(spike_weight) * spike_norm
    return np.clip(score, 0.0, 1.0)


def clip_audio_score(
    curve: EnergyCurve,
    scores: np.ndarray,
    start_sec: float,
    end_sec: float,
) -> float:
    """Peak-biased aggregate score for one highlight time range."""
    times = np.asarray(curve.times, dtype=float)
    score_arr = np.asarray(scores, dtype=float)
    n = min(times.size, score_arr.size)
    if n == 0 or float(end_sec) <= float(start_sec):
        return 0.0

    times = times[:n]
    score_arr = score_arr[:n]
    mask = (times >= float(start_sec)) & (times < float(end_sec))
    if not np.any(mask):
        return 0.0

    selected = score_arr[mask]
    value = 0.5 * float(np.mean(selected)) + 0.5 * float(np.max(selected))
    return float(np.clip(value, 0.0, 1.0))


def fuse_audio_energy(
    video_path: Path,
    highlights: list[dict],
    alpha: float = 0.35,
    snap: bool = False,
    snap_window: float = 2.0,
    min_duration=None,
    max_duration=None,
) -> list[dict]:
    """Fuse semantic rank with audio excitement, optionally snapping boundaries."""
    try:
        curve = compute_energy_curve(video_path)
        if curve is None:
            logger.warning("Audio energy fusion skipped: failed to compute energy curve")
            return highlights

        scores = excitement_scores(curve)
        if scores.size == 0 or not highlights:
            return highlights

        alpha = float(np.clip(float(alpha), 0.0, 1.0))
        count = len(highlights)
        updates: list[tuple[dict, dict]] = []
        for rank, highlight in enumerate(highlights, start=1):
            item = dict(highlight)
            start_sec = float(item.get("start_sec", 0.0))
            end_sec = float(item.get("end_sec", start_sec))
            audio_score = clip_audio_score(curve, scores, start_sec, end_sec)
            semantic_score = 1.0 if count == 1 else float((count - rank) / (count - 1))
            combined_score = (1.0 - alpha) * semantic_score + alpha * audio_score

            item["audio_score"] = float(np.clip(audio_score, 0.0, 1.0))
            item["combined_score"] = float(np.clip(combined_score, 0.0, 1.0))
            if snap:
                _snap_highlight(
                    item,
                    curve,
                    scores,
                    snap_window=float(snap_window),
                    min_duration=min_duration,
                    max_duration=max_duration,
                )
            updates.append((highlight, item))

        for original, item in updates:
            original.update(item)

        return sorted(highlights, key=lambda h: h["combined_score"], reverse=True)
    except Exception as exc:
        logger.warning("Audio energy fusion skipped: %s", exc, exc_info=True)
        return highlights


def _percentile_norm(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return np.array([], dtype=float)

    lo, hi = np.percentile(values, [10, 95])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.zeros_like(values, dtype=float)
    return np.clip((values - lo) / (hi - lo), 0.0, 1.0)


def _snap_highlight(
    highlight: dict,
    curve: EnergyCurve,
    scores: np.ndarray,
    *,
    snap_window: float,
    min_duration,
    max_duration,
) -> None:
    original_start = float(highlight.get("start_sec", 0.0))
    original_end = float(highlight.get("end_sec", original_start))
    if original_end <= original_start:
        return

    min_start = max(0.0, original_start - snap_window)
    max_start = max(0.0, original_start + snap_window)
    min_end = max(0.0, original_end - snap_window)
    max_end = max(0.0, original_end + snap_window)

    start = _nearest_peak_time(curve, scores, original_start, snap_window)
    end = _nearest_peak_time(curve, scores, original_end, snap_window)
    start = float(np.clip(start, min_start, max_start))
    end = float(np.clip(end, min_end, max_end))

    min_d = float(min_duration) if min_duration is not None else None
    max_d = float(max_duration) if max_duration is not None else None

    if min_d is not None:
        need = min_d - (end - start)
        if need > 0:
            grow_end = min(need, max_end - end)
            end += grow_end
            need -= grow_end
            if need > 0:
                shrink_start = min(need, start - min_start)
                start -= shrink_start

    if max_d is not None:
        excess = (end - start) - max_d
        if excess > 0:
            shrink_end = min(excess, end - min_end)
            end -= shrink_end
            excess -= shrink_end
            if excess > 0:
                grow_start = min(excess, max_start - start)
                start += grow_start

    duration = end - start
    if min_d is not None and duration < min_d:
        end = start + min_d
    if max_d is not None and end - start > max_d:
        end = start + max_d
    if end <= start:
        end = start + max(0.001, min_d or float(curve.hop_sec) or 0.001)

    highlight["start_sec"] = float(max(0.0, start))
    highlight["end_sec"] = float(max(highlight["start_sec"] + 0.001, end))
    highlight["duration"] = float(highlight["end_sec"] - highlight["start_sec"])
    highlight["start"] = _format_timestamp(highlight["start_sec"])
    highlight["end"] = _format_timestamp(highlight["end_sec"])


def _nearest_peak_time(
    curve: EnergyCurve,
    scores: np.ndarray,
    center_sec: float,
    snap_window: float,
) -> float:
    times = np.asarray(curve.times, dtype=float)
    score_arr = np.asarray(scores, dtype=float)
    n = min(times.size, score_arr.size)
    if n == 0:
        return float(center_sec)

    times = times[:n]
    score_arr = score_arr[:n]
    mask = (times >= center_sec - snap_window) & (times <= center_sec + snap_window)
    if not np.any(mask):
        return float(center_sec)

    local_times = times[mask]
    local_scores = score_arr[mask]
    best = np.flatnonzero(local_scores == np.max(local_scores))
    nearest = best[np.argmin(np.abs(local_times[best] - center_sec))]
    return float(local_times[nearest])


def _format_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    total_sec, ms = divmod(total_ms, 1000)
    minutes_total, sec = divmod(total_sec, 60)
    hours, minutes = divmod(minutes_total, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}.{ms:03d}"
