# Succorfish BLE & Serial Monitor — User Guide & Shortcuts

A complete tutorial and keyboard shortcut reference for running and using the monitor on **Windows** and **macOS**.

---

## 1. Quick Start (Zero Setup)

### Windows
- **Double-click** `Start_Monitor_Windows.bat`
- Or from PowerShell:
  ```powershell
  python portable_run.py
  ```

### macOS (Apple Silicon & Intel)
- **Double-click** `Start_Monitor_Mac.command`
- Or from Terminal:
  ```bash
  bash Start_Monitor_Mac.command
  ```
- On first launch, the launcher automatically downloads a self-contained portable Python 3.12 (with Tcl/Tk) into `.portable-env/` and installs `bleak`, `pyserial`, and `pycryptodome`. Subsequent launches start immediately offline.
- **Bluetooth permission:** When prompted, allow Bluetooth access in *System Settings → Privacy & Security → Bluetooth*.

### Portable Launcher Options
```bash
python3 portable_run.py --check     # Verify/prepare portable runtime without opening GUI
python3 portable_run.py --offline   # Run using existing .portable-env only (no network)
python3 portable_run.py --clean     # Delete .portable-env to start fresh
```

---

## 2. Tutorial: Serial Terminal (USB-UART / CLI)

1. Plug your device in via USB (e.g. `COM12` on Windows or `/dev/cu.usbmodem*` on macOS).
2. In the **SERIAL** bar:
   - Click **`↻`** if the port was plugged in after launch, then select the port.
   - Confirm **Baud** (default `115200`), **Data** (`8`), **Parity** (`None`), **Stop** (`1`), **Flow** (`None`).
   - Click **`Open port`**.
3. The **`Serial Terminal`** tab opens and focuses automatically — **type directly inside the terminal pane** just like Tera Term or VS Code:
   - Example (SC2 / STM32 CLI):
     - Type `seagull` + `Enter` → unlocks CLI (`CMD Prompt : Please continue`)
     - Type `help` + `Enter` → lists all available commands
     - Type `BATT?` + `Enter` → reads battery voltage & percentage
     - Type `gi=180` + `Enter` → sets GSM reporting interval; if the device resets, the monitor automatically releases the port (`--- reconnecting...`) and reconnects as soon as the USB port returns.
4. **Advanced Serial Options (optional):**
   - Click **`Advanced ▸`** on the right end of the Serial bar to reveal the manual Send bar, EOL selector (`CR (\r)` default, `CRLF`, `LF`, `None`), **`Hex`** send, **`Hex view`**, **`Local echo`**, and **`Timestamp`**.
   - Click **`Advanced ▾`** to hide it again.

---

## 3. Tutorial: BLE Monitoring & Multi-Device Configuration

1. **Scan:** Pick a **Filter** (default `All Succorfish devices`) and **Type** (default `Auto-detect`), then click **`Scan`**.
2. **Connect:** Choose a device from the dropdown and click **`Connect`**. A dedicated device tab opens with:
   - Live decrypted debug log (Valtrack / SC2) or live application view (GiGO IMU / Depth & Temp / Gas).
   - Floating interactive **GPS Satellite Map** (Valtrack / SC2) — drag to pan, mouse wheel to zoom, `◀`/`▶` to step waypoints.
   - **Resizable & Scrollable Right Telemetry Panel:** Drag the divider between the log and telemetry panel to resize; scroll with the mouse wheel to view `MAC`, `IMEI`, `Device`, `Dev Clock`, `Phase`, `GI`/`GPSI` countdowns, `GPS`, `Batt`, `Low Power`, `Motion`, `LEDs`, `GPS UTC`, `GPS Sentences`, `Lat/Lon`, and `Uptime`.
3. **Connect Multiple Devices Simultaneously (e.g. Valtrack + SC2):**
   - Select the second device in the dropdown and click **`Connect`** — a second tab opens.
   - Switch to any device's tab and click **`Config`** to open that device's dedicated configuration window (`[MAC]` is shown in the title bar). Valtrack and SC2 config windows stay strictly isolated and can be open side-by-side.
