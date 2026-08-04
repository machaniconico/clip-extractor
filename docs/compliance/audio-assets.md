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

This record documents provenance and implemented controls; it is not a legal
opinion or a guarantee of non-infringement.
