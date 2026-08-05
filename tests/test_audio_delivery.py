from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import audio_delivery
from audio_assets import InstalledAsset
from audio_delivery import (
    AudioDeliveryError,
    AudioDeliveryOptions,
    deliver_audio_groups,
    validate_audio_selection,
)
from audio_mix import AudioBatchResult, AudioDeliveryMode, AudioOutputResult
from user_media import UserMediaAsset, UserMediaError


def _asset(tmp_path: Path, asset_id: str, kind: str) -> InstalledAsset:
    path = tmp_path / f"{asset_id}.ogg"
    path.write_bytes(b"ogg")
    return InstalledAsset(
        id=asset_id,
        label=f"label-{asset_id}",
        kind=kind,
        path=path,
        pack_id="short-video-starter",
        pack_version="2026.08.2",
        size=3,
        sha256="a" * 64,
        creator="creator",
        source_page="https://example.invalid/source",
        license_id="CC0-1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        license_checked_at="2026-08-05",
        attribution_required=False,
    )


def _ready_status():
    return SimpleNamespace(
        ready=True,
        pack_id="short-video-starter",
        version="2026.08.2",
    )


def _owned_manifest(
    root: Path,
    groups,
    *,
    delivery_mode="both",
    notice_content: bytes = b"",
):
    for records in groups.values():
        for record in records:
            for artifact in record.get("artifacts", []):
                if artifact.get("kind") not in {
                    "bgm_stem",
                    "se_stem",
                    "mixed_video",
                    "clip_audio_manifest",
                }:
                    continue
                relative = Path(artifact["file"])
                candidate = root / relative
                content = (
                    candidate.read_bytes()
                    if ".." not in relative.parts
                    and candidate.is_file()
                    and not candidate.is_symlink()
                    else b""
                )
                artifact["size_bytes"] = len(content)
                artifact["sha256"] = hashlib.sha256(content).hexdigest()
    return {
        "schema_version": 2,
        "generator": "clip-extractor",
        "generated_at": "2026-08-05T00:00:00+00:00",
        "delivery_mode": delivery_mode,
        "settings": {},
        "pack": None,
        "selected_assets": {},
        "notices": {
            "file": audio_delivery.AUDIO_NOTICES_NAME,
            "size_bytes": len(notice_content),
            "sha256": hashlib.sha256(notice_content).hexdigest(),
        },
        "groups": groups,
    }


def _recovery_contents(root: Path) -> dict[str, bytes]:
    directories = list(root.glob(f"{audio_delivery._RECOVERY_PREFIX}*"))
    assert len(directories) == 1
    index = json.loads(
        (directories[0] / audio_delivery._RECOVERY_INDEX_NAME).read_text(
            encoding="utf-8"
        )
    )
    return {
        item["original_file"]: (directories[0] / item["backup_file"]).read_bytes()
        for item in index["files"]
    }


def _user_asset(tmp_path: Path, asset_id: str, kind: str) -> UserMediaAsset:
    path = tmp_path / f"{kind}.wav"
    path.write_bytes(b"user audio")
    digest = asset_id.rsplit(":", 1)[-1]
    return UserMediaAsset(
        id=asset_id,
        kind=kind,
        path=path.resolve(),
        filename=path.name,
        relative_path=path.name,
        size=path.stat().st_size,
        sha256=digest,
    )


def test_user_audio_resolves_without_downloaded_pack_and_writes_user_provenance(
    tmp_path, monkeypatch
):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clean = clips_dir / "clip.mp4"
    clean.write_bytes(b"clean")
    digest = "b" * 64
    bgm = _user_asset(tmp_path, f"user:bgm:{digest}", "bgm")
    monkeypatch.setattr(
        audio_delivery,
        "get_pack_status",
        lambda: pytest.fail("user-only audio must not require the downloaded pack"),
    )
    monkeypatch.setattr(
        audio_delivery,
        "resolve_user_media_asset",
        lambda folder, asset_id, kind: bgm,
    )
    monkeypatch.setattr(audio_delivery, "validate_user_media", lambda asset: None)

    captured = {}

    def fake_process(_paths, _highlights, **kwargs):
        captured.update(kwargs)
        stem = clips_dir / "clip_bgm.wav"
        sidecar = clips_dir / "clip_audio.json"
        stem.write_bytes(b"stem")
        sidecar.write_text("{}", encoding="utf-8")
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=(clean, stem, sidecar),
                    clean_video=clean,
                    bgm_stem=stem,
                    manifest=sidecar,
                ),
            )
        )

    monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)

    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clean]},
        [{"title": "clip", "start_sec": 0, "end_sec": 2}],
        options=AudioDeliveryOptions(
            delivery_mode="separate",
            bgm_asset_id=bgm.id,
            bgm_user_folder=str(tmp_path),
        ),
    )

    assert captured["bgm_path"] == bgm.path
    assert captured["se_path"] is None
    provenance = captured["provenance"]["bgm"]
    assert provenance == {
        "id": bgm.id,
        "kind": "bgm",
        "source_type": "user_provided",
        "original_filename": "bgm.wav",
        "source_sha256": digest,
        "source_size_bytes": len(b"user audio"),
    }
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert payload["pack"] is None
    assert payload["selected_assets"]["bgm"]["source_type"] == "user_provided"
    notices = result.notices_path.read_text(encoding="utf-8")
    assert "User-provided material" in notices
    assert "CC0 1.0 Universal" not in notices


