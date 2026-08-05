from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import clipper
from user_media import (
    UserMediaAsset,
    UserMediaError,
    scan_user_media,
    validate_user_media,
)
from video_effects import (
    EFFECTS_MANIFEST_NAME,
    ClipEffectPlan,
    EffectPreset,
    VideoEffectError,
    VfxAnchor,
    VfxOptions,
)


def _asset(tmp_path: Path, suffix: str) -> UserMediaAsset:
    path = tmp_path / f"effect{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"vfx")
    return UserMediaAsset(
        id=f"user:vfx:{'d' * 64}",
        kind="vfx",
        path=path.resolve(),
        filename=path.name,
        relative_path=path.name,
        size=3,
        sha256="d" * 64,
    )


def _capture_run(monkeypatch):
    captured = []
    monkeypatch.setattr(clipper, "validate_user_media", lambda asset: asset)

    def fake_run(command, **kwargs):
        captured.append(([str(part) for part in command], kwargs))
        Path(command[-1]).write_bytes(b"video")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(clipper.subprocess, "run", fake_run)
    return captured


def test_extract_clip_without_effects_keeps_legacy_command(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    clipper.extract_clip(
        tmp_path / "source.mp4",
        tmp_path / "out.mp4",
        2,
        5,
        shorts=True,
    )

    command = captured[0][0]
    assert "-vf" in command
    assert "-filter_complex" not in command
    assert "-map" not in command
    assert command.count("-i") == 1


def test_extract_clip_rejects_invalid_effect_plan_before_ffmpeg(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)

    with pytest.raises(TypeError, match="ClipEffectPlan"):
        clipper.extract_clip(
            tmp_path / "source.mp4",
            tmp_path / "out.mp4",
            0,
            1,
            effect_plan=object(),
        )

    assert captured == []


def test_vfx_failure_preserves_existing_output_and_removes_partial(
    monkeypatch, tmp_path
):
    output = tmp_path / "out.mp4"
    output.write_bytes(b"known-good")

    def fail_after_partial(command, **_kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(clipper.subprocess, "run", fail_after_partial)

    with pytest.raises(subprocess.CalledProcessError):
        clipper.extract_clip(
            tmp_path / "source.mp4",
            output,
            0,
            1,
            effect_plan=ClipEffectPlan(
                effect_preset=EffectPreset.FLASH,
                cue_seconds=0.25,
                duration_seconds=0.25,
            ),
        )

    assert output.read_bytes() == b"known-good"
    assert list(tmp_path.glob("*.vfx-*")) == []


def test_png_vfx_uses_one_pass_graph_and_keeps_captions_above_overlay(
    monkeypatch, tmp_path
):
    captured = _capture_run(monkeypatch)
    asset = _asset(tmp_path, ".png")
    plan = ClipEffectPlan(
        asset=asset,
        effect_preset=EffectPreset.PUNCH,
        cue_seconds=1.25,
        duration_seconds=1.0,
        anchor=VfxAnchor.BOTTOM_RIGHT,
        scale_percent=75,
        opacity_percent=60,
    )
    clipper.extract_clip(
        tmp_path / "source.mp4",
        tmp_path / "out.mp4",
        10,
        15,
        shorts=True,
        srt_path=tmp_path / "caption.srt",
        font_config=type(
            "Font",
            (),
            {
                "font_name": "Noto Sans JP",
                "font_size": 96,
                "font_color": "#FFFFFF",
                "outline_color": "#000000",
                "outline_width": 5,
                "position": "bottom",
                "margin_bottom": 120,
            },
        )(),
        title="見どころ",
        effect_plan=plan,
    )

    command = captured[0][0]
    assert command[command.index("-loop") + 1] == "1"
    assert command[command.index("-framerate") + 1] == "30"
    assert command.count("-i") == 2
    assert command[command.index("-map") + 1] == "[vout]"
    second_map = command.index("-map", command.index("-map") + 1)
    assert command[second_map + 1] == "0:a:0?"
    graph = command[command.index("-filter_complex") + 1]
    assert "scale=iw*0.75:ih*0.75" in graph
    assert "colorchannelmixer=aa=0.6" in graph
    assert "between(t\\,1.25\\,2.25)" in graph
    assert "overlay=x=W-w-W*0.04:y=H-h-H*0.06" in graph
    assert graph.index("[vfxout]") < graph.index("drawtext=")
    assert graph.index("drawtext=") < graph.index("subtitles=")
    assert "[vout]" in graph


def test_webm_vfx_loops_without_mapping_its_audio(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    plan = ClipEffectPlan(
        asset=_asset(tmp_path, ".webm"),
        effect_preset=EffectPreset.FLASH,
        cue_seconds=0.5,
        duration_seconds=0.8,
    )

    clipper.extract_clip(
        tmp_path / "source.mp4",
        tmp_path / "out.mp4",
        0,
        3,
        effect_plan=plan,
    )

    command = captured[0][0]
    assert command[command.index("-stream_loop") + 1] == "-1"
    assert "-loop" not in command
    assert command.count("-map") == 2
    assert "1:a" not in command
    graph = command[command.index("-filter_complex") + 1]
    assert "drawbox=" in graph
    assert "repeatlast=0" in graph
    assert "eof_action=pass" in graph


def test_transparent_vp9_webm_selects_alpha_capable_decoder(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    asset = replace(
        _asset(tmp_path, ".webm"),
        video_codec="vp9",
        video_has_alpha=True,
    )

    clipper.extract_clip(
        tmp_path / "source.mp4",
        tmp_path / "out.mp4",
        0,
        1,
        effect_plan=ClipEffectPlan(
            asset=asset,
            cue_seconds=0,
            duration_seconds=0.5,
        ),
    )

    command = captured[0][0]
    input_indexes = [index for index, value in enumerate(command) if value == "-i"]
    decoder_index = command.index("-c:v")
    assert command[decoder_index + 1] == "libvpx-vp9"
    assert input_indexes[0] < decoder_index < input_indexes[1]


def test_vfx_asset_is_revalidated_immediately_before_ffmpeg(monkeypatch, tmp_path):
    asset = _asset(tmp_path, ".png")
    monkeypatch.setattr(
        clipper,
        "validate_user_media",
        lambda _asset: (_ for _ in ()).throw(UserMediaError("replaced")),
    )
    monkeypatch.setattr(
        clipper.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("FFmpeg must not run"),
    )

    with pytest.raises(UserMediaError, match="replaced"):
        clipper.extract_clip(
            tmp_path / "source.mp4",
            tmp_path / "out.mp4",
            0,
            1,
            effect_plan=ClipEffectPlan(
                asset=asset,
                cue_seconds=0,
                duration_seconds=0.5,
            ),
        )


def test_extract_clips_writes_path_free_effect_manifest(monkeypatch, tmp_path):
    asset = _asset(tmp_path / "private-materials", ".png")
    output_dir = tmp_path / "clips"
    monkeypatch.setattr(clipper, "prepare_vfx_assets", lambda _options: (asset,))

    def fake_extract(_source, output, *_args, **_kwargs):
        output.write_bytes(b"video")
        return output

    monkeypatch.setattr(clipper, "extract_clip", fake_extract)
    options = VfxOptions(
        vfx_asset_id=asset.id,
        vfx_user_folder=str(asset.path.parent),
        effect_preset="flash",
        cue_seconds=1.25,
        duration_seconds=0.75,
        anchor="top-right",
        scale_percent=70,
        opacity_percent=80,
    )

    outputs = clipper.extract_clips(
        tmp_path / "source.mp4",
        [
            {
                "title": "clip",
                "start_sec": 10.0,
                "end_sec": 14.0,
            }
        ],
        output_dir,
        vfx_options=options,
    )

    manifest_path = output_dir / EFFECTS_MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["automatic_selection_and_placement"] is False
    assert payload["clips"][0]["output_file"] == outputs[0].name
    assert payload["clips"][0]["source_clean_video"] == outputs[0].name
    assert payload["clips"][0]["effect"]["cue_seconds"] == 1.25
    assert payload["clips"][0]["vfx_asset"]["source_type"] == "user_provided"
    assert payload["clips"][0]["vfx_asset"]["source_sha256"] == asset.sha256
    assert str(tmp_path.resolve()) not in serialized


def test_effect_manifest_collision_fails_before_render(monkeypatch, tmp_path):
    output_dir = tmp_path / "clips"
    output_dir.mkdir()
    manifest = output_dir / EFFECTS_MANIFEST_NAME
    manifest.write_text("USER-NOTES", encoding="utf-8")
    monkeypatch.setattr(
        clipper,
        "extract_clip",
        lambda *_args, **_kwargs: pytest.fail(
            "manifest collision must be detected before FFmpeg"
        ),
    )

    with pytest.raises(VideoEffectError, match="衝突"):
        clipper.extract_clips(
            tmp_path / "source.mp4",
            [{"title": "clip", "start_sec": 0.0, "end_sec": 1.0}],
            output_dir,
            vfx_options=VfxOptions(effect_preset="flash"),
        )

    assert manifest.read_text(encoding="utf-8") == "USER-NOTES"


def test_effect_batch_failure_rolls_back_all_clips_and_keeps_manifest(
    monkeypatch, tmp_path
):
    output_dir = tmp_path / "clips"
    output_dir.mkdir()
    highlights = [
        {"title": "first", "start_sec": 0.0, "end_sec": 1.0},
        {"title": "second", "start_sec": 1.0, "end_sec": 2.0},
    ]
    output_paths = [
        output_dir
        / clipper._build_clip_filename(
            clipper.format_time_range(item["start_sec"], item["end_sec"]),
            item["title"],
            False,
        )
        for item in highlights
    ]
    output_paths[0].write_bytes(b"old-first")
    output_paths[1].write_bytes(b"old-second")
    manifest = output_dir / EFFECTS_MANIFEST_NAME
    old_manifest = {
        "schema_version": 1,
        "generator": "clip-extractor",
        "clips": [],
    }
    manifest.write_text(json.dumps(old_manifest), encoding="utf-8")
    calls = 0

    def fail_second(_source, output, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        output.write_bytes(f"new-{calls}".encode())
        if calls == 2:
            raise subprocess.CalledProcessError(1, ["ffmpeg"])
        return output

    monkeypatch.setattr(clipper, "extract_clip", fail_second)

    with pytest.raises(subprocess.CalledProcessError):
        clipper.extract_clips(
            tmp_path / "source.mp4",
            highlights,
            output_dir,
            vfx_options=VfxOptions(effect_preset="flash"),
        )

    assert output_paths[0].read_bytes() == b"old-first"
    assert output_paths[1].read_bytes() == b"old-second"
    assert json.loads(manifest.read_text(encoding="utf-8")) == old_manifest
    assert not list(output_dir.glob(".vfx-batch-*"))


def test_removing_vfx_rolls_back_previous_effect_batch_on_failure(
    monkeypatch, tmp_path
):
    output_dir = tmp_path / "clips"
    output_dir.mkdir()
    highlights = [
        {"title": "first", "start_sec": 0.0, "end_sec": 1.0},
        {"title": "second", "start_sec": 1.0, "end_sec": 2.0},
    ]
    output_paths = [
        output_dir
        / clipper._build_clip_filename(
            clipper.format_time_range(item["start_sec"], item["end_sec"]),
            item["title"],
            False,
        )
        for item in highlights
    ]
    output_paths[0].write_bytes(b"old-vfx-first")
    output_paths[1].write_bytes(b"old-vfx-second")
    manifest = output_dir / EFFECTS_MANIFEST_NAME
    old_manifest = {
        "schema_version": 1,
        "generator": "clip-extractor",
        "clips": [
            {"output_file": path.name, "enabled": True}
            for path in output_paths
        ],
    }
    manifest.write_text(json.dumps(old_manifest), encoding="utf-8")
    calls = 0

    def fail_second(_source, output, *_args, **_kwargs):
        nonlocal calls
        calls += 1
        output.write_bytes(f"new-{calls}".encode())
        if calls == 2:
            raise subprocess.CalledProcessError(1, ["ffmpeg"])
        return output

    monkeypatch.setattr(clipper, "extract_clip", fail_second)

    with pytest.raises(subprocess.CalledProcessError):
        clipper.extract_clips(
            tmp_path / "source.mp4",
            highlights,
            output_dir,
            vfx_options=VfxOptions(),
        )

    assert output_paths[0].read_bytes() == b"old-vfx-first"
    assert output_paths[1].read_bytes() == b"old-vfx-second"
    assert json.loads(manifest.read_text(encoding="utf-8")) == old_manifest
    assert not list(output_dir.glob(".vfx-batch-*"))


def test_non_target_group_does_not_scan_vfx_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(
        clipper,
        "prepare_vfx_assets",
        lambda _options: pytest.fail("non-target group must not scan VFX assets"),
    )

    def fake_extract(_source, output, *_args, **_kwargs):
        output.write_bytes(b"video")
        return output

    monkeypatch.setattr(clipper, "extract_clip", fake_extract)

    paths = clipper.extract_clips(
        tmp_path / "source.mp4",
        [{"title": "clip", "start_sec": 0.0, "end_sec": 1.0}],
        tmp_path / "clips",
        shorts=False,
        vfx_options=VfxOptions(
            automatic=True,
            vfx_user_folder=str(tmp_path / "missing"),
            target="shorts",
        ),
    )

    assert len(paths) == 1
    assert not (tmp_path / "clips" / EFFECTS_MANIFEST_NAME).exists()


@pytest.mark.parametrize(
    ("preset", "fragment"),
    [
        (EffectPreset.FADE, "fade=t=in"),
        (EffectPreset.PUNCH, "[punchbase][punchzoom]overlay="),
        (EffectPreset.FLASH, "drawbox="),
    ],
)
def test_safe_effect_presets_generate_fixed_filters(
    monkeypatch, tmp_path, preset, fragment
):
    captured = _capture_run(monkeypatch)
    clipper.extract_clip(
        tmp_path / "source.mp4",
        tmp_path / "out.mp4",
        0,
        4,
        effect_plan=ClipEffectPlan(
            effect_preset=preset,
            cue_seconds=1,
            duration_seconds=0.5,
        ),
    )

    command = captured[0][0]
    graph = command[command.index("-filter_complex") + 1]
    assert fragment in graph
    assert "[vout]" in graph


def test_fade_runs_after_vfx_title_and_subtitles(monkeypatch, tmp_path):
    captured = _capture_run(monkeypatch)
    clipper.extract_clip(
        tmp_path / "source.mp4",
        tmp_path / "out.mp4",
        0,
        4,
        shorts=True,
        srt_path=tmp_path / "caption.srt",
        font_config=type(
            "Font",
            (),
            {
                "font_name": "Noto Sans JP",
                "font_size": 96,
                "font_color": "#FFFFFF",
                "outline_color": "#000000",
                "outline_width": 5,
                "position": "bottom",
                "margin_bottom": 120,
            },
        )(),
        title="見どころ",
        effect_plan=ClipEffectPlan(
            asset=_asset(tmp_path, ".png"),
            effect_preset=EffectPreset.FADE,
            cue_seconds=0,
            duration_seconds=1,
        ),
    )

    command = captured[0][0]
    graph = command[command.index("-filter_complex") + 1]
    fade_index = graph.rindex("fade=t=in")
    assert graph.index("[vfxout]") < fade_index
    assert graph.index("drawtext=") < fade_index
    assert graph.index("subtitles=") < fade_index


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_png_vfx_real_ffmpeg_changes_only_the_cue_window(tmp_path):
    np = pytest.importorskip("numpy")
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "source.mp4"
    overlay = tmp_path / "overlay.png"
    output = tmp_path / "output.mp4"
    image_module.new("RGBA", (64, 64), (255, 0, 0, 255)).save(overlay)
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
            "color=c=blue:s=320x180:r=24:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        capture_output=True,
        check=True,
    )
    asset = validate_user_media(scan_user_media(tmp_path, "vfx")[0])

    clipper.extract_clip(
        source,
        output,
        0,
        2,
        effect_plan=ClipEffectPlan(
            asset=asset,
            cue_seconds=0.5,
            duration_seconds=0.8,
            anchor=VfxAnchor.CENTER,
            scale_percent=100,
            opacity_percent=100,
        ),
    )

    def frame_at(timestamp: float):
        raw = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(timestamp),
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
        ).stdout
        return np.frombuffer(raw, dtype=np.uint8).reshape((180, 320, 3))

    before = frame_at(0.2)[70:110, 140:180].mean(axis=(0, 1))
    during = frame_at(0.9)[70:110, 140:180].mean(axis=(0, 1))
    after = frame_at(1.6)[70:110, 140:180].mean(axis=(0, 1))
    assert during[0] > before[0] + 150
    assert during[0] > after[0] + 150
    assert abs(float(before[2]) - float(after[2])) < 15


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_transparent_vp9_webm_keeps_alpha_with_real_ffmpeg(tmp_path):
    np = pytest.importorskip("numpy")
    image_module = pytest.importorskip("PIL.Image")
    source = tmp_path / "source.mp4"
    frame = tmp_path / "overlay-frame.png"
    overlay = tmp_path / "overlay.webm"
    output = tmp_path / "output.mp4"
    rgba = image_module.new("RGBA", (64, 64), (0, 0, 0, 0))
    rgba.paste((255, 0, 0, 255), (16, 16, 48, 48))
    rgba.save(frame)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=24:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(source),
        ],
        capture_output=True,
        check=True,
    )
    encoded = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-framerate", "24", "-i", str(frame),
            "-t", "0.5", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-auto-alt-ref", "0", str(overlay),
        ],
        capture_output=True,
        check=False,
    )
    if encoded.returncode != 0:
        pytest.skip("ffmpeg does not provide the libvpx-vp9 encoder")

    asset = validate_user_media(
        next(
            candidate
            for candidate in scan_user_media(tmp_path, "vfx")
            if candidate.path.suffix.lower() == ".webm"
        )
    )
    assert asset.video_codec == "vp9"
    assert asset.video_has_alpha is True
    clipper.extract_clip(
        source,
        output,
        0,
        2,
        effect_plan=ClipEffectPlan(
            asset=asset,
            cue_seconds=0.5,
            duration_seconds=0.8,
            anchor=VfxAnchor.CENTER,
        ),
    )
    raw = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-ss", "0.9", "-i", str(output), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    rendered = np.frombuffer(raw, dtype=np.uint8).reshape((180, 320, 3))
    transparent_corner = rendered[62:70, 132:140].mean(axis=(0, 1))
    red_center = rendered[84:96, 154:166].mean(axis=(0, 1))
    assert transparent_corner[2] > 150
    assert transparent_corner[0] < 80
    assert red_center[0] > 150
    assert red_center[2] < 100
