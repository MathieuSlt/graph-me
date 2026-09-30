#!/usr/bin/env bash
# Install a pinned msgvault release (used by CI and for local development).
# Usage: scripts/install_msgvault.sh [install_dir]   (default: ~/.local/bin)
# The download is verified against the release's SHA256SUMS before installing.
set -euo pipefail

VERSION="${MSGVAULT_VERSION:-0.20.0}"
DEST="${1:-$HOME/.local/bin}"

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) echo "unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac

asset="msgvault_${VERSION}_${os}_${arch}.tar.gz"
base="https://github.com/kenn-io/msgvault/releases/download/v${VERSION}"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

curl -fsSL -o "$tmp/$asset" "$base/$asset"
curl -fsSL -o "$tmp/SHA256SUMS" "$base/SHA256SUMS"
expected="$(grep " ${asset}\$" "$tmp/SHA256SUMS" | awk '{print $1}')"
if command -v sha256sum >/dev/null; then
  actual="$(sha256sum "$tmp/$asset" | awk '{print $1}')"
else
  actual="$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')"
fi
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
  echo "checksum mismatch for $asset" >&2
  exit 1
fi

tar -xzf "$tmp/$asset" -C "$tmp"
mkdir -p "$DEST"
install -m 0755 "$(find "$tmp" -type f -name msgvault | head -n 1)" "$DEST/msgvault"
"$DEST/msgvault" version | head -n 1
