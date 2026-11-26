# Valtrack — Engineering Work Log

**Session:** 2026-10-27
**Repository:** `D:\projects\valtrack_v4_mf`
**Branch:** `root` · `HEAD 7765b57` · **all changes uncommitted / unpushed**

---

## 1. Firmware changes applied

Build after all changes: **`app 99,444 B (75.87 %)`** · **`RAM 111,248 B (56.60 %)`**

| File | Change | Reason / evidence |
| :---- | :---- | :---- |
| `gsm_a76xx.h` | `GSM_MAX_DATA_LENGTH` 256 → **512** | `AT+COPS=?` listing is ~440 B; truncation dropped the trailing `OK`, so the command timed out after ~6 min |
| `gsm_at_parser.h` | `GSM_AT_MAX_LINE_LENGTH` 160 → **512** | `GSM AT: overlong line discarded` in field logs |
| `gsm_a76xx.h` | added `GSM_IDENTITY_TIMEOUT_SEC` **(15 s)** | `AT+CGSN` blocked bring-up for **181 s**; device vanished from BLE during the stall |
| `gsm_a76xx.c` | 10 bring-up commands → 15 s; `CGMI/CGMM/CGMR` non-fatal | `ATE0` timed out after **180 s** |
| `gsm_network.c` | manual reselect on **no-signal** and on **180 s budget expiry** | previously only fired on `CREG==3`, a state this SIM never reports (`0,0`) |
| `gsm_network.c` | `ratList[ratIndex]` **bounds check** added | a 6-network scan overflowed `networkType[6]` on the stack |
| `gsm_network.c` | `GSM_MANUAL_SEARCH_MAX_CANDIDATES` 3 → **6** | the two 2G entries were being skipped |
| `gsm_network.c` | scan logging (raw response, parsed list, each attempt) | the reselect was completely silent |
| `gsm/gsm_modem.c` | `GSM_MIN_SUPPLY_MV` 3000 → **3600** | pack at **3572 mV** passed the guard then failed SIM init (`PBDONE=0`) |
| `app_ble.c` | unconditional `aci_gap_set_non_discoverable()` + 20 ms before every start | `0x0C` Command Disallowed on reconnect → device advertised once, never again |
| `gps_se868.c` | `PMTK314` now enables **GSV** | without `$GPGSV` the log cannot distinguish "no antenna" from "no fix" |

**Rejected:** `gsmConnected = FALSE` → `TRUE` on socket open. It means *"session header sent"*, not
*"link up"* — the change would skip the header on every reconnect.

---

## 2. Field diagnoses reached

| Subsystem | Conclusion | Evidence |
| :---- | :---- | :---- |
| **GSM** | Device works; **network refuses the SIM** | registered `+COPS: 0,2,"40445",3`, `+CGATT: 1`, `+CGACT: 1,1`, sent 16 B OK. Then all 6 networks returned `+CME ERROR: phone failure`; `+CREG: 0,11` (emergency only); Onomondo logged `reason: 4g_…` |
| **GPS** | **0 satellites, antenna/RF** | 90 min uptime with `numSat = 0`; date `120180` (1980) proves satellite time was never received → dead backup cell too. GPS fixed (8–14 sats) whenever the modem was idle → RF desensitisation |
| **BLE** | **`0x0C` on re-advertise** | disconnect handler issued `FAST_ADV`, which matched neither existing stop branch |
| **Supply** | **Brown-out + half-initialised SIM** | `rst[BOR PIN] prev alive=0ms`; `ATREADY=1 CPIN=1 PBDONE=0` at 3572 mV |
| **CIPOPEN** | `11` = DNS fail, `1` = TCP timeout | `g.scstg.net` → `35.246.61.178`; server reachable from PC on :9221 but not from the device |

---

## 3. Documents produced — `Document\`

| File | Contents |
| :---- | :---- |
| `Server Protocol Specification.md` | 35,707 chars · 0 HTML tags · sections 1–11 contiguous · architecture diagram restored |
| `BLE Protocol Specification.md` | 24,623 chars · 55-byte-clean · §12 appendix (Python BLE + OTA). ⚠️ §2.2 diagram still corrupted; §5.3 vs §12 opcode conflict unresolved |
| `Code Architecture.md` | §1–§12 incl. **§12 Flow Architecture** (boot, report cycle, recovery, BLE write, DOTA, GPS, timing budget) |
| `Hardware Architecture.md` | 13 sections: block diagram, peripheral map, memory map, power, **antenna RF constraint**, modem, GNSS, EEPROM, stack budgets, 13 consolidated limitations, feature matrix, verification checklist |
| `Functional Feature Description.md` | Plain-language description of every feature + 12 missing test cases (M1–M12) |
| `Test Procedures.md` | All 55 procedures in steps + *Example check* format |
| `Setup Quickstart - Both Environments.md` | Windows (bundle + MSYS2), **macOS**, **Linux**, cross-platform table |
| `VScode steps.md` | Fixed: LaTeX arrows, heading hierarchy, added troubleshooting |

