"""Install and resolve the optional, license-tracked audio starter pack.

Downloads only happen through :func:`install_pack`, which is intended to be
called from an explicit user action.  Read APIs never access the network.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, BinaryIO, Iterable
from urllib import request
from urllib.parse import urlsplit
import uuid
import zipfile


DEFAULT_PACK_ID = "short-video-starter"
ASSET_CACHE_ENV = "CLIP_EXTRACTOR_ASSET_CACHE"
CATALOG_PATH = Path(__file__).resolve().parent / "assets" / "audio" / "catalog.json"
LICENSES_PATH = CATALOG_PATH.parent / "licenses"
DOWNLOAD_TIMEOUT_SECONDS = 45
DOWNLOAD_CHUNK_SIZE = 1024 * 256
ABSOLUTE_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
ABSOLUTE_MAX_ASSET_BYTES = 25 * 1024 * 1024
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DOWNLOAD_HOST_ALLOWLIST = frozenset(
    {
        "kenney.nl",
        "www.kenney.nl",
        "opengameart.org",
        "www.opengameart.org",
        "otologic.jp",
        "www.otologic.jp",
    }
)


class AudioAssetError(RuntimeError):
    """Base exception for the audio asset-pack subsystem."""


class UnknownPackError(AudioAssetError):
    """Raised when a pack ID is not present in the bundled catalog."""


class AssetPackNotInstalledError(AudioAssetError):
    """Raised when an asset is requested before an explicit pack install."""


class DownloadSecurityError(AudioAssetError):
    """Raised when a source URL or archive member fails a safety check."""


class AssetIntegrityError(AudioAssetError):
    """Raised when downloaded or installed bytes do not match the catalog."""


class MediaValidationError(AudioAssetError):
    """Raised when ffprobe cannot decode a downloaded audio asset."""


class _AllowlistedRedirectHandler(request.HTTPRedirectHandler):
    """Reject a redirect before urllib contacts a non-allowlisted hop."""

    def __init__(self, allowed_hosts: set[str]):
        super().__init__()
        self._allowed_hosts = set(allowed_hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl, self._allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class CatalogAsset:
    """An asset advertised by the bundled, pinned catalog."""

    id: str
    label: str
    kind: str
    pack_id: str
    pack_version: str
    license_checked_at: str
    creator: str
    source_page: str
    license_id: str
    license_url: str
    attribution_required: bool
    attribution_text: str = ""


@dataclass(frozen=True)
class InstalledAsset:
    """An installed asset and its verified local path."""

    id: str
    label: str
    kind: str
    path: Path
    pack_id: str
    pack_version: str
    size: int
    sha256: str
    creator: str
    source_page: str
    license_id: str
    license_url: str
    license_checked_at: str
    attribution_required: bool
    attribution_text: str = ""


@dataclass(frozen=True)
class PackStatus:
    """Current state of the catalog's active version of an asset pack."""

    pack_id: str
    version: str
    state: str
    path: Path
    asset_count: int
    message: str

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["path"] = str(self.path)
        result["ready"] = self.ready
        return result


