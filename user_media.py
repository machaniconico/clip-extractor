"""Index and validate user-provided BGM, SE, and lightweight VFX files.

The user chooses persistent folders.  Files are referenced in place and never
copied into the pinned CC0 asset-pack cache.  Content-addressed IDs keep saved
selections deterministic across renames while render-time resolution detects
deleted or replaced files before FFmpeg starts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Literal


UserMediaKind = Literal["bgm", "se", "vfx"]

AUDIO_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
)
VFX_EXTENSIONS = frozenset({".png", ".webm"})
MAX_ASSET_COUNT = 1_000
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 4 * 1024 * 1024 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
_USER_MEDIA_ID = re.compile(r"^user:(bgm|se|vfx):([0-9a-f]{64})$")


class UserMediaError(RuntimeError):
    """Raised when a user media folder or selected file cannot be trusted."""


@dataclass(frozen=True)
class UserMediaAsset:
    """One content-addressed file discovered in a user-selected folder."""

    id: str
    kind: UserMediaKind
    path: Path
    filename: str
    relative_path: str
    size: int
    sha256: str
    video_codec: str = ""
    video_has_alpha: bool = False

    @property
    def label(self) -> str:
        return self.relative_path

    @property
    def source_type(self) -> str:
        return "user_provided"


def scan_optional_user_media(
    folder: str | os.PathLike[str] | None,
    kind: UserMediaKind | str,
) -> tuple[UserMediaAsset, ...]:
    """Return no assets for an empty setting, otherwise scan the folder."""

    text = str(folder or "").strip()
    if not text:
        return ()
    return scan_user_media(text, kind)


def scan_user_media(
    folder: str | os.PathLike[str],
    kind: UserMediaKind | str,
) -> tuple[UserMediaAsset, ...]:
    """Recursively index supported regular files without following links."""

    normalized_kind = _normalize_kind(kind)
    root = Path(folder).expanduser()
    if _is_link_or_junction(root):
        raise UserMediaError(f"素材フォルダにシンボリックリンクは使えません: {root}")
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UserMediaError(f"素材フォルダが見つかりません: {root}") from exc
    if not root.is_dir():
        raise UserMediaError(f"素材フォルダではありません: {root}")

    try:
        candidates = _regular_candidates(root, normalized_kind)
        if len(candidates) > MAX_ASSET_COUNT:
            raise UserMediaError(
                f"{normalized_kind.upper()}素材が上限{MAX_ASSET_COUNT}件を超えています"
            )

        total_size = sum(path.stat().st_size for path in candidates)
        if total_size > MAX_TOTAL_ASSET_BYTES:
            raise UserMediaError(
                "素材フォルダの対応ファイル合計が走査上限4GBを超えています"
            )

        by_digest: dict[str, UserMediaAsset] = {}
        for path in candidates:
            size, digest = _hash_regular_file(path)
            asset_id = f"user:{normalized_kind}:{digest}"
            if digest in by_digest:
                continue
            relative = path.relative_to(root).as_posix()
            by_digest[digest] = UserMediaAsset(
                id=asset_id,
                kind=normalized_kind,
                path=path,
                filename=path.name,
                relative_path=relative,
                size=size,
                sha256=digest,
            )
    except OSError as exc:
        raise UserMediaError(
            f"素材フォルダの走査中にファイルが変更されました: {root}"
        ) from exc
    return tuple(by_digest.values())


def resolve_user_media_asset(
    folder: str | os.PathLike[str],
    asset_id: str,
    expected_kind: UserMediaKind | str,
) -> UserMediaAsset:
    """Resolve a saved content ID from its configured folder."""

    kind = _normalize_kind(expected_kind)
    identifier = str(asset_id or "").strip()
    match = _USER_MEDIA_ID.fullmatch(identifier)
    if match is None:
        raise UserMediaError(f"ユーザー素材IDが不正です: {identifier}")
    identifier_kind = match.group(1)
    if identifier_kind != kind:
        raise UserMediaError(f"選択した素材の種類が{kind.upper()}ではありません")
    for asset in scan_user_media(folder, kind):
        if asset.id == identifier:
            return asset
    raise UserMediaError(
        "選択した素材が参照フォルダに見つかりません。"
        "素材の状態を更新して選び直してください"
    )


def validate_user_media(
    asset: UserMediaAsset,
    *,
    ffprobe_bin: str = "ffprobe",
) -> UserMediaAsset:
    """Verify the selected file still matches its ID and has the right stream."""

    if not isinstance(asset, UserMediaAsset):
        raise TypeError("asset must be a UserMediaAsset")
    if _is_link_or_junction(asset.path):
        raise UserMediaError(f"素材ファイルにシンボリックリンクは使えません: {asset.path}")
    try:
        size, digest = _hash_regular_file(asset.path)
    except (OSError, UserMediaError) as exc:
        raise UserMediaError(f"素材ファイルを確認できません: {asset.path}") from exc
    if size != asset.size or digest != asset.sha256:
        raise UserMediaError(
            "素材ファイルが選択後に変更されました。素材の状態を更新してください"
        )

    stream_types = _probe_stream_types(asset.path, ffprobe_bin)
    required = "video" if asset.kind == "vfx" else "audio"
    if required not in stream_types:
        label = "映像" if required == "video" else "音声"
        raise UserMediaError(f"{asset.filename} に{label}ストリームがありません")
    validated = asset
    if asset.kind == "vfx":
        codec, has_alpha = _probe_video_details(asset.path, ffprobe_bin)
        validated = replace(
            asset,
            video_codec=codec,
            video_has_alpha=has_alpha,
        )
    final_size, final_digest = _hash_regular_file(asset.path)
    if final_size != asset.size or final_digest != asset.sha256:
        raise UserMediaError(
            "素材ファイルがメディア確認中に変更されました。素材の状態を更新してください"
        )
    return validated


def is_user_media_id(value: str | None, kind: UserMediaKind | str | None = None) -> bool:
    identifier = str(value or "").strip()
    if kind is None:
        return identifier.startswith("user:")
    return identifier.startswith(f"user:{_normalize_kind(kind)}:")


def _normalize_kind(kind: UserMediaKind | str) -> UserMediaKind:
    value = str(kind or "").strip().lower()
    if value not in {"bgm", "se", "vfx"}:
        raise ValueError("kind must be one of: bgm, se, vfx")
    return value  # type: ignore[return-value]


def _extensions_for(kind: UserMediaKind) -> frozenset[str]:
    return VFX_EXTENSIONS if kind == "vfx" else AUDIO_EXTENSIONS


def _regular_candidates(root: Path, kind: UserMediaKind) -> list[Path]:
    extensions = _extensions_for(kind)
    found: list[Path] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise UserMediaError(f"素材フォルダを読み取れません: {directory}") from exc
        for entry in entries:
            entry_path = Path(entry.path)
            if (
                entry.name.startswith(".")
                or entry.is_symlink()
                or _is_link_or_junction(entry_path)
            ):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry_path)
                elif (
                    entry.is_file(follow_symlinks=False)
                    and Path(entry.name).suffix.lower() in extensions
                ):
                    resolved = entry_path.resolve(strict=True)
                    try:
                        resolved.relative_to(root)
                    except ValueError as exc:
                        raise UserMediaError(
                            f"素材フォルダ外のファイルは参照できません: {entry.path}"
                        ) from exc
                    found.append(resolved)
            except OSError as exc:
                raise UserMediaError(f"素材ファイルを確認できません: {entry.path}") from exc
    found.sort(
        key=lambda path: (
            len(path.relative_to(root).parts),
            path.relative_to(root).as_posix().casefold(),
        )
    )
    return found


def _hash_regular_file(path: Path) -> tuple[int, str]:
    if _is_link_or_junction(path) or not path.is_file():
        raise UserMediaError(f"通常ファイルではありません: {path}")
    before = path.stat()
    if before.st_size > MAX_ASSET_BYTES:
        raise UserMediaError(
            f"素材ファイルが上限{MAX_ASSET_BYTES // (1024 * 1024)}MBを超えています: "
            f"{path.name}"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ino != after.st_ino
    ):
        raise UserMediaError(f"走査中に素材ファイルが変更されました: {path.name}")
    return after.st_size, digest.hexdigest()


def _is_link_or_junction(path: Path) -> bool:
    candidate = Path(path)
    if candidate.is_symlink():
        return True
    is_junction = getattr(candidate, "is_junction", None)
    return bool(is_junction and is_junction())


def _probe_stream_types(path: Path, ffprobe_bin: str) -> set[str]:
    try:
        completed = subprocess.run(
            [
                str(ffprobe_bin),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserMediaError(f"ffprobeで素材を確認できません: {path.name}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise UserMediaError(
            f"ffprobeが素材を読み取れません: {path.name}"
            + (f" ({detail})" if detail else "")
        )
    try:
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise UserMediaError(f"ffprobeの結果が不正です: {path.name}") from exc
    return {
        str(stream.get("codec_type"))
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type")
    }


def _probe_video_details(path: Path, ffprobe_bin: str) -> tuple[str, bool]:
    try:
        completed = subprocess.run(
            [
                str(ffprobe_bin),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,pix_fmt:stream_tags=alpha_mode",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UserMediaError(f"ffprobeでVFXを確認できません: {path.name}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise UserMediaError(
            f"ffprobeがVFXを読み取れません: {path.name}"
            + (f" ({detail})" if detail else "")
        )
    try:
        payload = json.loads(completed.stdout or "{}")
        streams = payload.get("streams", [])
        stream = streams[0] if isinstance(streams, list) and streams else {}
    except (json.JSONDecodeError, AttributeError, IndexError) as exc:
        raise UserMediaError(f"ffprobeのVFX結果が不正です: {path.name}") from exc
    if not isinstance(stream, dict):
        raise UserMediaError(f"ffprobeのVFX結果が不正です: {path.name}")
    tags = stream.get("tags")
    normalized_tags = {
        str(key).casefold(): str(value)
        for key, value in tags.items()
    } if isinstance(tags, dict) else {}
    pix_fmt = str(stream.get("pix_fmt") or "").casefold()
    has_alpha = normalized_tags.get("alpha_mode") == "1" or pix_fmt.startswith(
        "yuva"
    )
    return str(stream.get("codec_name") or "").casefold(), has_alpha