---

## 4. Tools — `Tools\`

| File | Purpose |
| :---- | :---- |
| `debug_monitor.py` | Tkinter + bleak + AES-CBC GUI, 1,391 lines, **no sidecar assets** |
| `requirements.txt` | `bleak 3.0.2`, `pycryptodome 3.23.0`, `pyinstaller 6.22.3` |
| `build_exe.ps1` | One script: build / `-RunFromSource` / `-Venv` / `-Clean` / `-Mode OneDir` / `-Version` |
| `README.md` | Develop, build, add-feature workflow, hidden-import guidance |
| `.gitignore` | Excludes venv, `.build/`, `dist/` |

**Portable exe built and smoke-tested:**
```
Tools\dist\ValtrackDebugMonitor.exe   14.66 MB   single file, no install
environment: Python 3.14.7 | bleak 3.0.2 | pycryptodome 3.23.0 | pyinstaller 6.22.3
smoke test: process stayed alive 8 s -> imports + Tk startup OK
```

---

## 5. In progress — Succorfish branding

**Requested:** official Succorfish branding — logo, tabs, frames, everything.

**Palette in use (pre-branding):** `#1f1f23` background · `#ef4444` error · `#10b981` ok ·
`#38bdf8` info · `#facc15` warn · `#f59e0b` amber · `#3b82f6` accent · `#8b5cf6` purple

**Structure to restyle:**
- `BleGuiApp` — root title `Succorfish Gigo / Valtrack BLE Debug & Config Monitor`
- `ConfigPanel` (`tk.Toplevel`) — `ttk.Notebook` with 5 tabs built by `_mk_tab()`:
  `Device Config (0xC1)` · `SIM / APN (0xC3)` · `Server (0xC4)` · `Actions & Validation` · `DOTA Update`

**Planned:**
1. `BRAND` constant block — colours, fonts, product name, version, copyright
2. Logo embedded as **base64 PNG** (loaded with `tk.PhotoImage(data=…)`, so no PIL at runtime and
   no sidecar file — keeps the exe single-file)
3. Generated logo + 256×256 icon, plus **auto-detect** `logo.png` beside the script/exe so the
   official asset can be dropped in without a code change
4. `ttk.Style` theme: custom Notebook tabs, Treeview, Scrollbar, Button, Entry, Combobox
5. Branded header bar on both windows; window icon

**Blocked on:** the logo generator failed with
`SyntaxError: (unicode error) 'unicodeescape' codec can't decode bytes ... truncated \UXXXXXXXX escape`
— Windows paths inside a PowerShell here-string passed to Python need escaping. Retry by writing the
generator to a `.py` file first rather than inlining it.

**Also blocked on:** the **official Succorfish logo file**. The generated mark is a placeholder
wordmark; the real asset should be dropped in when available.

---

## 6. Open items

| # | Item | Owner |
| :---: | :---- | :---- |
| 1 | `+CME ERROR: phone failure` on all 6 networks | SIM / Onomondo |
| 2 | GPS 0 satellites + dead backup cell | antenna / battery |
| 3 | `+CIPOPEN: 1,1` — server up, device cannot reach :9221 | APN / routing |
| 4 | `rst[BOR PIN]`, `PBDONE=0` | supply hardware |
| 5 | BLE §5.3 (reserved-empty) vs §12 (`0xA1/0xA2/0xC4/0xC5/0xE2`) conflict | document decision |
| 6 | BLE §11 / §12.4 / §10 — three different firmware-update paths documented | document decision |
| 7 | §2.2 advertising diagram still corrupted in BLE spec | regenerate as ASCII |
| 8 | `M1` diagnostics: matrix says *Not Built Yet*, server spec says *Implemented* | confirm against code |
| 9 | T2 battery LED thresholds — three conflicting accounts | read `status_led.c` |
| 10 | **Commit or stash** — 11 files uncommitted, files have silently reverted ~10× this session | developer |

---

## 7. Rebuild commands

