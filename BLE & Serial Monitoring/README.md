# Succorfish BLE Device Monitor (v2.3) + DOTA

A Tkinter desktop tool for engineering diagnosis, live logging, configuration and Over-The-Air (DOTA) firmware updates
over **BLE** and **Serial (UART/USB)** for every Succorfish / GiGO product family. Each device tab shows what that
application needs: the GPS map for the trackers, the attitude / drum / jack display for the IMU devices, depth and
temperature charts for the D&T logger and the air values for the gas logger.

---

## 1. Supported devices

| Device type | Hardware | BLE service / command char | Detected by | Configuration window |
| :--- | :--- | :--- | :--- | :--- |
| **Valtrack V1** | STM32WB55 + A7672 + GNSS | 0xFE40 / 0xFE41 | GATT | Valtrack: 0xC1 (incl. 32-bit intervals), 0xC3, 0xC4, system commands, DOTA |
| **Valtrack 2** | STM32WB55 + SIM7672G (same `Valtrack_App`) | 0xFE40 / 0xFE41 | GATT (pick "Valtrack 2" to label it) | as Valtrack V1 |
| **SC2 Gen2** | STM32 + nRF52840 | 0xBA00 / 0xBA01 | GATT + scan data (BLE FW < 7) | SC2 hub: 0xC1 (SC2 layout), industry 0xC2, SIM / server 0xC3 / 0xC4, D&T loggers 0xC8 / 0xC9, GPS 0x12 / 0xE3, DOTA 0xBA02 |
| **SC2 Gen3** | STM32 + nRF5340 | 0xBA00 / 0xBA01 | GATT + scan data (BLE FW >= 7) | as SC2 Gen2 |
| **GiGO Delphis** (MyGearTag) | nRF52 + ICM-20948 + NM3 acoustic nano-modem | 0xAB00 / **0xAB01** | GATT 0xAB01 + probe (no answer to 0xB8) | GiGO: config 0x06/0x05 (role), battery, self test, IMU (live stream, ranges, magnetometer, calibration, registers), **acoustic messages** (unicast / with acknowledge / broadcast, ping and range, remote battery, noise, console) and **picture transfer** (send / receive, optional encode / decode), reset, defaults 0x07 |
| **GiGO Rotation / Jack movement** beacon | nRF52 + ICM-20948 | 0xAB00 / **0xAB01** | GATT 0xAB01 + probe (answers 0xB8) | as Delphis without the modem, plus movement mode 0xB7/0xB8 and tuning 0xB9/0xBA |
| **GiGO Depth & Temp** logger | nRF52805 + Keller sensor | 0xAB00 / 0xAB05 | GATT 0xAB05 + name "Succorfish D&T" (else probe 0x34) | Depth & Temp logger window (Keller sensor, recording, records, CSV) |
| **GiGO Gas** logger | nRF52 + Bosch BME690 | 0xAB00 / 0xAB05 | GATT 0xAB05 + name "Succorfish Gas" (else probe 0x34) | Gas logger window: device info, live T / RH / P / gas / IAQ / CO2 eq. (read or stream), logging set-up, record download to CSV with acknowledge, clear / format |

Valtrack V1 / 2, GiGO Delphis (MyGearTag) and the Rotation / Jack beacon all advertise **"Succorfish Gigo"**; their
services and commands differ. The scan list already tells Valtrack (manufacturer company id 0x8301) from the two nRF
devices (Nordic id 0x0059 with battery and firmware); Delphis and the beacon are told apart after connecting. NetTag and
"GIGO_4G" devices are no longer in use and have no filter preset.

*Device type* in the main window is **Auto-detect** by default: the type is guessed from the advertisement, confirmed
from the GATT table after connecting (the command characteristic splits the GiGO devices: 0xAB01 Delphis / beacon,
0xAB05 D&T / gas; the Delphis and beacon documents say 0xAB05, their code uses 0xAB01) and decided by probing an
opcode only one device of the pair answers. Pick a type only where the device cannot tell (Valtrack V1 vs 2, an SC2
without scan data); a choice that contradicts the GATT table is reported and the device is detected instead.

