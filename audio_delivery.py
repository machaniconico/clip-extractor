"""Resolve optional audio assets and apply them to rendered media groups.

This module is the shared integration boundary for the Web and CLI pipelines.
It never downloads assets: rendering only accepts a previously installed,
verified starter pack from :mod:`audio_assets`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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


AUDIO_MANIFEST_NAME = "audio_manifest.json"
AUDIO_NOTICES_NAME = "THIRD_PARTY_NOTICES_AUDIO.txt"
_TRANSACTION_PREFIX = ".audio_delivery_transaction-"
_GENERATED_AUDIO_KINDS = frozenset(
    {"bgm_stem", "se_stem", "mixed_video", "clip_audio_manifest"}
)


class AudioDeliveryError(RuntimeError):
    """Raised when selected audio cannot be delivered safely."""


@dataclass(frozen=True)
class AudioDeliveryOptions:
    """User-selectable audio settings shared by every generated clip."""

    delivery_mode: AudioDeliveryMode | str = AudioDeliveryMode.BOTH
    bgm_asset_id: str = ""
    se_asset_id: str = ""
    bgm_gain_db: float = -18.0
    se_gain_db: float = -8.0
    se_cue_seconds: float = 0.0

    def __post_init__(self) -> None:
        bgm_id = str(self.bgm_asset_id or "").strip()
        se_id = str(self.se_asset_id or "").strip()
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
    ) -> None:
        self.root = root
        self.generated_paths = set(generated_paths)
        self.clean_paths = set(clean_paths)
        self.protect_clean = protect_clean
        self.backup_dir: Path | None = None
        self.moved_backups: list[tuple[Path, Path]] = []
        self.clean_backups: list[tuple[Path, Path]] = []
        self.active = False

    def begin(self, previous_manifest: Mapping[str, Any] | None) -> None:
        self.backup_dir = Path(
            tempfile.mkdtemp(prefix=_TRANSACTION_PREFIX, dir=str(self.root))
        )
        try:
            prior_paths = _manifest_generated_paths(self.root, previous_manifest)
            candidates = self.generated_paths | prior_paths
            candidates.update(
                {
                    self.root / AUDIO_MANIFEST_NAME,
                    self.root / AUDIO_NOTICES_NAME,
                }
            )
            for path in sorted(candidates, key=str):
                _reject_managed_link(path, "音声トランザクション対象")
                if path in self.clean_paths or not path.exists():
                    continue
                if not path.is_file():
                    raise AudioDeliveryError(
                        f"音声トランザクション対象がファイルではありません: {path}"
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
                    try:
                        os.link(clean, backup)
                    except OSError:
                        shutil.copy2(clean, backup)
                    self.clean_backups.append((backup, clean))
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
                if clean.exists():
                    backup.unlink()
                else:
                    clean.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, clean)
            except Exception as exc:
                rollback_error = rollback_error or exc

        try:
            _remove_transaction_tree(self.backup_dir, self.root)
        except Exception as exc:
            rollback_error = rollback_error or exc
        self.backup_dir = None
        self.active = False
        if rollback_error is not None:
            raise AudioDeliveryError(
                f"音声出力のrollbackに失敗しました: {rollback_error}"
            ) from rollback_error

    def complete(self) -> None:
        if self.backup_dir is None:
            return
        _remove_transaction_tree(self.backup_dir, self.root)
        self.backup_dir = None
        self.active = False

    def _next_backup(self, category: str, suffix: str) -> Path:
        if self.backup_dir is None:
            raise RuntimeError("audio delivery transaction has not started")
        index = len(self.moved_backups) + len(self.clean_backups)
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
    current_clean_paths = _current_clean_paths(root, normalized_groups)
    _validate_root_sidecars(root)
    previous_manifest = _read_previous_manifest(root)

    if not options.enabled:
        transaction = _AudioDeliveryTransaction(
            root,
            set(),
            current_clean_paths,
            protect_clean=False,
        )
        transaction.begin(previous_manifest)
        transaction.complete()
        return AudioDeliveryResult(enabled=False, media_groups=normalized_groups)

    selection = _resolve_selection(options)
    clean_paths, generated_paths = _potential_generated_paths(root, normalized_groups)
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
    )
    transaction.begin(previous_manifest)
    try:
        result = _deliver_enabled_audio(
            root,
            normalized_groups,
            highlights,
            selection=selection,
            settings=settings,
            previous_manifest=previous_manifest,
            clean_paths=clean_paths,
        )
    except BaseException:
        transaction.rollback()
        raise
    transaction.complete()
    return result


def _deliver_enabled_audio(
    root: Path,
    normalized_groups: Mapping[str, tuple[Path, ...]],
    highlights: Sequence[Mapping[str, Any]],
    *,
    selection: Mapping[str, Any],
    settings: AudioMixSettings,
    previous_manifest: Mapping[str, Any] | None,
    clean_paths: set[Path],
) -> AudioDeliveryResult:
    """Generate one enabled audio delivery inside an active transaction."""

    provenance = {
        "pack": {
            "id": selection["pack_id"],
            "version": selection["pack_version"],
            "license_checked_at": selection["license_checked_at"],
        },
        "bgm": _asset_provenance(selection.get("bgm")),
        "se": _asset_provenance(selection.get("se")),
    }

    primary_groups: dict[str, tuple[Path, ...]] = {}
    deliverables: list[Path] = []
    manifest_groups: dict[str, list[dict[str, Any]]] = {}
    keep_paths: set[Path] = set(clean_paths)

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
        for result in batch.clips:
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
            for artifact in artifacts:
                if artifact["kind"] in _GENERATED_AUDIO_KINDS:
                    keep_paths.add(
                        _managed_output_path(
                            root,
                            root / artifact["file"],
                            "生成した音声成果物",
                        )
                    )
            record: dict[str, Any] = {
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

    manifest = {
        "schema_version": 1,
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
        "groups": manifest_groups,
    }
    manifest_path = root / AUDIO_MANIFEST_NAME
    notices_path = root / AUDIO_NOTICES_NAME

    # New outputs are already complete at this point. Remove only prior files
    # identified by our own manifest and not retained by the current render.
    _cleanup_previous_artifacts(
        root,
        previous_manifest,
        keep=frozenset(keep_paths),
    )
    _write_text_atomic(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    _write_text_atomic(
        notices_path,
        _third_party_notices(selection),
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
    status = get_pack_status()
    if not status.ready:
        raise AudioDeliveryError(
            "BGM/SE素材パックが利用できません。Input画面の"
            "「CC0素材をダウンロード」を先に実行してください。"
        )
    resolved: dict[str, Any] = {
        "pack_id": status.pack_id,
        "pack_version": status.version,
        "license_checked_at": "",
    }
    try:
        for key, asset_id, expected_kind in (
            ("bgm", options.bgm_asset_id, "bgm"),
            ("se", options.se_asset_id, "se"),
        ):
            if not asset_id:
                continue
            asset = get_installed_asset(asset_id)
            if asset.kind != expected_kind:
                raise AudioDeliveryError(
                    f"{asset_id} は {expected_kind.upper()} 素材ではありません"
                )
            resolved[key] = asset
            resolved["license_checked_at"] = asset.license_checked_at
    except (AudioAssetError, KeyError) as exc:
        raise AudioDeliveryError(f"選択したBGM/SE素材を確認できません: {exc}") from exc
    return resolved


def _asset_provenance(asset: InstalledAsset | None) -> dict[str, Any]:
    if asset is None:
        return {}
    return {
        "id": asset.id,
        "label": asset.label,
        "kind": asset.kind,
        "creator": asset.creator,
        "source_page": asset.source_page,
        "license_id": asset.license_id,
        "license_url": asset.license_url,
        "license_checked_at": asset.license_checked_at,
        "attribution_required": asset.attribution_required,
        "pack_id": asset.pack_id,
        "pack_version": asset.pack_version,
        "source_sha256": asset.sha256,
        "source_size_bytes": asset.size,
        "modifications": "Source unchanged; gain/loop/cue are applied to generated outputs.",
    }


def _result_artifacts(root: Path, result: Any) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for kind, path in (
        ("clean_video", result.clean_video),
        ("mixed_video", result.mixed_video),
        ("bgm_stem", result.bgm_stem),
        ("se_stem", result.se_stem),
        ("clip_audio_manifest", result.manifest),
    ):
        if path is not None:
            artifacts.append({"kind": kind, "file": _relative_output(root, path)})
    return artifacts


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


def _cleanup_previous_artifacts(
    root: Path,
    manifest: Mapping[str, Any] | None,
    *,
    keep: frozenset[Path],
) -> None:
    for candidate in _manifest_generated_paths(root, manifest):
        if candidate not in keep:
            candidate.unlink(missing_ok=True)


def _manifest_generated_paths(
    root: Path,
    manifest: Mapping[str, Any] | None,
) -> set[Path]:
    """Return only generated files referenced by a validated prior manifest."""

    if not manifest:
        return set()
    groups = manifest.get("groups")
    if not isinstance(groups, Mapping):
        return set()
    generated: set[Path] = set()
    for records in groups.values():
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            artifacts = record.get("artifacts")
            if not isinstance(artifacts, list):
                continue
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                kind = artifact.get("kind")
                relative = artifact.get("file")
                if kind not in _GENERATED_AUDIO_KINDS or not isinstance(relative, str):
                    continue
                generated.add(_safe_manifest_output(root, relative, str(kind)))
    return generated


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
        f"Pack: {selection['pack_id']} {selection['pack_version']}",
        f"License review date: {selection['license_checked_at']}",
        "",
        "The selected works are provided under CC0 1.0 Universal.",
        "Attribution is not required, but provenance is retained here.",
        "https://creativecommons.org/publicdomain/zero/1.0/",
        "",
    ]
    for key, heading in (("bgm", "BGM"), ("se", "SE")):
        asset = selection.get(key)
        if asset is None:
            continue
        lines.extend(
            [
                f"[{heading}] {asset.label}",
                f"Creator: {asset.creator}",
                f"Source: {asset.source_page}",
                f"License: {asset.license_id} ({asset.license_url})",
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
