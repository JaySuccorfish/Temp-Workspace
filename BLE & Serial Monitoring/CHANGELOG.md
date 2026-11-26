# Changelog

All notable changes to **Succorfish BLE & Serial Monitor** are documented in this file.

## [2.4.0] - 2026-11-26

### Added
- **Cross-platform zero-setup launchers:**
  - `portable_run.py`: stdlib-only portable launcher for Windows, macOS, and Linux (`--check`, `--offline`, `--clean`) that automatically provisions a local runtime in `.portable-env/`.
  - `Start_Monitor_Mac.command`: double-clickable macOS launcher (Apple Silicon & Intel) with automatic standalone CPython 3.12 + Tcl/Tk bootstrap.
  - `Start_Monitor_Windows.bat`: double-clickable Windows launcher with automatic portable Python bootstrap.
  - `build_mac.sh`: macOS PyInstaller `.app` builder injecting Bluetooth & Retina `Info.plist` keys.
  - `.gitattributes`: enforces LF line endings for `.sh` and `.command` files and CRLF for `.bat` and `.ps1`.
- **Light & Dark themes:**
  - `THEME_PALETTES` (`dark` and `light`) with live **`☀ Light` / `🌙 Dark`** toggle persisted in `user_settings.py`.
  - `retheme_widget_tree()` for live re-theming of classic Tk widgets and open child windows.
  - `apply_os_titlebar_theme()` matching the native Windows 10/11 title bar to Dark/Light mode via `DwmSetWindowAttribute`.
- **VS Code-style Find overlay:**
  - Floating top-right Find widget triggered by `Ctrl+F` / `Cmd+F` or the **`🔍 Find`** button, with `Aa` (Match Case), `.*` (Regex), `1 of N` match counter, `↑`/`↓` navigation, and `Esc`/`×` close.
- **VS Code-style text zoom & tab management:**
  - `Ctrl/Cmd + Mouse Wheel` and `Ctrl/Cmd + + / - / 0` zoom across all log and serial terminal panes (`7–28 pt`).
  - Inline `×` close button on device tabs and drag-and-drop tab reordering.
  - Resizable horizontal `PanedWindow` and vertically scrollable right telemetry sidebar on every device tab.
- **Serial Terminal upgrades:**
  - Direct Tera Term-style interactive keyboard input inside `Serial Terminal` (including `\x08`/`\x7f` terminal backspace rendering, `Tab`, `Ctrl+C`, and `Ctrl+V` paste).
  - Native ANSI SGR colour rendering (`\x1b[...m`) adapted for Dark and Light themes.
  - Ordered background TX queue with 10 ms inter-byte pacing (`write_async`) and automatic DTR/RTS assertion for STM32/nRF USB CDC-ACM ports.
  - Automatic serial reconnect after device reset or USB replug (`Cancel reconnect` button).
  - Collapsible **`Advanced ▸`** toggle hiding the manual Send/Hex/EOL/Echo/Timestamp bar by default.
- **IMEI display & per-tab timestamped log saving:**
  - Automatic IMEI (`0xE1`) read on connect for Valtrack and SC2 devices, displayed directly below `MAC:`.
  - Tab-specific timestamped log filenames (`serial_<PORT>_YYYYMMDD_HHMMSS.log`, `<profile>_<MAC>_YYYYMMDD_HHMMSS.log`) with UTC/local headers and **Save Tab Log...** in the tab right-click menu.
- **Documentation:** Added `RELEASE_NOTES.md`, `CHANGELOG.md`, and `USER_GUIDE.md`.

### Changed
- Merged the separate `BrandHeader` title row into the single compact top toolbar (removed large logo banner and `System Log` tab).
- Compacted BLE controls into a single row (`Filter`, `Type`, `Scan`, `Device`, `Connect`, `Disconnect`, `Config`, `Logger`, `Dbg`, `Auto-Recon`).
- Brightened default terminal/log text to high-contrast white (`#f8fafc` dark / `#0f172a` light) at `10 pt`.
- Changed default Serial EOL from `CRLF (\r\n)` to `CR (\r)` and defaulted Local echo to `Off`.

### Fixed
- **Valtrack + SC2 config tab conflict:** `open_config_panel()` now resolves strictly from the active device tab, tracks open config windows per `session_id`, and `SessionView.selected_session` no longer falls back to another connected device.
- **macOS compatibility:** Added macOS Aqua dark-theme `tk.Button` shim, platform-native UI/mono fonts, `/usr/bin/sips` & Pillow JPEG satellite tile decoding, macOS tab right-click/middle-click bindings, `open`/`xdg-open` folder launch, `~/Library/Application Support` settings path, `python3` codec worker lookup, USB-UART port sorting, and CoreBluetooth permission hints.
- **Serial terminal backspace & TX echo:** Fixed `\x08 \x08` backspace erasing in `txt_serial` and routed local TX echo to `Serial Terminal` instead of `System Log`.

## [2.3.0] - 2026-11-21
- Multi-device BLE support across Valtrack V1/2, SC2 Gen2/Gen3, GiGO Delphis, GiGO Rotation/Jack, GiGO Depth & Temp, and GiGO Gas loggers.
- Auto-detection from advertisements, GATT tables, and post-auth probes.
- Per-application live views (GPS map, attitude indicator, drum/jack motion replay, depth/temp & gas charts), Delphis NM3 acoustic messaging & D128 picture transfer, and firmware DOTA updates.