```powershell
# firmware
cd D:\projects\valtrack_v4_mf
cmake --preset Release
cmake --build --preset Release --target Valtrack_App

# debug monitor - develop
cd Tools
.\build_exe.ps1 -RunFromSource

# debug monitor - portable exe
.\build_exe.ps1
#  -> Tools\dist\ValtrackDebugMonitor.exe
```


---

## 8. Session: 2026-11-05 — DOTA Flasher Fix & Package Maintenance

**Scope:** `BLE & Serial Monitoring` tool suite & DOTA OTA flasher.

### Issues Diagnosed & Resolved

1. **False "Upload ended but application did not become available" Error:**
   - **Symptom:** The tool streamed 100% of the binary chunks, but ended with a fatal error popup: `Upload ended but the application did not become available (finish indication was not received). The device may still be in the bootloader; check the image/SFSA and device logs.` Even though the device actually rebooted and the new application was running correctly.
   - **Root Cause 1 (Indication Drop / Timeout):** On Windows (WinRT BLE), GATT Indications sent by STM32WB upon `COMMAND_UPLOAD_FINISHED` often drop the `ValueChanged` dispatch when the MCU issues `NVIC_SystemReset()`. The 10-second timeout expired, marking `finish_confirmed = False`.
   - **Root Cause 2 (Overly Strict Verification Loop):** Immediately after closing `ota_client`, Windows WinRT radio requires 2–3s to release socket handles, and the STM32WB needs time to initialize FreeRTOS and start advertising. The 6-attempt scan loop checked strictly by low-level MAC address, and if connection failed due to Windows bonding renegotiation, it raised a fatal exception even though the device was up and advertising.
   - **Fix Applied (`monitor/config_panel.py`):**
     - Reduced finish indication wait to 6.0s with non-fatal timeout fallback.
     - Added a 3.0-second post-disconnect settling delay before scanning.
     - Implemented dual-criterion device matching (checking both BD_ADDR and advertised local name like `Succorfish Gigo` / `GIGO 4G`).
     - Added logic distinguishing between active `STM_OTA` bootloader advertisements and running application states. If 100% chunks were sent and `STM_OTA` is gone, reports success rather than false failure.

2. **Standalone Binary Rebuild & Verification:**
   - Executable regenerated via `build_exe.ps1` with Python 3.14 + Bleak 3.0.2.
   - Passed automated 8-second Tkinter startup smoke test.
   - Verified single-file standalone distribution binary: `dist/ValtrackDebugMonitor.exe` (14.87 MB).

3. **Workspace Cleanup & Modular Structure Documentation:**
   - Removed intermediate `__pycache__` artifacts, `.build` cache, and stale root `.spec` files.
   - Modernized `README.md` to document the modular `monitor/` architecture, multi-session tabs, SC2 panel, offline tests, and build instructions.

---

## 9. Session: 2026-11-14 — BLE OTA Crash Fix, GSM Time Sync, Event Timestamp Resolution & Portable Tool Build

**Scope:** `Valtrack_Boot`, `Valtrack_App`, and `Tools/BLE & Serial Monitoring`.

### Issues Diagnosed & Resolved

1. **BLE OTA "Stuck in Bootloader" Crash on Live Board (`00:80:E1:26:F1:2D`):**
   - **Symptom:** Flashing completed 100% chunks over BLE, but the device stayed trapped in `STM_OTA` mode and never booted into the application.
   - **Root Cause:**
     - In `Valtrack_Boot/STM32_WPAN/App/otas_app.c`, `OTA_FLASH_WRITE_MAX_RETRIES` was set to `1000U` (~0.55 ms). On STM32WB55, CPU2 (BLE stack) periodically holds Hardware Semaphore 7 (`CFG_HW_FLASH_SEMID`) for 2–4 ms during BLE radio events, causing CPU1 write attempts to fail prematurely.
     - On failure, remaining chunk bytes (`size_left`, up to 248 B) were copied into an 8-byte buffer (`write_value[8]`), corrupting adjacent FreeRTOS/WPAN queues (`LocalFreeBufQueue`) and causing a `HardFault_Handler` crash on CPU1 while CPU2 kept ACKing BLE packets.
     - `FD_WriteData()` could return on error without releasing HSEM 7 or locking flash.
   - **Fix Applied:**
     - Increased retry limit to `1000000U` (~550 ms) in `otas_app.c` to safely span CPU2 radio events.
     - Strictly guarded `write_value` buffer indexing against `.bss` overruns.
     - Ensured `LL_HSEM_ReleaseLock()` and `HAL_FLASH_Lock()` execute on every exit in `flash_driver.c`.
     - In `monitor/config_panel.py`, added auto-matching for target MAC and its `BD_ADDR[0] + 1` peer (`STM_OTA`), retry loop for dirty-flash reset, and direct DOTA launching from `STM_OTA` mode.
     - Verified full end-to-end BLE DOTA update cycle on live hardware.

