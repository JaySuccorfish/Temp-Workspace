#!/usr/bin/env bash
# Double-clickable zero-setup macOS launcher for Succorfish BLE & Serial Monitor.
# Works on Apple Silicon (arm64) and Intel (x86_64) Macs with NO manual install:
#   - Does not require Homebrew, Xcode Command Line Tools, or admin rights.
#   - On first run, downloads a self-contained portable Python 3.12 (with Tcl/Tk)
#     into .portable-env/darwin-<arch>/python and installs bleak, pyserial, pycryptodome.
#   - Subsequent runs launch immediately offline.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

ARCH_RAW="$(uname -m)"
if [ "$ARCH_RAW" = "arm64" ] || [ "$ARCH_RAW" = "aarch64" ]; then
    ARCH="arm64"
    TRIPLE="aarch64-apple-darwin"
else
    ARCH="x86_64"
    TRIPLE="x86_64-apple-darwin"
fi

ENV_ROOT="$SCRIPT_DIR/.portable-env/darwin-$ARCH"
PORTABLE_PY="$ENV_ROOT/python/bin/python3"

bootstrap_standalone_python() {
    echo "[mac] Downloading portable Python 3.12 (with Tcl/Tk) for $TRIPLE..."
    mkdir -p "$ENV_ROOT"
    local url=""
    # Query latest release; fall back to a known-good pinned release if GitHub API is rate-limited.
    url="$(curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" 2>/dev/null \
        | grep -oE "https://[^\"]+cpython-3\.12\.[0-9]+\+[0-9]+-${TRIPLE}-install_only\.tar\.gz" \
        | head -n 1 || true)"
    if [ -z "$url" ]; then
        url="https://github.com/astral-sh/python-build-standalone/releases/download/20250317/cpython-3.12.9+20250317-${TRIPLE}-install_only.tar.gz"
    fi
    echo "[mac]   $url"
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' RETURN
    curl -fL --progress-bar "$url" -o "$tmp_dir/python.tar.gz"
    tar -xzf "$tmp_dir/python.tar.gz" -C "$tmp_dir"
    rm -rf "$ENV_ROOT/python"
    mv "$tmp_dir/python" "$ENV_ROOT/python"
}

if [ ! -x "$PORTABLE_PY" ]; then
    bootstrap_standalone_python
fi

echo "[mac] Starting Succorfish BLE & Serial Monitor..."
exec "$PORTABLE_PY" "$SCRIPT_DIR/portable_run.py" "$@"
