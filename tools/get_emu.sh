#!/bin/sh
# Fetch the fm1-emu release binary into tools/fm1-emu-bin/ (gitignored).
# fm1-emu: https://github.com/joakimeriksson/fm1-emu (MIT)
# Override the version with FM1_EMU_VERSION=v0.4.0 if needed.
set -eu
VER="${FM1_EMU_VERSION:-v0.4.0}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/tools/fm1-emu-bin"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  ASSET="fm1-emu-${VER#v}-linux-x86_64.tar.gz" ;;
  Linux-aarch64) ASSET="fm1-emu-${VER#v}-linux-arm64.tar.gz" ;;
  Darwin-arm64)  ASSET="fm1-emu-${VER#v}-macos-arm64.tar.gz" ;;
  *) echo "no prebuilt fm1-emu for $(uname -s)-$(uname -m); build from source:" >&2
     echo "  git clone https://github.com/joakimeriksson/fm1-emu && cd fm1-emu && cargo build --release -p fm1-emu" >&2
     exit 1 ;;
esac

mkdir -p "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "downloading $ASSET ($VER)..."
curl -fsSL -o "$TMP/emu.tgz" \
  "https://github.com/joakimeriksson/fm1-emu/releases/download/$VER/$ASSET"
tar -xzf "$TMP/emu.tgz" -C "$TMP"
cp "$TMP"/fm1-emu-*/fm1-emu "$DEST/fm1-emu"
[ -f "$TMP"/fm1-emu-*/fm1-live ] && cp "$TMP"/fm1-emu-*/fm1-live "$DEST/fm1-live" || true
chmod +x "$DEST/fm1-emu" "$DEST/fm1-live" 2>/dev/null || chmod +x "$DEST/fm1-emu"
"$DEST/fm1-emu" --version
echo "installed: $DEST/fm1-emu"
