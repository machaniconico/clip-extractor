from __future__ import annotations

from pathlib import Path

import pytest

import user_media


def test_scan_user_media_builds_stable_content_ids_and_deduplicates(tmp_path):
    folder = tmp_path / "audio"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    first = folder / "Theme.MP3"
    duplicate = nested / "renamed.mp3"
    first.write_bytes(b"same-audio")
    duplicate.write_bytes(b"same-audio")
    (folder / "notes.txt").write_text("ignore", encoding="utf-8")

    assets = user_media.scan_user_media(folder, "bgm")

    assert len(assets) == 1
    asset = assets[0]
    assert asset.id.startswith("user:bgm:")
    assert asset.kind == "bgm"
    assert asset.path == first.resolve()
    assert asset.filename == "Theme.MP3"
    assert asset.sha256 == asset.id.rsplit(":", 1)[-1]

    first.rename(folder / "renamed-again.mp3")
    rescanned = user_media.scan_user_media(folder, "bgm")
    assert rescanned[0].id == asset.id


@pytest.mark.parametrize(
    ("kind", "accepted", "ignored"),
    [
        ("bgm", "music.flac", "overlay.png"),
        ("se", "hit.wav", "overlay.webm"),
        ("vfx", "spark.png", "music.ogg"),
        ("vfx", "spark.webm", "music.m4a"),
    ],
)
def test_scan_user_media_filters_extensions_by_kind(
    tmp_path, kind, accepted, ignored
):
    (tmp_path / accepted).write_bytes(b"accepted")
    (tmp_path / ignored).write_bytes(b"ignored")

    assets = user_media.scan_user_media(tmp_path, kind)

    assert [asset.filename for asset in assets] == [accepted]


def test_scan_user_media_skips_symlinks_without_following_them(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    outside_file = outside / "outside.mp3"
    outside_file.write_bytes(b"outside")
    linked_file = root / "linked.mp3"
    linked_file.symlink_to(outside_file)
    linked_dir = root / "linked-dir"
    linked_dir.symlink_to(outside, target_is_directory=True)
    (root / "inside.mp3").write_bytes(b"inside")

    assets = user_media.scan_user_media(root, "bgm")

    assert [asset.filename for asset in assets] == ["inside.mp3"]


def test_scan_user_media_rejects_missing_folder_and_too_many_assets(
    tmp_path, monkeypatch
):
    with pytest.raises(user_media.UserMediaError, match="フォルダ"):
        user_media.scan_user_media(tmp_path / "missing", "se")

    monkeypatch.setattr(user_media, "MAX_ASSET_COUNT", 1)
    (tmp_path / "one.wav").write_bytes(b"one")
    (tmp_path / "two.wav").write_bytes(b"two")
    with pytest.raises(user_media.UserMediaError, match="上限"):
        user_media.scan_user_media(tmp_path, "se")


def test_scan_user_media_wraps_file_disappearance_as_user_media_error(
    tmp_path, monkeypatch
):
    source = tmp_path / "vanishing.wav"
    source.write_bytes(b"audio")

    def remove_after_listing(_root, _kind):
        source.unlink()
        return [source]

    monkeypatch.setattr(user_media, "_regular_candidates", remove_after_listing)

    with pytest.raises(user_media.UserMediaError, match="走査中"):
        user_media.scan_user_media(tmp_path, "se")


def test_scan_user_media_wraps_hash_io_error_as_user_media_error(
    tmp_path, monkeypatch
):
    (tmp_path / "unstable.wav").write_bytes(b"audio")
    monkeypatch.setattr(
        user_media,
        "_hash_regular_file",
        lambda _path: (_ for _ in ()).throw(OSError("device disappeared")),
    )

    with pytest.raises(user_media.UserMediaError, match="走査中"):
        user_media.scan_user_media(tmp_path, "se")


def test_resolve_user_media_asset_rechecks_hash_and_kind(tmp_path):
    source = tmp_path / "hit.wav"
    source.write_bytes(b"hit")
    asset = user_media.scan_user_media(tmp_path, "se")[0]

    resolved = user_media.resolve_user_media_asset(tmp_path, asset.id, "se")
    assert resolved == asset

    with pytest.raises(user_media.UserMediaError, match="種類"):
        user_media.resolve_user_media_asset(tmp_path, asset.id, "bgm")

    source.write_bytes(b"changed")
    with pytest.raises(user_media.UserMediaError, match="見つかりません"):
        user_media.resolve_user_media_asset(tmp_path, asset.id, "se")


def test_probe_user_media_requires_the_expected_stream(tmp_path, monkeypatch):
    audio = tmp_path / "music.mp3"
    audio.write_bytes(b"audio")
    asset = user_media.scan_user_media(tmp_path, "bgm")[0]

    monkeypatch.setattr(
        user_media,
        "_probe_stream_types",
        lambda _path, _ffprobe: {"audio"},
    )
    user_media.validate_user_media(asset)

    monkeypatch.setattr(
        user_media,
        "_probe_stream_types",
        lambda _path, _ffprobe: {"video"},
    )
    with pytest.raises(user_media.UserMediaError, match="音声ストリーム"):
        user_media.validate_user_media(asset)


def test_validate_vfx_preserves_codec_and_alpha_probe(tmp_path, monkeypatch):
    source = tmp_path / "effect.webm"
    source.write_bytes(b"video")
    asset = user_media.scan_user_media(tmp_path, "vfx")[0]
    monkeypatch.setattr(
        user_media,
        "_probe_stream_types",
        lambda _path, _ffprobe: {"video"},
    )
    monkeypatch.setattr(
        user_media,
        "_probe_video_details",
        lambda _path, _ffprobe: ("vp9", True),
    )

    validated = user_media.validate_user_media(asset)

    assert validated.video_codec == "vp9"
    assert validated.video_has_alpha is True


def test_validate_rejects_file_replaced_during_media_probe(tmp_path, monkeypatch):
    source = tmp_path / "music.mp3"
    source.write_bytes(b"original")
    asset = user_media.scan_user_media(tmp_path, "bgm")[0]

    def mutate_during_probe(_path, _ffprobe):
        source.write_bytes(b"replacement")
        return {"audio"}

    monkeypatch.setattr(user_media, "_probe_stream_types", mutate_during_probe)

    with pytest.raises(user_media.UserMediaError, match="メディア確認中に変更"):
        user_media.validate_user_media(asset)


def test_empty_folder_text_means_no_user_assets():
    assert user_media.scan_optional_user_media("", "vfx") == ()


def test_resolve_rejects_malformed_id_before_scanning(tmp_path, monkeypatch):
    monkeypatch.setattr(
        user_media,
        "scan_user_media",
        lambda *_args, **_kwargs: pytest.fail("malformed IDs must fail before scanning"),
    )
    for malformed in (
        "user:bgm:../../outside",
        f"user:bgm:{'a' * 63}",
        f"user:bgm:{'g' * 64}",
    ):
        with pytest.raises(user_media.UserMediaError, match="ID"):
            user_media.resolve_user_media_asset(tmp_path, malformed, "bgm")


def test_nested_junction_like_directory_is_not_traversed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    nested = root / "junction"
    nested.mkdir(parents=True)
    (nested / "outside.mp3").write_bytes(b"outside")
    (root / "inside.mp3").write_bytes(b"inside")
    real_check = user_media._is_link_or_junction
    monkeypatch.setattr(
        user_media,
        "_is_link_or_junction",
        lambda path: Path(path) == nested or real_check(Path(path)),
    )

    assets = user_media.scan_user_media(root, "bgm")

    assert [asset.filename for asset in assets] == ["inside.mp3"]
