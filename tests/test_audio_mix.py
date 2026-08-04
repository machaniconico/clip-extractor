from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import subprocess
import wave

import pytest

import audio_mix
from audio_mix import (
    AudioDeliveryMode,
    AudioMixSettings,
    process_clip_audio,
    process_clip_batch,
)


def _touch(path: Path, content: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _fake_media_runner(captured: list[list[str]], *, probe_stdout: str = "0\n"):
    def run(command):
        cmd = [str(part) for part in command]
        captured.append(cmd)
        if Path(cmd[0]).name.lower().startswith("ffprobe"):
            return subprocess.CompletedProcess(cmd, 0, stdout=probe_stdout, stderr="")
        output = Path(cmd[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated media")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


def test_audio_mix_settings_validate_and_normalize_delivery_mode():
    settings = AudioMixSettings(
        delivery_mode=" BOTH ",
        bgm_gain_db="-16.5",
        se_gain_db=2,
        se_cue_seconds="0.25",
    )

    assert settings.delivery_mode is AudioDeliveryMode.BOTH
    assert settings.bgm_gain_db == -16.5
    assert settings.se_gain_db == 2.0
    assert settings.se_cue_seconds == 0.25

    with pytest.raises(ValueError, match="delivery_mode"):
        AudioMixSettings(delivery_mode="flattened")
    with pytest.raises(ValueError, match="finite"):
        AudioMixSettings(bgm_gain_db=float("nan"))
    with pytest.raises(ValueError, match="greater than or equal"):
        AudioMixSettings(se_cue_seconds=-0.1)
    with pytest.raises(ValueError, match="ffmpeg_bin"):
        AudioMixSettings(ffmpeg_bin="  ")


def test_decoded_peak_probe_records_silence_as_finite_json_safe_floor(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        audio_mix,
        "_run_command",
        lambda command: subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="[Parsed_astats_0] Peak level dB: -inf\n",
        ),
    )

    peak = audio_mix._measure_decoded_peak_4x_dbfs(tmp_path / "silent.mp4", "ffmpeg")

    assert peak == audio_mix.AAC_SILENCE_FLOOR_DB
    assert json.dumps({"peak": peak}, allow_nan=False)


def test_no_selected_audio_returns_clean_video_without_running_tools(
    tmp_path, monkeypatch
):
    clean = _touch(tmp_path / "clean.mp4", b"original")
    for stale_name in (
        "clean_bgm.wav",
        "clean_se.wav",
        "clean_mixed.mp4",
        "clean_audio.json",
    ):
        _touch(tmp_path / stale_name, b"stale generated output")

    def unexpected(_command):
        raise AssertionError("FFmpeg/FFprobe must not run without selected audio")

    monkeypatch.setattr(audio_mix, "_run_command", unexpected)
    result = process_clip_audio(
        clean,
        duration_seconds=3,
        settings=AudioMixSettings(delivery_mode="mixed"),
    )

    assert result.deliverables == (clean.resolve(),)
    assert result.clean_video == clean.resolve()
    assert result.mixed_video is None
    assert clean.read_bytes() == b"original"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["clean.mp4"]


def test_separate_mode_builds_looped_bgm_delayed_se_and_manifest(tmp_path, monkeypatch):
    clean = _touch(tmp_path / "日本語 clip name.mp4")
    bgm = _touch(tmp_path / "assets" / "BGM tone.ogg")
    se = _touch(tmp_path / "assets" / "SE hit.ogg")
    captured: list[list[str]] = []
    monkeypatch.setattr(audio_mix, "_run_command", _fake_media_runner(captured))

    result = process_clip_audio(
        clean,
        duration_seconds=2.75,
        settings=AudioMixSettings(
            delivery_mode="separate",
            bgm_gain_db=-15.5,
            se_gain_db=-4,
            se_cue_seconds=0.375,
        ),
        bgm_path=bgm,
        se_path=se,
        clip_metadata={"title": "見どころ", "start_sec": 10.0},
        provenance={
            "bgm": {"asset_id": "music-01", "license": "CC0-1.0"},
            "se": {"asset_id": "hit-01", "license": "CC0-1.0"},
            "pack_version": "2026.08.1",
        },
    )

    assert len(captured) == 2
    bgm_cmd, se_cmd = captured
    assert bgm_cmd[bgm_cmd.index("-stream_loop") + 1] == "-1"
    assert bgm_cmd[bgm_cmd.index("-i") + 1] == str(bgm.resolve())
    bgm_filter = bgm_cmd[bgm_cmd.index("-filter_complex") + 1]
    assert "volume=-15.5dB" in bgm_filter
    assert "apad=whole_dur=2.75" in bgm_filter
    assert bgm_cmd[bgm_cmd.index("-c:a") + 1] == "pcm_s16le"
    assert bgm_cmd[bgm_cmd.index("-ar") + 1] == "48000"
    assert bgm_cmd[bgm_cmd.index("-ac") + 1] == "2"

    se_filter = se_cmd[se_cmd.index("-filter_complex") + 1]
    assert "volume=-4dB" in se_filter
    assert "adelay=delays=18000S:all=1" in se_filter
    assert "atrim=start=0:end=2.375" in se_filter

    expected_names = {
        "日本語 clip name.mp4",
        "日本語 clip name_bgm.wav",
        "日本語 clip name_se.wav",
        "日本語 clip name_audio.json",
    }
    assert {path.name for path in result.deliverables} == expected_names
    assert result.mixed_video is None
    assert all(path.is_file() for path in result.deliverables)
    assert not list(tmp_path.glob(".*_audio_*"))

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["delivery_mode"] == "separate"
    assert manifest["clip"]["duration_seconds"] == 2.75
    assert manifest["clip"]["metadata"]["title"] == "見どころ"
    assert manifest["timeline"] == {"origin_seconds": 0.0, "duration_seconds": 2.75}
    assert manifest["stem_format"] == {
        "codec": "pcm_s16le",
        "sample_rate_hz": 48000,
        "channels": 2,
        "channel_layout": "stereo",
    }
    assert manifest["audio"]["bgm"]["provenance"]["license"] == "CC0-1.0"
    assert manifest["audio"]["se"]["cue_seconds"] == 0.375
    assert manifest["provenance"]["pack_version"] == "2026.08.1"


def test_mixed_mode_maps_dialogue_once_copies_video_and_removes_clean_last(
    tmp_path, monkeypatch
):
    clean = _touch(tmp_path / "clip.mp4", b"clean source")
    bgm = _touch(tmp_path / "music.ogg")
    stale_bgm = _touch(tmp_path / "clip_bgm.wav", b"stale")
    stale_se = _touch(tmp_path / "clip_se.wav", b"stale")
    stale_manifest = _touch(tmp_path / "clip_audio.json", b"stale")
    captured: list[list[str]] = []
    monkeypatch.setattr(audio_mix, "_run_command", _fake_media_runner(captured))
    monkeypatch.setattr(audio_mix, "_probe_has_audio", lambda *_args: True)
    monkeypatch.setattr(audio_mix, "_measure_decoded_peak_4x_dbfs", lambda *_args: -6.0)

    result = process_clip_audio(
        clean,
        duration_seconds=5,
        settings=AudioMixSettings(delivery_mode="mixed"),
        bgm_path=bgm,
    )

    assert len(captured) == 2
    mix_cmd = captured[-1]
    mix_filter = mix_cmd[mix_cmd.index("-filter_complex") + 1]
    assert mix_filter.count("[0:a:0]") == 1
    assert mix_filter.count("[dialogue]") == 2
    assert "amix=inputs=2" in mix_filter
    assert "normalize=0,alimiter=limit=0.95" in mix_filter
    assert mix_cmd[mix_cmd.index("-map") + 1] == "0:v:0"
    assert mix_cmd[mix_cmd.index("-c:v") + 1] == "copy"
    assert mix_cmd[mix_cmd.index("-c:a") + 1] == "aac"

    mixed = tmp_path / "clip_mixed.mp4"
    assert result.deliverables == (mixed.resolve(),)
    assert result.clean_video is None
    assert result.mixed_video == mixed.resolve()
    assert result.decoded_peak_4x_dbfs == -6.0
    assert result.post_mix_attenuation_db == 0.0
    assert mixed.is_file()
    assert not clean.exists()
    assert not stale_bgm.exists()
    assert not stale_se.exists()
    assert not stale_manifest.exists()
    assert not list(tmp_path.glob(".*_audio_*"))


def test_both_mode_keeps_clean_stems_manifest_and_mixed(tmp_path, monkeypatch):
    clean = _touch(tmp_path / "clip.mp4", b"clean source")
    se = _touch(tmp_path / "effect.ogg")
    stale_bgm = _touch(tmp_path / "clip_bgm.wav", b"stale")
    captured: list[list[str]] = []
    monkeypatch.setattr(audio_mix, "_run_command", _fake_media_runner(captured))
    monkeypatch.setattr(audio_mix, "_probe_has_audio", lambda *_args: False)
    monkeypatch.setattr(audio_mix, "_measure_decoded_peak_4x_dbfs", lambda *_args: -6.0)

    result = process_clip_audio(
        clean,
        duration_seconds=2,
        settings=AudioMixSettings(delivery_mode="both", se_cue_seconds=0.5),
        se_path=se,
    )

    assert clean.is_file()
    assert result.clean_video == clean.resolve()
    assert result.bgm_stem is None
    assert result.se_stem == (tmp_path / "clip_se.wav").resolve()
    assert result.manifest == (tmp_path / "clip_audio.json").resolve()
    assert result.mixed_video == (tmp_path / "clip_mixed.mp4").resolve()
    assert all(path.is_file() for path in result.deliverables)
    assert not stale_bgm.exists()
    mix_filter = captured[-1][captured[-1].index("-filter_complex") + 1]
    assert "[0:a:0]" not in mix_filter
    assert "amix=" not in mix_filter
    assert "alimiter=limit=0.95:level=false:latency=true" in mix_filter
    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["mixed_output_validation"] == {
        "method": "decoded AAC peak after 4x resampling",
        "decoded_peak_4x_dbfs": -6.0,
        "limit_dbfs": -1.0,
        "post_mix_attenuation_db": 0.0,
    }


def test_separate_mode_removes_stale_mixed_and_unselected_counterpart(
    tmp_path, monkeypatch
):
    clean = _touch(tmp_path / "clip.mp4")
    bgm = _touch(tmp_path / "music.ogg")
    stale_mixed = _touch(tmp_path / "clip_mixed.mp4", b"old mix")
    stale_se = _touch(tmp_path / "clip_se.wav", b"old stem")
    captured: list[list[str]] = []
    monkeypatch.setattr(audio_mix, "_run_command", _fake_media_runner(captured))

    result = process_clip_audio(
        clean,
        duration_seconds=2,
        settings=AudioMixSettings(delivery_mode="separate"),
        bgm_path=bgm,
    )

    assert result.bgm_stem.is_file()
    assert result.se_stem is None
    assert not stale_mixed.exists()
    assert not stale_se.exists()


def test_se_cue_at_or_after_clip_duration_creates_a_silent_stem_command(
    tmp_path, monkeypatch
):
    clean = _touch(tmp_path / "clip.mp4")
    se = _touch(tmp_path / "effect.ogg")
    captured: list[list[str]] = []
    monkeypatch.setattr(audio_mix, "_run_command", _fake_media_runner(captured))

    result = process_clip_audio(
        clean,
        duration_seconds=1.5,
        settings=AudioMixSettings(delivery_mode="separate", se_cue_seconds=4),
        se_path=se,
    )

    assert result.se_stem.is_file()
    assert len(captured) == 1
    assert "-f" in captured[0]
    assert "anullsrc=r=48000:cl=stereo:d=1.5" in captured[0]
    assert str(se.resolve()) not in captured[0]


def test_failed_mix_preserves_clean_input_and_leaves_no_partial_outputs(
    tmp_path, monkeypatch
):
    clean = _touch(tmp_path / "clip.mp4", b"clean source")
    bgm = _touch(tmp_path / "music.ogg")
    previous_mix = _touch(tmp_path / "clip_mixed.mp4", b"previous successful mix")
    calls = 0

    def fail_on_mix(command):
        nonlocal calls
        calls += 1
        if calls == 1:
            Path(command[-1]).write_bytes(b"temporary stem")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise RuntimeError("mix failed")

    monkeypatch.setattr(audio_mix, "_run_command", fail_on_mix)
    monkeypatch.setattr(audio_mix, "_probe_has_audio", lambda *_args: True)

    with pytest.raises(RuntimeError, match="mix failed"):
        process_clip_audio(
            clean,
            duration_seconds=2,
            settings=AudioMixSettings(delivery_mode="mixed"),
            bgm_path=bgm,
        )

    assert clean.read_bytes() == b"clean source"
    assert previous_mix.read_bytes() == b"previous successful mix"
    assert not (tmp_path / "clip_bgm.wav").exists()
    assert not list(tmp_path.glob(".*_audio_*"))


def test_batch_uses_highlight_duration_or_start_end_and_preserves_order(tmp_path):
    first = _touch(tmp_path / "one.mp4")
    second = _touch(tmp_path / "two.mp4")

    result = process_clip_batch(
        [first, second],
        [
            {"title": "one", "duration": 1.25},
            {"title": "two", "start_sec": 8.0, "end_sec": 10.5},
        ],
        settings=AudioMixSettings(delivery_mode="both"),
    )

    assert [clip.clean_video.name for clip in result.clips] == ["one.mp4", "two.mp4"]
    assert result.deliverables == (first.resolve(), second.resolve())

    with pytest.raises(ValueError, match="same length"):
        process_clip_batch(
            [first],
            [],
            settings=AudioMixSettings(),
        )


@pytest.mark.parametrize(
    ("mode", "select_audio"),
    (("separate", False), ("mixed", True), ("both", True)),
)
def test_batch_rejects_cross_clip_output_collision_before_changing_inputs(
    tmp_path, monkeypatch, mode, select_audio
):
    first = _touch(tmp_path / "clip.mp4", b"first input")
    second = _touch(tmp_path / "clip_mixed.mp4", b"second input")
    bgm = _touch(tmp_path / "music.ogg") if select_audio else None

    def unexpected(_command):
        raise AssertionError("batch collision must fail before running media tools")

    monkeypatch.setattr(audio_mix, "_run_command", unexpected)

    with pytest.raises(ValueError, match="overwrite an input"):
        process_clip_batch(
            [first, second],
            [{"duration": 1}, {"duration": 1}],
            settings=AudioMixSettings(delivery_mode=mode),
            bgm_path=bgm,
        )

    assert first.read_bytes() == b"first input"
    assert second.read_bytes() == b"second input"
    assert not list(tmp_path.glob(".clip_audio_*"))


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg and ffprobe are required")
def test_ffmpeg_e2e_stems_are_exact_48k_stereo_and_video_is_stream_copied(tmp_path):
    clean = tmp_path / "clip with audio.mp4"
    bgm = tmp_path / "short bgm.wav"
    se = tmp_path / "short se.wav"

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            str(clean),
        ],
        check=True,
        capture_output=True,
    )
    for destination, frequency, duration in ((bgm, 660, 0.2), (se, 880, 0.1)):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration={duration}",
                "-c:a",
                "pcm_s16le",
                str(destination),
            ],
            check=True,
            capture_output=True,
        )

    result = process_clip_audio(
        clean,
        duration_seconds=1,
        settings=AudioMixSettings(
            delivery_mode="both",
            bgm_gain_db=0,
            se_gain_db=0,
            se_cue_seconds=0.4,
        ),
        bgm_path=bgm,
        se_path=se,
    )

    for stem in (result.bgm_stem, result.se_stem):
        with wave.open(str(stem), "rb") as wav:
            assert wav.getframerate() == 48_000
            assert wav.getnchannels() == 2
            assert wav.getsampwidth() == 2
            assert wav.getnframes() == 48_000

    with wave.open(str(result.se_stem), "rb") as wav:
        before_cue = wav.readframes(round(0.39 * 48_000))
        wav.setpos(round(0.41 * 48_000))
        after_cue = wav.readframes(round(0.05 * 48_000))
    assert set(before_cue) <= {0}
    assert any(byte != 0 for byte in after_cue)

    def probe(path: Path, selector: str, entry: str) -> str:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                selector,
                "-show_entries",
                entry,
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    assert probe(clean, "v:0", "stream=codec_name") == probe(
        result.mixed_video, "v:0", "stream=codec_name"
    )
    assert probe(result.mixed_video, "a:0", "stream=codec_name") == "aac"
    assert float(probe(result.mixed_video, "a:0", "stream=duration")) == pytest.approx(
        1.0, abs=0.03
    )


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg and ffprobe are required")
def test_ffmpeg_e2e_post_aac_mix_does_not_clip_at_allowed_zero_db_gains(tmp_path):
    clean = tmp_path / "loud-clean.mp4"
    bgm = tmp_path / "loud-bgm.wav"
    se = tmp_path / "loud-se.wav"

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-af",
            "volume=7",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-q:v",
            "5",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(clean),
        ],
        check=True,
        capture_output=True,
    )
    for destination, frequency in ((bgm, 660), (se, 880)):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration=1",
                "-af",
                "volume=7",
                "-c:a",
                "pcm_s16le",
                str(destination),
            ],
            check=True,
            capture_output=True,
        )

    result = process_clip_audio(
        clean,
        duration_seconds=1,
        settings=AudioMixSettings(
            delivery_mode="both",
            bgm_gain_db=0,
            se_gain_db=0,
        ),
        bgm_path=bgm,
        se_path=se,
    )
    decoded = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(result.mixed_video),
            "-map",
            "0:a:0",
            "-f",
            "f32le",
            "-ac",
            "2",
            "-ar",
            "48000",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    peak = max(abs(sample[0]) for sample in struct.iter_unpack("<f", decoded))

    assert peak < 1.0
    assert result.decoded_peak_4x_dbfs <= audio_mix.AAC_TRUE_PEAK_LIMIT_DB
    assert result.post_mix_attenuation_db < 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg and ffprobe are required")
def test_ffmpeg_e2e_mixes_bgm_into_a_video_without_source_audio(tmp_path):
    clean = tmp_path / "silent-source.mp4"
    bgm = tmp_path / "music.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=25:d=0.8",
            "-c:v",
            "mpeg4",
            "-an",
            str(clean),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=550:sample_rate=48000:duration=0.2",
            "-c:a",
            "pcm_s16le",
            str(bgm),
        ],
        check=True,
        capture_output=True,
    )

    result = process_clip_audio(
        clean,
        duration_seconds=0.8,
        settings=AudioMixSettings(delivery_mode="mixed", bgm_gain_db=0),
        bgm_path=bgm,
    )

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,duration",
            "-of",
            "json",
            str(result.mixed_video),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "aac"
    assert float(stream["duration"]) == pytest.approx(0.8, abs=0.03)
    assert not clean.exists()
