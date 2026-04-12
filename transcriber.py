"""Audio transcription using faster-whisper."""

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Add NVIDIA CUDA DLL paths so faster-whisper can find cublas64_12.dll etc.
for _nvidia_dir in Path(os.path.dirname(os.__file__)).glob("site-packages/nvidia/*/bin"):
    os.add_dll_directory(str(_nvidia_dir))
    if str(_nvidia_dir) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(_nvidia_dir) + os.pathsep + os.environ.get("PATH", "")

from faster_whisper import WhisperModel
from tqdm import tqdm

logger = logging.getLogger("clip-extractor")

# Keep model reference alive to prevent CUDA segfault on garbage collection
_model_cache = {"model": None, "model_size": None}


@dataclass
class Segment:
    start: float
    end: float
    text: str


def extract_audio(video_path: Path, audio_path: Path) -> None:
    """Extract audio from video file using FFmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_path),
    ]
    subprocess.run(cmd, capture_output=True, encoding="utf-8", check=True)


def transcribe(video_path: Path, model_size: str = "large-v3", language: str = "ja") -> list[Segment]:
    """Transcribe video audio and return timestamped segments."""
    audio_path = video_path.parent / f"{video_path.stem}_audio.wav"

    logger.info(f"[Transcribe] Extracting audio: {video_path} -> {audio_path}")
    try:
        extract_audio(video_path, audio_path)
    except subprocess.CalledProcessError as e:
        logger.error(f"[Transcribe] FFmpeg audio extraction failed: {e}")
        if e.stderr:
            logger.error(f"[Transcribe] stderr: {e.stderr[:500]}")
        raise
    logger.info(f"[Transcribe] Audio extracted: {audio_path} ({audio_path.stat().st_size} bytes)")

    # Reuse cached model to avoid CUDA segfault on repeated load/unload
    if _model_cache["model"] is not None and _model_cache["model_size"] == model_size:
        model = _model_cache["model"]
        logger.info(f"[Transcribe] Reusing cached Whisper model ({model_size})")
    else:
        logger.info(f"[Transcribe] Loading Whisper model ({model_size})...")
        try:
            model = WhisperModel(model_size, device="auto", compute_type="auto")
        except RuntimeError as e:
            if "cublas" in str(e).lower() or "cuda" in str(e).lower():
                logger.warning(f"[Transcribe] GPU not available ({e}), falling back to CPU")
                model = WhisperModel(model_size, device="cpu", compute_type="int8")
            else:
                logger.error(f"[Transcribe] Failed to load Whisper model: {e}")
                raise
        except Exception as e:
            logger.error(f"[Transcribe] Failed to load Whisper model: {e}")
            raise
        _model_cache["model"] = model
        _model_cache["model_size"] = model_size
        logger.info("[Transcribe] Model loaded and cached")

    logger.info("[Transcribe] Starting transcription...")
    try:
        raw_segments, info = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=5,
            word_timestamps=True,
            vad_filter=True,
        )

        segments = []
        for seg in tqdm(raw_segments, desc="Processing segments"):
            segments.append(Segment(start=seg.start, end=seg.end, text=seg.text.strip()))
    except Exception as e:
        logger.error(f"[Transcribe] Transcription failed: {e}")
        raise

    # Cleanup temp audio (model is kept in cache to avoid CUDA segfault)
    audio_path.unlink(missing_ok=True)

    logger.info(f"[Transcribe] Complete: {len(segments)} segments, language: {info.language}")
    return segments


def segments_to_text(segments: list[Segment]) -> str:
    """Convert segments to timestamped text for LLM analysis."""
    lines = []
    for seg in segments:
        start_ts = _format_time(seg.start)
        end_ts = _format_time(seg.end)
        lines.append(f"[{start_ts} -> {end_ts}] {seg.text}")
    return "\n".join(lines)


def _format_time(seconds: float) -> str:
    """Format seconds to HH:MM:SS.mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
