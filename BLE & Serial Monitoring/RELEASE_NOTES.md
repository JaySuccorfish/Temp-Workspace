# Succorfish BLE & Serial Monitor — Release Notes (v2.4.0)

**Release Date:** 2026-11-26  
**Platforms:** Windows 10/11 (x64) · macOS (Apple Silicon `arm64` & Intel `x86_64`) · Linux

---

## Highlights

### 1. Cross-Platform macOS & Windows Zero-Setup Launchers
- **macOS (`Start_Monitor_Mac.command`):** Double-click to launch with no manual Python, Homebrew, or Xcode Command Line Tools setup. On first run it downloads a self-contained portable Python 3.12 (with Tcl/Tk bundled) into `.portable-env/` and installs `bleak`, `pyserial`, and `pycryptodome`. Subsequent runs start immediately offline.
- **Windows (`Start_Monitor_Windows.bat`):** Double-click zero-setup launcher that reuses `.portable-env` / `.build-venv` or downloads standalone portable Python automatically if none is installed.
- **Universal Launcher (`portable_run.py`):** Single standard-library Python script supporting Windows, macOS, and Linux (`--check`, `--offline`, `--clean`).
- **macOS App Builder (`build_mac.sh`):** Builds a standalone `dist/ValtrackDebugMonitor.app` bundle with `NSBluetoothAlwaysUsageDescription`, `NSBluetoothPeripheralUsageDescription`, and `NSHighResolutionCapable` injected into `Info.plist`.
- **Full macOS Compatibility:** Native Aqua dark-theme button rendering shim, macOS fonts (`Helvetica Neue` / `Menlo`), native JPEG satellite tile decoding via `/usr/bin/sips` (or Pillow), macOS tab right-click bindings (`<Button-2>` / `<Control-Button-1>`), `~/Library/Application Support` settings path, USB-UART port sorting (`/dev/cu.usb*` first), and Bluetooth permission guidance.

### 2. Reliable Interactive Serial Terminal (Tera Term / VS Code Style)
- **Byte-Paced TX (`10 ms` default):** Prevents dropped characters on STM32 / nRF USB CDC-ACM virtual COM ports (e.g. `seagull`, `help`, `BATT?`, `gi=180` work reliably without dropping bytes).
- **Direct Interactive Typing:** Type directly inside the `Serial Terminal` pane (with full backspace `\x08`/`\x7f` handling, `Enter`, `Tab`, `Ctrl+C`, and `Ctrl+V` paste).
- **Native ANSI Terminal Colours:** Renders device colour codes (`\x1b[...m` red, green, yellow, blue, cyan, bold, etc.) adapted cleanly for both Dark and Light themes.
- **Automatic Reconnect After Device Reset:** Immediately releases stale COM handles when a device resets (`gi=180`, `RES`, or USB replug) and automatically reconnects once the port re-enumerates.
- **Collapsible `Advanced ▸` Bar:** Keeps the send input, EOL selector (`CR (\r)` default), `Hex`, `Hex view`, `Local echo`, and `Timestamp` tucked away until clicked.

### 3. VS Code-Style Compact UI, Themes & Find
- **Light & Dark Themes:** Live **`☀ Light` / `🌙 Dark`** toggle in the top bar (persisted in per-user settings), including Windows 10/11 native OS title-bar tinting (`DwmSetWindowAttribute`).
- **Compact Single Top Bar:** Removed the large logo banner and `System Log` tab to maximise terminal and live-view space.
- **Floating `Ctrl+F` / `Cmd+F` Find Overlay:** Appears in the top-right corner of the active log/terminal pane (or via the **`🔍 Find`** button) with `Aa` (Match Case), `.*` (Regex), `1 of N` match counter, `↑`/`↓` navigation, and `Esc`/`×` close.
- **Text Wheel Zoom:** `Ctrl + Mouse Wheel` (`Cmd + Mouse Wheel` on macOS) and `Ctrl/Cmd + + / - / 0` zoom all log/terminal panes (`7–28 pt`) with crisp bright standard text (`10 pt` default).
- **Draggable Tabs & Inline Close (`×`):** Drag any tab left/right to reorder; click the inline `×` on device tabs to close.
- **Resizable & Scrollable Telemetry Sidebar:** Drag the vertical divider between the log/map pane and the right telemetry panel; scroll the sidebar with mouse wheel or scrollbar so every field (`Motion`, `LEDs`, `GPS UTC`, `Lat/Lon`, `Uptime`) is always accessible.

### 4. Multi-Device Stability & Identity
- **No Valtrack / SC2 Config Conflict:** Each configuration window (`ConfigPanel`, `Sc2HubConfigPanel`, `GigoFamilyPanel`) binds strictly to its own `session_id` from the active device tab and displays `[MAC]` in its window title, allowing Valtrack and SC2 config windows to run side-by-side safely.
- **IMEI Displayed Below MAC:** Automatically queries `0xE1` on connect for Valtrack and SC2 units and displays `IMEI:` right below `MAC:` in the telemetry panel.
- **Per-Tab Timestamped Log Saving:** Clicking **Save** (or right-click tab → **Save Tab Log...**) saves the active tab with a descriptive timestamped filename (`serial_COM12_YYYYMMDD_HHMMSS.log` or `valtrack_<MAC>_YYYYMMDD_HHMMSS.log`) and full UTC/local header.

---

## Platform Notes
- **macOS Bluetooth Permission:** On first BLE scan, allow Bluetooth access for Terminal / `ValtrackDebugMonitor` in *System Settings → Privacy & Security → Bluetooth*.
- **macOS Execute Bit (if copied via ZIP/USB):** Run `chmod +x Start_Monitor_Mac.command build_mac.sh` once if Finder does not treat `.command` as executable (Git clones preserve the executable bit automatically).

See [USER_GUIDE.md](USER_GUIDE.md) for tutorials and all keyboard shortcuts, and [CHANGELOG.md](CHANGELOG.md) for the full change history.
