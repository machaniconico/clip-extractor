"""Resolve optional audio assets and apply them to rendered media groups.

This module is the shared integration boundary for the Web and CLI pipelines.
It never downloads assets: rendering accepts either a previously installed,
verified starter pack or user-provided files from explicitly configured folders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping, Sequence
import uuid

from audio_assets import (
    AudioAssetError,
    InstalledAsset,
    get_installed_asset,
    get_pack_status,
)
from audio_mix import (
    AAC_TRUE_PEAK_LIMIT_DB,
    AudioDeliveryMode,
    AudioMixSettings,
    process_clip_batch,
)
from user_media import (
    UserMediaAsset,
    UserMediaError,
    is_user_media_id,
    resolve_user_media_asset,
    validate_user_media,
)
from video_effects import (
    EFFECTS_MANIFEST_NAME,
    VideoEffectError,
    has_owned_effects_manifest,
    remap_effects_manifest_outputs,
)


AUDIO_MANIFEST_NAME = "audio_manifest.json"
AUDIO_NOTICES_NAME = "THIRD_PARTY_NOTICES_AUDIO.txt"
_TRANSACTION_PREFIX = ".audio_delivery_transaction-"
_RECOVERY_PREFIX = ".audio_delivery_recovery-"
_RECOVERY_INDEX_NAME = "RECOVERY.json"
_GENERATED_AUDIO_KINDS = frozenset(
    {"bgm_stem", "se_stem", "mixed_video", "clip_audio_manifest"}
)
logger = logging.getLogger(__name__)


class AudioDeliveryError(RuntimeError):
    """Raised when selected audio cannot be delivered safely."""


@dataclass(frozen=True)
class AudioDeliveryOptions:
    """User-selectable audio settings shared by every generated clip."""

    delivery_mode: AudioDeliveryMode | str = AudioDeliveryMode.BOTH
    bgm_asset_id: str = ""
    se_asset_id: str = ""
    bgm_user_folder: str = ""
    se_user_folder: str = ""
    bgm_gain_db: float = -18.0
    se_gain_db: float = -8.0
    se_cue_seconds: float = 0.0

    def __post_init__(self) -> None:
        bgm_id = str(self.bgm_asset_id or "").strip()
        se_id = str(self.se_asset_id or "").strip()
        bgm_folder = str(self.bgm_user_folder or "").strip()
        se_folder = str(self.se_user_folder or "").strip()
        # AudioMixSettings owns the canonical mode and numeric validation.
        validated = AudioMixSettings(
            delivery_mode=self.delivery_mode,
            bgm_gain_db=self.bgm_gain_db,
            se_gain_db=self.se_gain_db,
            se_cue_seconds=self.se_cue_seconds,
        )
        object.__setattr__(self, "delivery_mode", validated.delivery_mode)
        object.__setattr__(self, "bgm_asset_id", bgm_id)
        object.__setattr__(self, "se_asset_id", se_id)
        object.__setattr__(self, "bgm_user_folder", bgm_folder)
        object.__setattr__(self, "se_user_folder", se_folder)
        object.__setattr__(self, "bgm_gain_db", validated.bgm_gain_db)
        object.__setattr__(self, "se_gain_db", validated.se_gain_db)
        object.__setattr__(self, "se_cue_seconds", validated.se_cue_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self.bgm_asset_id or self.se_asset_id)


@dataclass(frozen=True)
class AudioDeliveryResult:
    """Primary media paths and sidecars produced for downstream consumers."""

    enabled: bool
    media_groups: dict[str, tuple[Path, ...]]
    deliverables: tuple[Path, ...] = ()
    manifest_path: Path | None = None
    notices_path: Path | None = None


class _AudioDeliveryTransaction:
    """Rollback generated media and provenance sidecars as one delivery unit."""

    def __init__(
        self,
        root: Path,
        generated_paths: set[Path],
        clean_paths: set[Path],
        *,
        protect_clean: bool,
        manage_sidecars: bool,
        protected_paths: set[Path] | None = None,
    ) -> None:
        self.root = root
        self.generated_paths = set(generated_paths)
        self.clean_paths = set(clean_paths)
        self.protect_clean = protect_clean
        self.manage_sidecars = manage_sidecars
        self.protected_paths = set(protected_paths or ())
        self.backup_dir: Path | None = None
        self.moved_backups: list[tuple[Path, Path]] = []
        self.clean_backups: list[tuple[Path, Path]] = []
        self.protected_backups: list[tuple[Path, Path]] = []
        self.active = False

    def begin(self, previous_manifest: Mapping[str, Any] | None) -> None:
        self.backup_dir = Path(
            tempfile.mkdtemp(prefix=_TRANSACTION_PREFIX, dir=str(self.root))
        )
        try:
            owned_manifest = _is_owned_audio_manifest(previous_manifest)
            legacy_manifest = _is_legacy_audio_manifest(previous_manifest)
            recognized_manifest = owned_manifest or legacy_manifest
            if owned_manifest:
                _validate_owned_notice(self.root, previous_manifest)
            prior_paths = _manifest_generated_paths(
                self.root,
                previous_manifest,
            )
            candidates = self.generated_paths | prior_paths
            sidecars = {
                self.root / AUDIO_MANIFEST_NAME,
                self.root / AUDIO_NOTICES_NAME,
            }
            if self.manage_sidecars or recognized_manifest:
                candidates.update(sidecars)
            owned_paths = set(prior_paths)
            if recognized_manifest:
                owned_paths.update(sidecars)
            for path in sorted(candidates, key=str):
                _reject_managed_link(path, "音声トランザクション対象")
                if path in self.clean_paths or not path.exists():
                    continue
                if not path.is_file():
                    raise AudioDeliveryError(
                        f"音声トランザクション対象がファイルではありません: {path}"
                    )
                if path not in owned_paths:
                    raise AudioDeliveryError(
                        "既存のユーザーファイルと音声生成先が衝突します: "
                        f"{path}"
                    )
                backup = self._next_backup("existing", path.suffix)
                os.replace(path, backup)
                self.moved_backups.append((backup, path))

            if self.protect_clean:
                for clean in sorted(self.clean_paths, key=str):
                    if not clean.is_file():
                        raise AudioDeliveryError(
                            f"ミックス前のclean MP4を保護できません: {clean}"
                        )
                    backup = self._next_backup("clean", clean.suffix)
                    # A hard link is not an independent backup: an in-place
                    # encoder write would mutate both names.  Keep a physical
                    # copy so rollback can always restore the original bytes.
                    shutil.copy2(clean, backup)
                    self.clean_backups.append((backup, clean))
            for protected in sorted(self.protected_paths, key=str):
                if not protected.is_file():
                    raise AudioDeliveryError(
                        f"音声処理と連動する来歴ファイルを保護できません: {protected}"
                    )
                _reject_managed_link(protected, "連動する来歴ファイル")
                backup = self._next_backup("protected", protected.suffix)
                shutil.copy2(protected, backup)
                self.protected_backups.append((backup, protected))
            self.active = True
        except BaseException:
            self._restore(remove_new=False)
            raise

    def rollback(self) -> None:
        self._restore(remove_new=self.active)

    def _restore(self, *, remove_new: bool) -> None:
        if self.backup_dir is None:
            return
        rollback_error: Exception | None = None
        if remove_new:
            removable = self.generated_paths | {
                self.root / AUDIO_MANIFEST_NAME,
                self.root / AUDIO_NOTICES_NAME,
            }
            for path in sorted(removable, key=str, reverse=True):
                try:
                    if _is_managed_link(path):
                        path.unlink()
                    elif path.exists():
                        if not path.is_file():
                            raise AudioDeliveryError(
                                f"rollback対象がファイルではありません: {path}"
                            )
                        path.unlink()
                except Exception as exc:  # best-effort restoration continues
                    rollback_error = rollback_error or exc

        for backup, original in reversed(self.moved_backups):
            try:
                if backup.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, original)
            except Exception as exc:
                rollback_error = rollback_error or exc

        for backup, clean in reversed(self.clean_backups):
            try:
                if not backup.exists():
                    continue
                if _is_managed_link(clean):
                    clean.unlink()
                elif clean.exists():
                    if not clean.is_file():
                        raise AudioDeliveryError(
                            f"clean MP4のrollback対象がファイルではありません: {clean}"
                        )
                    clean.unlink()
                clean.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, clean)
            except Exception as exc:
                rollback_error = rollback_error or exc

        for backup, protected in reversed(self.protected_backups):
            try:
                if not backup.exists():
                    continue
                if _is_managed_link(protected):
                    protected.unlink()
                elif protected.exists():
                    if not protected.is_file():
                        raise AudioDeliveryError(
                            "連動する来歴ファイルのrollback対象が"
                            f"ファイルではありません: {protected}"
                        )
                    protected.unlink()
                protected.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, protected)
            except Exception as exc:
                rollback_error = rollback_error or exc

        self.active = False
        if rollback_error is not None:
            recovery_dir = self.backup_dir
            raise AudioDeliveryError(
                "音声出力のrollbackに失敗しました。復旧用バックアップを保持しました: "
                f"{recovery_dir} ({rollback_error})"
            ) from rollback_error
        _remove_transaction_tree(self.backup_dir, self.root)
        self.backup_dir = None

    def complete(self) -> None:
        if self.backup_dir is None:
            return
        if self.moved_backups:
            recovery_dir = self.root / f"{_RECOVERY_PREFIX}{uuid.uuid4().hex}"
            index = {
                "schema_version": 1,
                "generator": "clip-extractor",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "reason": (
                    "Previous audio outputs were moved here before regeneration. "
                    "They are retained because an in-folder manifest alone is not "
                    "proof of file ownership."
                ),
                "files": [
                    {
                        "backup_file": backup.name,
                        "original_file": original.relative_to(self.root).as_posix(),
                    }
                    for backup, original in self.moved_backups
                ],
            }
            (self.backup_dir / _RECOVERY_INDEX_NAME).write_text(
                json.dumps(index, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(self.backup_dir, recovery_dir)
            for backup, _original in self.clean_backups + self.protected_backups:
                try:
                    (recovery_dir / backup.name).unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "Unable to remove an extra transaction copy from %s",
                        recovery_dir,
                    )
            logger.warning(
                "Previous audio outputs were retained in recovery: %s",
                recovery_dir,
            )
            self.backup_dir = None
            self.moved_backups.clear()
            self.clean_backups.clear()
            self.protected_backups.clear()
            self.active = False
            return
        _remove_transaction_tree(self.backup_dir, self.root)
        self.backup_dir = None
        self.active = False

    def _next_backup(self, category: str, suffix: str) -> Path:
        if self.backup_dir is None:
            raise RuntimeError("audio delivery transaction has not started")
        index = (
            len(self.moved_backups)
            + len(self.clean_backups)
            + len(self.protected_backups)
        )
        return self.backup_dir / f"{category}-{index}{suffix}"


def validate_audio_selection(options: AudioDeliveryOptions) -> None:
    """Fail fast when a render references a missing or wrong-kind asset."""

    if not options.enabled:
        return
    _resolve_selection(options)


def deliver_audio_groups(
    output_dir: str | os.PathLike[str],
    media_groups: Mapping[str, Sequence[str | os.PathLike[str]]],
    highlights: Sequence[Mapping[str, Any]],
    *,
    options: AudioDeliveryOptions,
    effects_manifest_dirs: Mapping[str, str | os.PathLike[str]] | None = None,
) -> AudioDeliveryResult:
    """Apply BGM/SE to landscape and/or Shorts media in one shared pass.

    ``media_groups`` normally contains ``clips`` and ``shorts``.  Returned
    primary paths are clean videos for ``separate``/``both`` and mixed videos
    for ``mixed``.  A prior audio manifest is used to remove only stale files
    that this module generated on an earlier render.
    """

    if not isinstance(options, AudioDeliveryOptions):
        raise TypeError("options must be an AudioDeliveryOptions instance")
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    normalized_groups = {
        str(name): tuple(_normalize_media_input(root, path) for path in paths)
        for name, paths in media_groups.items()
    }
    normalized_effect_dirs = _normalize_effect_manifest_dirs(
        root,
        normalized_groups,
        effects_manifest_dirs,
    )
    try:
        protected_effect_manifests = {
            directory / EFFECTS_MANIFEST_NAME
            for directory in normalized_effect_dirs.values()
            if has_owned_effects_manifest(directory)
        }
    except VideoEffectError as exc:
        raise AudioDeliveryError(f"VFX来歴ファイルを保護できません: {exc}") from exc
    current_clean_paths = _current_clean_paths(root, normalized_groups)
    _validate_root_sidecars(root)
    previous_manifest = _read_previous_manifest(root)

    if not options.enabled:
        transaction = _AudioDeliveryTransaction(
            root,
            set(),
            current_clean_paths,
            protect_clean=False,
            manage_sidecars=False,
            protected_paths=set(),
        )
        transaction.begin(previous_manifest)
        try:
            transaction.complete()
        except BaseException:
            transaction.rollback()
            raise
        return AudioDeliveryResult(enabled=False, media_groups=normalized_groups)

    selection = _resolve_selection(options)
    clean_paths, generated_paths = _potential_generated_paths(root, normalized_groups)
    _reject_audio_source_output_collisions(
        root,
        selection,
        clean_paths,
        generated_paths,
    )
    settings = AudioMixSettings(
        delivery_mode=options.delivery_mode,
        bgm_gain_db=options.bgm_gain_db,
        se_gain_db=options.se_gain_db,
        se_cue_seconds=options.se_cue_seconds,
    )
    transaction = _AudioDeliveryTransaction(
        root,
        generated_paths,
        clean_paths,
        protect_clean=settings.delivery_mode is AudioDeliveryMode.MIXED,
        manage_sidecars=True,
        protected_paths=protected_effect_manifests,
    )
    transaction.begin(previous_manifest)
    try:
        result = _deliver_enabled_audio(
            root,
            normalized_groups,
            highlights,
            selection=selection,
            settings=settings,
        )
        for group_name, directory in normalized_effect_dirs.items():
            try:
                remap_effects_manifest_outputs(
                    directory,
                    normalized_groups.get(group_name, ()),
                    result.media_groups.get(group_name, ()),
                )
            except VideoEffectError as exc:
                raise AudioDeliveryError(
                    f"音声出力後のVFX来歴を更新できません: {exc}"
                ) from exc
        transaction.complete()
    except BaseException:
        transaction.rollback()
        raise
    return result


def _deliver_enabled_audio(
    root: Path,
    normalized_groups: Mapping[str, tuple[Path, ...]],
    highlights: Sequence[Mapping[str, Any]],
    *,
    selection: Mapping[str, Any],
    settings: AudioMixSettings,
) -> AudioDeliveryResult:
    """Generate one enabled audio delivery inside an active transaction."""

    selection = dict(selection)
    for key in ("bgm", "se"):
        asset = selection.get(key)
        if isinstance(asset, UserMediaAsset):
            try:
                validated = validate_user_media(asset)
            except UserMediaError as exc:
                raise AudioDeliveryError(
                    f"選択した{key.upper()}素材が生成直前に変更されました: {exc}"
                ) from exc
            if isinstance(validated, UserMediaAsset):
                selection[key] = validated

    pack_provenance = None
    if selection["pack_id"] != "user-provided":
        pack_provenance = {
            "id": selection["pack_id"],
            "version": selection["pack_version"],
            "license_checked_at": selection["license_checked_at"],
        }
    provenance = {
        "pack": pack_provenance,
        "bgm": _asset_provenance(selection.get("bgm")),
        "se": _asset_provenance(selection.get("se")),
    }

    primary_groups: dict[str, tuple[Path, ...]] = {}
    deliverables: list[Path] = []
    manifest_groups: dict[str, list[dict[str, Any]]] = {}

    for group_name, paths in normalized_groups.items():
        if not paths:
            primary_groups[group_name] = ()
            manifest_groups[group_name] = []
            continue
        if len(paths) != len(highlights):
            raise AudioDeliveryError(
                f"{group_name} の動画数とハイライト数が一致しません"
            )
        batch = process_clip_batch(
            paths,
            highlights,
            settings=settings,
            bgm_path=(selection["bgm"].path if selection.get("bgm") else None),
            se_path=(selection["se"].path if selection.get("se") else None),
            provenance=provenance,
        )
        primary_paths: list[Path] = []
        group_records: list[dict[str, Any]] = []
        for clean_input, result in zip(paths, batch.clips):
            primary = (
                result.mixed_video
                if settings.delivery_mode is AudioDeliveryMode.MIXED
                else result.clean_video
            )
            if primary is None:
                raise AudioDeliveryError(f"{group_name} の主出力を確定できませんでした")
            primary_paths.append(primary)
            deliverables.extend(result.deliverables)
            artifacts = _result_artifacts(root, result)
            record: dict[str, Any] = {
                "source_clean_video": _relative_output(root, clean_input),
                "primary_video": _relative_output(root, primary),
                "artifacts": artifacts,
            }
            if (
                result.mixed_video is not None
                and result.decoded_peak_4x_dbfs is not None
            ):
                record["mixed_output_validation"] = {
                    "method": "decoded AAC peak after 4x resampling",
                    "decoded_peak_4x_dbfs": result.decoded_peak_4x_dbfs,
                    "limit_dbfs": AAC_TRUE_PEAK_LIMIT_DB,
                    "post_mix_attenuation_db": result.post_mix_attenuation_db,
                }
            group_records.append(record)
        primary_groups[group_name] = tuple(primary_paths)
        manifest_groups[group_name] = group_records

    notices_text = _third_party_notices(selection)
    notices_bytes = notices_text.encode("utf-8")
    manifest = {
        "schema_version": 2,
        "generator": "clip-extractor",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "delivery_mode": settings.delivery_mode.value,
        "settings": {
            "bgm_gain_db": settings.bgm_gain_db,
            "se_gain_db": settings.se_gain_db,
            "se_cue_seconds": settings.se_cue_seconds,
            "stem_sample_rate_hz": 48_000,
            "stem_channels": 2,
        },
        "pack": provenance["pack"],
        "selected_assets": {
            key: value
            for key, value in (
                ("bgm", provenance["bgm"]),
                ("se", provenance["se"]),
            )
            if value
        },
        "notices": {
            "file": AUDIO_NOTICES_NAME,
            "size_bytes": len(notices_bytes),
            "sha256": hashlib.sha256(notices_bytes).hexdigest(),
        },
        "groups": manifest_groups,
    }
    manifest_path = root / AUDIO_MANIFEST_NAME
    notices_path = root / AUDIO_NOTICES_NAME

    _write_text_atomic(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    _write_text_atomic(
        notices_path,
        notices_text,
    )
    deliverables.extend((manifest_path, notices_path))
    return AudioDeliveryResult(
        enabled=True,
        media_groups=primary_groups,
        deliverables=tuple(deliverables),
        manifest_path=manifest_path,
        notices_path=notices_path,
    )


def _resolve_selection(options: AudioDeliveryOptions) -> dict[str, Any]:
    builtin_selected = any(
        asset_id and not is_user_media_id(asset_id)
        for asset_id in (options.bgm_asset_id, options.se_asset_id)
    )
    resolved: dict[str, Any] = {
        "pack_id": "user-provided",
        "pack_version": "",
        "license_checked_at": "",
    }
    if builtin_selected:
        status = get_pack_status()
        if not status.ready:
            raise AudioDeliveryError(
                "BGM/SE素材パックが利用できません。Input画面の"
                "「日本語ショート向け素材をダウンロード」を先に実行してください。"
            )
        resolved["pack_id"] = status.pack_id
        resolved["pack_version"] = status.version
    try:
        for key, asset_id, expected_kind, user_folder in (
            (
                "bgm",
                options.bgm_asset_id,
                "bgm",
                options.bgm_user_folder,
            ),
            (
                "se",
                options.se_asset_id,
                "se",
                options.se_user_folder,
            ),
        ):
            if not asset_id:
                continue
            if is_user_media_id(asset_id):
                if not user_folder:
                    raise AudioDeliveryError(
                        f"{expected_kind.upper()}ユーザー素材の参照フォルダを指定してください"
                    )
                asset = resolve_user_media_asset(
                    user_folder,
                    asset_id,
                    expected_kind,
                )
                validate_user_media(asset)
            else:
                asset = get_installed_asset(asset_id)
            if asset.kind != expected_kind:
                raise AudioDeliveryError(
                    f"{asset_id} は {expected_kind.upper()} 素材ではありません"
                )
            resolved[key] = asset
            if isinstance(asset, InstalledAsset):
                resolved["license_checked_at"] = asset.license_checked_at
    except (AudioAssetError, UserMediaError, KeyError) as exc:
        raise AudioDeliveryError(f"選択したBGM/SE素材を確認できません: {exc}") from exc
    return resolved


def _asset_provenance(
    asset: InstalledAsset | UserMediaAsset | None,
) -> dict[str, Any]:
    if asset is None:
        return {}
    if isinstance(asset, UserMediaAsset):
        return {
            "id": asset.id,
            "kind": asset.kind,
            "source_type": "user_provided",
            "original_filename": asset.filename,
            "source_sha256": asset.sha256,
            "source_size_bytes": asset.size,
        }
    return {
        "id": asset.id,
        "label": asset.label,
        "kind": asset.kind,
        "source_type": "downloaded_pack",
        "creator": asset.creator,
        "source_page": asset.source_page,
        "license_id": asset.license_id,
        "license_url": asset.license_url,
        "license_checked_at": asset.license_checked_at,
        "attribution_required": asset.attribution_required,
        "attribution_text": asset.attribution_text,
        "pack_id": asset.pack_id,
        "pack_version": asset.pack_version,
        "source_sha256": asset.sha256,
        "source_size_bytes": asset.size,
        "modifications": "Source unchanged; gain/loop/cue are applied to generated outputs.",
    }


def _reject_audio_source_output_collisions(
    root: Path,
    selection: Mapping[str, Any],
    clean_paths: set[Path],
    generated_paths: set[Path],
) -> None:
    protected = set(clean_paths) | set(generated_paths)
    protected.update(
        {
            root / AUDIO_MANIFEST_NAME,
            root / AUDIO_NOTICES_NAME,
        }
    )
    for key in ("bgm", "se"):
        asset = selection.get(key)
        if asset is None:
            continue
        try:
            source = Path(asset.path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise AudioDeliveryError(
                f"選択した{key.upper()}素材を確認できません"
            ) from exc
        if source in protected:
            raise AudioDeliveryError(
                f"選択した{key.upper()}素材が音声生成先と衝突します: {source}"
            )


def _result_artifacts(root: Path, result: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for kind, path in (
        ("clean_video", result.clean_video),
        ("mixed_video", result.mixed_video),
        ("bgm_stem", result.bgm_stem),
        ("se_stem", result.se_stem),
        ("clip_audio_manifest", result.manifest),
    ):
        if path is not None:
            record: dict[str, Any] = {
                "kind": kind,
                "file": _relative_output(root, path),
            }
            if kind in _GENERATED_AUDIO_KINDS:
                size, digest = _file_fingerprint(Path(path))
                record["size_bytes"] = size
                record["sha256"] = digest
            artifacts.append(record)
    return artifacts


def _file_fingerprint(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise AudioDeliveryError(f"生成した音声成果物を確認できません: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _is_managed_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _reject_managed_link(path: Path, label: str) -> None:
    if _is_managed_link(path):
        raise AudioDeliveryError(
            f"{label}にシンボリックリンクまたはジャンクションは使用できません: {path}"
        )


def _managed_output_path(root: Path, path: Path, label: str) -> Path:
    """Canonicalize the parent without following the managed file itself."""

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise AudioDeliveryError(f"{label}が出力フォルダ外です: {path}") from exc

    cursor = root
    for part in relative.parts:
        cursor /= part
        _reject_managed_link(cursor, label)
    try:
        parent = lexical.parent.resolve()
    except OSError as exc:
        raise AudioDeliveryError(
            f"{label}の親フォルダを確認できません: {path}"
        ) from exc
    managed = parent / lexical.name
    try:
        managed.relative_to(root)
    except ValueError as exc:
        raise AudioDeliveryError(f"{label}が出力フォルダ外です: {path}") from exc
    _reject_managed_link(managed, label)
    return managed


def _relative_output(root: Path, path: Path) -> str:
    candidate = _managed_output_path(root, path, "音声出力")
    return candidate.relative_to(root).as_posix()


def _normalize_media_input(
    root: Path,
    path: str | os.PathLike[str],
) -> Path:
    """Validate the caller's lexical path before any link can be resolved."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return _managed_output_path(root, candidate, "入力動画")


