#!/usr/bin/env bash
# One-shot helper to fetch the bundled Noto Sans JP Bold (weight 700).
# Run once:  bash fetch_font.sh
# Safe to re-run; it overwrites the existing file.
set -u

DEST_DIR="$(cd "$(dirname "$0")" && pwd)/fonts"
DEST="$DEST_DIR/NotoSansJP-Bold.otf"
LICENSE_DEST="$DEST_DIR/OFL.txt"
NOTO_CJK_REV="f8d157532fbfaeda587e826d4cd5b21a49186f7c"
URL="https://raw.githubusercontent.com/notofonts/noto-cjk/$NOTO_CJK_REV/Sans/SubsetOTF/JP/NotoSansJP-Bold.otf"
LICENSE_URL="https://raw.githubusercontent.com/notofonts/noto-cjk/$NOTO_CJK_REV/Sans/LICENSE"
EXPECTED_SHA256="1b0edfb500b73a4fa8a4fcaae1bbbd403994e08e73e3e0da37e70d3853f42c5f"

mkdir -p "$DEST_DIR"

echo "[1/2] Downloading Noto Sans JP Bold and its OFL license ..."
curl -fL "$URL" -o "$DEST" || exit 1
curl -fL "$LICENSE_URL" -o "$LICENSE_DEST" || exit 1

echo "[2/2] Verifying ..."
SIZE=$(stat -c%s "$DEST" 2>/dev/null || stat -f%z "$DEST")
echo "  File: $DEST"
echo "  Size: $SIZE bytes"
if command -v fc-scan >/dev/null 2>&1; then
  echo "  Family: $(fc-scan --format '%{family}\n' "$DEST" 2>/dev/null | head -1)"
  echo "  Style : $(fc-scan --format '%{style}\n' "$DEST" 2>/dev/null | head -1)"
fi
if [ "${SIZE:-0}" -lt 500000 ]; then
  echo "  WARNING: file looks too small to be a full CJK font — download may be incomplete." >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL_SHA256="$(sha256sum "$DEST" | awk '{print $1}')"
  if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then
    echo "  ERROR: SHA-256 mismatch for downloaded font." >&2
    exit 1
  fi
  echo "  SHA-256 verified"
fi
echo "Done. Font is ready at fonts/NotoSansJP-Bold.otf"
