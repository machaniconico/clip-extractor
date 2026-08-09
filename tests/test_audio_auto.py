import json
from pathlib import Path
from types import SimpleNamespace

from audio_delivery import AudioDeliveryOptions, deliver_audio_groups
from audio_mix import AudioBatchResult, AudioOutputResult
from user_media import UserMediaAsset


def _se_asset(tmp_path: Path, index: int) -> UserMediaAsset:
    path = tmp_path / f"se-{index}.mp3"
    path.write_bytes(f"se-{index}".encode())
    digest = f"{index + 1:x}" * 64
    return UserMediaAsset(
        id=f"user:se:{digest}",
        kind="se",
        path=path.resolve(),
        filename=path.name,
        relative_path=path.name,
        size=path.stat().st_size,
        sha256=digest,
    )


def test_auto_se_uses_folder_assets_at_slider_density(tmp_path, monkeypatch):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clips = []
    for index in range(4):
        path = clips_dir / f"clip-{index}.mp4"
        path.write_bytes(b"clean")
        clips.append(path)
    assets = tuple(_se_asset(tmp_path, index) for index in range(3))
    calls = []

    monkeypatch.setattr(
        "audio_delivery.scan_optional_user_media",
        lambda _folder, kind: assets if kind == "se" else (),
    )
    monkeypatch.setattr("audio_delivery.validate_user_media", lambda asset: asset)

    def fake_process(paths, _highlights, **kwargs):
        calls.append(kwargs)
        path = Path(paths[0])
        return AudioBatchResult(
            clips=(
                AudioOutputResult(
                    deliverables=(path,),
                    clean_video=path,
                ),
            )
        )

    monkeypatch.setattr("audio_delivery.process_clip_batch", fake_process)

    result = deliver_audio_groups(
        tmp_path,
        {"clips": clips},
        [
            {"title": f"clip-{index}", "start_sec": index, "end_sec": index + 2}
            for index in range(4)
        ],
        options=AudioDeliveryOptions(
            delivery_mode="separate",
            se_user_folder=str(tmp_path / "SE"),
            se_cue_seconds=7.0,
            se_usage_percent=50,
        ),
    )

    assert len(calls) == 4
    selected = [call["se_path"] for call in calls if call["se_path"] is not None]
    assert len(selected) == 2
    assert all(path in {asset.path for asset in assets} for path in selected)
    assert all(call["settings"].se_cue_seconds == 0.0 for call in calls)
    manifest = result.manifest_path.read_text(encoding="utf-8")
    assert '"source_type": "automatic_user_selection"' in manifest
    assert '"usage_percent": 50.0' in manifest


def test_content_analysis_places_matching_se_at_word_time(
    tmp_path, monkeypatch
):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clip = clips_dir / "clip.mp4"
    clip.write_bytes(b"clean")
    assets = (
        _se_asset(tmp_path, 0),
        _se_asset(tmp_path, 1),
    )
    assets = tuple(
        asset.__class__(
            **{
                **asset.__dict__,
                "filename": filename,
                "relative_path": filename,
            }
        )
        for asset, filename in zip(assets, ("悲鳴.mp3", "バーン.mp3"))
    )
    calls = []

    monkeypatch.setattr(
        "audio_delivery.scan_optional_user_media",
        lambda _folder, kind: assets if kind == "se" else (),
    )
    monkeypatch.setattr("audio_delivery.validate_user_media", lambda asset: asset)

    def fake_process(paths, _highlights, **kwargs):
        calls.append(kwargs)
        path = Path(paths[0])
        return AudioBatchResult(
            clips=(AudioOutputResult(deliverables=(path,), clean_video=path),)
        )

    monkeypatch.setattr("audio_delivery.process_clip_batch", fake_process)

    segments = [
        SimpleNamespace(
            start=0.0,
            end=3.0,
            text="うわ！",
            words=[SimpleNamespace(start=1.25, end=1.8, text="うわ！")],
        )
    ]
    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clip]},
        [{"title": "test", "start_sec": 0.0, "end_sec": 4.0}],
        options=AudioDeliveryOptions(
            delivery_mode="separate",
            se_user_folder=str(tmp_path / "SE"),
            se_usage_percent=100,
        ),
        transcript_segments=segments,
    )

    assert len(calls) == 1
    cue = calls[0]["se_cues_by_clip"][0][0]
    assert cue.cue_seconds == 1.25
    assert cue.category == "surprise"
    payload = result.manifest_path.read_text(encoding="utf-8")
    assert '"se_analysis"' in payload
    assert '"cue_seconds": 1.25' in payload