def _normalize_effect_manifest_dirs(
    root: Path,
    normalized_groups: Mapping[str, tuple[Path, ...]],
    directories: Mapping[str, str | os.PathLike[str]] | None,
) -> dict[str, Path]:
    """Validate optional per-group VFX manifest directories."""

    if not directories:
        return {}
    normalized: dict[str, Path] = {}
    for raw_group, raw_directory in directories.items():
        group = str(raw_group)
        if group not in normalized_groups:
            raise AudioDeliveryError(
                f"VFX来歴フォルダの出力グループが不明です: {group}"
            )
        paths = normalized_groups[group]
        if not paths:
            continue
        directory = _managed_output_path(
            root,
            Path(raw_directory).expanduser(),
            "VFX来歴フォルダ",
        )
        if not directory.is_dir():
            raise AudioDeliveryError(
                f"VFX来歴フォルダが見つかりません: {directory}"
            )
        if any(path.parent != directory for path in paths):
            raise AudioDeliveryError(
                f"VFX来歴フォルダと動画の保存先が一致しません: {directory}"
            )
        normalized[group] = directory
    return normalized


def _current_clean_paths(
    root: Path,
    normalized_groups: Mapping[str, tuple[Path, ...]],
) -> set[Path]:
    """Return validated current inputs that stale cleanup must never remove."""

    return {
        _managed_output_path(root, path, "入力動画")
        for paths in normalized_groups.values()
        for path in paths
    }