2. **GSM Network Time Sync (`AT+CCLK?`):**
   - Added `GsmModemBootstrapClock()` in `gsm_modem.c` to query `AT+CCLK?` upon cellular network registration, parse timezone offsets, and bootstrap the MCU RTC if unset (`< RTC_BUILD_TIME`) or behind network UTC, ensuring fast time sync even before GPS gets a fix.

3. **Stale Early-Boot Event Timestamps (`1566000062000` / Aug 2019):**
   - **Symptom:** `X05ResetEvent` and `X01BatteryEvent` logged at early boot transmitted with stale default epoch `1566000062000` (Aug 2019) while `X02WaypointMessage` transmitted with the live Nov 2026 UTC epoch.
   - **Fix Applied:**
     - In `Valtrack_App/Drivers_User/Src/event_log.c`: If `RtcGetEpochTime() < RTC_BUILD_TIME`, records relative boot uptime (`DiagGetUptimeSeconds()`).
     - In `Valtrack_App/Drivers_User/Src/gsm/gsm_protocol.c`: Prior to upload, reconciles pre-sync event timestamps against current synchronized UTC epoch: `currentUtcEpoch - (currentUptime - eventUptime)`.
     - Waypoint timestamps remain 100% untouched to preserve original GPS satellite fix time and coordinates.

4. **Global Portability for `build_exe.ps1` Across Any Laptop:**
   - Multi-source Python candidate discovery (`py -3.14..-3`, PATH, user/system local dirs, winget fallback).
   - Added `Invoke-NativeCapture` to prevent PowerShell treating native tool stderr INFO logs as fatal terminating errors.
   - Automated detection and termination of running `ValtrackDebugMonitor.exe` instances before PyInstaller overwrite.
   - Self-heals broken or copied virtual environments (`.build-venv`).
   - Smoke-tested standalone 14.88 MB executable (`Tools/BLE & Serial Monitoring/dist/ValtrackDebugMonitor.exe`).


---

## Session: 2026-11-17 — `optimization_fix`

### Firmware (`Valtrack_App`)
- `gsm_a76xx.h`: Relaxed `GSM_RESPONSE_NETWORK_TIME` from `"+CCLK: \""` to `"+CCLK"` so spacing/quote variants match.
- `gsm_a76xx.c`: Re-issued `AT+CTZU=1` inside `GsmPowerOn()` so NITZ time sync stays active after low-power modem cycling.
- `gsm/gsm_modem.c`: Rewrote `gsmModemReadCclk()` with `sscanf` parsing for 2/4-digit years and optional timezone offsets.
- `app_ble_p2p.c`: Added RTC epoch read support to `BLE_OPCODE_SET_TIME` (`0x11`) when payload length `<= 1`.

### Monitor Tool (`Tools/BLE & Serial Monitoring`)
- `theme.py`: Added live PC UTC clock in `BrandHeader` and removed duplicate header title text.
- `main_window.py`: Added per-device tab close controls (button, middle-click, right-click menu) and a right-side vertical telemetry dashboard (Device Clock, GI & GPSI countdowns, GPS fix/UTC/sentences/two-line decimal coords, Battery, Low Power, Motion, LED state, Uptime).
- `constants.py`, `protocol.py`, `gigo_device.py`, `ble_engine.py`, `sc2_hub_panel.py`: Added separate `SC2 Gen2` (`nRF52840`) and `SC2 Gen3` (`nRF5340`) profiles (`0xBA00`/`0xBA01`, SC2 AES key, multiplier-75 auth, GATT auto-detection, retry `find_device_by_address()` re-discovery) and dedicated `Sc2HubConfigPanel` with Industry Mode (`0xC2`), WiFi (`0xC6`), D&T hub relay (`0xC8`/`0xC9`), SC2 GPS (`0x12`), Geofences (`0xA2`–`0xA8`), Text (`0xB1`/`0xB2`), and SC2 DOTA (`0xBA02`).

---

## Session: 2026-11-21 — Multi-device monitor v2.0, fixed-rate reporting, 32-bit intervals

