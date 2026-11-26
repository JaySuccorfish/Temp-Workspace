"""@file gigo_imu_codecs.py
@brief GiGO Delphis / Rotation-Jack beacon frames: IMU stream, full scale, register access, nano-modem relay
@version 1.0
@date 2026-11-22

Pure encode / decode functions (no BLE, no Tk), unit tested in tests/test_hub_and_imu.py. Traced from
gigo-MGT-Delphis imu_icm20948.c / app_protocol.c / nanomodem.h and gigo-beacon-rotation-jack-VSCode icm20948.c /
app_protocol.c. The IMU commands are answered only while the IMU is enabled in the 0x06 configuration.
"""
import math
import struct


# ---------------------------------------------------------------------------------------------------------------------
# 0xB1 stream on / off -> [0xB1][1][1]; then 0xB2 frames every 500 ms: 12 floats (acceleration xyz in g, angular
# velocity xyz in deg/s, magnetometer xyz in uT, roll, pitch, yaw in deg). The firmware swaps every float before
# sending (swapEndianness, PYTON_TEST off), so they arrive BIG-endian although the documents say little-endian; a
# test build (PYTON_TEST on) sends them little-endian, which is detected below.
# ---------------------------------------------------------------------------------------------------------------------
IMU_STREAM_START = 1
IMU_STREAM_STOP = 0
IMU_STREAM_FLOATS = 12
IMU_STREAM_BYTES = IMU_STREAM_FLOATS * 4
IMU_STREAM_KEYS = ("acc_x", "acc_y", "acc_z", "gyro_x", "gyro_y", "gyro_z", "mag_x", "mag_y", "mag_z",
                   "roll", "pitch", "yaw")
IMU_PLAUSIBLE_LIMIT = 1.0e5


def _plausible(values: tuple) -> bool:
  """Every value finite and of a physical size (a wrong byte order gives huge or NaN numbers)."""
  return all(math.isfinite(value) and abs(value) < IMU_PLAUSIBLE_LIMIT for value in values)


def decode_imu_stream(packet: bytes) -> "dict | None":
  """Decode a decrypted 0xB2 frame -> dict of IMU_STREAM_KEYS (+ 'byte_order'), or None when it is not one."""
  if len(packet) < 2 + IMU_STREAM_BYTES or packet[0] != 0xB2:
    return None
  body = packet[2:2 + IMU_STREAM_BYTES]
  values = struct.unpack(">12f", body)
  order = "big"
  if not _plausible(values):
    little = struct.unpack("<12f", body)
    if _plausible(little):
      values, order = little, "little"
  result = {key: round(value, 4) for key, value in zip(IMU_STREAM_KEYS, values)}
  result["byte_order"] = order
  return result


# 0xB4 set [acc index][gyro index] (index >= 4 = keep) -> [0xB4][1][1]; 0xB5 -> [init status][acc index][gyro index].
IMU_ACC_SCALES = {0: "+-2 g", 1: "+-4 g", 2: "+-8 g", 3: "+-16 g"}
IMU_GYRO_SCALES = {0: "+-250 deg/s", 1: "+-500 deg/s", 2: "+-1000 deg/s", 3: "+-2000 deg/s"}
IMU_SCALE_KEEP = 0xFF


def decode_imu_full_scale(packet: bytes) -> "dict | None":
  """Decode a decrypted 0xB5 reply -> {'init_ok', 'acc_index', 'gyro_index'}."""
  if len(packet) < 5 or packet[0] != 0xB5:
    return None
  return {"init_status": packet[2], "acc_index": packet[3], "gyro_index": packet[4]}


def encode_imu_full_scale(acc_index: "int | None", gyro_index: "int | None") -> bytes:
  """0xB4 payload; None keeps that range (the firmware ignores an index >= 4)."""
  for value, table, label in ((acc_index, IMU_ACC_SCALES, "accelerometer"), (gyro_index, IMU_GYRO_SCALES, "gyro")):
    if value is not None and value not in table:
      raise ValueError(f"{label} range index {value} is not 0..3")
  return bytes([IMU_SCALE_KEEP if acc_index is None else acc_index, IMU_SCALE_KEEP if gyro_index is None else gyro_index])


# 0xB0 raw ICM-20948 register access: read [1][count][bank][register] -> register bytes after frame byte 5;
# write [2][1][bank][register][value] -> [0xB0][1][1]. Reads are limited because the reply buffer is 64 bytes.
IMU_REGISTER_READ = 1
IMU_REGISTER_WRITE = 2
IMU_REGISTER_MAX_READ = 16


def encode_imu_register_read(bank: int, register: int, count: int) -> bytes:
  """0xB0 read payload (count 1..16, bank 0..3)."""
  if not 1 <= count <= IMU_REGISTER_MAX_READ:
    raise ValueError(f"read count {count} is not 1..{IMU_REGISTER_MAX_READ}")
  if not 0 <= bank <= 3 or not 0 <= register <= 0x7F:
    raise ValueError("bank must be 0..3 and register 0x00..0x7F")
  return bytes([IMU_REGISTER_READ, count, bank << 4, register])


def encode_imu_register_write(bank: int, register: int, value: int) -> bytes:
  """0xB0 write payload."""
  if not 0 <= bank <= 3 or not 0 <= register <= 0x7F or not 0 <= value <= 0xFF:
    raise ValueError("bank must be 0..3, register 0x00..0x7F and value 0x00..0xFF")
  return bytes([IMU_REGISTER_WRITE, 1, bank << 4, register, value])


def decode_imu_register_read(packet: bytes, count: int) -> "bytes | None":
  """Register bytes of a decrypted 0xB0 read reply (they follow frame byte 5)."""
  if len(packet) < 6 + count or packet[0] != 0xB0:
    return None
  return bytes(packet[6:6 + count])


# ---------------------------------------------------------------------------------------------------------------------
# Delphis nano-modem relay: 0xA0 [plain length][AES(bytes for the modem UART)] both ways; the modem's lines (#U.., #R..,
# replies to $ commands) arrive as 0xA0 frames. 0xA1 [0x01][state] = modem off / busy.
# ---------------------------------------------------------------------------------------------------------------------
NANO_MODEM_DATA = 0xA0
NANO_MODEM_STATUS = 0xA1
NANO_MODEM_MAX_COMMAND = 48
NANO_MODEM_STATES = {0: "powered off", 1: "booting", 2: "idle", 3: "busy"}


def decode_nano_modem(packet: bytes) -> "tuple[str, str] | None":
  """('data', text) for a decrypted 0xA0 frame, ('status', text) for 0xA1, None otherwise."""
  if len(packet) < 2:
    return None
  if packet[0] == NANO_MODEM_DATA:
    # Byte 1 is the true line length (Delphis AppProtocolSendData); binary message bytes may be 0x00, so no strip.
    body = packet[2:2 + packet[1]]
    return "data", body.decode("latin-1")
  if packet[0] == NANO_MODEM_STATUS and len(packet) >= 4:
    # Delphis sends 0xA1 when the modem is switched off OR still busy with the previous command (NmBleTxDataUpdate).
    return "status", "nano-modem refused the command: switched off or still busy with the previous one"
  return None


def encode_nano_modem(command: str) -> bytes:
  """Bytes sent to the modem UART for a console line (no line ending is added: modem commands are framed by '$')."""
  data = (command or "").encode("ascii")
  if not data or len(data) > NANO_MODEM_MAX_COMMAND:
    raise ValueError(f"a modem command is 1..{NANO_MODEM_MAX_COMMAND} ASCII characters")
  return data
