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
            se_usage_percent=50,
        ),
    )

    assert len(calls) == 4
    selected = [call["se_path"] for call in calls if call["se_path"] is not None]
    assert len(selected) == 2
    assert all(path in {asset.path for asset in assets} for path in selected)
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