def get_cache_root(cache_root: str | Path | None = None) -> Path:
    """Return the asset-pack cache root without creating it."""

    if cache_root is not None:
        return Path(cache_root).expanduser().resolve()
    override = os.environ.get(ASSET_CACHE_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return (
            Path(local_app_data).expanduser().resolve()
            / "ClipExtractor"
            / "asset-packs"
        )
    return Path.home().resolve() / ".local" / "share" / "ClipExtractor" / "asset-packs"


def list_catalog_assets(pack_id: str = DEFAULT_PACK_ID) -> list[CatalogAsset]:
    """List catalog metadata without downloading or touching the cache."""

    catalog, _catalog_sha256 = _load_catalog()
    pack = _find_pack(catalog, pack_id)
    return [_catalog_asset(pack, asset) for _source, asset in _iter_assets(pack)]


def get_pack_status(
    pack_id: str = DEFAULT_PACK_ID,
    *,
    cache_root: str | Path | None = None,
    verify_files: bool = True,
) -> PackStatus:
    """Inspect the current catalog version; this function never downloads."""

    try:
        catalog, catalog_sha256 = _load_catalog()
        pack = _find_pack(catalog, pack_id)
    except AudioAssetError:
        raise
    version = str(pack["version"])
    target = _pack_path(pack_id, version, cache_root)
    if not target.is_dir():
        return PackStatus(
            pack_id,
            version,
            "not_installed",
            target,
            0,
            "素材パックは未導入です。明示的にダウンロードしてください。",
        )
    try:
        manifest = _read_installed_manifest(target)
        _verify_installed_manifest(
            target,
            manifest,
            pack,
            catalog_sha256,
            verify_files=verify_files,
        )
    except (AudioAssetError, OSError, ValueError, json.JSONDecodeError) as exc:
        return PackStatus(
            pack_id,
            version,
            "invalid",
            target,
            0,
            f"導入済み素材の検証に失敗しました: {exc}",
        )
    return PackStatus(
        pack_id,
        version,
        "ready",
        target,
        len(manifest["assets"]),
        "素材パックは利用できます。",
    )


def list_installed_assets(
    pack_id: str = DEFAULT_PACK_ID,
    *,
    cache_root: str | Path | None = None,
    verify_files: bool = True,
) -> list[InstalledAsset]:
    """Return installed assets, or an empty list when the pack is not ready."""

    status = get_pack_status(pack_id, cache_root=cache_root, verify_files=verify_files)
    if not status.ready:
        return []
    catalog, catalog_sha256 = _load_catalog()
    pack = _find_pack(catalog, pack_id)
    manifest = _read_installed_manifest(status.path)
    try:
        # Verify the same in-memory manifest used below.  This closes the gap
        # where the file could change after get_pack_status() verified it.
        _verify_installed_manifest(
            status.path,
            manifest,
            pack,
            catalog_sha256,
            verify_files=verify_files,
        )
    except (AudioAssetError, OSError, ValueError, json.JSONDecodeError):
        return []
    return [
        _installed_asset(status.path, entry, manifest) for entry in manifest["assets"]
    ]


def get_installed_asset(
    asset_id: str,
    pack_id: str = DEFAULT_PACK_ID,
    *,
    cache_root: str | Path | None = None,
    verify_files: bool = True,
) -> InstalledAsset:
    """Return one installed asset together with output-notice provenance."""

    assets = list_installed_assets(
        pack_id, cache_root=cache_root, verify_files=verify_files
    )
    if not assets:
        status = get_pack_status(
            pack_id, cache_root=cache_root, verify_files=verify_files
        )
        raise AssetPackNotInstalledError(status.message)
    for asset in assets:
        if asset.id == asset_id:
            return asset
    raise KeyError(f"Unknown audio asset ID in installed pack: {asset_id}")


def resolve_asset(
    asset_id: str,
    pack_id: str = DEFAULT_PACK_ID,
    *,
    cache_root: str | Path | None = None,
    verify_files: bool = True,
) -> Path:
    """Resolve one installed asset without ever initiating a download."""

    return get_installed_asset(
        asset_id,
        pack_id,
        cache_root=cache_root,
        verify_files=verify_files,
    ).path


def install_pack(
    pack_id: str = DEFAULT_PACK_ID,
    *,
    cache_root: str | Path | None = None,
    force: bool = False,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
) -> PackStatus:
    """Explicitly download, verify, and atomically activate an asset pack.

    A complete existing installation is reused unless ``force`` is true.  All
    network and media validation happens in a private staging directory, so a
    failed refresh leaves the previous installation untouched.
    """

    catalog, catalog_sha256 = _load_catalog()
    pack = _find_pack(catalog, pack_id)
    version = str(pack["version"])
    current = get_pack_status(pack_id, cache_root=cache_root)
    if current.ready and not force:
        return current

    target = _pack_path(pack_id, version, cache_root)
    pack_parent = target.parent
    pack_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{version}.install-", dir=str(pack_parent))
    )
    try:
        manifest = _build_installation(
            staging,
            pack,
            catalog_sha256,
            timeout=timeout,
        )
        _write_json_atomic(staging / "manifest.json", manifest)
        _verify_installed_manifest(
            staging,
            manifest,
            pack,
            catalog_sha256,
            verify_files=True,
        )
        _activate_installation(staging, target)
    except Exception:
        _remove_private_tree(staging, pack_parent)
        raise

    installed = get_pack_status(pack_id, cache_root=cache_root)
    if not installed.ready:
        raise AssetIntegrityError(installed.message)
    return installed


