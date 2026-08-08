"""Integrity and transparency checks for the bundled expanded VFX pack."""

from pathlib import Path

from PIL import Image

from user_media import scan_user_media


PACK_DIR = Path(__file__).parent.parent / "VFX" / "expanded"
EXPECTED_SHA256 = {
    "crescent-slash-thin.png": "cb4787978122bb863866a1681af22a7dec39a4566f08c400f233740eb1d3730c",
    "crescent-slash-wide.png": "a79a9e297790e6126b78d41015a3045cda3f1863b421c88321d2442ed6f9032e",
    "debris-burst.png": "427d6dcad5a8d16b8143d412a5a48ce859716150a074ea5bb36eca577c0ae541",
    "energy-burst-cloud.png": "e2677330b0668033b0744276da74de605164aa5d58203bf251a8ec84e62c2108",
    "energy-burst-dense.png": "9727a9743ac6ab8dd4dd79f5fc5b0266290efae45f4d4452279eb5e4bcf3a978",
    "energy-orb-arc.png": "f1bdb57478703fec539f7b8eeceb494c4a23664652b296389f310104be80cfe0",
    "energy-orb-ripple.png": "caedcc5df5bcac4470244d2520d6a64b45b20c1ccf4d918da516c55e3fa365a6",
    "energy-orb-wave.png": "b68627734d08aab3eaf3f94718521553628c75aace1de6c74de28501c1a4c147",
    "flame-wisp.png": "92a70bcb90b1e21c57b2dae42843135703b2e988abee97433f84c3119723d144",
    "lens-flare-pinpoint.png": "254f5bd4f0548612bf29305d27899efdee8cb8d8306292761ebbddebd3e57935",
    "lightning-burst.png": "5902da3a324cad0a4b5a88ce1e3c9cc18fee50922c358ce93d8c9f1ad7708f0a",
    "magic-compass.png": "c2ef5fe86cd2fd08e5b9768389ab6580b346c391db3c7c09fc9b0a63e00ec4ce",
    "magic-octagon.png": "6443e90834abcae4c3848d7ebca99ff0d9fcad19a07f49f77ad64710a3e39b36",
    "magic-pentagon.png": "398479ea280c8161d0b6d509ca97eefb27e989c7832b404abcb65faae3e922ff",
    "shockwave-ring-bold.png": "ed6c6f082e666c16bcaca89f15aefb55df9bf69450090518bcae1d3d61f82936",
    "shockwave-ring-soft.png": "742da1a1b96b93ae446700f6085385d1f62844352da870c89427307b7b7cf03b",
    "star-glint.png": "3ba3999c944a0767677449412d306c1f230daa1743b31f929533cf77a22ecf5d",
}


def test_expanded_vfx_pack_is_complete_and_content_addressed():
    assets = scan_user_media(PACK_DIR, "vfx")
    observed = {asset.relative_path: asset.sha256 for asset in assets}

    assert observed == EXPECTED_SHA256


def test_expanded_vfx_pack_uses_transparent_512px_pngs():
    for filename in EXPECTED_SHA256:
        with Image.open(PACK_DIR / filename) as image:
            assert image.size == (512, 512), filename
            alpha_min, alpha_max = image.convert("RGBA").getchannel("A").getextrema()
            assert alpha_min == 0, filename
            assert alpha_max > 0, filename


def test_expanded_vfx_pack_keeps_license_and_source_evidence():
    source_record = (PACK_DIR / "SOURCES.md").read_text(encoding="utf-8")
    upstream_license = (PACK_DIR / "LICENSE-KENNEY.txt").read_text(
        encoding="utf-8"
    )

    assert "https://www.kenney.nl/assets/particle-pack" in source_record
    assert "Creative Commons Zero 1.0 Universal" in source_record
    assert "commercial projects" in upstream_license
    assert "not mandatory" in upstream_license
    for filename, digest in EXPECTED_SHA256.items():
        assert f"`{filename}`" in source_record
        assert f"`{digest}`" in source_record