def test_user_audio_is_revalidated_immediately_before_mixing(
    tmp_path, monkeypatch
):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    digest = "c" * 64
    bgm = _user_asset(tmp_path, f"user:bgm:{digest}", "bgm")
    monkeypatch.setattr(
        audio_delivery,
        "get_pack_status",
        lambda: pytest.fail("user-only audio must not require the downloaded pack"),
    )
    monkeypatch.setattr(
        audio_delivery,
        "resolve_user_media_asset",
        lambda _folder, _asset_id, _kind: bgm,
    )
    validations = 0

    def validate(asset):
        nonlocal validations
        validations += 1
        if validations == 2:
            raise UserMediaError("replaced")
        return asset

    monkeypatch.setattr(audio_delivery, "validate_user_media", validate)
    monkeypatch.setattr(
        audio_delivery,
        "process_clip_batch",
        lambda *_args, **_kwargs: pytest.fail("mixing must not start"),
    )

    with pytest.raises(AudioDeliveryError, match="生成直前に変更"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"title": "clip", "start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(
                bgm_asset_id=bgm.id,
                bgm_user_folder=str(tmp_path),
            ),
        )

    assert clean.read_bytes() == b"clean"


def test_user_audio_reference_requires_its_matching_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(
        audio_delivery,
        "get_pack_status",
        lambda: pytest.fail("user audio must not inspect the downloaded pack"),
    )
    with pytest.raises(AudioDeliveryError, match="参照フォルダ"):
        validate_audio_selection(
            AudioDeliveryOptions(bgm_asset_id=f"user:bgm:{'c' * 64}")
        )


def test_user_audio_cannot_collide_with_generated_output(tmp_path, monkeypatch):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clean = clips_dir / "clip.mp4"
    clean.write_bytes(b"clean")
    source = clips_dir / "clip_bgm.wav"
    source.write_bytes(b"must stay unchanged")
    digest = "e" * 64
    asset = UserMediaAsset(
        id=f"user:bgm:{digest}",
        kind="bgm",
        path=source.resolve(),
        filename=source.name,
        relative_path=source.name,
        size=source.stat().st_size,
        sha256=digest,
    )
    monkeypatch.setattr(
        audio_delivery,
        "resolve_user_media_asset",
        lambda *_args: asset,
    )
    monkeypatch.setattr(audio_delivery, "validate_user_media", lambda _asset: None)
    monkeypatch.setattr(
        audio_delivery,
        "process_clip_batch",
        lambda *_args, **_kwargs: pytest.fail("FFmpeg must not run on a collision"),
    )

    with pytest.raises(AudioDeliveryError, match="衝突"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(
                delivery_mode="separate",
                bgm_asset_id=asset.id,
                bgm_user_folder=str(tmp_path),
            ),
        )

    assert source.read_bytes() == b"must stay unchanged"
    assert not list(tmp_path.glob(".audio_delivery_transaction-*"))


def test_disabled_audio_preserves_clean_media_and_removes_prior_generated_files(
    tmp_path,
):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clean = clips_dir / "clip.mp4"
    clean.write_bytes(b"clean")
    stale_mixed = clips_dir / "clip_mixed.mp4"
    stale_stem = clips_dir / "clip_bgm.wav"
    stale_mixed.write_bytes(b"mixed")
    stale_stem.write_bytes(b"stem")
    manifest = _owned_manifest(
        tmp_path,
        {
            "clips": [
                {
                    "source_clean_video": "clips/clip.mp4",
                    "primary_video": "clips/clip.mp4",
                    "artifacts": [
                        {"kind": "clean_video", "file": "clips/clip.mp4"},
                        {
                            "kind": "mixed_video",
                            "file": "clips/clip_mixed.mp4",
                        },
                        {"kind": "bgm_stem", "file": "clips/clip_bgm.wav"},
                    ]
                }
            ]
        },
        notice_content=b"old",
    )
    (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (tmp_path / audio_delivery.AUDIO_NOTICES_NAME).write_text("old", encoding="utf-8")

    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clean]},
        [{"start_sec": 0, "end_sec": 2}],
        options=AudioDeliveryOptions(),
    )

    assert result.enabled is False
    assert result.media_groups["clips"] == (clean.resolve(),)
    assert clean.exists()
    assert not stale_mixed.exists()
    assert not stale_stem.exists()
    assert not (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).exists()
    assert not (tmp_path / audio_delivery.AUDIO_NOTICES_NAME).exists()
    recovery = _recovery_contents(tmp_path)
    assert recovery["clips/clip_mixed.mp4"] == b"mixed"
    assert recovery["clips/clip_bgm.wav"] == b"stem"