def _load_catalog() -> tuple[dict[str, Any], str]:
    try:
        raw = CATALOG_PATH.read_bytes()
        catalog = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AudioAssetError(f"Audio asset catalog could not be read: {exc}") from exc
    _validate_catalog(catalog)
    return catalog, hashlib.sha256(raw).hexdigest()


def _validate_catalog(catalog: dict[str, Any]) -> None:
    if catalog.get("schema_version") != 1 or not isinstance(catalog.get("packs"), list):
        raise AudioAssetError("Unsupported or malformed audio asset catalog")
    pack_ids: set[str] = set()
    for pack in catalog["packs"]:
        pack_id = str(pack.get("id", ""))
        version = str(pack.get("version", ""))
        _validate_id(pack_id, "pack ID")
        _validate_id(version, "pack version")
        if pack_id in pack_ids:
            raise AudioAssetError(f"Duplicate pack ID: {pack_id}")
        pack_ids.add(pack_id)
        if not isinstance(pack.get("sources"), list) or not pack["sources"]:
            raise AudioAssetError(f"Pack has no sources: {pack_id}")
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", str(pack.get("license_checked_at", ""))
        ):
            raise AudioAssetError(f"Pack has no valid license check date: {pack_id}")
        asset_ids: set[str] = set()
        asset_paths: set[str] = set()
        for source in pack["sources"]:
            _validate_source(source)
            source_hosts = {
                _normalize_host(str(host)) for host in source["allowed_hosts"]
            }
            for asset in source["assets"]:
                asset_id = str(asset.get("id", ""))
                _validate_id(asset_id, "asset ID")
                if asset_id in asset_ids:
                    raise AudioAssetError(f"Duplicate asset ID: {asset_id}")
                asset_ids.add(asset_id)
                relative = _safe_relative_path(str(asset.get("path", "")))
                relative_text = relative.as_posix()
                if relative_text in asset_paths:
                    raise AudioAssetError(f"Duplicate asset path: {relative_text}")
                asset_paths.add(relative_text)
                _validate_digest_and_size(asset, f"asset {asset_id}")
                _validate_asset_metadata(asset, source_hosts)
                if source["type"] == "zip":
                    _safe_archive_member(str(asset.get("archive_member", "")))


def _validate_source(source: dict[str, Any]) -> None:
    source_id = str(source.get("id", ""))
    _validate_id(source_id, "source ID")
    if source.get("type") not in {"file", "zip"}:
        raise AudioAssetError(f"Unsupported source type for {source_id}")
    _validate_digest_and_size(source, f"source {source_id}")
    max_bytes = source.get("max_bytes")
    if (
        not isinstance(max_bytes, int)
        or not 0 < max_bytes <= ABSOLUTE_MAX_DOWNLOAD_BYTES
    ):
        raise AudioAssetError(f"Invalid max_bytes for source {source_id}")
    if int(source["size"]) > max_bytes:
        raise AudioAssetError(f"Source {source_id} exceeds its max_bytes")
    allowed_hosts = source.get("allowed_hosts")
    if not isinstance(allowed_hosts, list) or not allowed_hosts:
        raise AudioAssetError(f"Source {source_id} has no final-host allowlist")
    normalized_hosts = {_normalize_host(str(host)) for host in allowed_hosts}
    if not normalized_hosts.issubset(_DOWNLOAD_HOST_ALLOWLIST):
        raise AudioAssetError(f"Source {source_id} contains an unapproved host")
    _validate_url(str(source.get("url", "")), normalized_hosts)
    _validate_url(str(source.get("source_page", "")), normalized_hosts)
    if not isinstance(source.get("assets"), list) or not source["assets"]:
        raise AudioAssetError(f"Source {source_id} has no selected assets")
    if source["type"] == "file" and len(source["assets"]) != 1:
        raise AudioAssetError(f"Direct source {source_id} must contain one asset")


