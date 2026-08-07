from __future__ import annotations

import json
from pathlib import Path
import subprocess

import audio_mix
from audio_mix import AudioMixSettings, SeCue, process_clip_audio


def _touch(path: Path, content: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _fake_runner(captured: list[list[str]]):
    def run(command):
        cmd = [str(part) for part in command]
        captured.append(cmd)
        output = Path(cmd[-1])
        output.write_bytes(b"generated")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return run


def test_multiple_se_cues_are_delayed_and_recorded(tmp_path, monkeypatch):
    clean = _touch(tmp_path / "clip.mp4")
    first = _touch(tmp_path / "バーン.mp3")
    second = _touch(tmp_path / "悲鳴.mp3")
    captured: list[list[str]] = []
    monkeypatch.setattr(audio_mix, "_run_command", _fake_runner(captured))
    monkeypatch.setattr(audio_mix, "_probe_has_audio", lambda *_args: False)
    monkeypatch.setattr(audio_mix, "_measure_decoded_peak_4x_dbfs", lambda *_args: -6.0)

    result = process_clip_audio(
        clean,
        duration_seconds=4.0,
        settings=AudioMixSettings(delivery_mode="both", se_gain_db=-6),
        se_cues=(
            SeCue(first, cue_seconds=0.25, event_id="evt-1", category="impact"),
            SeCue(second, cue_seconds=1.5, event_id="evt-2", category="surprise"),
        ),
    )

    se_command = captured[0]
    se_filter = se_command[se_command.index("-filter_complex") + 1]
    assert se_filter.count("adelay=delays=") == 2
    assert "adelay=delays=12000S:all=1" in se_filter
    assert "adelay=delays=72000S:all=1" in se_filter
    assert "amix=inputs=2" in se_filter

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    cues = manifest["audio"]["se"]["cues"]
    assert [cue["event_id"] for cue in cues] == ["evt-1", "evt-2"]
    assert [cue["cue_seconds"] for cue in cues] == [0.25, 1.5]
