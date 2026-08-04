"""Validated lightweight VFX settings and deterministic per-clip planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

from user_media import (
    UserMediaAsset,
    UserMediaError,
    resolve_user_media_asset,
    scan_optional_user_media,
    validate_user_media,
)


logger = logging.getLogger(__name__)
EFFECTS_MANIFEST_NAME = "effects_manifest.json"


class VideoEffectError(RuntimeError):
    """Raised when selected effect settings or files cannot be applied."""


class EffectPreset(str, Enum):
    NONE = "none"
    FADE = "fade"
    PUNCH = "punch"
    FLASH = "flash"


class VfxAnchor(str, Enum):
    TOP_LEFT = "top-left"
    TOP = "top"
    TOP_RIGHT = "top-right"
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM = "bottom"
    BOTTOM_RIGHT = "bottom-right"


class VfxTarget(str, Enum):
    BOTH = "both"
    CLIPS = "clips"
    SHORTS = "shorts"


@dataclass(frozen=True)
class VfxOptions:
    """Saved VFX choices shared by normal clips and Shorts."""

    vfx_asset_id: str = ""
    vfx_user_folder: str = ""
    effect_preset: EffectPreset | str = EffectPreset.NONE
    automatic: bool = False
    cue_seconds: float = 0.0
    duration_seconds: float = 1.0
    anchor: VfxAnchor | str = VfxAnchor.CENTER
    scale_percent: float = 100.0
    opacity_percent: float = 100.0
    target: VfxTarget | str = VfxTarget.BOTH

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effect_preset",
            _enum_value(EffectPreset, self.effect_preset, "effect_preset"),
        )
        object.__setattr__(
            self,
            "anchor",
            _enum_value(VfxAnchor, self.anchor, "anchor"),
        )
        object.__setattr__(
            self,
            "target",
            _enum_value(VfxTarget, self.target, "target"),
        )
        object.__setattr__(
            self,
            "vfx_asset_id",
            str(self.vfx_asset_id or "").strip(),
        )
        object.__setattr__(
            self,
            "vfx_user_folder",
            str(self.vfx_user_folder or "").strip(),
        )
        object.__setattr__(self, "automatic", bool(self.automatic))

        cue = _finite(self.cue_seconds, "cue_seconds")
        duration = _finite(self.duration_seconds, "duration_seconds")
        scale = _finite(self.scale_percent, "scale_percent")
        opacity = _finite(self.opacity_percent, "opacity_percent")
        if cue < 0:
            raise ValueError("cue_seconds must be greater than or equal to 0")
        if duration <= 0:
            raise ValueError("duration_seconds must be greater than 0")
        if not 1 <= scale <= 300:
            raise ValueError("scale_percent must be between 1 and 300")
        if not 0 <= opacity <= 100:
            raise ValueError("opacity_percent must be between 0 and 100")
        object.__setattr__(self, "cue_seconds", cue)
        object.__setattr__(self, "duration_seconds", duration)
        object.__setattr__(self, "scale_percent", scale)
        object.__setattr__(self, "opacity_percent", opacity)

    @property
    def enabled(self) -> bool:
        return bool(
            self.automatic
            or self.vfx_asset_id
            or self.effect_preset is not EffectPreset.NONE
        )

    def applies_to(self, *, shorts: bool) -> bool:
        return self.enabled and _target_applies(self.target, shorts)


@dataclass(frozen=True)
class ClipEffectPlan:
    """Fully resolved effect plan for one generated clip."""

    asset: UserMediaAsset | None = None
    effect_preset: EffectPreset = EffectPreset.NONE
    cue_seconds: float = 0.0
    duration_seconds: float = 0.0
    anchor: VfxAnchor = VfxAnchor.CENTER
    scale_percent: float = 100.0
    opacity_percent: float = 100.0

    @property
    def enabled(self) -> bool:
        return self.asset is not None or self.effect_preset is not EffectPreset.NONE


def prepare_vfx_assets(options: VfxOptions) -> tuple[UserMediaAsset, ...]:
    """Resolve the manual selection or index candidates for automatic mode."""

    if not isinstance(options, VfxOptions):
        raise TypeError("options must be a VfxOptions instance")
    if not options.enabled:
        return ()
    if (
        not options.automatic
        and options.vfx_asset_id
        and not options.vfx_user_folder
    ):
        raise VideoEffectError("VFXユーザー素材の参照フォルダを指定してください")
    try:
        if options.automatic:
            assets = scan_optional_user_media(options.vfx_user_folder, "vfx")
        elif options.vfx_asset_id:
            assets = (
                resolve_user_media_asset(
                    options.vfx_user_folder,
                    options.vfx_asset_id,
                    "vfx",
                ),
            )
        else:
            assets = ()
        if options.automatic:
            valid_assets = []
            for asset in assets:
                try:
                    validated = validate_user_media(asset)
                except UserMediaError as exc:
                    logger.warning(
                        "Skipping invalid automatic VFX candidate %s: %s",
                        asset.filename,
                        exc,
                    )
                    continue
                valid_assets.append(
                    validated
                    if isinstance(validated, UserMediaAsset)
                    else asset
                )
            return tuple(sorted(valid_assets, key=lambda asset: asset.id))

        validated_assets = []
        for asset in assets:
            validated = validate_user_media(asset)
            validated_assets.append(
                validated if isinstance(validated, UserMediaAsset) else asset
            )
        return tuple(sorted(validated_assets, key=lambda asset: asset.id))
    except UserMediaError as exc:
        raise VideoEffectError(f"VFX素材を確認できません: {exc}") from exc


def resolve_clip_effect_plan(
    options: VfxOptions,
    highlight: Mapping[str, Any],
    clip_duration: float,
    candidates: Sequence[UserMediaAsset],
    *,
    shorts: bool,
) -> ClipEffectPlan:
    """Return a manual or deterministic automatic plan for one clip."""

    if not isinstance(options, VfxOptions):
        raise TypeError("options must be a VfxOptions instance")
    duration = _finite(clip_duration, "clip_duration")
    if duration <= 0:
        raise VideoEffectError("クリップ長は0秒より長くしてください")
    if not options.enabled or not _target_applies(options.target, shorts):
        return ClipEffectPlan()

    ordered = tuple(sorted(candidates, key=lambda asset: asset.id))
    if options.automatic:
        effect_duration = min(options.duration_seconds, duration)
        max_cue = max(0.0, duration - effect_duration)
        digest = _highlight_digest(highlight)
        asset = ordered[digest[0] % len(ordered)] if ordered else None
        presets = tuple(EffectPreset)
        if asset is None:
            presets = (EffectPreset.FADE, EffectPreset.PUNCH, EffectPreset.FLASH)
        preset = presets[digest[1] % len(presets)]
        cue_ratio = 0.12 + (digest[2] / 255.0) * 0.5
        cue = round(max_cue * cue_ratio, 6)
        anchors = tuple(VfxAnchor)
        anchor = anchors[digest[3] % len(anchors)]
    else:
        cue = options.cue_seconds
        visible_duration = (
            min(options.duration_seconds, duration - cue)
            if cue < duration
            else 0.0
        )
        asset = None
        if options.vfx_asset_id:
            asset = next(
                (item for item in ordered if item.id == options.vfx_asset_id),
                None,
            )
            if asset is None:
                raise VideoEffectError(
                    "選択したVFXが参照フォルダに見つかりません。再スキャンしてください"
                )
            if visible_duration <= 0:
                asset = None
        preset = options.effect_preset
        if visible_duration <= 0 and preset in {
            EffectPreset.PUNCH,
            EffectPreset.FLASH,
        }:
            preset = EffectPreset.NONE
        effect_duration = (
            min(options.duration_seconds, duration)
            if preset is EffectPreset.FADE
            else visible_duration
        )
        anchor = options.anchor

    return ClipEffectPlan(
        asset=asset,
        effect_preset=preset,
        cue_seconds=cue,
        duration_seconds=effect_duration,
        anchor=anchor,
        scale_percent=options.scale_percent,
        opacity_percent=options.opacity_percent,
    )


def write_effects_manifest(
    output_dir: str | os.PathLike[str],
    output_paths: Sequence[str | os.PathLike[str]],
    plans: Sequence[ClipEffectPlan],
    *,
    options: VfxOptions,
    shorts: bool,
) -> Path | None:
    """Write an editable, path-free record of baked effects for one group."""

    if not isinstance(options, VfxOptions):
        raise TypeError("options must be a VfxOptions instance")
    if len(output_paths) != len(plans):
        raise VideoEffectError("VFX来歴の動画数とプラン数が一致しません")
    for plan in plans:
        if not isinstance(plan, ClipEffectPlan):
            raise TypeError("plans must contain ClipEffectPlan instances")

    root = Path(output_dir).expanduser().resolve()
    manifest_path = validate_effects_manifest_target(root, plans)
    if not any(plan.enabled for plan in plans):
        _remove_owned_effects_manifest(manifest_path)
        return None

    records = []
    for output_path, plan in zip(output_paths, plans):
        output = Path(output_path).expanduser().resolve()
        try:
            output_name = output.relative_to(root).as_posix()
        except ValueError as exc:
            raise VideoEffectError(
                f"VFX適用動画が出力フォルダ外です: {output}"
            ) from exc
        record: dict[str, Any] = {
            "source_clean_video": output_name,
            "output_file": output_name,
            "enabled": plan.enabled,
        }
        if plan.enabled:
            record["effect"] = {
                "preset": plan.effect_preset.value,
                "cue_seconds": plan.cue_seconds,
                "duration_seconds": plan.duration_seconds,
                "anchor": plan.anchor.value,
                "scale_percent": plan.scale_percent,
                "opacity_percent": plan.opacity_percent,
            }
        if plan.asset is not None:
            record["vfx_asset"] = {
                "id": plan.asset.id,
                "kind": plan.asset.kind,
                "source_type": "user_provided",
                "original_filename": plan.asset.filename,
                "source_sha256": plan.asset.sha256,
                "source_size_bytes": plan.asset.size,
            }
        records.append(record)

    payload = {
        "schema_version": 1,
        "generator": "clip-extractor",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_group": "shorts" if shorts else "clips",
        "automatic_selection_and_placement": options.automatic,
        "target": options.target.value,
        "clips": records,
    }
    _write_effects_payload_atomic(root, payload)
    return manifest_path


def validate_effects_manifest_target(
    output_dir: str | os.PathLike[str],
    plans: Sequence[ClipEffectPlan],
) -> Path:
    """Fail before FFmpeg if an enabled batch would overwrite user data."""

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / EFFECTS_MANIFEST_NAME
    _reject_manifest_link(manifest_path)
    if (
        any(plan.enabled for plan in plans)
        and manifest_path.exists()
        and not _is_owned_effects_manifest(manifest_path)
    ):
        raise VideoEffectError(
            f"既存のユーザーファイルとVFX来歴出力が衝突します: {manifest_path}"
        )
    return manifest_path


def has_owned_effects_manifest(
    output_dir: str | os.PathLike[str],
) -> bool:
    """Return whether a prior Clip Extractor effects manifest is present."""

    root = Path(output_dir).expanduser().resolve()
    manifest_path = root / EFFECTS_MANIFEST_NAME
    _reject_manifest_link(manifest_path)
    return _is_owned_effects_manifest(manifest_path)


def remap_effects_manifest_outputs(
    output_dir: str | os.PathLike[str],
    source_paths: Sequence[str | os.PathLike[str]],
    final_paths: Sequence[str | os.PathLike[str]],
) -> Path | None:
    """Atomically point an owned manifest at post-audio primary outputs."""

    if len(source_paths) != len(final_paths):
        raise VideoEffectError("VFX来歴の元動画数と最終動画数が一致しません")
    root = Path(output_dir).expanduser().resolve()
    manifest_path = root / EFFECTS_MANIFEST_NAME
    _reject_manifest_link(manifest_path)
    if not manifest_path.exists():
        return None
    if not _is_owned_effects_manifest(manifest_path):
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoEffectError("VFX来歴ファイルを再読込できません") from exc
    records = payload.get("clips")
    if not isinstance(records, list) or len(records) != len(source_paths):
        raise VideoEffectError("VFX来歴の動画数が生成結果と一致しません")

    for record, source_path, final_path in zip(records, source_paths, final_paths):
        if not isinstance(record, dict):
            raise VideoEffectError("VFX来歴の動画情報が不正です")
        source_name = _relative_effect_output(root, source_path)
        final_name = _relative_effect_output(root, final_path)
        recorded_source = record.get("source_clean_video", record.get("output_file"))
        if recorded_source != source_name:
            raise VideoEffectError(
                "VFX来歴の元動画が今回の音声出力対象と一致しません"
            )
        record["source_clean_video"] = source_name
        record["output_file"] = final_name

    _write_effects_payload_atomic(root, payload)
    return manifest_path


def _relative_effect_output(
    root: Path,
    output_path: str | os.PathLike[str],
) -> str:
    output = Path(output_path).expanduser().resolve()
    try:
        return output.relative_to(root).as_posix()
    except ValueError as exc:
        raise VideoEffectError(f"VFX適用動画が出力フォルダ外です: {output}") from exc


def _write_effects_payload_atomic(root: Path, payload: Mapping[str, Any]) -> None:
    manifest_path = root / EFFECTS_MANIFEST_NAME
    temporary = root / f".{EFFECTS_MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, manifest_path)
    finally:
        temporary.unlink(missing_ok=True)


def _reject_manifest_link(path: Path) -> None:
    if path.is_symlink():
        raise VideoEffectError(f"VFX来歴ファイルにリンクは使えません: {path}")
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        raise VideoEffectError(f"VFX来歴ファイルにリンクは使えません: {path}")


def _remove_owned_effects_manifest(path: Path) -> None:
    if not _is_owned_effects_manifest(path):
        return

    path.unlink()


def _is_owned_effects_manifest(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("generator") == "clip-extractor"
        and payload.get("schema_version") == 1
        and isinstance(payload.get("clips"), list)
    )


def _target_applies(target: VfxTarget, shorts: bool) -> bool:
    if target is VfxTarget.BOTH:
        return True
    return (target is VfxTarget.SHORTS) if shorts else (target is VfxTarget.CLIPS)


def _highlight_digest(highlight: Mapping[str, Any]) -> bytes:
    stable = {
        "title": str(highlight.get("title") or ""),
        "start_sec": _stable_number(highlight.get("start_sec", 0)),
        "end_sec": _stable_number(highlight.get("end_sec", 0)),
    }
    payload = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _stable_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if not math.isfinite(number):
        number = 0.0
    return format(number, ".9g")


def _finite(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def _enum_value(enum_type, value: Any, field_name: str):
    text = value.value if isinstance(value, enum_type) else str(value or "")
    try:
        return enum_type(text.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{field_name} must be one of: {allowed}") from exc
