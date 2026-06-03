"""Unit tests for audio loudness/excitement fusion."""

import logging
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import audio_energy
from audio_energy import (
    EnergyCurve,
    clip_audio_score,
    compute_energy_curve,
    excitement_scores,
    fuse_audio_energy,
)


def _highlight(title: str, start: float, end: float) -> dict:
    return {
        "start": f"00:00:{start:06.3f}",
        "end": f"00:00:{end:06.3f}",
        "title": title,
        "reason": title,
        "start_sec": start,
        "end_sec": end,
        "duration": end - start,
    }


def test_compute_energy_curve_reads_pcm_wav_and_orders_loudness(monkeypatch, tmp_path):
    written_paths: list[Path] = []

    def fake_run(cmd, **kwargs):
        assert cmd[cmd.index("-vn"):cmd.index("-vn") + 7] == [
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        ]
        out_path = Path(cmd[-1])
        written_paths.append(out_path)
        sample_rate = 16000
        samples = np.concatenate([
            np.full(sample_rate // 2, 1000, dtype=np.int16),
            np.full(sample_rate // 2, 4000, dtype=np.int16),
            np.full(sample_rate // 2, 12000, dtype=np.int16),
            np.full(sample_rate // 2, 2000, dtype=np.int16),
        ])
        with wave.open(str(out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(samples.tobytes())
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(audio_energy.subprocess, "run", fake_run)

    curve = compute_energy_curve(tmp_path / "input.mp4", win_sec=0.5, hop_sec=0.5)

    assert curve is not None
    assert len(curve.db) == 4
    assert np.allclose(curve.times, [0.0, 0.5, 1.0, 1.5])
    assert curve.db[2] > curve.db[1] > curve.db[3] > curve.db[0]
    assert written_paths and not written_paths[0].exists()


def test_excitement_scores_are_bounded_and_peak_percentile_tracks_loud_spike():
    curve = EnergyCurve(
        times=np.arange(10, dtype=float) * 0.5,
        db=np.array([-40, -39, -38, -37, -10, -36, -35, -34, -33, -32], dtype=float),
        hop_sec=0.5,
    )

    scores = excitement_scores(curve)

    assert np.all(scores >= 0.0)
    assert np.all(scores <= 1.0)
    assert scores[4] > 0.95
    assert scores[0] == pytest.approx(0.0)


def test_clip_audio_score_is_peak_biased_and_empty_range_is_zero():
    curve = EnergyCurve(
        times=np.array([0.0, 1.0, 2.0, 3.0]),
        db=np.array([-40.0, -30.0, -20.0, -35.0]),
        hop_sec=1.0,
    )
    scores = np.array([0.1, 0.4, 0.8, 0.2])

    assert clip_audio_score(curve, scores, 1.0, 4.0) == pytest.approx(0.6333333333)
    assert clip_audio_score(curve, scores, 4.0, 5.0) == 0.0


def test_fuse_audio_energy_ranking_modes(monkeypatch, tmp_path):
    curve = EnergyCurve(
        times=np.array([0.0, 1.0, 2.0]),
        db=np.array([-40.0, -40.0, -5.0]),
        hop_sec=1.0,
    )
    monkeypatch.setattr(audio_energy, "compute_energy_curve", lambda _path: curve)

    highlights = [
        _highlight("rank1", 0.0, 1.0),
        _highlight("rank2", 1.0, 2.0),
        _highlight("rank3_loud", 2.0, 3.0),
    ]

    alpha_zero = fuse_audio_energy(tmp_path / "video.mp4", highlights, alpha=0.0)
    alpha_one = fuse_audio_energy(tmp_path / "video.mp4", highlights, alpha=1.0)
    alpha_mix = fuse_audio_energy(tmp_path / "video.mp4", highlights, alpha=0.35)

    assert [h["title"] for h in alpha_zero] == ["rank1", "rank2", "rank3_loud"]
    assert [h["title"] for h in alpha_one] == ["rank3_loud", "rank1", "rank2"]
    assert [h["title"] for h in alpha_mix] == ["rank1", "rank3_loud", "rank2"]
    for highlight in alpha_mix:
        assert 0.0 <= highlight["audio_score"] <= 1.0
        assert 0.0 <= highlight["combined_score"] <= 1.0


@pytest.mark.parametrize("failure_mode", ["raise", "none"])
def test_fuse_audio_energy_fail_open_returns_original(monkeypatch, caplog, tmp_path, failure_mode):
    highlights = [_highlight("rank1", 0.0, 1.0)]

    if failure_mode == "raise":
        def fail(_path):
            raise RuntimeError("decode failed")
        monkeypatch.setattr(audio_energy, "compute_energy_curve", fail)
    else:
        monkeypatch.setattr(audio_energy, "compute_energy_curve", lambda _path: None)

    with caplog.at_level(logging.WARNING, logger="clip-extractor"):
        result = fuse_audio_energy(tmp_path / "video.mp4", highlights)

    assert result is highlights
    assert "audio_score" not in highlights[0]
    assert "Audio energy fusion skipped" in caplog.text


def test_snap_keeps_boundaries_valid_and_respects_duration_limits(monkeypatch, tmp_path):
    curve = EnergyCurve(
        times=np.array([8.0, 10.0, 12.0, 18.0, 20.0, 22.0]),
        db=np.array([-5.0, -40.0, -40.0, -40.0, -40.0, -5.0]),
        hop_sec=1.0,
    )
    monkeypatch.setattr(audio_energy, "compute_energy_curve", lambda _path: curve)
    highlights = [_highlight("snap", 10.0, 20.0)]

    result = fuse_audio_energy(
        tmp_path / "video.mp4",
        highlights,
        alpha=1.0,
        snap=True,
        snap_window=2.0,
        min_duration=12,
        max_duration=13,
    )
    snapped = result[0]

    assert snapped["end_sec"] > snapped["start_sec"]
    assert 12.0 <= snapped["duration"] <= 13.0
    assert 8.0 <= snapped["start_sec"] <= 12.0
    assert 18.0 <= snapped["end_sec"] <= 22.0
    assert snapped["start"] == "00:00:08.000"