### Firmware (`Valtrack_App`)
- `app_sm.c`: timed reports at a **fixed rate** (`appSmRearmReportingTimer()`): the next report is due one GSM interval
  after the cycle START. Before, the one-shot timer was re-armed with a whole interval after the GPS fix and upload, so
  GI 900 s gave about 19 minutes between reports. New log line `Report timer: next report in N s (...)`.
- `app_ble_p2p.c` / `app_ble.h`: 0xC1 extended frame, GSM and GPS intervals as u32 at bytes 22..29 (write 28 bytes;
  read with selector 0xA5), so 86400 s (1 day) can be set and read over BLE. 16-bit fields read 0xFFFF when too long.
- `gsm_protocol.c`: 0xC4 configuration report saturates the 16-bit GPS interval (86400 used to read as 20864).
- `-Wconversion` scan of all user sources: no other truncating conversion; the size problems were the wire fields.

### Monitor (`Tools/BLE & Serial Monitoring`) v2.0
- New `device_profiles.py` (7 device types, detection by advertisement, GATT and probing) and `config_codecs.py`
  (all layouts and ranges, unit tested), `gigo_family_panel.py` (Delphis, Rotation/Jack), tests in `tests/`.
- Bugs fixed: 86400 in a 16-bit field crashed the write (OverflowError); SC2 0xC1 used the Valtrack layout and wrote
  big-endian (SC2 reads little-endian); SC2 server port and UTC time written big-endian (SC2: little-endian); D&T
  device-control replies parsed with a status byte they do not have (config fields shifted by one, rec-mode and reset
  reported FAILED); D&T config write sent 0xFFFF as "keep" voltage (stored as 65.535 V; the logger keeps on 0);
  System tab sent 0xD1..0xD3, which no firmware implements; live panel assumed GI/GPSI 900 s when unknown and was not
  updated after a config write or an SMS/server change.

---

## Session: 2026-11-22 — Monitor v2.1: protocols audited against every sibling firmware

Every device's BLE code was read in the sibling repositories (Delphis, Rotation/Jack beacon, Depth & Temp, gas sensor,
SC2 Gen2 / Gen3 STM32 and BLE co-processors, camera image link) and compared with what the tool sends.

### Bugs fixed
- Delphis and the beacon use command characteristic **0xAB01** (their 03 documents say 0xAB05). The tool listed 0xAB01
  as a "legacy Valtrack" characteristic, so both devices were connected and driven as a Valtrack.
- SC2 STM32 DOTA could never succeed: CRC-8 with polynomial 0x07 instead of CRC-8/MAXIM, CRC-16/CCITT instead of
  CRC-16/MODBUS, and the image CRC sent at payload byte 4 instead of 0; acknowledges were not checked.
- SC2 SIM write sent 0xFF in the URAT and profile bytes as "keep": the SC2 resets URAT to its default and stores
  profile 0xFF. Now read-modify-write.
- D&T: duration 0 offered as "continuous" (the logger refuses it; endless is recording-mode bit 2); "Error log"
  always failed (read a key the decoder never returns); no recording-mode write.
- SC2 window: geofence, Wi-Fi and text-message buttons removed (compiled out / no handler / dropped on Gen2).

### New
- GiGO Gas logger (BME690) support: detection, `gas_device.py`, `gas_panel.py`, protocol copied from its repository.
- SC2: structured industry mode, D&T logger list and recording plan, GPS fix and live tracking, SIM type.
- Delphis / beacon: IMU live stream (floats are big-endian on the wire, the documents say little-endian), ranges,
  magnetometer, recalibration, register access, defaults; Delphis nano-modem console.
- Notification listeners in `Gigo4gDevice` for unsolicited frames (IMU stream, modem lines, GPS fixes).
- Tests: 51 offline tests, incl. DOTA run through a model of the firmware path.

### Not verified on hardware
Every change above is checked against the firmware source and offline tests only.

---

## Session: 2026-11-22 (2) — Monitor v2.2: one tool for every application

Valtrack V1, Delphis (MGT) and the Rotation / Jack beacon all advertise "Succorfish Gigo" with different services and
commands; NetTag and GIGO_4G are retired. Each device tab now shows what its application needs.

### Detection
- Scan list tells Valtrack (manufacturer company id 0x8301: ST SDK byte 0x01 + CFG_DEV_ID_P2P_SERVER1 0x83) from the
  nRF devices (0x0059) and names the kind; NetTag / GIGO_4G presets and name matching removed; "Succorfish Gas" preset.