def test_forged_v2_manifest_can_only_quarantine_claimed_user_content(tmp_path):
    claimed = tmp_path / "important_bgm.wav"
    claimed.write_bytes(b"USER-CONTENT")
    notices = tmp_path / audio_delivery.AUDIO_NOTICES_NAME
    notices.write_text("forged notice", encoding="utf-8")
    manifest = _owned_manifest(
        tmp_path,
        {
            "clips": [
                {
                    "source_clean_video": "important.mp4",
                    "artifacts": [
                        {"kind": "bgm_stem", "file": "important_bgm.wav"}
                    ],
                }
            ]
        },
        notice_content=b"forged notice",
    )
    (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    deliver_audio_groups(tmp_path, {}, [], options=AudioDeliveryOptions())

    assert not claimed.exists()
    recovery = _recovery_contents(tmp_path)
    assert recovery["important_bgm.wav"] == b"USER-CONTENT"


def test_legacy_v1_manifest_is_migrated_to_recovery_when_audio_disabled(
    tmp_path,
):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    legacy_stem = tmp_path / "clip_bgm.wav"
    legacy_stem.write_bytes(b"legacy stem")
    legacy = {
        "schema_version": 1,
        "delivery_mode": "both",
        "groups": {
            "clips": [
                {
                    "primary_video": "clip.mp4",
                    "artifacts": [
                        {"kind": "clean_video", "file": "clip.mp4"},
                        {"kind": "bgm_stem", "file": "clip_bgm.wav"},
                    ],
                }
            ]
        },
    }
    (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(legacy),
        encoding="utf-8",
    )
    (tmp_path / audio_delivery.AUDIO_NOTICES_NAME).write_text(
        "legacy notice",
        encoding="utf-8",
    )

    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clean]},
        [{"start_sec": 0, "end_sec": 2}],
        options=AudioDeliveryOptions(),
    )

    assert result.enabled is False
    assert clean.read_bytes() == b"clean"
    assert not legacy_stem.exists()
    recovery = _recovery_contents(tmp_path)
    assert recovery["clip_bgm.wav"] == b"legacy stem"
    assert audio_delivery.AUDIO_MANIFEST_NAME in recovery
    assert audio_delivery.AUDIO_NOTICES_NAME in recovery


def test_legacy_v1_manifest_is_quarantined_before_enabled_regeneration(
    tmp_path, monkeypatch
):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    old_stem = tmp_path / "clip_bgm.wav"
    old_stem.write_bytes(b"legacy stem")
    old_sidecar = tmp_path / "clip_audio.json"
    old_sidecar.write_text("legacy sidecar", encoding="utf-8")
    legacy = {
        "schema_version": 1,
        "delivery_mode": "separate",
        "groups": {
            "clips": [
                {
                    "primary_video": "clip.mp4",
                    "artifacts": [
                        {"kind": "clean_video", "file": "clip.mp4"},
                        {"kind": "bgm_stem", "file": "clip_bgm.wav"},
                        {
                            "kind": "clip_audio_manifest",
                            "file": "clip_audio.json",
                        },
                    ],
                }
            ]
        },
    }
    (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(legacy),
        encoding="utf-8",
    )
    (tmp_path / audio_delivery.AUDIO_NOTICES_NAME).write_text(
        "legacy notice",
        encoding="utf-8",
    )
    bgm = _asset(tmp_path, "NEW-BGM", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

    def fake_process(_paths, _highlights, **_kwargs):
        old_stem.write_bytes(b"new stem")
        old_sidecar.write_text("new sidecar", encoding="utf-8")
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=(clean, old_stem, old_sidecar),
                    clean_video=clean,
                    bgm_stem=old_stem,
                    manifest=old_sidecar,
                ),
            )
        )

    monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)

    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clean]},
        [{"title": "clip", "start_sec": 0, "end_sec": 2}],
        options=AudioDeliveryOptions(
            delivery_mode="separate",
            bgm_asset_id=bgm.id,
        ),
    )

    assert result.enabled is True
    assert old_stem.read_bytes() == b"new stem"
    assert old_sidecar.read_text(encoding="utf-8") == "new sidecar"
    recovery = _recovery_contents(tmp_path)
    assert recovery["clip_bgm.wav"] == b"legacy stem"
    assert recovery["clip_audio.json"] == b"legacy sidecar"


def test_disabled_audio_leaves_unknown_provenance_files_untouched(tmp_path):
    manifest = tmp_path / audio_delivery.AUDIO_MANIFEST_NAME
    notices = tmp_path / audio_delivery.AUDIO_NOTICES_NAME
    manifest.write_text("USER-NOTES", encoding="utf-8")
    notices.write_text("USER-NOTICE", encoding="utf-8")

    result = deliver_audio_groups(
        tmp_path,
        {},
        [],
        options=AudioDeliveryOptions(),
    )

    assert result.enabled is False
    assert manifest.read_text(encoding="utf-8") == "USER-NOTES"
    assert notices.read_text(encoding="utf-8") == "USER-NOTICE"


def test_enabled_audio_rejects_unowned_generated_target_before_mixing(
    tmp_path, monkeypatch
):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    collision = tmp_path / "clip_bgm.wav"
    collision.write_bytes(b"USER-STEM")
    bgm = _asset(tmp_path, "bgm", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)
    monkeypatch.setattr(
        audio_delivery,
        "process_clip_batch",
        lambda *_args, **_kwargs: pytest.fail(
            "collision must be rejected before audio mixing"
        ),
    )

    with pytest.raises(AudioDeliveryError, match="衝突"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(bgm_asset_id=bgm.id),
        )

    assert collision.read_bytes() == b"USER-STEM"


