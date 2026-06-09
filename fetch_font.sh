#!/usr/bin/env bash
# One-shot helper to fetch the bundled subtitle font (Noto Sans JP Black / 源ノ角ゴシック Heavy 相当).
# Run once:  bash fetch_font.sh
# Safe to re-run; it overwrites the existing file.
set -u

DEST_DIR="$(cd "$(dirname "$0")" && pwd)/fonts"
DEST="$DEST_DIR/NotoSansJP-Black.ttf"
URL="https://cdn.jsdelivr.net/npm/@expo-google-fonts/noto-sans-jp/NotoSansJP_900Black.ttf"
FALLBACK_ZIP_URL="https://fonts.google.com/download?family=Noto+Sans+JP"

mkdir -p "$DEST_DIR"

echo "[1/2] Downloading Noto Sans JP Black (static heavy weight) ..."
if curl -fL "$URL" -o "$DEST" && [ -s "$DEST" ]; then
  echo "  OK: primary source (jsDelivr)"
else
  echo "  Primary source failed; trying Google Fonts zip fallback ..."
  TMP_ZIP="$(mktemp --suffix=.zip)"
  TMP_DIR="$(mktemp -d)"
  if curl -fL "$FALLBACK_ZIP_URL" -o "$TMP_ZIP" \
     && unzip -o "$TMP_ZIP" "static/NotoSansJP-Black.ttf" -d "$TMP_DIR" \
     && cp "$TMP_DIR/static/NotoSansJP-Black.ttf" "$DEST"; then
    echo "  OK: fallback source (Google Fonts)"
  else
    echo "  ERROR: both sources failed. Check your network/proxy and retry." >&2
    rm -rf "$TMP_ZIP" "$TMP_DIR"
    exit 1
  fi
  rm -rf "$TMP_ZIP" "$TMP_DIR"
fi

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
echo "Done. Font is ready at fonts/NotoSansJP-Black.ttf"