def _potential_generated_paths(
    root: Path,
    normalized_groups: Mapping[str, tuple[Path, ...]],
) -> tuple[set[Path], set[Path]]:
    """Preflight every clean input and return all paths delivery may mutate."""

    ordered_clean = [path for paths in normalized_groups.values() for path in paths]
    clean_paths = _current_clean_paths(root, normalized_groups)
    if len(clean_paths) != len(ordered_clean):
        raise AudioDeliveryError("同じ動画を複数の音声出力グループで処理できません")

    generated_paths: set[Path] = set()
    owners: dict[Path, Path] = {}
    for clean in ordered_clean:
        clean = _managed_output_path(root, clean, "入力動画")
        base = clean.with_suffix("")
        for generated in (
            base.with_name(f"{base.name}_bgm.wav"),
            base.with_name(f"{base.name}_se.wav"),
            base.with_name(f"{base.name}_mixed.mp4"),
            base.with_name(f"{base.name}_audio.json"),
        ):
            generated = _managed_output_path(root, generated, "音声出力")
            if generated in clean_paths:
                raise AudioDeliveryError(
                    f"音声出力パスが別の入力動画と衝突します: {generated}"
                )
            previous_owner = owners.get(generated)
            if previous_owner is not None and previous_owner != clean:
                raise AudioDeliveryError(
                    f"複数の動画が同じ音声出力パスを生成します: {generated}"
                )
            owners[generated] = clean
            generated_paths.add(generated)
    return clean_paths, generated_paths


