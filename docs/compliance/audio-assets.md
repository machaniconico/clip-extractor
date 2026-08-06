# Downloadable audio asset compliance record

Checked: 2026-08-06

The optional `short-video-starter` pack is not bundled into the application and is not
downloaded during rendering. A user must explicitly start installation. The
versioned cache is stored under
`%LOCALAPPDATA%/ClipExtractor/asset-packs/short-video-starter/2026.08.3/` on
Windows. The pinned source downloads total 13,385,336 bytes and install 8 BGM
and 23 SE files.

## Included sources

| Content | Creator | Official source | License shown by source | Attribution |
| --- | --- | --- | --- | --- |
| Interface Sounds | Kenney | https://kenney.nl/assets/interface-sounds | CC0 1.0 | Not required |
| Impact Sounds | Kenney | https://kenney.nl/assets/impact-sounds | CC0 1.0 | Not required |
| Short Loops Background Music Pack | hernandack | https://opengameart.org/content/short-loops-background-music-pack | CC0 1.0 | Not required |
| Camera Motion 11 (short) | OtoLogic | https://otologic.jp/free/se/camera-motion01.html | CC BY 4.0 | Required |
| Quiz Ding Dong / Buzzer 05 (short) | OtoLogic | https://otologic.jp/free/se/quiz01.html | CC BY 4.0 | Required |
| Short Accent 06 (dry) | OtoLogic | https://otologic.jp/free/se/short-accent01.html | CC BY 4.0 | Required |
| bo-tto hidamari (Narr) | OtoLogic | https://otologic.jp/free/bgm/pop-music01.html | CC BY 4.0 | Required |
| Inspiration 11 (mid) | OtoLogic | https://otologic.jp/free/se/inspiration01.html | CC BY 4.0 | Required |
| Countdown 06 (pop) | OtoLogic | https://otologic.jp/free/se/countdown01.html | CC BY 4.0 | Required |
| Censor Bleep 1kHz 01 (short) | OtoLogic | https://otologic.jp/free/se/censor-bleep01.html | CC BY 4.0 | Required |
| 木陰でゆったり (fast) | OtoLogic | https://otologic.jp/free/bgm/wood_mallet01.html | CC BY 4.0 | Required |
| ドタバタパニック (fast) | OtoLogic | https://otologic.jp/free/bgm/wood_mallet01.html | CC BY 4.0 | Required |
| 雲行きが怪しいぞ (slow) | OtoLogic | https://otologic.jp/free/bgm/wood_mallet01.html | CC BY 4.0 | Required |

Kenney's official support page states that game assets on the asset pages are
CC0: https://kenney.nl/support

CC0 summary and legal code:

- https://creativecommons.org/publicdomain/zero/1.0/
- https://creativecommons.org/publicdomain/zero/1.0/legalcode

OtoLogic's official license and FAQ state that CC BY 4.0 permits commercial
use, modification, and redistribution when attribution is preserved:

- https://otologic.jp/free/license.html
- https://otologic.jp/free/faq.html
- https://creativecommons.org/licenses/by/4.0/

Required publication credit:

> 音素材：OtoLogic (https://otologic.jp/) / CC BY 4.0

This credit is copied into `THIRD_PARTY_NOTICES_AUDIO.txt` whenever an
OtoLogic asset is selected. The UI also marks those choices `要クレジット`.
Works containing the shared audio must not be registered with Content ID or
otherwise used to claim exclusive rights in the source material.

## Distribution and integrity behavior

- Only fixed HTTPS URLs on the Kenney, OpenGameArt, and OtoLogic host
  allowlists are used.
- Each request sends only the catalog-pinned official material page as its
  `Referer`; that page is validated against the same source host allowlist.
- Redirect destinations must remain on the source-specific allowlist.
- Every archive/file has a pinned byte size and SHA-256 in
  `assets/audio/catalog.json`.
- Only named ZIP members are extracted, and each selected member has its own
  pinned byte size and SHA-256.
- Installation is staged and verified before the version directory is swapped;
  a failed refresh keeps the previous complete installation.
- `ffprobe` is required; every installed OGG or MP3 must expose a decodable audio
  stream before activation. If media validation cannot run, installation fails.
- The installed manifest preserves source URLs, final URLs, creator, license,
  modification state, byte size, and SHA-256.

## User-provided BGM, SE, and VFX

User-selected folders are a separate input path from the downloadable pack.
Their files are referenced in place and are not copied into the application,
uploaded, bundled, or described as CC0. The application does not determine or
approve their license; the user is responsible for selecting material whose
commercial-use and editing permissions they have already verified.

## External source guide

Guide links were reviewed against the official terms pages on 2026-08-06.
They are discovery links, not an approved-material allowlist. No media from
these sites is bundled, scraped, or downloaded merely by opening the guide.
The separate explicit starter-pack action downloads only the catalog-pinned
pack files, including the eleven OtoLogic files listed above. Their
redistribution permission and required credit were independently recorded.

| Source | Media shown in the guide | Official terms | Material conditions surfaced in the UI |
| --- | --- | --- | --- |
| DOVA-SYNDROME | BGM, SE | https://dova-s.jp/help/articles/license-usage/ | Commercial background use is permitted; creator-specific conditions and prohibited uses can still apply. |
| Sound Effect Lab | SE | https://soundeffect-lab.info/agreement/ | Commercial video use is permitted without credit; redistribution and bundling as default editor material are prohibited. |
| OtoLogic | BGM, SE | https://otologic.jp/free/license.html | Free use is under CC BY 4.0 and requires an `OtoLogic` credit; a paid no-credit license is separate. |
| Pixabay | BGM, SE, video/VFX | https://pixabay.com/service/license-summary/ | Free use and adaptation are permitted subject to prohibited uses; standalone redistribution is prohibited and music can be registered with Content ID. |
| Mixkit | BGM, SE, video/VFX | https://mixkit.co/license/ | Licenses differ by item type, and stock video can be Free or Restricted; the applicable item license must be checked. |

For material outside that fixed starter selection, the UI tells users to
download from the official site themselves, save the file in a user-selected
folder, and retain the material page, terms URL, and acquisition date. Each
item's page, creator-specific terms, and current terms take precedence over
this guide.

Selections use a content-addressed ID and are rechecked for file replacement
and a matching media stream before rendering. Output provenance records only
the material kind, original filename, byte size, and SHA-256; it does not copy
the absolute source path or claim an independently verified license. BGM/SE
records are written to `audio_manifest.json` and
`THIRD_PARTY_NOTICES_AUDIO.txt`; baked VFX/effect decisions are written to
`effects_manifest.json` in the generated clip group.

On regeneration, prior audio artifacts named by a recognized current or legacy
manifest are moved to a recoverable `.audio_delivery_recovery-*` directory
with a `RECOVERY.json` path index. They are not irreversibly deleted based only
on provenance stored inside the user-writable output directory. A mixed audio
delivery also remaps the owned `effects_manifest.json` from its clean source
name to the final `_mixed.mp4` output atomically.

This record documents provenance and implemented controls; it is not a legal
opinion or a guarantee of non-infringement.
