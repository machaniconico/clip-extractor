import hashlib
from io import BytesIO
import json
import subprocess
import zipfile

import pytest

import audio_assets


LICENSE = {
    "id": "CC0-1.0",
    "name": "CC0 1.0 Universal",
    "url": "https://creativecommons.org/publicdomain/zero/1.0/",
    "attribution_required": False,
}


class FakeResponse:
    def __init__(self, payload, url, *, final_url=None, include_length=True):
        self._stream = BytesIO(payload)
        self._final_url = final_url or url
        self.headers = {}
        if include_length:
            self.headers["Content-Length"] = str(len(payload))

    def read(self, size=-1):
        return self._stream.read(size)

    def geturl(self):
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _zip_bytes(member, payload):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, payload)
    return output.getvalue()


def _asset(asset_id, kind, path, payload, **extra):
    return {
        "id": asset_id,
        "label": asset_id,
        "kind": kind,
        "path": path,
        "size": len(payload),
        "sha256": _sha(payload),
        "creator": "Test creator",
        "source_page": "https://kenney.nl/assets/interface-sounds",
        "modifications": "None",
        "license": LICENSE,
        **extra,
    }


def _catalog(zip_member="Audio/click.ogg"):
    se_payload = b"OggS-test-effect"
    archive_payload = _zip_bytes(zip_member, se_payload)
    bgm_payload = b"OggS-test-background-loop"
    return (
        {
            "schema_version": 1,
            "packs": [
                {
                    "id": "test-pack",
                    "version": "1.0",
                    "display_name": "Test pack",
                    "license_checked_at": "2026-08-04",
                    "license_files": [],
                    "sources": [
                        {
                            "id": "test-zip",
                            "type": "zip",
                            "url": "https://kenney.nl/test.zip",
                            "allowed_hosts": ["kenney.nl"],
                            "source_page": "https://kenney.nl/assets/interface-sounds",
                            "size": len(archive_payload),
                            "max_bytes": 1000000,
                            "sha256": _sha(archive_payload),
                            "assets": [
                                _asset(
                                    "test-se",
                                    "se",
                                    "se/click.ogg",
                                    se_payload,
                                    archive_member=zip_member,
                                )
                            ],
                        },
                        {
                            "id": "test-file",
                            "type": "file",
                            "url": "https://opengameart.org/test.ogg",
                            "allowed_hosts": ["opengameart.org"],
                            "source_page": "https://opengameart.org/content/test",
                            "size": len(bgm_payload),
                            "max_bytes": 1000000,
                            "sha256": _sha(bgm_payload),
                            "assets": [
                                _asset(
                                    "test-bgm",
                                    "bgm",
                                    "bgm/loop.ogg",
                                    bgm_payload,
                                )
                            ],
                        },
                    ],
                }
            ],
        },
        {
            "https://kenney.nl/test.zip": archive_payload,
            "https://opengameart.org/test.ogg": bgm_payload,
        },
    )


def _install_test_catalog(tmp_path, monkeypatch, *, catalog=None, payloads=None):
    if catalog is None or payloads is None:
        catalog, payloads = _catalog()
    audio_assets._validate_catalog(catalog)
    monkeypatch.setattr(audio_assets, "_load_catalog", lambda: (catalog, "a" * 64))
    monkeypatch.setattr(
        audio_assets,
        "_validate_ogg",
        lambda _path: {
            "tool": "ffprobe",
            "status": "passed",
            "codec": "vorbis",
            "sample_rate": "48000",
            "channels": 2,
        },
    )
    calls = []

    def fake_urlopen(http_request, timeout, allowed_hosts=None):
        calls.append((http_request.full_url, timeout))
        payload = payloads[http_request.full_url]
        return FakeResponse(payload, http_request.full_url)

    monkeypatch.setattr(audio_assets, "_open_download", fake_urlopen)
    status = audio_assets.install_pack("test-pack", cache_root=tmp_path, timeout=7)
    return status, calls