Every byte layout was traced from that device's firmware **source** (sibling repositories; where a document differs,
the code wins) and lives in pure codec modules with offline unit tests: `config_codecs.py` (configuration frames),
`sc2_hub_codecs.py` (SC2 industry mode, D&T relay, GPS, DOTA), `gigo_imu_codecs.py` (IMU, nano-modem) and
`gas_protocol.py` (a verbatim copy of `gigo-gas-sensors/tests/gas_protocol.py`, checked identical by a test).

## 2. Project structure

```
BLE & Serial Monitoring/
├── debug_monitor.py            # launcher
├── sc2_protocol.py             # GiGO Depth & Temp logger protocol (historic name)
├── brand_assets.py             # embedded logos and icon
├── build_exe.ps1, requirements.txt
├── monitor/
│   ├── device_profiles.py      # registry of the 8 device types: UUIDs, keys, auth, detection rules
│   ├── config_codecs.py        # every config/status frame as pure encode/decode + range checks
│   ├── sc2_hub_codecs.py       # SC2 0xC2, 0xC8/0xC9, 0x12, DOTA CRCs / frames / image check
│   ├── gigo_imu_codecs.py      # Delphis / beacon IMU stream, ranges, registers, nano-modem frames
│   ├── gas_protocol.py         # gas logger protocol (verbatim copy from gigo-gas-sensors/tests)
│   ├── ble_engine.py           # sessions, scan, connect loop, GATT detection + GiGO probing
│   ├── gigo_device.py          # command channel (AES framing, listeners) for Valtrack, SC2, Delphis, beacon
│   ├── gas_device.py           # gas logger channel (D&T transport + 0x30..0x37)
│   ├── main_window.py          # main window, device type selector, per-device live panel
│   ├── config_panel.py         # Valtrack window (0xC1/0xC3/0xC4, system commands, STM_OTA DOTA)
│   ├── sc2_hub_panel.py        # SC2 Gen2 / Gen3 window
│   ├── gigo_family_panel.py    # GiGO Delphis / Rotation-Jack window
│   ├── sc2_panel.py            # GiGO Depth & Temp logger window
│   ├── gas_panel.py            # GiGO Gas logger window
│   ├── live_views.py           # per-application live view of a device tab + record plots of the logger windows
│   ├── motion_analysis.py      # beacon rotation.c / jack.c replayed on the IMU stream
│   ├── charts.py               # time-series chart on a Tk canvas (no matplotlib)
│   ├── acoustic_link.py        # Delphis NM3 commands / replies, message log, picture send / receive (per session)
│   ├── acoustic_panel.py       # GiGO window tabs: acoustic messages, picture transfer
│   ├── img_protocol.py         # picture protocol (verbatim copy from succorfish-Camera)
│   ├── latent_codec.py, latent_codec_worker.py  # optional picture encode / decode in a child process
│   ├── user_settings.py        # remembered paths (picture models)
│   ├── gps_map_widget.py, ota_image.py, protocol.py, logbus.py, theme.py, constants.py, serial_engine.py
└── tests/
    ├── test_codecs.py          # every layout, range check, log parser and detection rule (offline)
    ├── test_device_sim.py      # Gigo4gDevice against a simulated Valtrack 0xC1 handler (new and old firmware)
    ├── test_hub_and_imu.py     # SC2 frames + DOTA through a model of the firmware path, IMU, nano-modem, gas
    ├── test_live_views.py      # rotation / jack replay, chart maths, D&T snapshot, shared streams, detection
    └── test_acoustic.py        # NM3 commands / replies, picture round trip through the receive path, retries, roles
```

Plans: `Plans/SC2_Orientation_over_BLE.md` (SC2 IMU orientation over BLE: firmware and monitor tasks).

## 3. Key features

