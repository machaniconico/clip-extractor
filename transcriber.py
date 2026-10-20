"""Audio transcription using faster-whisper."""

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Add NVIDIA CUDA DLL paths so faster-whisper can find cublas64_12.dll etc.
# The previous implementation globbed from os.__file__, which points to stdlib
# (not site-packages) under venvs / embeddable Python / Anaconda — the glob
# silently returned zero matches and the GPU was falsely marked unavailable.
# importlib.util.find_spec resolves the real site-packages location wherever
# `nvidia` was actually installed (base install or venv).
import importlib.util as _importlib_util
_nvidia_spec = _importlib_util.find_spec("nvidia")
if _nvidia_spec and _nvidia_spec.submodule_search_locations:
    _nvidia_root = Path(_nvidia_spec.submodule_search_locations[0])
    for _nvidia_dir in _nvidia_root.glob("*/bin"):
        if _nvidia_dir.is_dir():
            try:
                os.add_dll_directory(str(_nvidia_dir))
            except (AttributeError, OSError):
                # add_dll_directory is Windows-only (Python 3.8+); other
                # platforms rely on LD_LIBRARY_PATH instead.
                pass
            if str(_nvidia_dir) not in os.environ.get("PATH", ""):
                os.environ["PATH"] = str(_nvidia_dir) + os.pathsep + os.environ.get("PATH", "")

from tqdm import tqdm

logger = logging.getLogger("clip-extractor")

# Keep model reference alive to prevent CUDA segfault on garbage collection
_model_cache = {"model": None, "model_size": None}


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


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
        from faster_whisper import WhisperModel
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
            segments.append(Segment(
                start=seg.start,
                end=seg.end,
                text=seg.text.strip(),
                words=[Word(w.start, w.end, w.word) for w in (seg.words or [])],
            ))
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


def segments_to_llm_text(segments: list[Segment]) -> str:
    """Compact transcript for LLM analysis: ``[H:MM:SS] text`` per segment.

    Only the start time (floored to whole seconds) is kept, and consecutive
    duplicate lines are dropped, to keep long recordings within token quotas.
    """
    lines = []
    previous_text = None
    for seg in segments:
        text = seg.text.strip()
        if text == previous_text:
            continue
        previous_text = text
        total = int(seg.start)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        lines.append(f"[{h}:{m:02d}:{s:02d}] {text}")
    return "\n".join(lines)


def _format_time(seconds: float) -> str:
    """Format seconds to HH:MM:SS.mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


TRANSCRIPT_SEGMENTS_FILENAME = "transcript_segments.json"
_TRANSCRIPT_LINE_RE = re.compile(
    r"^\[(\d+):(\d{2}):(\d{2}(?:\.\d+)?) -> (\d+):(\d{2}):(\d{2}(?:\.\d+)?)\]\s?(.*)$"
)


def parse_transcript_text(text: str) -> list[Segment]:
    """Parse ``[HH:MM:SS.mmm -> HH:MM:SS.mmm] text`` lines (see segments_to_text).

    Blank and malformed lines are skipped. Word timings are not stored in the
    text format, so the returned segments have no words.
    """
    segments = []
    for line in str(text or "").splitlines():
        m = _TRANSCRIPT_LINE_RE.match(line.strip())
        if not m:
            continue
        start = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        end = int(m[4]) * 3600 + int(m[5]) * 60 + float(m[6])
        segments.append(Segment(start=start, end=end, text=m[7].strip()))
    if not segments:
        raise ValueError(
            "文字起こしファイルから有効な行を読み取れませんでした"
            "（[HH:MM:SS.mmm -> HH:MM:SS.mmm] 形式の行がありません）"
        )
    return segments


def source_fingerprint(path) -> dict:
    """Identify an input media file by name, size and mtime."""
    p = Path(path)
    st = p.stat()
    return {"name": p.name, "size": st.st_size, "mtime": st.st_mtime}


def write_transcript_segments(
    path: Path,
    segments: list[Segment],
    source_path,
    language: str,
    whisper_model: str,
) -> None:
    """Persist segments (with word timings) atomically for later reuse."""
    try:
        source = source_fingerprint(source_path)
    except OSError:
        source = None
    payload = {
        "version": 1,
        "source": source,
        "language": language,
        "whisper_model": whisper_model,
        "segments": [
            {
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "words": [[w.start, w.end, w.text] for w in seg.words],
            }
            for seg in segments
        ],
    }
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_transcript_file(path) -> tuple[list[Segment], dict | None]:
    """Load transcript_segments.json or transcript.txt.

    Returns ``(segments, source_fingerprint_or_None)``; the fingerprint is None
    for the text format (it cannot be verified against the input).
    """
    p = Path(path)
    if p.suffix.lower() != ".json":
        return parse_transcript_text(p.read_text(encoding="utf-8")), None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        segments = [
            Segment(
                start=float(item["start"]),
                end=float(item["end"]),
                text=str(item["text"]),
                words=[Word(float(w[0]), float(w[1]), str(w[2])) for w in item.get("words") or []],
            )
            for item in data["segments"]
        ]
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ValueError(f"文字起こしJSONを読み取れませんでした: {p.name}") from exc
    if not segments:
        raise ValueError("文字起こしJSONにセグメントがありません")
    source = data.get("source")
    return segments, source if isinstance(source, dict) else None


def load_transcript_meta(path) -> tuple[str | None, str | None]:
    """Return ``(whisper_model, language)`` stored in a transcript JSON, else Nones."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        model, lang = data.get("whisper_model"), data.get("language")
    except (OSError, ValueError, AttributeError):
        return None, None
    return (
        model if isinstance(model, str) else None,
        lang if isinstance(lang, str) else None,
    )


def _is_auto_language(language) -> bool:
    return language is None or str(language).strip().lower() in ("", "auto")


def find_reusable_transcript(
    base_dir, input_path, whisper_model=None, language=None
) -> Path | None:
    """Newest ``output_*/transcript_segments.json`` made from the same input file.

    Also requires the same Whisper model and language; an auto/None requested
    language accepts any stored language.
    """
    try:
        wanted = source_fingerprint(input_path)
    except OSError:
        return None
    found = []
    for candidate in Path(base_dir).glob(f"output_*/{TRANSCRIPT_SEGMENTS_FILENAME}"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            source = data.get("source") or {}
            if (
                source.get("name") == wanted["name"]
                and source.get("size") == wanted["size"]
                and source.get("mtime") == wanted["mtime"]
                and data.get("segments")
                and (whisper_model is None or data.get("whisper_model") == whisper_model)
                and (
                    _is_auto_language(language)
                    or data.get("language") == language
                )
            ):
                found.append((candidate.stat().st_mtime, candidate))
        except (OSError, ValueError, AttributeError):
            continue
    return max(found)[1] if found else None