def test_manual_se_uses_authoritative_llm_cue_without_fixed_offset(
    tmp_path, monkeypatch
):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clip = clips_dir / "clip.mp4"
    clip.write_bytes(b"clean")
    asset = _se_asset(tmp_path, 0)
    calls = []

    monkeypatch.setattr(
        "audio_delivery.resolve_user_media_asset",
        lambda _folder, _asset_id, _kind: asset,
    )
    monkeypatch.setattr("audio_delivery.validate_user_media", lambda candidate: candidate)
    monkeypatch.setattr("audio_energy.compute_energy_curve", lambda _path: None)

    def fake_process(paths, _highlights, **kwargs):
        calls.append(kwargs)
        path = Path(paths[0])
        return AudioBatchResult(
            clips=(AudioOutputResult(deliverables=(path,), clean_video=path),)
        )

    monkeypatch.setattr("audio_delivery.process_clip_batch", fake_process)

    result = deliver_audio_groups(
        tmp_path,
        {"clips": [clip]},
        [
            {
                "title": "reveal",
                "start_sec": 10.0,
                "end_sec": 20.0,
                "se_cues": [
                    {
                        "time_sec": 12.25,
                        "category": "impact",
                        "intensity": 0.9,
                        "reason": "結論が出る瞬間",
                    }
                ],
            }
        ],
        options=AudioDeliveryOptions(
            delivery_mode="separate",
            se_asset_id=asset.id,
            se_user_folder=str(tmp_path),
            se_cue_seconds=7.0,
            se_usage_percent=100,
        ),
    )

    assert len(calls) == 1
    cue = calls[0]["se_cues_by_clip"][0][0]
    assert cue.source == asset.path
    assert cue.cue_seconds == 2.25
    assert cue.category == "impact"
    assert cue.reason == "llm_scene:結論が出る瞬間"
    assert "se_path" not in calls[0]

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["settings"]["se_timing_strategy"] == "llm_scene_audio_peak"
    assert "se_cue_seconds" not in manifest["settings"]
    event = manifest["groups"]["clips"][0]["se_analysis"]["events"][0]
    assert event["source"] == "llm"
    assert event["evidence"] == "llm_scene:結論が出る瞬間"


def test_manual_se_explicit_empty_llm_cues_produce_no_se(tmp_path, monkeypatch):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clip = clips_dir / "clip.mp4"
    clip.write_bytes(b"clean")
    asset = _se_asset(tmp_path, 0)
    calls = []

    monkeypatch.setattr(
        "audio_delivery.resolve_user_media_asset",
        lambda _folder, _asset_id, _kind: asset,
    )
    monkeypatch.setattr("audio_delivery.validate_user_media", lambda candidate: candidate)

    def fake_process(paths, _highlights, **kwargs):
        calls.append(kwargs)
        path = Path(paths[0])
        return AudioBatchResult(
            clips=(AudioOutputResult(deliverables=(path,), clean_video=path),)
        )

    monkeypatch.setattr("audio_delivery.process_clip_batch", fake_process)

    deliver_audio_groups(
        tmp_path,
        {"clips": [clip]},
        [{"start_sec": 0.0, "end_sec": 4.0, "se_cues": []}],
        options=AudioDeliveryOptions(
            delivery_mode="separate",
            se_asset_id=asset.id,
            se_user_folder=str(tmp_path),
            se_cue_seconds=3.0,
            se_usage_percent=100,
        ),
    )

    assert len(calls) == 1
    assert calls[0]["se_cues_by_clip"] == ((),)
    assert "se_path" not in calls[0]


def test_manual_se_legacy_path_ignores_obsolete_fixed_cue(
    tmp_path, monkeypatch
):
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    clip = clips_dir / "clip.mp4"
    clip.write_bytes(b"clean")
    asset = _se_asset(tmp_path, 0)
    calls = []

    monkeypatch.setattr(
        "audio_delivery.resolve_user_media_asset",
        lambda _folder, _asset_id, _kind: asset,
    )
    monkeypatch.setattr("audio_delivery.validate_user_media", lambda candidate: candidate)

    def fake_process(paths, _highlights, **kwargs):
        calls.append(kwargs)
        path = Path(paths[0])
        return AudioBatchResult(
            clips=(AudioOutputResult(deliverables=(path,), clean_video=path),)
        )

    monkeypatch.setattr("audio_delivery.process_clip_batch", fake_process)

    deliver_audio_groups(
        tmp_path,
        {"clips": [clip]},
        [{"start_sec": 0.0, "end_sec": 10.0}],
        options=AudioDeliveryOptions(
            delivery_mode="separate",
            se_asset_id=asset.id,
            se_user_folder=str(tmp_path),
            se_cue_seconds=7.0,
            se_usage_percent=100,
        ),
    )

    assert len(calls) == 1
    assert calls[0]["settings"].se_cue_seconds == 0.0
    assert calls[0]["se_path"] == asset.path