def _read_previous_manifest(root: Path) -> dict[str, Any] | None:
    path = root / AUDIO_MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _validate_root_sidecars(root: Path) -> None:
    for name in (AUDIO_MANIFEST_NAME, AUDIO_NOTICES_NAME):
        _managed_output_path(root, root / name, "音声来歴ファイル")


def _manifest_generated_paths(
    root: Path,
    manifest: Mapping[str, Any] | None,
) -> set[Path]:
    """Return only generated files referenced by a validated prior manifest."""

    current_manifest = _is_owned_audio_manifest(manifest)
    if not current_manifest and not _is_legacy_audio_manifest(manifest):
        return set()
    assert manifest is not None
    groups = manifest.get("groups")
    assert isinstance(groups, Mapping)
    generated: set[Path] = set()
    for records in groups.values():
        if not isinstance(records, list):
            raise AudioDeliveryError("以前の音声manifestのgroupsが不正です")
        for record in records:
            if not isinstance(record, Mapping):
                raise AudioDeliveryError("以前の音声manifestのrecordが不正です")
            source_relative = _manifest_source_clean_relative(manifest, record)
            clean = _safe_manifest_clean_video(root, source_relative)
            base = clean.with_suffix("")
            expected_paths = {
                "bgm_stem": base.with_name(f"{base.name}_bgm.wav"),
                "se_stem": base.with_name(f"{base.name}_se.wav"),
                "mixed_video": base.with_name(f"{base.name}_mixed.mp4"),
                "clip_audio_manifest": base.with_name(f"{base.name}_audio.json"),
            }
            artifacts = record.get("artifacts")
            if not isinstance(artifacts, list):
                raise AudioDeliveryError("以前の音声manifestのartifactsが不正です")
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    raise AudioDeliveryError("以前の音声manifestのartifactが不正です")
                kind = artifact.get("kind")
                relative = artifact.get("file")
                if kind not in _GENERATED_AUDIO_KINDS or not isinstance(relative, str):
                    continue
                candidate = _safe_manifest_output(root, relative, str(kind))
                if candidate != expected_paths[str(kind)]:
                    raise AudioDeliveryError(
                        "以前の音声manifestの生成物がclean動画名と一致しません"
                    )
                if current_manifest:
                    _validate_owned_artifact(candidate, artifact)
                generated.add(candidate)
    return generated