def test_validate_selection_never_installs_missing_pack(monkeypatch):
    monkeypatch.setattr(
        audio_delivery,
        "get_pack_status",
        lambda: SimpleNamespace(ready=False),
    )
    install_called = False

    def unexpected_install(*_args, **_kwargs):
        nonlocal install_called
        install_called = True

    # The delivery module deliberately has no installer reference to call.
    monkeypatch.setattr(
        audio_delivery,
        "install_pack",
        unexpected_install,
        raising=False,
    )

    with pytest.raises(AudioDeliveryError, match="ダウンロード"):
        validate_audio_selection(
            AudioDeliveryOptions(bgm_asset_id="bgm-brand-new-wisdom")
        )
    assert install_called is False


@pytest.mark.parametrize(
    ("mode", "expected_primary"),
    [("separate", "clip.mp4"), ("mixed", "clip_mixed.mp4"), ("both", "clip.mp4")],
)
def test_delivery_resolves_assets_writes_provenance_and_selects_primary_video(
    tmp_path,
    monkeypatch,
    mode,
    expected_primary,
):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clean = clips_dir / "clip.mp4"
    clean.write_bytes(b"clean")
    bgm = _asset(tmp_path, "bgm-1", "bgm")
    se = _asset(tmp_path, "se-1", "se")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(
        audio_delivery,
        "get_installed_asset",
        lambda asset_id: {bgm.id: bgm, se.id: se}[asset_id],
    )
    captured = {}

    def fake_process(paths, highlights, **kwargs):
        captured.update(kwargs)
        mixed = clips_dir / "clip_mixed.mp4"
        bgm_stem = clips_dir / "clip_bgm.wav"
        se_stem = clips_dir / "clip_se.wav"
        sidecar = clips_dir / "clip_audio.json"
        mixed.write_bytes(b"mixed")
        bgm_stem.write_bytes(b"bgm")
        se_stem.write_bytes(b"se")
        sidecar.write_text("{}", encoding="utf-8")
        delivery_mode = kwargs["settings"].delivery_mode
        if delivery_mode is AudioDeliveryMode.MIXED:
            return AudioBatchResult(
                clips=(
                    AudioOutputResult(
                        deliverables=(mixed,),
                        clean_video=None,
                        mixed_video=mixed,
                        decoded_peak_4x_dbfs=-2.0,
                        post_mix_attenuation_db=-1.5,
                    ),
                )
            )
        deliverables = (clean, bgm_stem, se_stem, sidecar)
        mixed_result = None
        if delivery_mode is AudioDeliveryMode.BOTH:
            deliverables += (mixed,)
            mixed_result = mixed
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=deliverables,
                    clean_video=clean,
                    mixed_video=mixed_result,
                    bgm_stem=bgm_stem,
                    se_stem=se_stem,
                    manifest=sidecar,
                    decoded_peak_4x_dbfs=(-2.0 if mixed_result else None),
                    post_mix_attenuation_db=(-1.5 if mixed_result else 0.0),
                ),
            )
        )

    monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)

    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clean]},
        [{"title": "clip", "start_sec": 1, "end_sec": 3}],
        options=AudioDeliveryOptions(
            delivery_mode=mode,
            bgm_asset_id=bgm.id,
            se_asset_id=se.id,
        ),
    )

    assert result.media_groups["clips"][0].name == expected_primary
    assert captured["bgm_path"] == bgm.path
    assert captured["se_path"] == se.path
    assert captured["provenance"]["bgm"]["source_sha256"] == "a" * 64
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert payload["delivery_mode"] == mode
    assert payload["selected_assets"]["bgm"]["source_type"] == "downloaded_pack"
    assert payload["selected_assets"]["bgm"]["license_id"] == "CC0-1.0"
    assert payload["groups"]["clips"][0]["primary_video"].endswith(expected_primary)
    if mode in {"mixed", "both"}:
        assert payload["groups"]["clips"][0]["mixed_output_validation"] == {
            "method": "decoded AAC peak after 4x resampling",
            "decoded_peak_4x_dbfs": -2.0,
            "limit_dbfs": -1.0,
            "post_mix_attenuation_db": -1.5,
        }
    notices = result.notices_path.read_text(encoding="utf-8")
    assert "License: CC0-1.0" in notices
    assert "Creator: creator" in notices


def test_cc_by_audio_notice_contains_copyable_required_credit(tmp_path):
    asset = replace(
        _asset(tmp_path, "otologic-bgm", "bgm"),
        creator="OtoLogic",
        source_page="https://otologic.jp/free/bgm/pop-music01.html",
        license_id="CC-BY-4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        attribution_required=True,
        attribution_text=(
            "音素材：OtoLogic (https://otologic.jp/) / CC BY 4.0"
        ),
    )

    notices = audio_delivery._third_party_notices(
        {
            "pack_id": "short-video-starter",
            "pack_version": "2026.08.2",
            "license_checked_at": "2026-08-05",
            "bgm": asset,
        }
    )

    assert "Attribution required: yes" in notices
    assert "公開時に必要なクレジット" in notices
    assert "音素材：OtoLogic (https://otologic.jp/) / CC BY 4.0" in notices
    assert "Content IDへ登録" in notices
    assert "独占権を主張" in notices
    assert "The bundled works are provided under CC0" not in notices