def _validate_asset_metadata(asset: dict[str, Any], allowed_hosts: set[str]) -> None:
    asset_id = str(asset.get("id", ""))
    if asset.get("kind") not in {"bgm", "se"}:
        raise AudioAssetError(f"Invalid media kind for asset {asset_id}")
    for field in ("label", "creator", "modifications"):
        if not isinstance(asset.get(field), str) or not asset[field].strip():
            raise AudioAssetError(f"Asset {asset_id} has no {field}")
    _validate_url(str(asset.get("source_page", "")), allowed_hosts)

    license_info = asset.get("license")
    if not isinstance(license_info, dict):
        raise AudioAssetError(f"Asset {asset_id} has no license metadata")
    for field in ("id", "name", "url"):
        if not isinstance(license_info.get(field), str) or not license_info[field].strip():
            raise AudioAssetError(f"Asset {asset_id} has no license {field}")
    attribution_required = license_info.get("attribution_required")
    if not isinstance(attribution_required, bool):
        raise AudioAssetError(f"Asset {asset_id} has invalid attribution metadata")
    if attribution_required and not str(license_info.get("attribution_text", "")).strip():
        raise AudioAssetError(f"Asset {asset_id} has no required attribution text")


def _validate_digest_and_size(item: dict[str, Any], label: str) -> None:
    digest = str(item.get("sha256", ""))
    size = item.get("size")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AudioAssetError(f"Invalid SHA-256 for {label}")
    if not isinstance(size, int) or not 0 < size <= ABSOLUTE_MAX_ASSET_BYTES:
        raise AudioAssetError(f"Invalid byte size for {label}")


def _find_pack(catalog: dict[str, Any], pack_id: str) -> dict[str, Any]:
    for pack in catalog["packs"]:
        if pack["id"] == pack_id:
            return pack
    raise UnknownPackError(f"Unknown audio asset pack: {pack_id}")


def _iter_assets(
    pack: dict[str, Any],
) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    for source in pack["sources"]:
        for asset in source["assets"]:
            yield source, asset


def _catalog_asset(pack: dict[str, Any], asset: dict[str, Any]) -> CatalogAsset:
    license_info = asset["license"]
    return CatalogAsset(
        id=asset["id"],
        label=asset["label"],
        kind=asset["kind"],
        pack_id=pack["id"],
        pack_version=pack["version"],
        license_checked_at=pack["license_checked_at"],
        creator=asset["creator"],
        source_page=asset["source_page"],
        license_id=license_info["id"],
        license_url=license_info["url"],
        attribution_required=bool(license_info["attribution_required"]),
        attribution_text=str(license_info.get("attribution_text", "")),
    )


def _pack_path(pack_id: str, version: str, cache_root: str | Path | None) -> Path:
    _validate_id(pack_id, "pack ID")
    _validate_id(version, "pack version")
    return get_cache_root(cache_root) / pack_id / version