4. **DOTA Firmware Update:**
   - Open **`Config`** → **DOTA Update** tab (Valtrack STM_OTA or SC2 0xBA02 relay), choose the `.bin` image, and start the update.

---

## 4. Saving Logs & Managing Tabs

- **Save Active Tab Log:** Click **`Save`** in the top bar (or right-click any tab → **Save Tab Log...**).
  - Serial tab default filename: `serial_<PORT>_YYYYMMDD_HHMMSS.log`
  - BLE device tab default filename: `<profile>_<MAC>_YYYYMMDD_HHMMSS.log`
  - Every saved file includes a header with local & UTC timestamps, port or MAC/IMEI, and line/GPS counts.
- **Reorder Tabs:** Click and drag any tab left or right.
- **Close a Device Tab:** Click the inline **`×`** on the tab, middle-click the tab, or right-click → **Close Tab** / **Close Disconnected Tabs**.
- **Switch Theme:** Click **`☀ Light`** / **`🌙 Dark`** in the top-right bar (saved automatically for next launch).

---

## 5. Keyboard & Mouse Shortcuts

### Global & Find (VS Code Style)
| Action | Windows / Linux | macOS |
| :--- | :--- | :--- |
| Open floating **Find** overlay | `Ctrl + F` (or **`🔍 Find`** button) | `Cmd + F` (or **`🔍 Find`** button) |
| Next search match | `Enter` (or **`↓`**) | `Enter` (or **`↓`**) |
| Previous search match | `Shift + Enter` (or **`↑`**) | `Shift + Enter` (or **`↑`**) |
| Close Find & clear highlights | `Esc` (or **`×`**) | `Esc` (or **`×`**) |
| Match Case / Regex | **`Aa`** / **`.*`** buttons in Find bar | **`Aa`** / **`.*`** buttons in Find bar |

### Text Zoom (All Log & Serial Tabs)
| Action | Windows / Linux | macOS |
| :--- | :--- | :--- |
| Zoom text in / out (`7–28 pt`) | `Ctrl + Mouse Wheel` | `Cmd + Mouse Wheel` |
| Zoom in | `Ctrl + +` or `Ctrl + =` | `Cmd + +` or `Cmd + =` |
| Zoom out | `Ctrl + -` | `Cmd + -` |
| Reset zoom (`10 pt`) | `Ctrl + 0` | `Cmd + 0` |

### Serial Terminal
| Action | Windows / Linux | macOS |
| :--- | :--- | :--- |
| Send typed characters | Type directly in `Serial Terminal` | Type directly in `Serial Terminal` |
| Submit line (EOL) | `Enter` | `Return` |
| Backspace (`0x08`) | `Backspace` | `Delete (Backspace)` |
| Copy selected text / Send `Ctrl+C` (`0x03`) | `Ctrl + C` | `Cmd + C` (copy) / `Ctrl + C` (`0x03`) |
| Paste text over serial (byte-paced) | `Ctrl + V` | `Cmd + V` |
| Command history (in `Advanced` Send box) | `Up` / `Down` | `Up` / `Down` |

### Tabs, Sidebar & GPS Map
| Action | Windows / Linux | macOS |
| :--- | :--- | :--- |
| Reorder tabs | Drag tab left / right | Drag tab left / right |
| Close device tab | Click **`×`** on tab or Middle-click | Click **`×`** on tab or Middle-click |
| Tab context menu (Save / Clear / Close) | Right-click tab | Right-click or `Ctrl + Click` tab |
| Scroll right telemetry sidebar | Mouse wheel over sidebar | Trackpad / Mouse wheel over sidebar |
| Resize telemetry sidebar | Drag vertical divider sash | Drag vertical divider sash |
| Pan / Zoom GPS map | Drag map / Mouse wheel | Drag map / Mouse wheel |