### Live views (live_views.py, charts.py, motion_analysis.py)
- GPS map only for Valtrack and the SC2 hubs; SC2 0x12 GPS fixes (requested in the SC2 window or live tracking) go on
  the map too.
- Delphis: attitude indicator, IMU values, nano-modem lines, attitude chart.
- Beacon: reads mode 0xB8 and tuning 0xBA, then replays rotation.c (drum dial, count to the rotation count, suppressed /
  advertising) or jack.c (moving / stationary against the sensitivity limits) on the 0xB2 stream. The beacon runs both
  every 500 ms and streams the sample of the same tick (main.c), so the replay follows its decisions.
- D&T: 0x12 snapshot every 1..30 s (depth, P1, TOB1 = logged temperature, T), logger state, depth + temperature chart.
- Gas: shared live stream, IAQ with its Bosch band, all air values, chart of a chosen pair.
- Side panel per device type (GPS / report lines only for the trackers); live views resume after an auto-reconnect.
- Logger windows: D&T "Live chart" and "Records plot" tabs (download plots, then saves; Open CSV); gas window live tab
  is the same view, Records tab plots downloads and CSV files.

### Bugs fixed
- D&T window: "Read pressure / temperature" failed on a missing key (ret_code / depth_m); "Read all" only showed hex
  (0x12 now decoded); the CSV save dialog ran on the BLE thread.
- D&T / gas transport had no command lock: two windows (or a window and the tab) could take each other's replies.
  Gas stream frames could be taken as the reply of the next command.
- Listeners were one per opcode: a window and the tab could not both receive the IMU / gas / GPS frames, and closing
  one window stopped the other's stream. Now several listeners and stream owners (the last one stops the stream).
- Depth: the logger's own formula (P - 0.061 bar) / 0.09807 bar/m instead of P x 10.197.
- The gas protocol copy check had been skipping since the gas project moved the file from tools/ to tests/.
- Valtrack, SC2 and GiGO windows: tab titles were cut off (wider windows, shorter titles).

### Verification
73 offline tests (22 new), pyflakes clean except the old `eol` variable, GUI smoke of all 8 tab variants and 5 windows
with fake data and screenshots. Nothing tested on hardware.

---

## Session: 2026-11-22 (3) — Monitor v2.3: Delphis acoustic link, device roles

### Findings (firmware source, schematics, NM3 manual)
- IMUs: ICM-20948 on SC2 Gen2 / Gen3 (Gen3 0x69, switched 1.8 V), on the beacon REV5 board (D&T, gas) and Delphis;
  Valtrack MMA8652. Only Delphis and the beacon firmware use theirs over BLE. SC2: `ACCEL_FEATURE_ENABLE` never defined
  and no driver, so the server's orientation report (0xC7) carries zeros; no BLE path. Plan:
  `Plans/SC2_Orientation_over_BLE.md`. D&T / gas / Valtrack IMUs are not offered.
- Roles: Delphis `nanoModemEnabled` (default on) / `imuEnabled` (default off), beacon IMU enable + mode decide what
  the device does; nothing enabled = BLE advertising only.
- Delphis relay: BLE writes over 64 bytes are dropped -> modem commands <= 48 bytes; received lines cut at '#' / '$'
  and ended at CR LF; 0xA1 = modem off OR busy; the 0xA0 length byte is the true length (zero bytes are data).

### New
- `acoustic_link.py`: NM3 command builders with the Delphis limits, reply parser (manual examples), line splitter
  that keeps binary #U / #B messages whole, per-session link (message log, picture reassembly and saving, one
  command at a time, $M retries for pictures).
- `acoustic_panel.py`: GiGO window tabs "Acoustic messages" and "Picture transfer" (Delphis).
- `img_protocol.py` copied verbatim from succorfish-Camera (identity test), `latent_codec.py` +
  `latent_codec_worker.py` (optional D128 encode / decode in a child process; verified with the real models: 256 B
  latent, picture back in about 3 s, PSNR 20.6 dB), `user_settings.py`.
- Roles in the GiGO window, side panel and live view; the live view asks for the IMU stream only when it is enabled.

### Fixed
- 0xA0 lines were stripped of trailing zero bytes (binary data); 0xA1 text said "powered off" for "busy" too.
- onnxruntime imported into the monitor crashed it (access violation): moved to a child process.

### Verification
88 offline tests (13 new), pyflakes clean except the old `eol` variable, GUI construction of all windows with
simulated modem traffic (messages, status, a whole picture through the receive path). Screenshots were not possible
(screen locked). Nothing tested on hardware.