### Configuration that cannot silently go wrong
- **Range checks before sending.** Each field carries its accepted range from the firmware (for example Valtrack GSM
  interval 0 or 5..86400 s, SC2 GSM timeout 60..300 s). A value that does not fit is refused with a message naming the
  field; nothing is cut to its low bits.
- **Day-long intervals on Valtrack.** The 0xC1 16-bit fields cannot hold 86400 s. The tool uses the extended 0xC1 frame
  (firmware 2026-11-21: u32 intervals at bytes 22..29, read selector 0xA5). Intervals accept `900`, `15m`, `2h`, `1d`.
  Older firmware ignores the extra bytes; the read-back after a write reports "written but not applied".
- **Only changed fields are written**, and a write is verified by reading back (Valtrack). Where the device resets to
  apply a change (Valtrack GSM/GPS interval, SC2 0xC1, GiGO config) the tool says so and re-reads after reconnecting.
- **Byte order per device:** SC2 0xC1/0xC4/0x11 SET frames are little-endian, its replies big-endian; GiGO voltages are
  written little-endian and read big-endian (D&T little-endian both ways); GiGO flags have no "keep" value, so a GiGO
  write needs a read first. Earlier builds got several of these wrong (see Work Log, 2026-11-21).

### Live monitoring (Valtrack and SC2)
- Multi-session tabs with decrypted debug stream, colour-coded milestones, regex search, auto-reconnect.
- Per-device live panel: phase (GPS fix / GSM state / idle), GI and GPSI countdowns, GPS fix result, battery, low
  power, motion, LEDs, device clock, uptime, satellite map.
- **Report schedule check:** the GI countdown follows the firmware's `Report timer: next report in N s` line, and from
  the second cycle the panel shows the **measured gap** between report cycles next to the configured GI (green when it
  holds, amber when it drifts). Configuration the firmware prints at each cycle (also after an SMS or server change)
  updates the panel without a manual read.

### Live view per application (device tab)
Nothing starts on connect (streams and sensor polling cost battery): press **Start** in the tab. A view that was
running starts again after an auto-reconnect. Streams are shared with the device windows, so closing a window never
stops the tab's view and the other way round. Charts show UTC, a hover read-out, and save their samples as CSV.

| Device | Live view | Data |
| :--- | :--- | :--- |
| Valtrack V1 / 2 | GPS map + report-cycle panel (GI / GPSI countdowns, measured gap, GPS, low power, motion, LEDs) | debug log 0xFD |
| SC2 Gen2 / Gen3 | GPS map (fixes from the debug log and every 0x12 GPS fix / live-tracking frame) + report panel | 0xFD, 0x12 |
| GiGO Delphis | attitude indicator (roll, pitch, heading), accel. / rate / magnetometer values, last nano-modem lines, attitude chart | 0xB2 stream, 0xA0 / 0xA1 |
| GiGO Rotation / Jack, net-drum mode | drum dial (turns from the rate about z), the beacon's own count towards its rotation count, advertising / suppressed, rate and turns chart | 0xB8 mode, 0xBA tuning, 0xB2 |
| GiGO Rotation / Jack, jack mode | MOVING / STATIONARY, acceleration and turn-rate bars against the jack-sensitivity limits, movements and time moving, chart with the limits | 0xB8, 0xBA, 0xB2 |
| GiGO Depth & Temp | depth (as the logger computes it), pressure, logged temperature (TOB1), T sensor, logger state; depth (axis down) + temperature chart | 0x12 every 1..30 s, 0x26 |
| GiGO Gas | IAQ with its Bosch band, CO2 eq., temperature, humidity, pressure, gas resistance, static IAQ, battery; chart of a chosen pair | 0x31 stream, 0x30 |

The beacon sends neither its rotation count nor the jack state: it runs rotation.c / jack.c every 500 ms and streams
the IMU sample of the same tick, so `motion_analysis.py` replays those two files on the stream. The counts match the
beacon's from the moment both start together (beacon reset or mode write); the beacon streams nothing in mode
"disabled". The side panel of a GiGO tab keeps only what applies (no GPS / report lines) plus a one-line summary.

