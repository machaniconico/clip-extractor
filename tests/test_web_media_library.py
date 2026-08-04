from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("gradio")

import web_app
from user_media import UserMediaAsset


def _asset(kind: str, name: str, digest_char: str) -> UserMediaAsset:
    digest = digest_char * 64
    return UserMediaAsset(
        id=f"user:{kind}:{digest}",
        kind=kind,
        path=Path(f"D:/media/{kind}/{name}"),
        filename=name,
        relative_path=name,
        size=123,
        sha256=digest,
    )


def test_custom_audio_remains_selectable_without_cc0_pack(monkeypatch):
    bgm = _asset("bgm", "theme.mp3", "a")
    se = _asset("se", "click.wav", "b")
    vfx = _asset("vfx", "spark.png", "c")
    assets = {"bgm": (bgm,), "se": (se,), "vfx": (vfx,)}

    monkeypatch.setattr(web_app, "list_catalog_assets", lambda: ())
    monkeypatch.setattr(
        web_app,
        "get_audio_pack_status",
        lambda: SimpleNamespace(ready=False, state="missing", message="missing"),
    )
    monkeypatch.setattr(
        web_app,
        "_user_media_for_ui",
        lambda _folder, kind: (assets[kind], ""),
    )

    status, bgm_update, se_update, vfx_update = (
        web_app.refresh_media_library_ui(
            bgm.id,
            se.id,
            vfx.id,
            "D:/media/bgm",
            "D:/media/se",
            "D:/media/vfx",
            False,
        )
    )

    assert "BGM 1件 / SE 1件 / VFX 1件" in status
    assert bgm_update["interactive"] is True
    assert bgm_update["value"] == bgm.id
    assert se_update["interactive"] is True
    assert se_update["value"] == se.id
    assert vfx_update["interactive"] is True
    assert vfx_update["value"] == vfx.id


def test_refresh_clears_deleted_selections_and_auto_locks_manual_vfx(monkeypatch):
    monkeypatch.setattr(web_app, "list_catalog_assets", lambda: ())
    monkeypatch.setattr(
        web_app,
        "get_audio_pack_status",
        lambda: SimpleNamespace(ready=False, state="missing", message="missing"),
    )
    monkeypatch.setattr(
        web_app,
        "_user_media_for_ui",
        lambda _folder, _kind: ((), ""),
    )

    _status, bgm_update, se_update, vfx_update = (
        web_app.refresh_media_library_ui(
            "user:bgm:" + ("a" * 64),
            "user:se:" + ("b" * 64),
            "user:vfx:" + ("c" * 64),
            "D:/media/bgm",
            "D:/media/se",
            "D:/media/vfx",
            True,
        )
    )

    assert bgm_update["value"] == ""
    assert se_update["value"] == ""
    assert vfx_update["value"] == ""
    assert vfx_update["interactive"] is False


def test_refresh_does_not_offer_uninstalled_builtin_audio(monkeypatch):
    builtin = SimpleNamespace(
        id="bgm-not-installed",
        kind="bgm",
        label="Missing pack track",
        creator="Creator",
    )
    monkeypatch.setattr(web_app, "list_catalog_assets", lambda: (builtin,))
    monkeypatch.setattr(
        web_app,
        "get_audio_pack_status",
        lambda: SimpleNamespace(ready=False, state="missing", message="missing"),
    )
    monkeypatch.setattr(
        web_app,
        "_user_media_for_ui",
        lambda _folder, _kind: ((), ""),
    )

    status, bgm_update, _se_update, _vfx_update = (
        web_app.refresh_media_library_ui("bgm-not-installed")
    )

    assert bgm_update["value"] == ""
    assert "bgm-not-installed" not in {
        value for _label, value in bgm_update["choices"]
    }
    assert "保存済みBGM選択は現在利用できない" in status