def _is_owned_audio_manifest(
    manifest: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(manifest, Mapping):
        return False
    return bool(
        manifest.get("schema_version") == 2
        and manifest.get("generator") == "clip-extractor"
        and isinstance(manifest.get("groups"), Mapping)
        and isinstance(manifest.get("notices"), Mapping)
    )


def _is_legacy_audio_manifest(
    manifest: Mapping[str, Any] | None,
) -> bool:
    """Recognize the pre-fingerprint schema for recoverable migration only."""

    if not isinstance(manifest, Mapping):
        return False
    return bool(
        manifest.get("schema_version") == 1
        and isinstance(manifest.get("groups"), Mapping)
    )


def _manifest_source_clean_relative(
    manifest: Mapping[str, Any],
    record: Mapping[str, Any],
) -> str:
    source = record.get("source_clean_video")
    if isinstance(source, str):
        return source
    artifacts = record.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if (
                isinstance(artifact, Mapping)
                and artifact.get("kind") == "clean_video"
                and isinstance(artifact.get("file"), str)
            ):
                return str(artifact["file"])
    primary = record.get("primary_video")
    if (
        manifest.get("delivery_mode") == "mixed"
        and isinstance(primary, str)
        and primary.endswith("_mixed.mp4")
    ):
        return primary[: -len("_mixed.mp4")] + ".mp4"
    raise AudioDeliveryError(
        "以前の音声manifestにsource_clean_videoがありません"
    )


def _safe_manifest_clean_video(root: Path, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise AudioDeliveryError("以前の音声manifestに安全でないパスがあります")
    candidate = _managed_output_path(root, root / rel, "以前の音声manifest")
    if candidate.suffix.lower() != ".mp4":
        raise AudioDeliveryError("以前の音声manifestのclean動画名が不正です")
    return candidate


def _validate_owned_artifact(
    path: Path,
    artifact: Mapping[str, Any],
) -> None:
    expected_size = artifact.get("size_bytes")
    expected_digest = artifact.get("sha256")
    if (
        not isinstance(expected_size, int)
        or expected_size < 0
        or not isinstance(expected_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
    ):
        raise AudioDeliveryError("以前の音声manifestに成果物の検証値がありません")
    if not path.exists():
        return
    size, digest = _file_fingerprint(path)
    if size != expected_size or digest != expected_digest:
        raise AudioDeliveryError(
            f"以前の生成物が別ファイルへ変更されているため保持します: {path}"
        )


def _validate_owned_notice(
    root: Path,
    manifest: Mapping[str, Any] | None,
) -> None:
    if not isinstance(manifest, Mapping):
        return
    notice = manifest.get("notices")
    if not isinstance(notice, Mapping) or notice.get("file") != AUDIO_NOTICES_NAME:
        raise AudioDeliveryError("以前の音声manifestのnotice情報が不正です")
    path = root / AUDIO_NOTICES_NAME
    _validate_owned_artifact(path, notice)


def _safe_manifest_output(root: Path, relative: str, kind: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise AudioDeliveryError("以前の音声manifestに安全でないパスがあります")
    candidate = _managed_output_path(root, root / rel, "以前の音声manifest")
    expected = {
        "bgm_stem": "_bgm.wav",
        "se_stem": "_se.wav",
        "mixed_video": "_mixed.mp4",
        "clip_audio_manifest": "_audio.json",
    }[kind]
    if not candidate.name.endswith(expected):
        raise AudioDeliveryError("以前の音声manifestの出力名が不正です")
    return candidate


def _remove_transaction_tree(path: Path, root: Path) -> None:
    """Remove only a transaction directory created directly under output root."""

    expected_root = root.resolve()
    if path.parent.resolve() != expected_root or not path.name.startswith(
        _TRANSACTION_PREFIX
    ):
        raise AudioDeliveryError(
            f"安全でない音声トランザクション領域は削除できません: {path}"
        )
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        if not path.is_dir():
            raise AudioDeliveryError(
                f"音声トランザクション領域がディレクトリではありません: {path}"
            )
        shutil.rmtree(path)


def _third_party_notices(selection: Mapping[str, Any]) -> str:
    lines = [
        "Clip Extractor - optional audio material notices",
        "",
    ]
    installed_assets = [
        asset
        for key in ("bgm", "se")
        if isinstance((asset := selection.get(key)), InstalledAsset)
    ]
    if installed_assets:
        lines.extend(
            [
                f"Pack: {selection['pack_id']} {selection['pack_version']}",
                f"License review date: {selection['license_checked_at']}",
                "",
            ]
        )
        licenses: dict[tuple[str, str], bool] = {}
        for asset in installed_assets:
            key = (asset.license_id, asset.license_url)
            licenses[key] = licenses.get(key, False) or asset.attribution_required
        for (license_id, license_url), attribution_required in licenses.items():
            lines.extend(
                [
                    f"License: {license_id} ({license_url})",
                    "Attribution required: "
                    + ("yes" if attribution_required else "no"),
                    "",
                ]
            )

        required_credits = list(
            dict.fromkeys(
                asset.attribution_text
                for asset in installed_assets
                if asset.attribution_required and asset.attribution_text
            )
        )
        if required_credits:
            lines.append("公開時に必要なクレジット / Required publication credit:")
            lines.extend(required_credits)
            lines.extend(
                [
                    "動画の概要欄など、作品と合理的に結びつく場所へ記載してください。",
                    "",
                ]
            )
        if any(asset.creator == "OtoLogic" for asset in installed_assets):
            lines.extend(
                [
                    "注意: OtoLogic素材を含む作品をContent IDへ登録したり、"
                    "素材の独占権を主張したりしないでください。",
                    "",
                ]
            )
    for key, heading in (("bgm", "BGM"), ("se", "SE")):
        asset = selection.get(key)
        if asset is None:
            continue
        if isinstance(asset, UserMediaAsset):
            lines.extend(
                [
                    f"[{heading}] {asset.filename}",
                    "User-provided material",
                    f"Source SHA-256: {asset.sha256}",
                    "License and permission are managed by the user and were not "
                    "independently verified by Clip Extractor.",
                    "Modifications: output gain/loop/cue may be applied.",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    f"[{heading}] {asset.label}",
                    f"Creator: {asset.creator}",
                    f"Source: {asset.source_page}",
                    f"License: {asset.license_id} ({asset.license_url})",
                    "Attribution required: "
                    + ("yes" if asset.attribution_required else "no"),
                    *(
                        [f"Required credit: {asset.attribution_text}"]
                        if asset.attribution_required and asset.attribution_text
                        else []
                    ),
                    f"Source SHA-256: {asset.sha256}",
                    "Modifications: source unchanged; output gain/loop/cue may be applied.",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _write_text_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