### Device roles (GiGO configuration flags)
Delphis and the beacon keep the flags of the original combined "GIGO beacon + MyGearTag" firmware, and the flags decide
what the device does: Delphis *nano-modem enabled* -> acoustic modem (messages, pictures), *IMU enabled* -> attitude;
beacon *IMU enabled* + mode 1 / 2 -> rotation (net drum) / jack detection; nothing enabled -> the device only
advertises. The role is shown after a configuration read (GiGO window), in the device tab's side panel at connect and
in the live view, which asks for the IMU stream only when the IMU is enabled. IMUs that a firmware does not use (SC2,
D&T, gas, Valtrack's accelerometer) are not offered; for SC2 see `Plans/SC2_Orientation_over_BLE.md`.

### Delphis acoustic link (`acoustic_link.py`, `acoustic_panel.py`)
The session's link listens from the moment Delphis connects (window open or not): messages and pictures are never
missed. Delphis passes modem commands of up to 48 bytes (it drops BLE writes over 64 bytes), so a message part
carries 41 characters (44 broadcast); longer text is sent in parts. Text must be printable ASCII without `#` and `$`
(a receiving Delphis cuts lines there).
- **Acoustic messages** tab: modem status ($?: address, supply, firmware), own address ($A), wake, noise ($N), link
  quality on/off ($XQ), ping with range ($P; range = counts x 1500 m/s x 3.125e-5), the other modem's battery ($V),
  messages as $M (acknowledged, with range), $U or $B, conversation log, raw console with every modem line.
- **Picture transfer** tab: the camera project's protocol (`img_protocol.py`, a verbatim copy of
  succorfish-Camera/.../gigo_python_script/img_protocol.py, checked identical by a test): OFFER / DATA / DONE frames
  with CRC-16, byte stuffing and CRC-32, sized for Delphis (48-byte $M commands, about 8-10 frames for a 256-byte
  picture), each frame repeated up to 7 times until acknowledged. Received pictures are listed, saved as `.latent` in
  Documents\Succorfish BLE Monitor\Delphis\<date>, decoded and saved as PNG when the codec is available.
- **Picture codec (optional):** D128 latent (256 B) <-> 256 x 256 picture with `d128_encoder.onnx` /
  `d128_decoder.onnx` (about 104 / 232 MB, chosen once with the model buttons) and numpy + onnxruntime + Pillow. The
  models run in a separate Python process (`latent_codec_worker.py`): importing onnxruntime into the monitor crashed it
  next to tkinter / bleak. About 3 s per picture; source version only (the exe has no Python for it).

### Record plots (logger windows)
- **Depth & Temp window:** tabs "Live chart" (the tab's view) and "Records plot": the downloaded records (depth axis
  down, temperature; or pressure / battery) are plotted, then offered for saving as CSV; "Open CSV..." plots a saved
  file (depth recomputed from the pressure, so files of older builds plot the same).
- **Gas window:** "Live values" is the tab's view; the Records tab plots the downloaded records (temperature /
  humidity, IAQ / battery, pressure, gas resistance) and saved CSV files.
- Depth is computed like the logger's state detector: (P - 0.061 bar) / 0.09807 bar/m, never below 0 (earlier builds
  used P x 10.197 and showed 0.6 m at the surface).

### Per-device features (all traced from the firmware)
- **SC2 Gen2 / Gen3:** 0xC1 device configuration; industry mode 0xC2 (garage mode, GIGO / D&T report channels); SIM
  0xC3 incl. SIM type, written read-modify-write because the SC2 stores 0xFF in the modem-mode / profile bytes instead
  of keeping them; server 0xC4; the Depth & Temp loggers the hub collects from (address list 0xC8, per-logger
  recording plan 0xC9); GPS fix 0x12 and live tracking 0xE3; firmware, IMEI, battery, UTC time, debug log, reset,
  token; an expert box limited to the opcodes the BLE co-processor forwards. Not offered because the firmware has no
  working handler: geofences (compiled out), Wi-Fi 0xC6 (no Wi-Fi), app text messages 0xB1 (dropped on Gen2).
