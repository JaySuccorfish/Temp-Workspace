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
APP_NAME="SuccorfishDebugMonitor"

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

# Generate macOS .icns from assets/sf_icon_512.png using built-in sips + iconutil
ICON_ARGS=()
SRC_ICON="$SCRIPT_DIR/assets/sf_icon_512.png"
if [ -f "$SRC_ICON" ] && [ -x /usr/bin/sips ] && [ -x /usr/bin/iconutil ]; then
    ICONSET="$SCRIPT_DIR/.build/sf_icon.iconset"
    ICNS_OUT="$SCRIPT_DIR/.build/sf_icon.icns"
    rm -rf "$ICONSET"
    mkdir -p "$ICONSET"
    for sz in 16 32 128 256 512; do
        /usr/bin/sips -z "$sz" "$sz" "$SRC_ICON" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1 || true
        dbl=$((sz * 2))
        if [ "$dbl" -le 512 ]; then
            /usr/bin/sips -z "$dbl" "$dbl" "$SRC_ICON" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1 || true
        fi
    done
    if /usr/bin/iconutil -c icns "$ICONSET" -o "$ICNS_OUT" 2>/dev/null && [ -f "$ICNS_OUT" ]; then
        ICON_ARGS=(--icon "$ICNS_OUT")
        echo "[mac] Built app icon: $ICNS_OUT"
    fi
fi

echo "[mac] Building $APP_NAME.app..."
"$PORTABLE_PY" -m PyInstaller \
    --noconfirm --clean \
    --name "$APP_NAME" \
    --windowed --onedir \
    "${ICON_ARGS[@]}" \
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
