# Downloadable audio asset compliance record

Checked: 2026-08-04

The optional `cc0-starter` pack is not bundled into the application and is not
downloaded during rendering. A user must explicitly start installation. The
versioned cache is stored under
`%LOCALAPPDATA%/ClipExtractor/asset-packs/cc0-starter/2026.08.1/` on Windows.

## Included sources

| Content | Creator | Official source | License shown by source | Attribution |
| --- | --- | --- | --- | --- |
| Interface Sounds | Kenney | https://kenney.nl/assets/interface-sounds | CC0 1.0 | Not required |
| Impact Sounds | Kenney | https://kenney.nl/assets/impact-sounds | CC0 1.0 | Not required |
| Short Loops Background Music Pack | hernandack | https://opengameart.org/content/short-loops-background-music-pack | CC0 1.0 | Not required |

Kenney's official support page states that game assets on the asset pages are
CC0: https://kenney.nl/support

CC0 summary and legal code:

- https://creativecommons.org/publicdomain/zero/1.0/
- https://creativecommons.org/publicdomain/zero/1.0/legalcode

## Distribution and integrity behavior

- Only fixed HTTPS URLs on the Kenney and OpenGameArt host allowlists are used.
- Redirect destinations must remain on the source-specific allowlist.
- Every archive/file has a pinned byte size and SHA-256 in
  `assets/audio/catalog.json`.
- Only named ZIP members are extracted, and each selected member has its own
  pinned byte size and SHA-256.
- Installation is staged and verified before the version directory is swapped;
  a failed refresh keeps the previous complete installation.
- `ffprobe` is required; every installed OGG must expose a decodable audio
  stream before activation. If media validation cannot run, installation fails.
- The installed manifest preserves source URLs, final URLs, creator, license,
  modification state, byte size, and SHA-256.

## User-provided BGM, SE, and VFX

User-selected folders are a separate input path from the downloadable pack.
Their files are referenced in place and are not copied into the application,
uploaded, bundled, or described as CC0. The application does not determine or
approve their license; the user is responsible for selecting material whose
commercial-use and editing permissions they have already verified.

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
