from __future__ import annotations

from pathlib import Path

import pytest

import video_effects
from user_media import UserMediaAsset, UserMediaError
from video_effects import (
    EffectPreset,
    VfxAnchor,
    VfxOptions,
    VfxTarget,
    prepare_vfx_assets,
    resolve_clip_effect_plan,
)


def _vfx_asset(tmp_path: Path, name: str, digest: str) -> UserMediaAsset:
    path = tmp_path / name
    path.write_bytes(name.encode("utf-8"))
    return UserMediaAsset(
        id=f"user:vfx:{digest}",
        kind="vfx",
        path=path.resolve(),
        filename=name,
        relative_path=name,
        size=path.stat().st_size,
        sha256=digest,
    )


def test_vfx_options_normalize_and_validate_values():
    options = VfxOptions(
        effect_preset=" FLASH ",
        cue_seconds="1.5",
        duration_seconds="0.75",
        anchor="bottom-right",
        scale_percent="80",
        opacity_percent="65",
        target="shorts",
    )

    assert options.effect_preset is EffectPreset.FLASH
    assert options.anchor is VfxAnchor.BOTTOM_RIGHT
    assert options.target is VfxTarget.SHORTS
    assert options.cue_seconds == 1.5
    assert options.duration_seconds == 0.75
    assert options.scale_percent == 80
    assert options.opacity_percent == 65

    with pytest.raises(ValueError, match="duration_seconds"):
        VfxOptions(duration_seconds=0)
    with pytest.raises(ValueError, match="scale_percent"):
        VfxOptions(scale_percent=0)
    with pytest.raises(ValueError, match="opacity_percent"):
        VfxOptions(opacity_percent=101)


def test_prepare_manual_vfx_resolves_and_validates_selected_asset(
    tmp_path, monkeypatch
):
    asset = _vfx_asset(tmp_path, "spark.png", "a" * 64)
    calls = []
    monkeypatch.setattr(
        video_effects,
        "resolve_user_media_asset",
        lambda folder, asset_id, kind: asset,
    )
    monkeypatch.setattr(
        video_effects,
        "validate_user_media",
        lambda selected: calls.append(selected),
    )

    assets = prepare_vfx_assets(
        VfxOptions(vfx_asset_id=asset.id, vfx_user_folder=str(tmp_path))
    )

    assert assets == (asset,)
    assert calls == [asset]


def test_prepare_vfx_requires_folder_for_user_reference():
    with pytest.raises(video_effects.VideoEffectError, match="参照フォルダ"):
        prepare_vfx_assets(VfxOptions(vfx_asset_id=f"user:vfx:{'b' * 64}"))


def test_prepare_automatic_vfx_skips_broken_candidates(tmp_path, monkeypatch):
    broken = _vfx_asset(tmp_path, "broken.webm", "b" * 64)
    valid = _vfx_asset(tmp_path, "valid.png", "c" * 64)
    monkeypatch.setattr(
        video_effects,
        "scan_optional_user_media",
        lambda _folder, _kind: (broken, valid),
    )

    def validate(asset):
        if asset is broken:
            raise UserMediaError("broken stream")

    monkeypatch.setattr(video_effects, "validate_user_media", validate)

    assets = prepare_vfx_assets(
        VfxOptions(automatic=True, vfx_user_folder=str(tmp_path))
    )

    assert assets == (valid,)


def test_automatic_plan_is_stable_when_candidate_order_changes(tmp_path):
    first = _vfx_asset(tmp_path, "first.png", "1" * 64)
    second = _vfx_asset(tmp_path, "second.webm", "2" * 64)
    options = VfxOptions(
        automatic=True,
        duration_seconds=1.25,
        scale_percent=70,
        opacity_percent=80,
    )
    highlight = {"title": "大逆転", "start_sec": 12.5, "end_sec": 35.0}

    plan_a = resolve_clip_effect_plan(
        options,
        highlight,
        22.5,
        (first, second),
        shorts=False,
    )
    plan_b = resolve_clip_effect_plan(
        options,
        highlight,
        22.5,
        (second, first),
        shorts=False,
    )

    assert plan_a == plan_b
    assert plan_a.enabled
    assert plan_a.asset in {first, second}
    assert plan_a.effect_preset in {
        EffectPreset.NONE,
        EffectPreset.FADE,
        EffectPreset.PUNCH,
        EffectPreset.FLASH,
    }
    assert 0 <= plan_a.cue_seconds <= 22.5 - plan_a.duration_seconds
    assert plan_a.anchor in set(VfxAnchor)


def test_manual_plan_outside_clip_is_disabled_instead_of_moved(tmp_path):
    asset = _vfx_asset(tmp_path, "spark.png", "3" * 64)
    options = VfxOptions(
        vfx_asset_id=asset.id,
        effect_preset="punch",
        cue_seconds=9,
        duration_seconds=2,
        anchor="top-left",
        scale_percent=60,
        opacity_percent=55,
    )

    plan = resolve_clip_effect_plan(
        options,
        {"title": "short", "start_sec": 0, "end_sec": 1},
        1,
        (asset,),
        shorts=False,
    )

    assert plan.asset is None
    assert plan.effect_preset is EffectPreset.NONE
    assert plan.enabled is False
    assert plan.cue_seconds == 9
    assert plan.duration_seconds == 0
    assert plan.anchor is VfxAnchor.TOP_LEFT
    assert plan.scale_percent == 60
    assert plan.opacity_percent == 55


def test_manual_plan_preserves_relative_cue_and_trims_at_clip_end(tmp_path):
    asset = _vfx_asset(tmp_path, "spark.png", "4" * 64)
    options = VfxOptions(
        vfx_asset_id=asset.id,
        effect_preset="flash",
        cue_seconds=9,
        duration_seconds=2,
    )

    plan = resolve_clip_effect_plan(
        options,
        {"title": "tail", "start_sec": 0, "end_sec": 10},
        10,
        (asset,),
        shorts=False,
    )

    assert plan.asset == asset
    assert plan.effect_preset is EffectPreset.FLASH
    assert plan.cue_seconds == 9
    assert plan.duration_seconds == 1


@pytest.mark.parametrize(
    ("target", "shorts", "enabled"),
    [
        ("both", False, True),
        ("both", True, True),
        ("clips", False, True),
        ("clips", True, False),
        ("shorts", False, False),
        ("shorts", True, True),
    ],
)
def test_vfx_target_controls_landscape_and_shorts(target, shorts, enabled):
    plan = resolve_clip_effect_plan(
        VfxOptions(effect_preset="fade", target=target),
        {"title": "clip", "start_sec": 0, "end_sec": 5},
        5,
        (),
        shorts=shorts,
    )
    assert plan.enabled is enabled


def test_disabled_options_produce_no_effect_plan():
    plan = resolve_clip_effect_plan(
        VfxOptions(),
        {"title": "clip", "start_sec": 0, "end_sec": 5},
        5,
        (),
        shorts=False,
    )
    assert plan.enabled is False
    assert plan.asset is None
    assert plan.effect_preset is EffectPreset.NONE


def test_automatic_mode_ignores_stale_manual_asset_without_folder(monkeypatch):
    monkeypatch.setattr(
        video_effects,
        "scan_optional_user_media",
        lambda folder, kind: ()
        if folder == "" and kind == "vfx"
        else pytest.fail("automatic mode must not scan an unrelated folder"),
    )

    assets = prepare_vfx_assets(
        VfxOptions(
            automatic=True,
            vfx_asset_id="user:vfx:" + ("a" * 64),
            vfx_user_folder="",
        )
    )

    assert assets == ()
