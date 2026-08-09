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


def test_llm_cue_is_clip_relative_and_overrides_keyword_fallback(tmp_path):
    segments = [
        SimpleNamespace(
            start=11.0,
            end=12.0,
            text="うわ！",
            words=[SimpleNamespace(start=11.25, end=11.8, text="うわ！")],
        )
    ]
    highlight = {
        "start_sec": 10.0,
        "end_sec": 20.0,
        "title": "",
        "se_cues": [
            {
                "time": "00:00:16.000",
                "time_sec": 16.0,
                "category": "success",
                "intensity": 0.9,
                "reason": "結果が確定する瞬間",
            }
        ],
    }

    events = build_se_content_events(highlight, segments)

    assert len(events) == 1
    assert events[0].source == "llm"
    assert events[0].category == "success"
    assert events[0].cue_seconds == 6.0
    assert events[0].evidence == "llm_scene:結果が確定する瞬間"


def test_llm_empty_cues_mean_no_sound_effect_even_with_keyword():
    segments = [
        SimpleNamespace(
            start=0.0,
            end=1.0,
            text="うわ！",
            words=[SimpleNamespace(start=0.25, end=0.5, text="うわ！")],
        )
    ]

    events = build_se_content_events(
        {"start_sec": 0.0, "end_sec": 2.0, "se_cues": []},
        segments,
    )

    assert events == ()


def test_llm_cue_snaps_to_nearby_audio_excitement_peak(monkeypatch, tmp_path):
    import numpy as np

    from audio_energy import EnergyCurve

    monkeypatch.setattr(
        "audio_energy.compute_energy_curve",
        lambda _path: EnergyCurve(
            times=np.array([1.5, 2.0, 2.5]),
            db=np.array([-20.0, -4.0, -18.0]),
            hop_sec=0.5,
        ),
    )
    monkeypatch.setattr(
        "audio_energy.excitement_scores",
        lambda _curve: np.array([0.1, 0.95, 0.2]),
    )
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"clip")
    highlight = {
        "start_sec": 10.0,
        "end_sec": 20.0,
        "se_cues": [
            {
                "time_sec": 12.2,
                "category": "impact",
                "intensity": 0.8,
                "reason": "盛り上がりの頂点",
            }
        ],
    }

    events = build_se_content_events(highlight, clip_path=clip)

    assert len(events) == 1
    assert events[0].cue_seconds == 2.0
    assert events[0].source == "llm_audio_snap"


def test_llm_cue_keeps_semantic_time_when_nearby_audio_is_below_threshold(
    monkeypatch, tmp_path
):
    import numpy as np

    from audio_energy import EnergyCurve

    monkeypatch.setattr(
        "audio_energy.compute_energy_curve",
        lambda _path: EnergyCurve(
            times=np.array([1.5, 2.0, 2.5]),
            db=np.array([-20.0, -10.0, -18.0]),
            hop_sec=0.5,
        ),
    )
    monkeypatch.setattr(
        "audio_energy.excitement_scores",
        lambda _curve: np.array([0.1, 0.54, 0.2]),
    )
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"clip")

    events = build_se_content_events(
        {
            "start_sec": 10.0,
            "end_sec": 20.0,
            "se_cues": [
                {
                    "time_sec": 12.2,
                    "category": "impact",
                    "intensity": 0.8,
                    "reason": "意味上の頂点",
                }
            ],
        },
        clip_path=clip,
    )

    assert len(events) == 1
    assert round(events[0].cue_seconds, 3) == 2.2
    assert events[0].source == "llm"


def test_llm_out_of_range_cues_are_dropped_without_legacy_fallback():
    segments = [
        SimpleNamespace(
            start=11.0,
            end=12.0,
            text="うわ！",
            words=[SimpleNamespace(start=11.25, end=11.8, text="うわ！")],
        )
    ]

    events = build_se_content_events(
        {
            "start_sec": 10.0,
            "end_sec": 20.0,
            "se_cues": [
                {"time_sec": 9.9, "category": "impact", "intensity": 1.0},
                {"time_sec": 20.0, "category": "impact", "intensity": 1.0},
            ],
        },
        segments,
    )

    assert events == ()


def test_llm_cue_near_clip_end_is_not_shifted_by_safety_margin(
    monkeypatch, tmp_path
):
    import numpy as np

    from audio_energy import EnergyCurve

    highlight = {
        "start_sec": 10.0,
        "end_sec": 20.0,
        "se_cues": [
            {
                "time_sec": 19.99,
                "category": "impact",
                "intensity": 0.9,
                "reason": "終端直前の意味イベント",
            }
        ],
    }
    semantic_only = build_se_content_events(highlight)

    monkeypatch.setattr(
        "audio_energy.compute_energy_curve",
        lambda _path: EnergyCurve(
            times=np.array([9.5, 9.9]),
            db=np.array([-12.0, -10.0]),
            hop_sec=0.5,
        ),
    )
    monkeypatch.setattr(
        "audio_energy.excitement_scores",
        lambda _curve: np.array([0.54, 0.2]),
    )
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"clip")
    below_threshold = build_se_content_events(highlight, clip_path=clip)
    asset = _asset(tmp_path, "バーン.mp3", 2)

    for events in (semantic_only, below_threshold):
        assert len(events) == 1
        assert abs(events[0].cue_seconds - 9.99) < 1e-9
        plans = plan_se_cues((asset,), events, 100)
        assert len(plans) == 1
        assert abs(plans[0].cue_seconds - 9.99) < 1e-9
