from pathlib import Path
from types import SimpleNamespace

from se_analysis import build_se_content_events, plan_se_cues
from user_media import UserMediaAsset


def _asset(tmp_path: Path, filename: str, index: int) -> UserMediaAsset:
    path = tmp_path / filename
    path.write_bytes(filename.encode("utf-8"))
    digest = f"{index + 1:x}" * 64
    return UserMediaAsset(
        id=f"user:se:{digest}",
        kind="se",
        path=path.resolve(),
        filename=filename,
        relative_path=filename,
        size=path.stat().st_size,
        sha256=digest,
    )


def test_keyword_event_uses_word_timestamp_and_matching_filename(tmp_path):
    segments = [
        SimpleNamespace(
            start=10.0,
            end=14.0,
            text="うわ！",
            words=[SimpleNamespace(start=11.25, end=11.8, text="うわ！")],
        )
    ]
    highlight = {"start_sec": 10.0, "end_sec": 20.0, "title": ""}
    events = build_se_content_events(highlight, segments)
    plans = plan_se_cues(
        (_asset(tmp_path, "悲鳴.mp3", 0), _asset(tmp_path, "バーン.mp3", 1)),
        events,
        100,
    )

    assert len(events) == 1
    assert events[0].category == "surprise"
    assert events[0].cue_seconds == 1.25
    assert len(plans) == 1
    assert plans[0].asset.filename == "悲鳴.mp3"
    assert plans[0].cue_seconds == 1.25


def test_usage_density_and_matching_are_reproducible(tmp_path):
    segments = [
        SimpleNamespace(
            start=float(index * 2),
            end=float(index * 2) + 0.5,
            text=text,
            words=[
                SimpleNamespace(
                    start=float(index * 2),
                    end=float(index * 2) + 0.5,
                    text=text,
                )
            ],
        )
        for index, text in enumerate(("うわ", "笑", "やった", "注意"), start=0)
    ]
    highlight = {"start_sec": 0.0, "end_sec": 10.0, "title": ""}
    events = build_se_content_events(highlight, segments, max_events=8)
    assets = (
        _asset(tmp_path, "悲鳴.mp3", 0),
        _asset(tmp_path, "間抜け.mp3", 1),
        _asset(tmp_path, "ファンファーレ.mp3", 2),
        _asset(tmp_path, "警報が鳴る.mp3", 3),
    )

    first = plan_se_cues(assets, events, 50, max_events_per_clip=8)
    second = plan_se_cues(assets, events, 50, max_events_per_clip=8)

    assert len(events) == 4
    assert len(first) == 2
    assert [plan.to_manifest() for plan in first] == [
        plan.to_manifest() for plan in second
    ]
