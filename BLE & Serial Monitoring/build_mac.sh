#!/usr/bin/env bash
# macOS run & standalone .app builder for Succorfish BLE & Serial Monitor.
#
# Usage:
#   ./build_mac.sh              # build dist/ValtrackDebugMonitor.app (standalone macOS app)
#   ./build_mac.sh --run        # run directly from source using portable Python
#   ./build_mac.sh --venv       # prepare the portable Python runtime only
#   ./build_mac.sh --version 2.3.0
#   ./build_mac.sh --clean      # remove .portable-env, .build, dist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

MODE="build"
VERSION="2.3.0"
APP_NAME="ValtrackDebugMonitor"

while [ $# -gt 0 ]; do
    case "$1" in
        --run)     MODE="run"; shift ;;
        --venv)    MODE="venv"; shift ;;
        --clean)   MODE="clean"; shift ;;
        --version) VERSION="${2:-2.3.0}"; shift 2 ;;
        -h|--help)
            sed -n '2,10p' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

if [ "$MODE" = "clean" ]; then
    rm -rf "$SCRIPT_DIR/.portable-env" "$SCRIPT_DIR/.build" "$SCRIPT_DIR/dist"
    echo "[mac] Cleaned .portable-env, .build, dist"
    exit 0
fi

# Ensure portable Python (with Tcl/Tk + runtime deps) is ready via Start_Monitor_Mac.command logic.
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

if [ ! -x "$PORTABLE_PY" ]; then
    "$SCRIPT_DIR/Start_Monitor_Mac.command" --check
else
    "$PORTABLE_PY" "$SCRIPT_DIR/portable_run.py" --check
fi

if [ "$MODE" = "venv" ]; then
    echo "[mac] Portable environment ready at $ENV_ROOT"
    exit 0
fi

if [ "$MODE" = "run" ]; then
    exec "$PORTABLE_PY" "$SCRIPT_DIR/debug_monitor.py"
fi

echo "[mac] Installing PyInstaller into portable runtime..."
"$PORTABLE_PY" -m pip install --disable-pip-version-check --no-input "pyinstaller>=6.10.0"

echo "[mac] Building $APP_NAME.app..."
"$PORTABLE_PY" -m PyInstaller \
    --noconfirm --clean \
    --name "$APP_NAME" \
    --windowed --onedir \
    --paths "$SCRIPT_DIR" \
    --collect-submodules monitor \
    --collect-submodules serial \
    --hidden-import serial \
    --hidden-import serial.tools \
    --hidden-import serial.tools.list_ports \
    --hidden-import sc2_protocol \
    --hidden-import brand_assets \
    --osx-bundle-identifier "com.succorfish.blemonitor" \
    --distpath "$SCRIPT_DIR/dist" \
    --workpath "$SCRIPT_DIR/.build/build" \
    --specpath "$SCRIPT_DIR/.build" \
    "$SCRIPT_DIR/debug_monitor.py"

APP_BUNDLE="$SCRIPT_DIR/dist/${APP_NAME}.app"
INFO_PLIST="$APP_BUNDLE/Contents/Info.plist"

if [ -f "$INFO_PLIST" ] && [ -x /usr/libexec/PlistBuddy ]; then
    echo "[mac] Injecting Bluetooth & Retina metadata into Info.plist..."
    /usr/libexec/PlistBuddy -c "Delete :NSBluetoothAlwaysUsageDescription" "$INFO_PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :NSBluetoothAlwaysUsageDescription string 'Succorfish BLE Device Monitor uses Bluetooth to scan, configure, monitor, and update Succorfish/GiGO devices.'" "$INFO_PLIST"
    /usr/libexec/PlistBuddy -c "Delete :NSBluetoothPeripheralUsageDescription" "$INFO_PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :NSBluetoothPeripheralUsageDescription string 'Succorfish BLE Device Monitor uses Bluetooth to communicate with Succorfish/GiGO devices.'" "$INFO_PLIST"
    /usr/libexec/PlistBuddy -c "Delete :CFBundleShortVersionString" "$INFO_PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string '${VERSION}'" "$INFO_PLIST"
    /usr/libexec/PlistBuddy -c "Delete :NSHighResolutionCapable" "$INFO_PLIST" 2>/dev/null || true
    /usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$INFO_PLIST"
fi

echo "[mac] Done! App bundle created at: $APP_BUNDLE"
echo "[mac] Launch with: open \"$APP_BUNDLE\""