def test_wrong_asset_kind_is_rejected_before_render(tmp_path, monkeypatch):
    wrong = _asset(tmp_path, "not-bgm", "se")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: wrong)

    with pytest.raises(AudioDeliveryError, match="BGM 素材ではありません"):
        validate_audio_selection(AudioDeliveryOptions(bgm_asset_id=wrong.id))


def test_previous_manifest_cannot_delete_outside_output(tmp_path):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(
            _owned_manifest(
                tmp_path,
                {
                    "clips": [
                        {
                            "source_clean_video": "clip.mp4",
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "../outside_mixed.mp4",
                                }
                            ]
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(AudioDeliveryError, match="安全でないパス"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(),
        )


def test_previous_manifest_cannot_claim_unrelated_suffixed_video(tmp_path):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    victim = tmp_path / "vacation_mixed.mp4"
    victim.write_bytes(b"USER-VIDEO")
    manifest = _owned_manifest(
        tmp_path,
        {
            "clips": [
                {
                    "source_clean_video": "clip.mp4",
                    "artifacts": [
                        {
                            "kind": "mixed_video",
                            "file": "vacation_mixed.mp4",
                        }
                    ],
                }
            ]
        }
    )
    (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    with pytest.raises(AudioDeliveryError, match="clean動画名と一致"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(),
        )

    assert victim.read_bytes() == b"USER-VIDEO"


def test_previous_generated_path_replaced_by_user_is_never_deleted(tmp_path):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    generated = tmp_path / "clip_mixed.mp4"
    generated.write_bytes(b"OLD-GENERATED")
    manifest = _owned_manifest(
        tmp_path,
        {
            "clips": [
                {
                    "source_clean_video": "clip.mp4",
                    "artifacts": [
                        {
                            "kind": "mixed_video",
                            "file": "clip_mixed.mp4",
                        }
                    ],
                }
            ]
        },
    )
    (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    generated.write_bytes(b"USER-REPLACEMENT")

    with pytest.raises(AudioDeliveryError, match="別ファイルへ変更"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(),
        )

    assert generated.read_bytes() == b"USER-REPLACEMENT"


def test_rollback_failure_keeps_recovery_backup(tmp_path):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    generated = tmp_path / "clip_bgm.wav"
    generated.write_bytes(b"OLD-STEM")
    manifest = _owned_manifest(
        tmp_path,
        {
            "clips": [
                {
                    "source_clean_video": "clip.mp4",
                    "artifacts": [
                        {"kind": "bgm_stem", "file": "clip_bgm.wav"}
                    ],
                }
            ]
        },
    )
    transaction = audio_delivery._AudioDeliveryTransaction(
        tmp_path,
        {generated},
        {clean},
        protect_clean=False,
        manage_sidecars=False,
    )
    transaction.begin(manifest)
    generated.mkdir()

    with pytest.raises(AudioDeliveryError, match="復旧用バックアップを保持"):
        transaction.rollback()

    assert transaction.backup_dir is not None
    recovery_files = list(transaction.backup_dir.iterdir())
    assert len(recovery_files) == 1
    assert recovery_files[0].read_bytes() == b"OLD-STEM"


@pytest.mark.parametrize(
    ("sidecar_name", "outside_content"),
    [
        (audio_delivery.AUDIO_MANIFEST_NAME, "{}"),
        (audio_delivery.AUDIO_NOTICES_NAME, "outside notice"),
    ],
)
def test_root_provenance_symlink_is_rejected_without_touching_target(
    tmp_path, monkeypatch, sidecar_name, outside_content
):
    output = tmp_path / "output"
    output.mkdir()
    clean = output / "clip.mp4"
    clean.write_bytes(b"clean")
    outside = tmp_path / "outside.txt"
    outside.write_text(outside_content, encoding="utf-8")
    sidecar = output / sidecar_name
    sidecar.symlink_to(outside)
    bgm = _asset(tmp_path, "bgm", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

    with pytest.raises(AudioDeliveryError, match="シンボリックリンク"):
        deliver_audio_groups(
            output,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(bgm_asset_id=bgm.id),
        )

    assert outside.read_text(encoding="utf-8") == outside_content
    assert sidecar.is_symlink()
    assert not list(output.glob(".audio_delivery_transaction-*"))


def test_previous_manifest_artifact_symlink_cannot_delete_its_target(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    clean = output / "clip.mp4"
    clean.write_bytes(b"clean")
    victim = output / "important_mixed.mp4"
    victim.write_bytes(b"unrelated")
    linked_output = output / "clip_mixed.mp4"
    linked_output.symlink_to(victim)
    (output / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(
            _owned_manifest(
                output,
                {
                    "clips": [
                        {
                            "source_clean_video": "clip.mp4",
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "clip_mixed.mp4",
                                }
                            ]
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(AudioDeliveryError, match="シンボリックリンク"):
        deliver_audio_groups(
            output,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(),
        )

    assert victim.read_bytes() == b"unrelated"
    assert linked_output.is_symlink()


def test_previous_manifest_cannot_follow_symlinked_parent_directory(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    clean = output / "clip.mp4"
    clean.write_bytes(b"clean")
    real_directory = output / "real"
    real_directory.mkdir()
    victim = real_directory / "clip_mixed.mp4"
    victim.write_bytes(b"unrelated")
    linked_directory = output / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    (output / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(
            _owned_manifest(
                output,
                {
                    "clips": [
                        {
                            "source_clean_video": "clip.mp4",
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "linked/clip_mixed.mp4",
                                }
                            ]
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(AudioDeliveryError, match="シンボリックリンク"):
        deliver_audio_groups(
            output,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(),
        )

    assert victim.read_bytes() == b"unrelated"
    assert linked_directory.is_symlink()


def test_generated_output_symlink_is_rejected_before_render(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    clean = output / "clip.mp4"
    clean.write_bytes(b"clean")
    victim = output / "important.wav"
    victim.write_bytes(b"unrelated")
    generated = output / "clip_bgm.wav"
    generated.symlink_to(victim)
    bgm = _asset(tmp_path, "bgm", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)
    process_called = False

    def unexpected_process(*_args, **_kwargs):
        nonlocal process_called
        process_called = True

    monkeypatch.setattr(audio_delivery, "process_clip_batch", unexpected_process)

    with pytest.raises(AudioDeliveryError, match="シンボリックリンク"):
        deliver_audio_groups(
            output,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(bgm_asset_id=bgm.id),
        )

    assert process_called is False
    assert victim.read_bytes() == b"unrelated"
    assert generated.is_symlink()


def test_input_video_symlink_is_rejected_before_normalization(tmp_path, monkeypatch):
    output = tmp_path / "output"
    output.mkdir()
    target = output / "important.mp4"
    target.write_bytes(b"real input")
    linked_input = output / "clip.mp4"
    linked_input.symlink_to(target)
    bgm = _asset(tmp_path, "bgm", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)
    process_called = False

    def unexpected_process(*_args, **_kwargs):
        nonlocal process_called
        process_called = True

    monkeypatch.setattr(audio_delivery, "process_clip_batch", unexpected_process)

    with pytest.raises(AudioDeliveryError, match="シンボリックリンク"):
        deliver_audio_groups(
            output,
            {"clips": [linked_input]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(delivery_mode="mixed", bgm_asset_id=bgm.id),
        )

    assert process_called is False
    assert target.read_bytes() == b"real input"
    assert linked_input.is_symlink()


@pytest.mark.parametrize("enabled", [False, True])
def test_previous_artifact_never_deletes_current_clean_input(
    tmp_path, monkeypatch, enabled
):
    clean = tmp_path / "clip_mixed.mp4"
    clean.write_bytes(b"current input")
    (tmp_path / audio_delivery.AUDIO_MANIFEST_NAME).write_text(
        json.dumps(
            _owned_manifest(
                tmp_path,
                {
                    "clips": [
                        {
                            "source_clean_video": "clip.mp4",
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "clip_mixed.mp4",
                                }
                            ]
                        }
                    ]
                },
                notice_content=b"old notice",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / audio_delivery.AUDIO_NOTICES_NAME).write_text(
        "old notice", encoding="utf-8"
    )

    options = AudioDeliveryOptions()
    if enabled:
        bgm = _asset(tmp_path, "bgm", "bgm")
        monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
        monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

        def fake_process(_paths, _highlights, **_kwargs):
            stem = tmp_path / "clip_mixed_bgm.wav"
            sidecar = tmp_path / "clip_mixed_audio.json"
            stem.write_bytes(b"stem")
            sidecar.write_text("{}", encoding="utf-8")
            return AudioBatchResult(
                clips=(
                    AudioOutputResult(
                        deliverables=(clean, stem, sidecar),
                        clean_video=clean,
                        bgm_stem=stem,
                        manifest=sidecar,
                    ),
                )
            )

        monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)
        options = AudioDeliveryOptions(delivery_mode="separate", bgm_asset_id=bgm.id)

    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clean]},
        [{"start_sec": 0, "end_sec": 2}],
        options=options,
    )

    assert result.media_groups["clips"] == (clean.resolve(),)
    assert clean.read_bytes() == b"current input"


def test_transaction_begin_failure_preserves_existing_provenance(tmp_path, monkeypatch):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    manifest_path = tmp_path / audio_delivery.AUDIO_MANIFEST_NAME
    notices_path = tmp_path / audio_delivery.AUDIO_NOTICES_NAME
    manifest_path.write_text(
        json.dumps(
            _owned_manifest(
                tmp_path,
                {
                    "clips": [
                        {
                            "source_clean_video": "clip.mp4",
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "../outside_mixed.mp4",
                                }
                            ]
                        }
                    ]
                },
                notice_content=b"OLD NOTICE",
            )
        ),
        encoding="utf-8",
    )
    notices_path.write_text("OLD NOTICE", encoding="utf-8")
    old_manifest_bytes = manifest_path.read_bytes()
    old_notice_bytes = notices_path.read_bytes()
    bgm = _asset(tmp_path, "bgm", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

    with pytest.raises(AudioDeliveryError, match="安全でないパス"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(bgm_asset_id=bgm.id),
        )

    assert clean.read_bytes() == b"clean"
    assert manifest_path.read_bytes() == old_manifest_bytes
    assert notices_path.read_bytes() == old_notice_bytes
    assert not list(tmp_path.glob(".audio_delivery_transaction-*"))


def test_notice_write_failure_restores_previous_provenance_and_audio_outputs(
    tmp_path, monkeypatch
):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clean = clips_dir / "clip.mp4"
    clean.write_bytes(b"clean")
    old_stem = clips_dir / "clip_bgm.wav"
    old_sidecar = clips_dir / "clip_audio.json"
    old_stem.write_bytes(b"old stem")
    old_sidecar.write_text('{"asset":"OLD-ASSET"}', encoding="utf-8")
    old_manifest = _owned_manifest(
        tmp_path,
        {
            "clips": [
                {
                    "source_clean_video": "clips/clip.mp4",
                    "artifacts": [
                        {"kind": "bgm_stem", "file": "clips/clip_bgm.wav"},
                        {
                            "kind": "clip_audio_manifest",
                            "file": "clips/clip_audio.json",
                        },
                    ]
                }
            ]
        },
        notice_content=b"OLD NOTICE FOR OLD-ASSET",
    )
    old_manifest["selected_assets"] = {"bgm": {"id": "OLD-ASSET"}}
    manifest_path = tmp_path / audio_delivery.AUDIO_MANIFEST_NAME
    notices_path = tmp_path / audio_delivery.AUDIO_NOTICES_NAME
    manifest_path.write_text(json.dumps(old_manifest), encoding="utf-8")
    notices_path.write_text("OLD NOTICE FOR OLD-ASSET", encoding="utf-8")
    old_manifest_bytes = manifest_path.read_bytes()
    old_notice_bytes = notices_path.read_bytes()

    bgm = _asset(tmp_path, "NEW-ASSET", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

    def fake_process(_paths, _highlights, **_kwargs):
        mixed = clips_dir / "clip_mixed.mp4"
        mixed.write_bytes(b"new mixed")
        old_stem.write_bytes(b"new stem")
        old_sidecar.write_text('{"asset":"NEW-ASSET"}', encoding="utf-8")
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=(clean, old_stem, old_sidecar, mixed),
                    clean_video=clean,
                    mixed_video=mixed,
                    bgm_stem=old_stem,
                    manifest=old_sidecar,
                    decoded_peak_4x_dbfs=-2.0,
                ),
            )
        )

    monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)
    real_write = audio_delivery._write_text_atomic

    def fail_notice(path, text):
        if path.name == audio_delivery.AUDIO_NOTICES_NAME:
            raise OSError("injected notice write failure")
        return real_write(path, text)

    monkeypatch.setattr(audio_delivery, "_write_text_atomic", fail_notice)

    with pytest.raises(OSError, match="injected notice write failure"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"title": "clip", "start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(delivery_mode="both", bgm_asset_id=bgm.id),
        )

    assert clean.read_bytes() == b"clean"
    assert old_stem.read_bytes() == b"old stem"
    assert old_sidecar.read_text(encoding="utf-8") == '{"asset":"OLD-ASSET"}'
    assert manifest_path.read_bytes() == old_manifest_bytes
    assert notices_path.read_bytes() == old_notice_bytes
    assert not (clips_dir / "clip_mixed.mp4").exists()
    assert not list(tmp_path.glob(".audio_delivery_transaction-*"))


def test_mixed_notice_failure_restores_clean_video_deleted_by_mixer(
    tmp_path, monkeypatch
):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"original clean video")
    manifest_path = tmp_path / audio_delivery.AUDIO_MANIFEST_NAME
    notices_path = tmp_path / audio_delivery.AUDIO_NOTICES_NAME
    previous_manifest = _owned_manifest(
        tmp_path,
        {"clips": []},
        delivery_mode="mixed",
        notice_content=b"OLD NOTICE",
    )
    previous_manifest["selected_assets"] = {"bgm": {"id": "OLD"}}
    manifest_path.write_text(json.dumps(previous_manifest), encoding="utf-8")
    notices_path.write_text("OLD NOTICE", encoding="utf-8")
    old_manifest_bytes = manifest_path.read_bytes()
    old_notice_bytes = notices_path.read_bytes()

    bgm = _asset(tmp_path, "NEW-ASSET", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

    def fake_process(_paths, _highlights, **_kwargs):
        mixed = tmp_path / "clip_mixed.mp4"
        mixed.write_bytes(b"new mixed")
        clean.unlink()
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=(mixed,),
                    clean_video=None,
                    mixed_video=mixed,
                    decoded_peak_4x_dbfs=-2.0,
                ),
            )
        )

    monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)
    real_write = audio_delivery._write_text_atomic

    def fail_notice(path, text):
        if path.name == audio_delivery.AUDIO_NOTICES_NAME:
            raise OSError("injected notice write failure")
        return real_write(path, text)

    monkeypatch.setattr(audio_delivery, "_write_text_atomic", fail_notice)

    with pytest.raises(OSError, match="injected notice write failure"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"title": "clip", "start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(delivery_mode="mixed", bgm_asset_id=bgm.id),
        )

    assert clean.read_bytes() == b"original clean video"
    assert not (tmp_path / "clip_mixed.mp4").exists()
    assert manifest_path.read_bytes() == old_manifest_bytes
    assert notices_path.read_bytes() == old_notice_bytes
    assert not list(tmp_path.glob(".audio_delivery_transaction-*"))


def test_mixed_notice_failure_restores_clean_video_overwritten_by_mixer(
    tmp_path, monkeypatch
):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"original clean video")
    bgm = _asset(tmp_path, "NEW-ASSET", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

    def fake_process(_paths, _highlights, **_kwargs):
        mixed = tmp_path / "clip_mixed.mp4"
        mixed.write_bytes(b"new mixed")
        clean.write_bytes(b"REPLACED")
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=(mixed,),
                    clean_video=None,
                    mixed_video=mixed,
                    decoded_peak_4x_dbfs=-2.0,
                ),
            )
        )

    monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)
    monkeypatch.setattr(
        audio_delivery,
        "_write_text_atomic",
        lambda path, _text: (_ for _ in ()).throw(OSError("notice failed"))
        if path.name == audio_delivery.AUDIO_NOTICES_NAME
        else None,
    )

    with pytest.raises(OSError, match="notice failed"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"title": "clip", "start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(delivery_mode="mixed", bgm_asset_id=bgm.id),
        )

    assert clean.read_bytes() == b"original clean video"
    assert not (tmp_path / "clip_mixed.mp4").exists()
    assert not list(tmp_path.glob(".audio_delivery_transaction-*"))


def test_mixed_audio_atomically_remaps_effects_manifest_to_final_video(
    tmp_path, monkeypatch
):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    effects_path = tmp_path / audio_delivery.EFFECTS_MANIFEST_NAME
    effects_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generator": "clip-extractor",
                "clips": [
                    {
                        "source_clean_video": "clip.mp4",
                        "output_file": "clip.mp4",
                        "enabled": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    bgm = _asset(tmp_path, "BGM", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

    def fake_process(_paths, _highlights, **_kwargs):
        mixed = tmp_path / "clip_mixed.mp4"
        mixed.write_bytes(b"mixed")
        clean.unlink()
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=(mixed,),
                    clean_video=None,
                    mixed_video=mixed,
                    decoded_peak_4x_dbfs=-2.0,
                ),
            )
        )

    monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)

    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clean]},
        [{"title": "clip", "start_sec": 0, "end_sec": 2}],
        options=AudioDeliveryOptions(delivery_mode="mixed", bgm_asset_id=bgm.id),
        effects_manifest_dirs={"clips": tmp_path},
    )

    payload = json.loads(effects_path.read_text(encoding="utf-8"))
    assert result.media_groups["clips"] == (tmp_path / "clip_mixed.mp4",)
    assert payload["clips"][0]["source_clean_video"] == "clip.mp4"
    assert payload["clips"][0]["output_file"] == "clip_mixed.mp4"


