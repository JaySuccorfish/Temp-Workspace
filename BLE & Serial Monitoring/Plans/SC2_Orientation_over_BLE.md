# Plan: SC2 Gen2 / Gen3 orientation (IMU) over BLE, testable from the monitor

Status: **plan, nothing implemented** (2026-11-22). Needs firmware work on three chips per generation; the monitor
side is small and listed at the end.

## 1. Where things stand (firmware source)

| Item | SC2 Gen2 | SC2 Gen3 |
| :--- | :--- | :--- |
| IMU on the board | ICM-20948 (`SC2_GEN2_SC4_..._HW_0.3/0.4.pdf`, "ACCELEROMETER") | ICM-20948 at I2C **0x69**, its own **1.8 V** supply that can be switched off; the I2C pull-ups are lost when it is off (extra pull-ups fitted); interrupt must be configured active-low (note on `SC2_gen3_board_birng_up.PDF`) |
| Driver | none: `AccelInit()` / `AccelGetXoutput()` are called but no driver file exists | none |
| Feature switch | `ACCEL_FEATURE_ENABLE` used in `system.c`, `diag_feature_test.c` ("TEST ACC"), `server_protocol.c`, **never defined** | not used |
| Server report | `CMD_REQ_DEVICE_ORIENTATION` -> `ServerSendOrientation()`: report type **0xC7**, `{msgType, correlationId[4], int32 x, y, z}` in 32 bytes; x/y/z filled only with `ACCEL_FEATURE_ENABLE` -> **zeros today** | same command and report, x/y/z never filled -> **zeros today** |
| BLE | no opcode; 0xB1 / 0xB2 are text message / LoRa bypass on SC2 (the Delphis IMU opcodes cannot be reused) | same |

So today the orientation the server asks for is always 0, 0, 0, and nothing on BLE can show it.

## 2. Proposed BLE command 0xC7 "orientation" (same number as the server report)

Phone -> SC2: `[0xC7][len][AES( 0x00 )]` (GET, like the other SC2 config GETs).
SC2 -> phone, decrypted, big-endian like every SC2 GET reply:

| Byte | Field | Notes |
| :--- | :--- | :--- |
| 0 | 0xC7 | opcode |
| 1 | length | 21 |
| 2 | status | 0 ok, 1 IMU disabled / not fitted, 2 read failed (I2C), 3 not calibrated |
| 3..6 | x | int32, milli-g (same values as the server report) |
| 7..10 | y | int32, milli-g |
| 11..14 | z | int32, milli-g |
| 15..16 | roll | int16, 0.1 degree, from gravity |
| 17..18 | pitch | int16, 0.1 degree |
| 19..20 | temperature | int16, 0.01 degC (ICM-20948 die), optional |
| 21..22 | sample age | uint16, ms since the sample was taken |

Optional later: `[0xC7][len][AES(0x01, period_s)]` to stream it every period while connected (0 = stop), sent as
unsolicited 0xC7 frames; the monitor's listener mechanism already handles that pattern (SC2 0x12 GPS).

## 3. Firmware tasks

STM32 (Gen2 `SC2_Gen2/`, Gen3 `SC2_Gen3/`):
1. ICM-20948 driver in `User-Drivers/` (user module, style guide): power the 1.8 V rail, wait, WHO_AM_I 0xEA, wake,
   accelerometer range +-2 g, read x/y/z; power down after the read (battery). Gen3 I2C address 0x69, Gen2 from the
   schematic.
2. Define `ACCEL_FEATURE_ENABLE` (Gen2) and add the same switch to Gen3, so the server's 0xC7 report and "TEST ACC"
   finally carry real values.
3. `ble_cmd_dispatch.c`: handler for 0xC7 building the reply above (reuse the read of step 1; roll / pitch =
   atan2 from gravity).

BLE co-processor:
4. Gen2 nRF52840 `SC2_gen2_BLE/ble_sc2_bridge.c`: add 0xC7 to the forwarded opcode group (phone -> STM32 and back).
5. Gen3 nRF5340 `SC2-Gen3-BLE-VsCode/SC2_gen3_BLE/src/sc2_opcodes.h` + `sc2_cmd.c`: same.

## 4. Monitor tasks (when the firmware has it)

1. `sc2_hub_codecs.py`: `sc2_orientation_request()` / `sc2_decode_orientation()` + unit tests with the table above.
2. `gigo_device.py`: `sc2_read_orientation()`; add 0xC7 to `SC2_FORWARDED_OPCODES`.
3. SC2 window, System tab: "Orientation (0xC7)" button with x / y / z, roll / pitch; device tab: the attitude
   indicator of `live_views.py` fed from 0xC7 (one read per Start press, or the stream).
4. Hardware test: device flat (z about +1000 mg), on each side (x / y about +-1000 mg), upside down; compare with the
   server's 0xC7 report for the same position; switch the IMU rail off -> status 1.