def test_bundled_catalog_has_pinned_official_cc0_sources():
    catalog, _catalog_hash = audio_assets._load_catalog()
    pack = next(pack for pack in catalog["packs"] if pack["id"] == "cc0-starter")
    sources = {source["id"]: source for source in pack["sources"]}

    assert pack["version"] == "2026.08.1"
    assert sources["kenney-interface-sounds"]["size"] == 834536
    assert sources["kenney-interface-sounds"]["sha256"] == (
        "f2193d072726d6758a5f7871b2dcc54dcce0d5c35c6f0a62f92549b327c81232"
    )
    assert sources["kenney-impact-sounds"]["size"] == 800850
    assert sources["kenney-impact-sounds"]["sha256"] == (
        "029d734af1582474edf3a694d1b0cebc97c1c152f2f39fa34d4c2bafc5de77f8"
    )
    assert all(source["url"].startswith("https://") for source in sources.values())
    assets = [asset for source in sources.values() for asset in source["assets"]]
    assert len(assets) == 20
    assert sum(asset["kind"] == "bgm" for asset in assets) == 4
    assert sum(asset["kind"] == "se" for asset in assets) == 16
    assert all(asset["license"] == LICENSE for asset in assets)


def test_explicit_install_verifies_and_writes_provenance_manifest(
    tmp_path, monkeypatch
):
    status, calls = _install_test_catalog(tmp_path, monkeypatch)

    assert status.ready
    assert status.asset_count == 2
    assert calls == [
        ("https://kenney.nl/test.zip", 7),
        ("https://opengameart.org/test.ogg", 7),
    ]
    assert (status.path / "se" / "click.ogg").read_bytes() == b"OggS-test-effect"
    assert (status.path / "bgm" / "loop.ogg").read_bytes() == (
        b"OggS-test-background-loop"
    )
    manifest = json.loads((status.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["catalog_sha256"] == "a" * 64
    assert manifest["license_checked_at"] == "2026-08-04"
    assert manifest["sources"][0]["verified"] is True
    assert manifest["assets"][0]["creator"] == "Test creator"
    assert manifest["assets"][0]["media_validation"]["status"] == "passed"


def test_install_requires_ffprobe_for_media_validation(tmp_path, monkeypatch):
    catalog, payloads = _catalog()
    audio_assets._validate_catalog(catalog)
    monkeypatch.setattr(audio_assets, "_load_catalog", lambda: (catalog, "a" * 64))
    monkeypatch.setattr(audio_assets.shutil, "which", lambda _name: None)

    def fake_urlopen(http_request, timeout, allowed_hosts=None):
        return FakeResponse(payloads[http_request.full_url], http_request.full_url)

    monkeypatch.setattr(audio_assets, "_open_download", fake_urlopen)

    with pytest.raises(audio_assets.MediaValidationError, match="ffprobe"):
        audio_assets.install_pack("test-pack", cache_root=tmp_path)
    assert not (tmp_path / "test-pack" / "1.0").exists()


def test_read_apis_never_download_and_missing_pack_is_explicit(tmp_path, monkeypatch):
    catalog, _payloads = _catalog()
    audio_assets._validate_catalog(catalog)
    monkeypatch.setattr(audio_assets, "_load_catalog", lambda: (catalog, "a" * 64))

    def network_must_not_run(*_args, **_kwargs):
        raise AssertionError("read API attempted a network request")

    monkeypatch.setattr(audio_assets, "_open_download", network_must_not_run)

    status = audio_assets.get_pack_status("test-pack", cache_root=tmp_path)
    assert status.state == "not_installed"
    assert audio_assets.list_installed_assets("test-pack", cache_root=tmp_path) == []
    with pytest.raises(audio_assets.AssetPackNotInstalledError):
        audio_assets.resolve_asset("test-se", "test-pack", cache_root=tmp_path)


def test_resolve_returns_verified_local_asset_without_network(tmp_path, monkeypatch):
    status, _calls = _install_test_catalog(tmp_path, monkeypatch)

    def network_must_not_run(*_args, **_kwargs):
        raise AssertionError("resolve attempted a network request")

    monkeypatch.setattr(audio_assets, "_open_download", network_must_not_run)

    resolved = audio_assets.resolve_asset("test-bgm", "test-pack", cache_root=tmp_path)
    assert resolved == status.path / "bgm" / "loop.ogg"
    metadata = audio_assets.get_installed_asset(
        "test-bgm", "test-pack", cache_root=tmp_path
    )
    assert metadata.pack_version == "1.0"
    assert metadata.creator == "Test creator"
    assert metadata.license_url == LICENSE["url"]
    assert metadata.license_checked_at == "2026-08-04"


def test_install_reuses_verified_version_without_network(tmp_path, monkeypatch):
    first, _calls = _install_test_catalog(tmp_path, monkeypatch)

    def network_must_not_run(*_args, **_kwargs):
        raise AssertionError("cached install attempted a network request")

    monkeypatch.setattr(audio_assets, "_open_download", network_must_not_run)
    second = audio_assets.install_pack("test-pack", cache_root=tmp_path)

    assert second.ready
    assert second.path == first.path


def test_failed_forced_refresh_keeps_previous_good_install(tmp_path, monkeypatch):
    status, _calls = _install_test_catalog(tmp_path, monkeypatch)
    manifest_before = (status.path / "manifest.json").read_bytes()
    asset_before = (status.path / "bgm" / "loop.ogg").read_bytes()

    catalog, payloads = _catalog()
    monkeypatch.setattr(audio_assets, "_load_catalog", lambda: (catalog, "a" * 64))
    corrupt = {url: (b"x" * len(payload)) for url, payload in payloads.items()}

    def corrupt_urlopen(http_request, timeout, allowed_hosts=None):
        return FakeResponse(corrupt[http_request.full_url], http_request.full_url)

    monkeypatch.setattr(audio_assets, "_open_download", corrupt_urlopen)

    with pytest.raises(audio_assets.AssetIntegrityError, match="SHA-256 mismatch"):
        audio_assets.install_pack("test-pack", cache_root=tmp_path, force=True)

    assert (status.path / "manifest.json").read_bytes() == manifest_before
    assert (status.path / "bgm" / "loop.ogg").read_bytes() == asset_before
    assert audio_assets.get_pack_status("test-pack", cache_root=tmp_path).ready
    assert not list((tmp_path / "test-pack").glob(".*.install-*"))


def test_redirect_to_non_allowlisted_host_is_rejected(tmp_path, monkeypatch):
    catalog, payloads = _catalog()
    audio_assets._validate_catalog(catalog)
    monkeypatch.setattr(audio_assets, "_load_catalog", lambda: (catalog, "a" * 64))
    monkeypatch.setattr(audio_assets.shutil, "which", lambda _name: None)

    def redirected_urlopen(http_request, timeout, allowed_hosts=None):
        return FakeResponse(
            payloads[http_request.full_url],
            http_request.full_url,
            final_url="https://untrusted.example/payload",
        )

    monkeypatch.setattr(audio_assets, "_open_download", redirected_urlopen)

    with pytest.raises(audio_assets.DownloadSecurityError):
        audio_assets.install_pack("test-pack", cache_root=tmp_path)
    assert not (tmp_path / "test-pack" / "1.0").exists()


def test_redirect_handler_rejects_non_allowlisted_intermediate_host():
    handler = audio_assets._AllowlistedRedirectHandler({"kenney.nl"})
    original = audio_assets.request.Request("https://kenney.nl/start")

    with pytest.raises(audio_assets.DownloadSecurityError):
        handler.redirect_request(
            original,
            None,
            302,
            "Found",
            {},
            "https://untrusted.example/intermediate",
        )


def test_unsafe_zip_member_is_rejected_without_escape(tmp_path, monkeypatch):
    catalog, payloads = _catalog(zip_member="../escape.ogg")
    monkeypatch.setattr(audio_assets, "_load_catalog", lambda: (catalog, "a" * 64))
    monkeypatch.setattr(audio_assets.shutil, "which", lambda _name: None)

    def fake_urlopen(http_request, timeout, allowed_hosts=None):
        return FakeResponse(payloads[http_request.full_url], http_request.full_url)

    monkeypatch.setattr(audio_assets, "_open_download", fake_urlopen)

    with pytest.raises(audio_assets.DownloadSecurityError, match="ZIP member"):
        audio_assets.install_pack("test-pack", cache_root=tmp_path)
    assert not (tmp_path / "escape.ogg").exists()
    assert not (tmp_path / "test-pack" / "1.0").exists()


def test_ffprobe_is_used_when_available(tmp_path, monkeypatch):
    catalog, payloads = _catalog()
    audio_assets._validate_catalog(catalog)
    monkeypatch.setattr(audio_assets, "_load_catalog", lambda: (catalog, "a" * 64))
    monkeypatch.setattr(audio_assets.shutil, "which", lambda _name: "ffprobe-test")
    probe_calls = []

    def fake_run(args, **kwargs):
        probe_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "audio",
                            "codec_name": "vorbis",
                            "sample_rate": "48000",
                            "channels": 2,
                        }
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(audio_assets.subprocess, "run", fake_run)

    def fake_urlopen(http_request, timeout, allowed_hosts=None):
        return FakeResponse(payloads[http_request.full_url], http_request.full_url)

    monkeypatch.setattr(audio_assets, "_open_download", fake_urlopen)

    status = audio_assets.install_pack("test-pack", cache_root=tmp_path)
    manifest = json.loads((status.path / "manifest.json").read_text(encoding="utf-8"))
    assert len(probe_calls) == 2
    assert all(
        asset["media_validation"]["status"] == "passed" for asset in manifest["assets"]
    )


def test_corrupt_installed_asset_changes_status_to_invalid(tmp_path, monkeypatch):
    status, _calls = _install_test_catalog(tmp_path, monkeypatch)
    (status.path / "se" / "click.ogg").write_bytes(b"tampered")

    checked = audio_assets.get_pack_status("test-pack", cache_root=tmp_path)
    assert checked.state == "invalid"
    assert "corrupt" in checked.message


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("label", "Forged label"),
        ("kind", "bgm"),
        ("creator", "Mallory"),
        ("source_page", "https://kenney.nl/assets/forged"),
        ("modifications", "Unknown rewrite"),
        ("license", {**LICENSE, "attribution_required": True}),
    ],
)
def test_tampered_asset_provenance_changes_status_to_invalid(
    tmp_path, monkeypatch, field, tampered
):
    status, _calls = _install_test_catalog(tmp_path, monkeypatch)
    manifest_path = status.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0][field] = tampered
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    checked = audio_assets.get_pack_status("test-pack", cache_root=tmp_path)
    assert checked.state == "invalid"
    assert audio_assets.list_installed_assets("test-pack", cache_root=tmp_path) == []


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("url", "https://kenney.nl/forged.zip"),
        ("source_page", "https://kenney.nl/assets/forged"),
        ("size", 1),
        ("sha256", "f" * 64),
        ("verified", False),
        ("final_url", "https://untrusted.example/forged.zip"),
    ],
)
def test_tampered_source_provenance_changes_status_to_invalid(
    tmp_path, monkeypatch, field, tampered
):
    status, _calls = _install_test_catalog(tmp_path, monkeypatch)
    manifest_path = status.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"][0][field] = tampered
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    checked = audio_assets.get_pack_status("test-pack", cache_root=tmp_path)
    assert checked.state == "invalid"


def test_cache_root_uses_localappdata_and_supports_override(tmp_path, monkeypatch):
    local = tmp_path / "Local App Data"
    monkeypatch.delenv(audio_assets.ASSET_CACHE_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert (
        audio_assets.get_cache_root()
        == (local / "ClipExtractor" / "asset-packs").resolve()
    )

    explicit = tmp_path / "test-cache"
    assert audio_assets.get_cache_root(explicit) == explicit.resolve()