def test_effects_manifest_remap_failure_rolls_back_mixed_audio_and_manifest(
    tmp_path, monkeypatch
):
    clean = tmp_path / "clip.mp4"
    clean.write_bytes(b"clean")
    effects_path = tmp_path / audio_delivery.EFFECTS_MANIFEST_NAME
    original_effects = json.dumps(
        {
            "schema_version": 1,
            "generator": "clip-extractor",
            "clips": [{"output_file": "clip.mp4", "enabled": True}],
        }
    )
    effects_path.write_text(original_effects, encoding="utf-8")
    bgm = _asset(tmp_path, "BGM", "bgm")
    monkeypatch.setattr(audio_delivery, "get_pack_status", _ready_status)
    monkeypatch.setattr(audio_delivery, "get_installed_asset", lambda _id: bgm)

    def fake_process(_paths, _highlights, **_kwargs):
        mixed = tmp_path / "clip_mixed.mp4"
        mixed.write_bytes(b"mixed")
        clean.unlink()
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=(mixed,),
                    clean_video=None,
                    mixed_video=mixed,
                    decoded_peak_4x_dbfs=-2.0,
                ),
            )
        )

    def fail_remap(*_args, **_kwargs):
        effects_path.write_text("PARTIAL", encoding="utf-8")
        raise audio_delivery.VideoEffectError("injected remap failure")

    monkeypatch.setattr(audio_delivery, "process_clip_batch", fake_process)
    monkeypatch.setattr(
        audio_delivery,
        "remap_effects_manifest_outputs",
        fail_remap,
    )

    with pytest.raises(AudioDeliveryError, match="VFX来歴"):
        deliver_audio_groups(
            tmp_path,
            {"clips": [clean]},
            [{"title": "clip", "start_sec": 0, "end_sec": 2}],
            options=AudioDeliveryOptions(delivery_mode="mixed", bgm_asset_id=bgm.id),
            effects_manifest_dirs={"clips": tmp_path},
        )

    assert clean.read_bytes() == b"clean"
    assert not (tmp_path / "clip_mixed.mp4").exists()
    assert effects_path.read_text(encoding="utf-8") == original_effects
