"""Post-process rendered clips with editable BGM and sound-effect audio.

The clip renderer remains responsible for producing a clean MP4 containing the
original dialogue.  This module adds audio in a separate, transactional pass so
that a failed mix never destroys the clean render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Mapping, Sequence


STEM_SAMPLE_RATE = 48_000
STEM_CHANNELS = 2
STEM_CODEC = "pcm_s16le"
MANIFEST_SCHEMA_VERSION = 1
AAC_TRUE_PEAK_LIMIT_DB = -1.0
AAC_TRUE_PEAK_TARGET_DB = -1.5
AAC_TRUE_PEAK_OVERSAMPLE_RATE = STEM_SAMPLE_RATE * 4
AAC_PEAK_MAX_ATTEMPTS = 3
AAC_SILENCE_FLOOR_DB = -120.0
_PEAK_LEVEL_DB_PATTERN = re.compile(
    r"Peak level dB:\s*(-?inf|[-+]?(?:\d+(?:\.\d*)?|\.\d+))",
    re.IGNORECASE,
)


class AudioDeliveryMode(str, Enum):
    """Files retained after audio post-processing."""

    SEPARATE = "separate"
    MIXED = "mixed"
    BOTH = "both"


@dataclass(frozen=True)
class AudioMixSettings:
    """Validated settings shared by one or more rendered clips."""

    delivery_mode: AudioDeliveryMode | str = AudioDeliveryMode.BOTH
    bgm_gain_db: float = -18.0
    se_gain_db: float = -8.0
    se_cue_seconds: float = 0.0
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    def __post_init__(self) -> None:
        raw_mode = (
            self.delivery_mode.value
            if isinstance(self.delivery_mode, AudioDeliveryMode)
            else str(self.delivery_mode).strip().lower()
        )
        try:
            mode = AudioDeliveryMode(raw_mode)
        except ValueError as exc:
            allowed = ", ".join(mode.value for mode in AudioDeliveryMode)
            raise ValueError(f"delivery_mode must be one of: {allowed}") from exc

        bgm_gain = _finite_number(self.bgm_gain_db, "bgm_gain_db")
        se_gain = _finite_number(self.se_gain_db, "se_gain_db")
        cue = _finite_number(self.se_cue_seconds, "se_cue_seconds")
        if cue < 0:
            raise ValueError("se_cue_seconds must be greater than or equal to 0")
        if not str(self.ffmpeg_bin).strip():
            raise ValueError("ffmpeg_bin must not be empty")
        if not str(self.ffprobe_bin).strip():
            raise ValueError("ffprobe_bin must not be empty")

        object.__setattr__(self, "delivery_mode", mode)
        object.__setattr__(self, "bgm_gain_db", bgm_gain)
        object.__setattr__(self, "se_gain_db", se_gain)
        object.__setattr__(self, "se_cue_seconds", cue)
        object.__setattr__(self, "ffmpeg_bin", str(self.ffmpeg_bin))
        object.__setattr__(self, "ffprobe_bin", str(self.ffprobe_bin))


@dataclass(frozen=True)
class AudioOutputResult:
    """Audio artifacts retained for a single clip."""

    deliverables: tuple[Path, ...]
    clean_video: Path | None
    mixed_video: Path | None = None
    bgm_stem: Path | None = None
    se_stem: Path | None = None
    manifest: Path | None = None
    decoded_peak_4x_dbfs: float | None = None
    post_mix_attenuation_db: float = 0.0


@dataclass(frozen=True)
class AudioBatchResult:
    """Results from :func:`process_clip_batch`, in input order."""

    clips: tuple[AudioOutputResult, ...] = field(default_factory=tuple)

    @property
    def deliverables(self) -> tuple[Path, ...]:
        return tuple(path for clip in self.clips for path in clip.deliverables)


def process_clip_audio(
    clean_video: str | os.PathLike[str],
    *,
    duration_seconds: float,
    settings: AudioMixSettings,
    bgm_path: str | os.PathLike[str] | None = None,
    se_path: str | os.PathLike[str] | None = None,
    clip_metadata: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> AudioOutputResult:
    """Create audio deliverables for one already-rendered clean MP4.

    ``mixed`` is intentionally destructive only after every output succeeds: the
    clean input is removed so the directory contains the mixed deliverable alone.
    If neither BGM nor SE is selected, no FFmpeg command runs and the clean input
    is returned unchanged for every delivery mode.
    """

    if not isinstance(settings, AudioMixSettings):
        raise TypeError("settings must be an AudioMixSettings instance")

    clean = _existing_file(clean_video, "clean_video")
    if clean.suffix.lower() != ".mp4":
        raise ValueError("clean_video must be an MP4 file")
    duration = _finite_number(duration_seconds, "duration_seconds")
    if duration <= 0:
        raise ValueError("duration_seconds must be greater than 0")

    bgm = _optional_existing_file(bgm_path, "bgm_path")
    se = _optional_existing_file(se_path, "se_path")
    metadata = _json_mapping(clip_metadata, "clip_metadata")
    source_provenance = _json_mapping(provenance, "provenance")

    base = clean.with_suffix("")
    bgm_target = base.with_name(f"{base.name}_bgm.wav")
    se_target = base.with_name(f"{base.name}_se.wav")
    bgm_final = bgm_target if bgm else None
    se_final = se_target if se else None
    mixed_final = base.with_name(f"{base.name}_mixed.mp4")
    manifest_final = base.with_name(f"{base.name}_audio.json")
    _reject_source_output_collisions(
        sources=(clean, bgm, se),
        outputs=(bgm_target, se_target, mixed_final, manifest_final),
    )

    if bgm is None and se is None:
        with tempfile.TemporaryDirectory(
            prefix=".clip_audio_", dir=str(clean.parent)
        ) as staging_name:
            _commit_artifacts(
                (),
                Path(staging_name),
                remove_after=(bgm_target, se_target, mixed_final, manifest_final),
            )
        return AudioOutputResult(deliverables=(clean,), clean_video=clean)

    mode = settings.delivery_mode
    keep_stems = mode in (AudioDeliveryMode.SEPARATE, AudioDeliveryMode.BOTH)
    make_mixed = mode in (AudioDeliveryMode.MIXED, AudioDeliveryMode.BOTH)

    with tempfile.TemporaryDirectory(
        prefix=".clip_audio_", dir=str(clean.parent)
    ) as staging_name:
        staging = Path(staging_name)
        bgm_staged = staging / "bgm.wav" if bgm else None
        se_staged = staging / "se.wav" if se else None

        if bgm and bgm_staged:
            _run_command(
                _bgm_stem_command(
                    settings.ffmpeg_bin,
                    bgm,
                    bgm_staged,
                    duration,
                    settings.bgm_gain_db,
                )
            )
            _require_generated_file(bgm_staged, "BGM stem")

        if se and se_staged:
            _run_command(
                _se_stem_command(
                    settings.ffmpeg_bin,
                    se,
                    se_staged,
                    duration,
                    settings.se_gain_db,
                    settings.se_cue_seconds,
                )
            )
            _require_generated_file(se_staged, "SE stem")

        mixed_staged: Path | None = None
        decoded_peak_4x_dbfs: float | None = None
        post_mix_attenuation_db = 0.0
        if make_mixed:
            mixed_staged = staging / "mixed.mp4"
            has_dialogue = _probe_has_audio(clean, settings.ffprobe_bin)
            decoded_peak_4x_dbfs, post_mix_attenuation_db = (
                _render_mixed_with_peak_guard(
                    settings.ffmpeg_bin,
                    clean,
                    mixed_staged,
                    duration,
                    bgm_staged,
                    se_staged,
                    has_dialogue=has_dialogue,
                )
            )

        manifest_staged: Path | None = None
        if keep_stems:
            manifest_staged = staging / "audio.json"
            manifest_data = _build_manifest(
                clean=clean,
                duration=duration,
                mode=mode,
                bgm=bgm,
                se=se,
                bgm_final=bgm_final,
                se_final=se_final,
                mixed_final=mixed_final if make_mixed else None,
                settings=settings,
                metadata=metadata,
                provenance=source_provenance,
                decoded_peak_4x_dbfs=decoded_peak_4x_dbfs,
                post_mix_attenuation_db=post_mix_attenuation_db,
            )
            manifest_staged.write_text(
                json.dumps(manifest_data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        staged_outputs: list[tuple[Path, Path]] = []
        if keep_stems:
            if bgm_staged and bgm_final:
                staged_outputs.append((bgm_staged, bgm_final))
            if se_staged and se_final:
                staged_outputs.append((se_staged, se_final))
            if manifest_staged:
                staged_outputs.append((manifest_staged, manifest_final))
        if mixed_staged:
            staged_outputs.append((mixed_staged, mixed_final))

        stale_outputs: list[Path] = []
        if mode is AudioDeliveryMode.SEPARATE:
            stale_outputs.append(mixed_final)
        if mode is AudioDeliveryMode.MIXED:
            stale_outputs.extend((clean, bgm_target, se_target, manifest_final))
        else:
            if bgm is None:
                stale_outputs.append(bgm_target)
            if se is None:
                stale_outputs.append(se_target)

        _commit_artifacts(
            staged_outputs,
            staging,
            remove_after=stale_outputs,
        )

    if mode is AudioDeliveryMode.MIXED:
        return AudioOutputResult(
            deliverables=(mixed_final,),
            clean_video=None,
            mixed_video=mixed_final,
            decoded_peak_4x_dbfs=decoded_peak_4x_dbfs,
            post_mix_attenuation_db=post_mix_attenuation_db,
        )

    deliverables: list[Path] = [clean]
    if bgm_final:
        deliverables.append(bgm_final)
    if se_final:
        deliverables.append(se_final)
    deliverables.append(manifest_final)
    if mode is AudioDeliveryMode.BOTH:
        deliverables.append(mixed_final)
    return AudioOutputResult(
        deliverables=tuple(deliverables),
        clean_video=clean,
        mixed_video=mixed_final if mode is AudioDeliveryMode.BOTH else None,
        bgm_stem=bgm_final,
        se_stem=se_final,
        manifest=manifest_final,
        decoded_peak_4x_dbfs=decoded_peak_4x_dbfs,
        post_mix_attenuation_db=post_mix_attenuation_db,
    )


def process_clip_batch(
    clip_paths: Sequence[str | os.PathLike[str]],
    highlights: Sequence[Mapping[str, Any]],
    *,
    settings: AudioMixSettings,
    bgm_path: str | os.PathLike[str] | None = None,
    se_path: str | os.PathLike[str] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> AudioBatchResult:
    """Apply identical audio selections to rendered clips and their highlights."""

    if len(clip_paths) != len(highlights):
        raise ValueError("clip_paths and highlights must have the same length")
    if not isinstance(settings, AudioMixSettings):
        raise TypeError("settings must be an AudioMixSettings instance")

    durations = tuple(
        _highlight_duration(item, index) for index, item in enumerate(highlights)
    )
    clean_paths, bgm, se = _preflight_batch_paths(clip_paths, bgm_path, se_path)
    for index, highlight in enumerate(highlights):
        _json_mapping(highlight, f"highlights[{index}]")
    _json_mapping(provenance, "provenance")
    results = tuple(
        process_clip_audio(
            clip_path,
            duration_seconds=duration,
            settings=settings,
            bgm_path=bgm,
            se_path=se,
            clip_metadata=highlight,
            provenance=provenance,
        )
        for clip_path, highlight, duration in zip(clean_paths, highlights, durations)
    )
    return AudioBatchResult(clips=results)


def _bgm_stem_command(
    ffmpeg_bin: str,
    source: Path,
    output: Path,
    duration: float,
    gain_db: float,
) -> list[str]:
    end = _format_number(duration)
    audio_filter = (
        "[0:a:0]aresample=48000,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"volume={_format_number(gain_db)}dB,"
        f"atrim=start=0:end={end},asetpts=N/SR/TB,"
        f"apad=whole_dur={end},atrim=start=0:end={end}[aout]"
    )
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(source),
        "-filter_complex",
        audio_filter,
        "-map",
        "[aout]",
        "-vn",
        "-c:a",
        STEM_CODEC,
        "-ar",
        str(STEM_SAMPLE_RATE),
        "-ac",
        str(STEM_CHANNELS),
        "-t",
        end,
        str(output),
    ]


def _se_stem_command(
    ffmpeg_bin: str,
    source: Path,
    output: Path,
    duration: float,
    gain_db: float,
    cue_seconds: float,
) -> list[str]:
    end = _format_number(duration)
    if cue_seconds >= duration:
        return [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={STEM_SAMPLE_RATE}:cl=stereo:d={end}",
            "-vn",
            "-c:a",
            STEM_CODEC,
            "-ar",
            str(STEM_SAMPLE_RATE),
            "-ac",
            str(STEM_CHANNELS),
            "-t",
            end,
            str(output),
        ]

    available = _format_number(max(0.0, duration - cue_seconds))
    delay_samples = round(cue_seconds * STEM_SAMPLE_RATE)
    audio_filter = (
        "[0:a:0]aresample=48000,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"volume={_format_number(gain_db)}dB,"
        f"atrim=start=0:end={available},asetpts=N/SR/TB,"
        f"adelay=delays={delay_samples}S:all=1,"
        f"apad=whole_dur={end},atrim=start=0:end={end}[aout]"
    )
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        audio_filter,
        "-map",
        "[aout]",
        "-vn",
        "-c:a",
        STEM_CODEC,
        "-ar",
        str(STEM_SAMPLE_RATE),
        "-ac",
        str(STEM_CHANNELS),
        "-t",
        end,
        str(output),
    ]


def _mixed_video_command(
    ffmpeg_bin: str,
    clean: Path,
    output: Path,
    duration: float,
    bgm_stem: Path | None,
    se_stem: Path | None,
    *,
    has_dialogue: bool,
    post_mix_attenuation_db: float = 0.0,
) -> list[str]:
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(clean),
    ]
    stem_inputs: list[tuple[str, int]] = []
    next_input = 1
    if bgm_stem:
        command.extend(("-i", str(bgm_stem)))
        stem_inputs.append(("bgm", next_input))
        next_input += 1
    if se_stem:
        command.extend(("-i", str(se_stem)))
        stem_inputs.append(("se", next_input))

    end = _format_number(duration)
    filters: list[str] = []
    mix_labels: list[str] = []
    if has_dialogue:
        filters.append(
            "[0:a:0]aresample=48000,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"atrim=start=0:end={end},asetpts=N/SR/TB[dialogue]"
        )
        mix_labels.append("[dialogue]")

    for label, input_index in stem_inputs:
        filters.append(
            f"[{input_index}:a:0]aresample=48000,"
            "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"atrim=start=0:end={end},asetpts=N/SR/TB[{label}]"
        )
        mix_labels.append(f"[{label}]")

    if len(mix_labels) == 1:
        source_label = mix_labels[0]
        filters.append(
            f"{source_label}alimiter=limit=0.95:level=false:latency=true,"
            f"volume={_format_number(post_mix_attenuation_db)}dB,"
            f"apad=whole_dur={end},"
            f"atrim=start=0:end={end}[aout]"
        )
    else:
        filters.append(
            "".join(mix_labels)
            + f"amix=inputs={len(mix_labels)}:duration=longest:"
            + "dropout_transition=0:normalize=0,"
            + "alimiter=limit=0.95:level=false:latency=true,"
            + f"volume={_format_number(post_mix_attenuation_db)}dB,"
            + f"apad=whole_dur={end},atrim=start=0:end={end}[aout]"
        )

    command.extend(
        (
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(STEM_SAMPLE_RATE),
            "-ac",
            str(STEM_CHANNELS),
            "-t",
            end,
            "-movflags",
            "+faststart",
            str(output),
        )
    )
    return command


def _render_mixed_with_peak_guard(
    ffmpeg_bin: str,
    clean: Path,
    output: Path,
    duration: float,
    bgm_stem: Path | None,
    se_stem: Path | None,
    *,
    has_dialogue: bool,
) -> tuple[float, float]:
    """Encode AAC, measure its decoded 4x peak, and attenuate if necessary."""

    attenuation_db = 0.0
    measured_peak_db = math.inf
    for _attempt in range(AAC_PEAK_MAX_ATTEMPTS):
        _run_command(
            _mixed_video_command(
                ffmpeg_bin,
                clean,
                output,
                duration,
                bgm_stem,
                se_stem,
                has_dialogue=has_dialogue,
                post_mix_attenuation_db=attenuation_db,
            )
        )
        _require_generated_file(output, "mixed MP4")
        measured_peak_db = _measure_decoded_peak_4x_dbfs(output, ffmpeg_bin)
        if measured_peak_db <= AAC_TRUE_PEAK_LIMIT_DB:
            return measured_peak_db, attenuation_db
        attenuation_db += AAC_TRUE_PEAK_TARGET_DB - measured_peak_db

    raise RuntimeError(
        "Encoded AAC peak validation failed: "
        f"{measured_peak_db:.3f} dBFS exceeds {AAC_TRUE_PEAK_LIMIT_DB:.1f} dBFS"
    )


def _measure_decoded_peak_4x_dbfs(path: Path, ffmpeg_bin: str) -> float:
    """Measure decoded sample peaks after 4x resampling as a true-peak guard."""

    result = _run_command(
        [
            ffmpeg_bin,
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "info",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            (f"aresample={AAC_TRUE_PEAK_OVERSAMPLE_RATE},astats=metadata=0:reset=0"),
            "-f",
            "null",
            "-",
        ]
    )
    matches = _PEAK_LEVEL_DB_PATTERN.findall(result.stderr or "")
    if not matches:
        raise RuntimeError("FFmpeg did not report a decoded AAC peak")
    try:
        peaks = [float(value) for value in matches]
    except ValueError as exc:
        raise RuntimeError("FFmpeg reported an invalid decoded AAC peak") from exc
    peak = max(peaks)
    if math.isnan(peak):
        raise RuntimeError("FFmpeg reported a NaN decoded AAC peak")
    if peak == -math.inf:
        return AAC_SILENCE_FLOOR_DB
    if not math.isfinite(peak):
        raise RuntimeError("FFmpeg reported a non-finite decoded AAC peak")
    return peak


def _probe_has_audio(path: Path, ffprobe_bin: str) -> bool:
    result = _run_command(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    return bool((result.stdout or "").strip())


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as exc:
        executable = Path(str(command[0])).name
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"{executable} failed with exit code {exc.returncode}"
        if detail:
            message += f": {detail}"
        raise RuntimeError(message) from exc
    except OSError as exc:
        executable = Path(str(command[0])).name
        raise RuntimeError(f"Could not run {executable}: {exc}") from exc


def _build_manifest(
    *,
    clean: Path,
    duration: float,
    mode: AudioDeliveryMode,
    bgm: Path | None,
    se: Path | None,
    bgm_final: Path | None,
    se_final: Path | None,
    mixed_final: Path | None,
    settings: AudioMixSettings,
    metadata: Mapping[str, Any],
    provenance: Mapping[str, Any],
    decoded_peak_4x_dbfs: float | None,
    post_mix_attenuation_db: float,
) -> dict[str, Any]:
    artifacts = [{"kind": "clean_video", "file": clean.name}]
    if bgm_final:
        artifacts.append({"kind": "bgm_stem", "file": bgm_final.name})
    if se_final:
        artifacts.append({"kind": "se_stem", "file": se_final.name})
    if mixed_final:
        artifacts.append({"kind": "mixed_video", "file": mixed_final.name})

    audio: dict[str, Any] = {}
    if bgm and bgm_final:
        audio["bgm"] = {
            "source_file": bgm.name,
            "stem_file": bgm_final.name,
            "gain_db": settings.bgm_gain_db,
            "looped": True,
            "cue_seconds": 0.0,
            "provenance": provenance.get("bgm", {}),
        }
    if se and se_final:
        audio["se"] = {
            "source_file": se.name,
            "stem_file": se_final.name,
            "gain_db": settings.se_gain_db,
            "looped": False,
            "cue_seconds": settings.se_cue_seconds,
            "provenance": provenance.get("se", {}),
        }

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "delivery_mode": mode.value,
        "clip": {
            "clean_video": clean.name,
            "duration_seconds": duration,
            "metadata": dict(metadata),
        },
        "timeline": {"origin_seconds": 0.0, "duration_seconds": duration},
        "stem_format": {
            "codec": STEM_CODEC,
            "sample_rate_hz": STEM_SAMPLE_RATE,
            "channels": STEM_CHANNELS,
            "channel_layout": "stereo",
        },
        "audio": audio,
        "artifacts": artifacts,
        "provenance": dict(provenance),
    }
    if mixed_final is not None and decoded_peak_4x_dbfs is not None:
        manifest["mixed_output_validation"] = {
            "method": "decoded AAC peak after 4x resampling",
            "decoded_peak_4x_dbfs": decoded_peak_4x_dbfs,
            "limit_dbfs": AAC_TRUE_PEAK_LIMIT_DB,
            "post_mix_attenuation_db": post_mix_attenuation_db,
        }
    return manifest


def _commit_artifacts(
    staged_outputs: Sequence[tuple[Path, Path]],
    staging: Path,
    *,
    remove_after: Sequence[Path],
) -> None:
    """Install staged files and roll back if any rename fails."""

    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    removed_backups: list[tuple[Path, Path]] = []
    try:
        for index, (staged, final) in enumerate(staged_outputs):
            if final.exists():
                backup = staging / f"existing_{index}{final.suffix}"
                os.replace(final, backup)
                backups.append((backup, final))
            os.replace(staged, final)
            installed.append(final)

        for index, stale in enumerate(remove_after):
            if not stale.exists():
                continue
            if not stale.is_file():
                raise RuntimeError(f"stale audio output is not a file: {stale}")
            removed_backup = staging / f"removed_{index}{stale.suffix}"
            os.replace(stale, removed_backup)
            removed_backups.append((removed_backup, stale))
    except Exception:
        for final in reversed(installed):
            try:
                final.unlink(missing_ok=True)
            except OSError:
                continue
        for backup, final in reversed(backups):
            if backup.exists():
                os.replace(backup, final)
        for removed_backup, stale in reversed(removed_backups):
            if removed_backup.exists():
                os.replace(removed_backup, stale)
        raise


def _highlight_duration(highlight: Mapping[str, Any], index: int) -> float:
    if not isinstance(highlight, Mapping):
        raise TypeError(f"highlights[{index}] must be a mapping")
    raw_duration = highlight.get("duration")
    if raw_duration is None:
        try:
            raw_duration = float(highlight["end_sec"]) - float(highlight["start_sec"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"highlights[{index}] needs duration or numeric start_sec/end_sec"
            ) from exc
    duration = _finite_number(raw_duration, f"highlights[{index}].duration")
    if duration <= 0:
        raise ValueError(f"highlights[{index}].duration must be greater than 0")
    return duration


def _preflight_batch_paths(
    clip_paths: Sequence[str | os.PathLike[str]],
    bgm_path: str | os.PathLike[str] | None,
    se_path: str | os.PathLike[str] | None,
) -> tuple[tuple[Path, ...], Path | None, Path | None]:
    """Reject batch path relationships that could overwrite a later input."""

    clean_paths = tuple(
        _existing_file(path, f"clip_paths[{index}]")
        for index, path in enumerate(clip_paths)
    )
    for index, clean in enumerate(clean_paths):
        if clean.suffix.lower() != ".mp4":
            raise ValueError(f"clip_paths[{index}] must be an MP4 file")
    if len(set(clean_paths)) != len(clean_paths):
        raise ValueError("clip_paths must not contain duplicate files")

    bgm = _optional_existing_file(bgm_path, "bgm_path")
    se = _optional_existing_file(se_path, "se_path")
    clean_set = set(clean_paths)
    for label, asset in (("bgm_path", bgm), ("se_path", se)):
        if asset is not None and asset in clean_set:
            raise ValueError(f"{label} must not also be a batch clip input")

    protected_inputs = clean_set | {asset for asset in (bgm, se) if asset is not None}
    claimed_outputs: dict[Path, Path] = {}
    for clean in clean_paths:
        base = clean.with_suffix("")
        derived = (
            base.with_name(f"{base.name}_bgm.wav"),
            base.with_name(f"{base.name}_se.wav"),
            base.with_name(f"{base.name}_mixed.mp4"),
            base.with_name(f"{base.name}_audio.json"),
        )
        for output in derived:
            resolved_output = output.resolve()
            if resolved_output in protected_inputs:
                raise ValueError(
                    "batch output/stale path would overwrite an input: "
                    f"{resolved_output}"
                )
            previous_owner = claimed_outputs.get(resolved_output)
            if previous_owner is not None and previous_owner != clean:
                raise ValueError(
                    f"batch clips produce the same audio output path: {resolved_output}"
                )
            claimed_outputs[resolved_output] = clean

    return clean_paths, bgm, se


def _existing_file(path: str | os.PathLike[str], field_name: str) -> Path:
    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_file():
        raise ValueError(f"{field_name} must point to a file")
    return candidate


def _optional_existing_file(
    path: str | os.PathLike[str] | None, field_name: str
) -> Path | None:
    return None if path is None else _existing_file(path, field_name)


def _reject_source_output_collisions(
    *,
    sources: Sequence[Path | None],
    outputs: Sequence[Path | None],
) -> None:
    source_set = {path.resolve() for path in sources if path is not None}
    for output in outputs:
        if output is not None and output.resolve() in source_set:
            raise ValueError(f"generated output would overwrite an input: {output}")


def _require_generated_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"FFmpeg did not create a valid {label}: {path}")


def _json_mapping(value: Mapping[str, Any] | None, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-serializable values") from exc


def _finite_number(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def _format_number(value: float) -> str:
    formatted = f"{value:.9f}".rstrip("0").rstrip(".")
    return formatted or "0"
