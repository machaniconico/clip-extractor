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


def _asset(tmp_path: Path, asset_id: str, kind: str) -> InstalledAsset:
    path = tmp_path / f"{asset_id}.ogg"
    path.write_bytes(b"ogg")
    return InstalledAsset(
        id=asset_id,
        label=f"label-{asset_id}",
        kind=kind,
        path=path,
        pack_id="cc0-starter",
        pack_version="2026.08.1",
        size=3,
        sha256="a" * 64,
        creator="creator",
        source_page="https://example.invalid/source",
        license_id="CC0-1.0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        license_checked_at="2026-08-04",
        attribution_required=False,
    )


def _ready_status():
    return SimpleNamespace(
        ready=True,
        pack_id="cc0-starter",
        version="2026.08.1",
    )


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
    manifest = {
        "groups": {
            "clips": [
                {
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
        }
    }
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
    assert "CC0 1.0 Universal" in notices
    assert "Creator: creator" in notices


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
            {
                "groups": {
                    "clips": [
                        {
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "../outside_mixed.mp4",
                                }
                            ]
                        }
                    ]
                }
            }
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
            {
                "groups": {
                    "clips": [
                        {
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "clip_mixed.mp4",
                                }
                            ]
                        }
                    ]
                }
            }
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
            {
                "groups": {
                    "clips": [
                        {
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "linked/clip_mixed.mp4",
                                }
                            ]
                        }
                    ]
                }
            }
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
            {
                "groups": {
                    "clips": [
                        {
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "clip_mixed.mp4",
                                }
                            ]
                        }
                    ]
                }
            }
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
            {
                "groups": {
                    "clips": [
                        {
                            "artifacts": [
                                {
                                    "kind": "mixed_video",
                                    "file": "../outside_mixed.mp4",
                                }
                            ]
                        }
                    ]
                }
            }
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
    old_manifest = {
        "selected_assets": {"bgm": {"id": "OLD-ASSET"}},
        "groups": {
            "clips": [
                {
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
    }
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
    manifest_path.write_text(
        '{"selected_assets":{"bgm":{"id":"OLD"}}}', encoding="utf-8"
    )
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