- **GiGO Delphis / Rotation-Jack:** live IMU stream (12 values every 0.5 s), measuring ranges, magnetometer init,
  recalibration + save, ICM-20948 register access; Delphis nano-modem console (lines to and from the acoustic modem,
  log saved to a file); reset to defaults.
- **GiGO Depth & Temp / Gas:** logger windows; durations must be 600..864000 s (both loggers refuse 0; endless logging
  is recording-mode bit 2).

### Firmware updates
- Valtrack: STM32WB BLE OTA (0xFE20) with vector-table and end-tag image checks, MTU negotiation, double-word pacing.
- SC2: STM32 DOTA relay over 0xBA02 with the firmware's checks: CRC-8/MAXIM per slice (each slice acknowledged,
  retried on a CRC error), CRC-16/MODBUS of the image in the final step, the image's device name ('Succorfish SC2
  GEN2' / 'GEN3' at the pointer at 0x188) checked before anything is sent. Earlier builds used other CRCs and put the
  CRC at the wrong offset, so the hub refused every image.

## 4. Quick start (Windows & macOS — zero setup)

Works on **Windows 10/11 (x64)** and **macOS (Apple Silicon & Intel)** with no manual Python or package setup:

- **macOS (double-click, zero setup):** double-click **`Start_Monitor_Mac.command`** (or run `bash Start_Monitor_Mac.command` in Terminal). On first launch it automatically downloads a self-contained portable Python 3.12 with Tcl/Tk into `.portable-env/` and installs `bleak`, `pyserial`, and `pycryptodome`. Subsequent launches start immediately offline.
  - When prompted by macOS, allow **Bluetooth** access for Terminal / the app in *System Settings → Privacy & Security → Bluetooth*.
- **Windows (double-click, zero setup):** double-click **`Start_Monitor_Windows.bat`** (or run `.\build_exe.ps1 -RunFromSource`).
- **Any OS via single portable Python launcher:**

```bash
python3 portable_run.py          # macOS / Linux (auto-prepares portable runtime on first run)
python portable_run.py           # Windows
python3 portable_run.py --check  # verify runtime without opening the GUI
```

Manual run (if you already manage your own Python 3.10–3.14 environment):

```powershell
pip install -r requirements.txt
python debug_monitor.py
```

Offline tests (no hardware):

```powershell
python -m unittest discover -s tests -v
```

## 5. Building standalone apps (Windows `.exe` & macOS `.app`)

**Windows:**
```powershell
.\build_exe.ps1                  # dist\ValtrackDebugMonitor.exe, 8 s smoke test
.\build_exe.ps1 -Mode OneDir     # folder build
.\build_exe.ps1 -Version 2.0.0   # stamp file version
.\build_exe.ps1 -Clean           # remove .build, dist and the venv
```

**macOS:**
```bash
chmod +x Start_Monitor_Mac.command build_mac.sh
./build_mac.sh                   # dist/ValtrackDebugMonitor.app (with Bluetooth Info.plist keys)
./build_mac.sh --run             # run from source via portable Python
./build_mac.sh --clean           # remove .portable-env, .build and dist
```

## 6. Security and protocol notes

- After authentication every payload is AES-128-ECB in 16-byte blocks; opcode and length stay plain. Keys and the
  challenge transforms (multiplier 79: Valtrack and GiGO; 75: SC2 hub) are in `constants.py` / `protocol.py`.
- DOTA reboot requests are sent only on authenticated links.
- The tool never sends an opcode a device type does not implement (the old 0xD1..0xD3 buttons were removed: the
  Valtrack firmware never had them; motion, low power, log erase are 0xC1 fields, LEDs 0xFC, live position 0xE3).