def _validate_id(value: str, label: str) -> None:
    if not _SAFE_ID.fullmatch(value):
        raise AudioAssetError(f"Invalid {label}: {value!r}")


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _validate_url(url: str, allowed_hosts: set[str]) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DownloadSecurityError(f"Invalid source URL: {url!r}") from exc
    host = _normalize_host(parsed.hostname or "")
    if (
        parsed.scheme.lower() != "https"
        or not host
        or host not in allowed_hosts
        or host not in _DOWNLOAD_HOST_ALLOWLIST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise DownloadSecurityError(f"Source URL is not allowlisted HTTPS: {url}")
    return host


def _build_installation(
    staging: Path,
    pack: dict[str, Any],
    catalog_sha256: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    downloads = staging / ".downloads"
    downloads.mkdir()
    manifest_sources: list[dict[str, Any]] = []
    manifest_assets: list[dict[str, Any]] = []
    try:
        for source in pack["sources"]:
            downloaded = downloads / f"{source['id']}.download"
            final_url = _download_source(source, downloaded, timeout=timeout)
            manifest_sources.append(
                {
                    "id": source["id"],
                    "url": source["url"],
                    "final_url": final_url,
                    "source_page": source["source_page"],
                    "size": source["size"],
                    "sha256": source["sha256"],
                    "verified": True,
                }
            )
            if source["type"] == "zip":
                installed = _extract_selected_assets(downloaded, staging, source)
            else:
                installed = _install_direct_asset(downloaded, staging, source)
            manifest_assets.extend(installed)
    finally:
        _remove_private_tree(downloads, staging)

    license_target = staging / "LICENSES"
    license_target.mkdir()
    copied_licenses: list[str] = []
    for filename in pack.get("license_files", []):
        source_file = LICENSES_PATH / filename
        if not source_file.is_file():
            raise AudioAssetError(f"Bundled license file is missing: {filename}")
        target_file = license_target / filename
        shutil.copyfile(source_file, target_file)
        copied_licenses.append(f"LICENSES/{filename}")

    return {
        "schema_version": 1,
        "pack_id": pack["id"],
        "pack_version": pack["version"],
        "display_name": pack["display_name"],
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "license_checked_at": pack["license_checked_at"],
        "catalog_sha256": catalog_sha256,
        "license_files": copied_licenses,
        "sources": manifest_sources,
        "assets": manifest_assets,
    }


def _download_source(
    source: dict[str, Any], destination: Path, *, timeout: float
) -> str:
    allowed_hosts = {_normalize_host(host) for host in source["allowed_hosts"]}
    _validate_url(source["url"], allowed_hosts)
    http_request = request.Request(
        source["url"],
        headers={
            "User-Agent": "ClipExtractor-audio-assets/1",
            "Accept-Encoding": "identity",
            "Referer": source["source_page"],
        },
    )
    digest = hashlib.sha256()
    total = 0
    expected_size = int(source["size"])
    max_bytes = min(int(source["max_bytes"]), ABSOLUTE_MAX_DOWNLOAD_BYTES)
    with _open_download(
        http_request,
        timeout=timeout,
        allowed_hosts=allowed_hosts,
    ) as response:
        final_url = response.geturl()
        _validate_url(final_url, allowed_hosts)
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                reported_size = int(content_length)
            except ValueError as exc:
                raise AssetIntegrityError(
                    f"Invalid Content-Length for {source['id']}"
                ) from exc
            if reported_size != expected_size or reported_size > max_bytes:
                raise AssetIntegrityError(
                    f"Unexpected Content-Length for {source['id']}: {reported_size}"
                )
        with destination.open("xb") as output:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes or total > expected_size:
                    raise AssetIntegrityError(
                        f"Download exceeded the pinned size for {source['id']}"
                    )
                digest.update(chunk)
                output.write(chunk)
    if total != expected_size:
        raise AssetIntegrityError(
            f"Size mismatch for {source['id']}: expected {expected_size}, got {total}"
        )
    actual_digest = digest.hexdigest()
    if actual_digest != source["sha256"]:
        raise AssetIntegrityError(
            f"SHA-256 mismatch for {source['id']}: expected {source['sha256']}, "
            f"got {actual_digest}"
        )
    return final_url


def _open_download(
    http_request: request.Request,
    timeout: float,
    allowed_hosts: set[str],
):
    """Open a source while validating every automatic redirect target."""

    opener = request.build_opener(_AllowlistedRedirectHandler(allowed_hosts))
    return opener.open(http_request, timeout=timeout)


def _extract_selected_assets(
    archive_path: Path, staging: Path, source: dict[str, Any]
) -> list[dict[str, Any]]:
    installed: list[dict[str, Any]] = []
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise AssetIntegrityError(f"Invalid ZIP source {source['id']}: {exc}") from exc
    with archive:
        for asset in source["assets"]:
            member_name = _safe_archive_member(asset["archive_member"])
            matches = [
                info for info in archive.infolist() if info.filename == member_name
            ]
            if len(matches) != 1:
                raise AssetIntegrityError(
                    f"Expected one ZIP member {member_name!r} in {source['id']}"
                )
            info = matches[0]
            mode = info.external_attr >> 16
            if info.is_dir() or stat.S_ISLNK(mode) or info.flag_bits & 0x1:
                raise DownloadSecurityError(
                    f"Unsafe ZIP member {member_name!r} in {source['id']}"
                )
            if (
                info.file_size != asset["size"]
                or info.file_size > ABSOLUTE_MAX_ASSET_BYTES
            ):
                raise AssetIntegrityError(
                    f"Unexpected extracted size for asset {asset['id']}"
                )
            destination = _asset_destination(staging, asset["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as asset_input:
                _copy_verified_asset(asset_input, destination, asset)
            installed.append(_manifest_asset(asset, destination, staging))
    return installed


def _install_direct_asset(
    downloaded: Path, staging: Path, source: dict[str, Any]
) -> list[dict[str, Any]]:
    asset = source["assets"][0]
    if asset["size"] != source["size"] or asset["sha256"] != source["sha256"]:
        raise AssetIntegrityError(
            f"Direct asset pins differ from source pins for {source['id']}"
        )
    destination = _asset_destination(staging, asset["path"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(downloaded, destination)
    return [_manifest_asset(asset, destination, staging)]


def _copy_verified_asset(
    source_file: BinaryIO, destination: Path, asset: dict[str, Any]
) -> None:
    digest = hashlib.sha256()
    total = 0
    with destination.open("xb") as output:
        while True:
            chunk = source_file.read(DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > int(asset["size"]) or total > ABSOLUTE_MAX_ASSET_BYTES:
                raise AssetIntegrityError(
                    f"Asset {asset['id']} exceeded its pinned size"
                )
            digest.update(chunk)
            output.write(chunk)
    if total != asset["size"] or digest.hexdigest() != asset["sha256"]:
        destination.unlink(missing_ok=True)
        raise AssetIntegrityError(f"Extracted bytes do not match asset {asset['id']}")


def _manifest_asset(
    asset: dict[str, Any], destination: Path, staging: Path
) -> dict[str, Any]:
    media_validation = _validate_ogg(destination)
    return {
        "id": asset["id"],
        "label": asset["label"],
        "kind": asset["kind"],
        "path": destination.relative_to(staging).as_posix(),
        "size": asset["size"],
        "sha256": asset["sha256"],
        "creator": asset["creator"],
        "source_page": asset["source_page"],
        "license": asset["license"],
        "modifications": asset.get("modifications", "None"),
        "media_validation": media_validation,
    }


def _validate_ogg(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise MediaValidationError(
            "ffprobe is required to validate downloaded audio before activation"
        )
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type,codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise MediaValidationError(
            f"ffprobe rejected {path.name}: {completed.stderr.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
        stream = payload["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise MediaValidationError(
            f"ffprobe found no audio stream in {path.name}"
        ) from exc
    if stream.get("codec_type") not in {None, "audio"} or not stream.get("codec_name"):
        raise MediaValidationError(f"ffprobe found no audio codec in {path.name}")
    return {
        "tool": "ffprobe",
        "status": "passed",
        "codec": stream.get("codec_name"),
        "sample_rate": stream.get("sample_rate"),
        "channels": stream.get("channels"),
    }


def _safe_archive_member(member: str) -> str:
    if not member or "\\" in member or member.startswith("/"):
        raise DownloadSecurityError(f"Unsafe ZIP member path: {member!r}")
    path = PurePosixPath(member)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DownloadSecurityError(f"Unsafe ZIP member path: {member!r}")
    if ":" in path.parts[0]:
        raise DownloadSecurityError(f"Unsafe ZIP member path: {member!r}")
    return path.as_posix()


def _safe_relative_path(relative: str) -> Path:
    if not relative or "\\" in relative:
        raise DownloadSecurityError(f"Unsafe asset path: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise DownloadSecurityError(f"Unsafe asset path: {relative!r}")
    if ":" in pure.parts[0]:
        raise DownloadSecurityError(f"Unsafe asset path: {relative!r}")
    return Path(*pure.parts)


def _asset_destination(staging: Path, relative: str) -> Path:
    destination = staging / _safe_relative_path(relative)
    root = staging.resolve()
    resolved = destination.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DownloadSecurityError(
            f"Asset path escaped staging: {relative!r}"
        ) from exc
    return destination


def _read_installed_manifest(root: Path) -> dict[str, Any]:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _verify_installed_manifest(
    root: Path,
    manifest: dict[str, Any],
    pack: dict[str, Any],
    catalog_sha256: str,
    *,
    verify_files: bool,
) -> None:
    if not isinstance(manifest, dict):
        raise AssetIntegrityError("Installed manifest must be a JSON object")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("pack_id") != pack["id"]
        or manifest.get("pack_version") != pack["version"]
        or manifest.get("display_name") != pack["display_name"]
        or manifest.get("license_checked_at") != pack["license_checked_at"]
        or manifest.get("catalog_sha256") != catalog_sha256
    ):
        raise AssetIntegrityError(
            "Installed manifest does not match the bundled catalog"
        )
    expected_sources = {source["id"]: source for source in pack["sources"]}
    source_entries = manifest.get("sources")
    if not isinstance(source_entries, list) or len(source_entries) != len(
        expected_sources
    ):
        raise AssetIntegrityError("Installed manifest has an unexpected source count")
    seen_sources: set[str] = set()
    for entry in source_entries:
        if not isinstance(entry, dict):
            raise AssetIntegrityError("Installed manifest has a malformed source")
        source_id = entry.get("id")
        expected_source = expected_sources.get(source_id)
        if expected_source is None or source_id in seen_sources:
            raise AssetIntegrityError(f"Unexpected installed source: {source_id}")
        seen_sources.add(source_id)
        expected_source_fields = {
            "id": expected_source["id"],
            "url": expected_source["url"],
            "source_page": expected_source["source_page"],
            "size": expected_source["size"],
            "sha256": expected_source["sha256"],
            "verified": True,
        }
        if any(
            entry.get(key) != value for key, value in expected_source_fields.items()
        ):
            raise AssetIntegrityError(f"Source metadata mismatch for {source_id}")
        final_url = entry.get("final_url")
        if not isinstance(final_url, str):
            raise AssetIntegrityError(f"Source final URL is missing for {source_id}")
        allowed_hosts = {
            _normalize_host(str(host)) for host in expected_source["allowed_hosts"]
        }
        _validate_url(final_url, allowed_hosts)

    expected_assets = {asset["id"]: asset for _source, asset in _iter_assets(pack)}
    entries = manifest.get("assets")
    if not isinstance(entries, list) or len(entries) != len(expected_assets):
        raise AssetIntegrityError("Installed manifest has an unexpected asset count")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise AssetIntegrityError("Installed manifest has a malformed asset")
        asset_id = entry.get("id")
        expected = expected_assets.get(asset_id)
        if expected is None or asset_id in seen:
            raise AssetIntegrityError(f"Unexpected installed asset: {asset_id}")
        seen.add(asset_id)
        expected_asset_fields = {
            "id": expected["id"],
            "label": expected["label"],
            "kind": expected["kind"],
            "path": expected["path"],
            "size": expected["size"],
            "sha256": expected["sha256"],
            "creator": expected["creator"],
            "source_page": expected["source_page"],
            "license": expected["license"],
            "modifications": expected.get("modifications", "None"),
        }
        if any(entry.get(key) != value for key, value in expected_asset_fields.items()):
            raise AssetIntegrityError(f"Manifest metadata mismatch for {asset_id}")
        media_validation = entry.get("media_validation")
        if (
            not isinstance(media_validation, dict)
            or media_validation.get("tool") != "ffprobe"
            or media_validation.get("status") != "passed"
            or not media_validation.get("codec")
        ):
            raise AssetIntegrityError(f"Media validation is missing for {asset_id}")
        try:
            sample_rate = int(media_validation.get("sample_rate", 0))
            channels = int(media_validation.get("channels", 0))
        except (TypeError, ValueError) as exc:
            raise AssetIntegrityError(
                f"Media validation is malformed for {asset_id}"
            ) from exc
        if sample_rate <= 0 or channels <= 0:
            raise AssetIntegrityError(f"Media validation is malformed for {asset_id}")
        path = _asset_destination(root, entry["path"])
        if not path.is_file():
            raise AssetIntegrityError(f"Installed asset is missing: {asset_id}")
        if verify_files:
            size, digest = _hash_file(path)
            if size != expected["size"] or digest != expected["sha256"]:
                raise AssetIntegrityError(f"Installed asset is corrupt: {asset_id}")
    expected_license_files = [
        f"LICENSES/{filename}" for filename in pack.get("license_files", [])
    ]
    if manifest.get("license_files") != expected_license_files:
        raise AssetIntegrityError("Installed license file list does not match catalog")
    for relative in expected_license_files:
        license_path = _asset_destination(root, relative)
        if not license_path.is_file():
            raise AssetIntegrityError(f"Installed license file is missing: {relative}")
        if verify_files:
            bundled = LICENSES_PATH / Path(relative).name
            if license_path.read_bytes() != bundled.read_bytes():
                raise AssetIntegrityError(
                    f"Installed license file is corrupt: {relative}"
                )


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
    return total, digest.hexdigest()


def _installed_asset(
    root: Path, entry: dict[str, Any], manifest: dict[str, Any]
) -> InstalledAsset:
    license_info = entry["license"]
    return InstalledAsset(
        id=entry["id"],
        label=entry["label"],
        kind=entry["kind"],
        path=_asset_destination(root, entry["path"]),
        pack_id=manifest["pack_id"],
        pack_version=manifest["pack_version"],
        size=entry["size"],
        sha256=entry["sha256"],
        creator=entry["creator"],
        source_page=entry["source_page"],
        license_id=license_info["id"],
        license_url=license_info["url"],
        license_checked_at=manifest["license_checked_at"],
        attribution_required=bool(license_info["attribution_required"]),
        attribution_text=str(license_info.get("attribution_text", "")),
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _activate_installation(staging: Path, target: Path) -> None:
    parent = target.parent
    backup = parent / f".{target.name}.backup-{uuid.uuid4().hex}"
    if not target.exists():
        os.replace(staging, target)
        return
    os.replace(target, backup)
    try:
        os.replace(staging, target)
    except Exception:
        os.replace(backup, target)
        raise
    _remove_private_tree(backup, parent)


def _remove_private_tree(path: Path, expected_parent: Path) -> None:
    if not path.exists():
        return
    resolved_path = path.resolve()
    resolved_parent = expected_parent.resolve()
    if resolved_path.parent != resolved_parent or not resolved_path.name.startswith(
        "."
    ):
        raise DownloadSecurityError(f"Refusing to remove non-private path: {path}")
    if path.is_symlink():
        path.unlink()
    else:
        shutil.rmtree(path)
