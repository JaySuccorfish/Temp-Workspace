"""@file sc2_hub_codecs.py
@brief SC2 Gen2 / Gen3 hub frames beyond 0xC1: industry mode, D&T relay, GPS fix and the STM32 DOTA relay
@version 1.0
@date 2026-11-22

Pure encode / decode functions (no BLE, no Tk), unit tested in tests/test_hub_and_imu.py. Every layout was traced from
the SC2 firmware: sc2-gen*-STM32CubeIDE App/Src/ble_cmd_dispatch.c and gps_mgr.c, the BLE co-processors
sc2-gen2-BLE-VsCode ble_sc2_bridge.c and SC2-Gen3-BLE-VsCode sc2_dota.c / sc2_cmd.c. Where the SC2 documents differ
from the code, the code is followed (noted at each place).
"""
from .config_codecs import ConfigValueError
from .config_codecs import FieldSpec
import struct


# ---------------------------------------------------------------------------------------------------------------------
# Acknowledgements (ble.h): the generic ack carries BLE_ACK_SUCCESS = 1 (the Gen3 document says 0, the code says 1).
# The D&T relay commands 0xC8 / 0xC9 answer 0 = done instead.
# ---------------------------------------------------------------------------------------------------------------------
SC2_ACK_SUCCESS = 1
SC2_DT_ACK_DONE = 0

# The BLE co-processor forwards only these opcodes from the phone to the STM32 (BleSc2BridgeDataHandler /
# Sc2CmdHandleWrite); anything else is dropped without a reply.
SC2_FORWARDED_OPCODES = (0x11, 0x12, 0xA2, 0xA3, 0xA4, 0xA5, 0xA7, 0xA8, 0xB1, 0xB2, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5,
                         0xC6, 0xC8, 0xC9, 0xE1, 0xE2, 0xE3, 0xE4, 0xE5, 0xFD)


# ---------------------------------------------------------------------------------------------------------------------
# 0xC2 industry configuration. Get: [flight (0xFF)][garage][GIGO channels][D&T channels]. Set: same order; flight mode
# 0xFF = leave alone, garage 0..2, channels 0..3. Garage and the channels have no 'keep' value, so a write needs a read
# first. Success -> ack 1 and the hub resets; any refused value -> ack 0 and no reset.
# ---------------------------------------------------------------------------------------------------------------------
SC2_INDUSTRY_FLIGHT_KEEP = 0xFF
SC2_CHANNEL_NAMES = {0: "off", 1: "GSM", 2: "Iridium", 3: "GSM + Iridium"}
SC2_GARAGE_NAMES = {0: "off", 1: "on", 2: "on (mode 2)"}
SC2_INDUSTRY_FIELDS = (
  FieldSpec("garage_mode", "Garage mode", "", 0, 2, "No reporting while the garage beacon is seen"),
  FieldSpec("gigo_channels", "GIGO beacon reports", "", 0, 3, "0 off, 1 GSM, 2 Iridium, 3 both"),
  FieldSpec("dt_channels", "Depth & Temp reports", "", 0, 3, "0 off, 1 GSM, 2 Iridium, 3 both"),
)


def sc2_decode_industry(packet: bytes) -> dict:
  """Decode a decrypted 0xC2 reply (frame incl. opcode and length)."""
  if len(packet) < 6 or packet[0] != 0xC2:
    raise ValueError(f"not an SC2 0xC2 reply: {packet.hex(' ')}")
  return {"flight_mode": None if packet[2] == SC2_INDUSTRY_FLIGHT_KEEP else packet[2], "garage_mode": packet[3],
          "gigo_channels": packet[4], "dt_channels": packet[5]}


def sc2_encode_industry(fields: dict, current: dict) -> bytes:
  """Build the 0xC2 set payload; a field missing from fields is taken from current (the last read)."""
  values = [SC2_INDUSTRY_FLIGHT_KEEP]
  for spec in SC2_INDUSTRY_FIELDS:
    value = fields.get(spec.key)
    if value is None:
      value = current.get(spec.key)
    if value is None:
      raise ConfigValueError(f"{spec.label}: unknown - read the industry configuration first")
    values.append(spec.check(int(value)))
  return bytes(values)


# ---------------------------------------------------------------------------------------------------------------------
# 0xC8 / 0xC9 Depth & Temp loggers the hub collects from (DEPTH_AND_TEMP_PER_DEVICE builds, Gen2 and Gen3).
# 0xC8 get = length 0; reply 5 x [slot 1..5][6-byte address]. Set: [count][count x 6-byte address]; the hub resets.
# Addresses travel most significant byte first, as printed ("AA:BB:..": the nRF converts them, depth_and_temp_addr_list).
# 0xC9 get = [6-byte address]; reply [address][mode][3 x (interval u16 LE, duration u32 LE)], mode 0xFF = unknown
# logger. Set: [address][mode][3 x (interval, duration)], 0xFF.. = keep; ack 0 = stored, 1 = address not in the list;
# the hub resets in both cases. Intervals 1..3600 s, durations 0 or 600..864000 s (what the hub forwards to the logger).
# ---------------------------------------------------------------------------------------------------------------------
SC2_DT_SLOTS = 5
SC2_DT_ADDRESS_LEN = 6
SC2_DT_PHASES = 3
SC2_DT_MODE_FLAGS = {0x01: "phase 1", 0x02: "phase 3", 0x04: "infinite duration"}
SC2_DT_MODE_FIELD = FieldSpec("mode", "Recording mode flags", "", 0, 7, "bit 0 phase 1, bit 1 phase 3, bit 2 infinite")
SC2_DT_INTERVAL_FIELD = FieldSpec("interval", "Interval", "s", 1, 3600, "")
SC2_DT_DURATION_FIELD = FieldSpec("duration", "Duration", "s", 600, 864000, "", zero_allowed=True)


def parse_mac(text: str) -> bytes:
  """'AA:BB:CC:DD:EE:FF' (or with '-' / no separator) -> 6 bytes, most significant first."""
  cleaned = (text or "").replace(":", "").replace("-", "").replace(" ", "")
  if len(cleaned) != 2 * SC2_DT_ADDRESS_LEN:
    raise ConfigValueError(f"'{text}' is not a 6-byte BLE address")
  try:
    return bytes.fromhex(cleaned)
  except ValueError:
    raise ConfigValueError(f"'{text}' is not a 6-byte BLE address") from None


def format_mac(address: bytes) -> str:
  """6 bytes -> 'AA:BB:CC:DD:EE:FF'; '' for an empty slot (all zero or all 0xFF)."""
  if not address or all(byte == 0 for byte in address) or all(byte == 0xFF for byte in address):
    return ""
  return ":".join(f"{byte:02X}" for byte in address)


def sc2_decode_dt_addresses(packet: bytes) -> list:
  """Decode a 0xC8 get reply -> list of 5 address strings ('' = empty slot)."""
  if len(packet) < 2 or packet[0] != 0xC8:
    raise ValueError(f"not an SC2 0xC8 reply: {packet.hex(' ')}")
  stride = 1 + SC2_DT_ADDRESS_LEN
  addresses = [""] * SC2_DT_SLOTS
  for index in range(SC2_DT_SLOTS):
    start = 2 + index * stride
    if len(packet) < start + stride:
      break
    slot = packet[start]
    if 1 <= slot <= SC2_DT_SLOTS:
      addresses[slot - 1] = format_mac(packet[start + 1:start + stride])
  return addresses


def sc2_encode_dt_addresses(addresses: list) -> bytes:
  """Build the 0xC8 set payload from up to 5 address strings (blank entries are skipped, order kept)."""
  used = [parse_mac(text) for text in addresses if (text or "").strip()]
  if len(used) > SC2_DT_SLOTS:
    raise ConfigValueError(f"at most {SC2_DT_SLOTS} Depth & Temp loggers")
  return bytes([len(used)]) + b"".join(used)


def sc2_decode_dt_device(packet: bytes) -> dict:
  """Decode a 0xC9 get reply -> {'address', 'known', 'mode', 'phases': [(interval, duration) x 3]}."""
  if len(packet) < 2 + SC2_DT_ADDRESS_LEN + 1 + SC2_DT_PHASES * 6 or packet[0] != 0xC9:
    raise ValueError(f"not an SC2 0xC9 reply: {packet.hex(' ')}")
  mode = packet[8]
  phases = []
  for index in range(SC2_DT_PHASES):
    interval, duration = struct.unpack_from("<HI", packet, 9 + index * 6)
    phases.append((interval, duration))
  return {"address": format_mac(packet[2:8]), "known": mode != 0xFF, "mode": None if mode == 0xFF else mode,
          "phases": phases}


def sc2_encode_dt_device(address: str, mode: "int | None", phases: list) -> bytes:
  """Build the 0xC9 set payload. mode None = keep; phases is 3 x (interval or None, duration or None), None = keep."""
  payload = bytearray(parse_mac(address))
  payload.append(0xFF if mode is None else SC2_DT_MODE_FIELD.check(int(mode)))
  for index in range(SC2_DT_PHASES):
    interval, duration = phases[index] if index < len(phases) else (None, None)
    interval_value = 0xFFFF if interval is None else SC2_DT_INTERVAL_FIELD.check(int(interval))
    duration_value = 0xFFFFFFFF if duration is None else SC2_DT_DURATION_FIELD.check(int(duration))
    payload.extend(struct.pack("<HI", interval_value, duration_value))
  return bytes(payload)


def sc2_dt_get_request(address: str) -> bytes:
  """0xC9 get payload: the 6-byte address (the hub tells get from set by the length 6)."""
  return parse_mac(address)


# ---------------------------------------------------------------------------------------------------------------------
# 0x12 GPS fix (gpsSendFixToBLE, Gen2 and Gen3 identical): success [lat deg][lat min x 100000 u32 BE][lon deg]
# [lon min u32 BE][(lat sign << 4) | lon sign], sign 1 = south / west; failure [error][0]. Sent after 0x12 and, while
# live tracking (0xE3 [1] start / [0] stop) is on, after every fix.
# ---------------------------------------------------------------------------------------------------------------------
SC2_GPS_ERRORS = {1: "GPS interval is 0", 2: "GPS power failure", 3: "GPS not responding", 4: "GPS fix timeout",
                  5: "fix not accurate enough"}
SC2_LIVE_TRACK_START = 1
SC2_LIVE_TRACK_STOP = 0
SC2_GPS_MINUTE_SCALE = 100000.0


def sc2_decode_gps(packet: bytes) -> "dict | None":
  """Decode a decrypted 0x12 frame -> {'valid', 'latitude', 'longitude'} or {'valid': False, 'error'}; None if not
  a 0x12 frame.
  """
  if len(packet) < 4 or packet[0] != 0x12:
    return None
  if packet[1] >= 11 and len(packet) >= 13:
    lat_deg = packet[2]
    lat_min = struct.unpack_from(">I", packet, 3)[0] / SC2_GPS_MINUTE_SCALE
    lon_deg = packet[7]
    lon_min = struct.unpack_from(">I", packet, 8)[0] / SC2_GPS_MINUTE_SCALE
    signs = packet[12]
    latitude = (lat_deg + lat_min / 60.0) * (-1.0 if (signs >> 4) & 0x0F else 1.0)
    longitude = (lon_deg + lon_min / 60.0) * (-1.0 if signs & 0x0F else 1.0)
    return {"valid": True, "latitude": round(latitude, 6), "longitude": round(longitude, 6)}
  code = packet[2]
  return {"valid": False, "error": SC2_GPS_ERRORS.get(code, f"error {code}")}


# ---------------------------------------------------------------------------------------------------------------------
# STM32 firmware update through the DOTA characteristic 0xBA02 (ble_cmd_dispatch.c BLE_DOTA_CMD, ble_sc2_bridge.c).
# Phone writes, step in the high nibble of byte 0:
#   1 [0x10][AES block: file size u32 LE, zeros]            -> STM32 erases the download area, echoes the frame
#   2 [0x20][n][seq u16 LE][CRC-8/MAXIM of the n plain bytes][AES(data padded to 16)], n <= 112
#                                                           -> ack [0x2n][4][seq][0][AES block: 1 ok / 0 CRC error]
#   3 [0x30][AES block: CRC-16/MODBUS of the whole image u16 LE, zeros]
#                                                           -> ack [0x3n][AES block: 1 accepted (hub resets) / 0 failed]
# The STM32 accepts the image only when the pointer at a vector-table offset leads to its device name
# (FlashFinishDOTAWrite, DOTA_DEVICE_NAME_LOC_VECTOR): 0x188 on Gen2, 0x1AC on Gen3 (its MCU has a longer vector
# table; the Gen3 source comment still says 0x188). That is checked here before anything is sent.
# ---------------------------------------------------------------------------------------------------------------------
DOTA_STEP_FILE_LENGTH = 0x10
DOTA_STEP_DATA = 0x20
DOTA_STEP_CHECKSUM = 0x30
DOTA_MAX_SLICE = 112
DOTA_AES_BLOCK = 16
DOTA_DATA_HEADER = 5
DOTA_NAME_POINTER_OFFSETS = {"sc2_gen2": 0x188, "sc2_gen3": 0x1AC}
DOTA_NAME_POINTER_OFFSET = DOTA_NAME_POINTER_OFFSETS["sc2_gen2"]
SC2_FW_SECTOR_ADDRESS = 0x08010000
SC2_DOTA_DEVICE_NAMES = {"sc2_gen2": "Succorfish SC2 GEN2", "sc2_gen3": "Succorfish SC2 GEN3"}
DOTA_NAME_MAX_LEN = 64


def crc8_maxim(data: bytes) -> int:
  """CRC-8/MAXIM (reflected polynomial 0x8C, initial 0), as generateCRCu8() of the STM32."""
  crc = 0
  for byte in data:
    crc ^= byte
    for _bit in range(8):
      crc = ((crc >> 1) ^ 0x8C) if (crc & 0x01) else (crc >> 1)
  return crc


def crc16_modbus(data: bytes) -> int:
  """CRC-16/MODBUS (reflected polynomial 0xA001, initial 0xFFFF, no final xor), as generateCRCu16() of the STM32."""
  crc = 0xFFFF
  for byte in data:
    crc ^= byte
    for _bit in range(8):
      crc = ((crc >> 1) ^ 0xA001) if (crc & 0x0001) else (crc >> 1)
  return crc


def sc2_dota_image_name(image: bytes, profile: str = "sc2_gen2") -> "str | None":
  """Device name the image carries (pointer at the generation's vector offset into the firmware sector), or None
  when there is none.
  """
  pointer_offset = DOTA_NAME_POINTER_OFFSETS.get(profile, DOTA_NAME_POINTER_OFFSET)
  if len(image) < pointer_offset + 4:
    return None
  pointer = struct.unpack_from("<I", image, pointer_offset)[0]
  offset = pointer - SC2_FW_SECTOR_ADDRESS
  if offset < 0 or offset >= len(image):
    return None
  raw = image[offset:offset + DOTA_NAME_MAX_LEN].split(b"\x00", 1)[0]
  try:
    name = raw.decode("ascii")
  except UnicodeDecodeError:
    return None
  return name if name and name.isprintable() else None


def sc2_dota_check_image(image: bytes, profile: str) -> str:
  """Return the image's device name when the hub of this profile will accept it, else raise ValueError."""
  if not image:
    raise ValueError("the firmware file is empty")
  name = sc2_dota_image_name(image, profile)
  expected = SC2_DOTA_DEVICE_NAMES.get(profile)
  if name is None:
    other = next((key for key in DOTA_NAME_POINTER_OFFSETS if key != profile and sc2_dota_image_name(image, key)),
                 None)
    if other:
      raise ValueError(f"the image is for '{sc2_dota_image_name(image, other)}', the connected hub expects "
                       f"'{expected}'")
    offset = DOTA_NAME_POINTER_OFFSETS.get(profile, DOTA_NAME_POINTER_OFFSET)
    raise ValueError(f"no SC2 device name at vector offset 0x{offset:X}: this is not an SC2 STM32 application image "
                     "(.bin linked at 0x08010000)")
  if expected and name != expected:
    raise ValueError(f"the image is for '{name}', the connected hub expects '{expected}'")
  return name


def sc2_dota_slice_size(mtu: int) -> int:
  """Largest data slice (multiple of 16, at most 112) that fits one write at this ATT MTU."""
  room = mtu - 3 - DOTA_DATA_HEADER
  size = min(DOTA_MAX_SLICE, (room // DOTA_AES_BLOCK) * DOTA_AES_BLOCK)
  if size < DOTA_AES_BLOCK:
    raise ValueError(f"ATT MTU {mtu} is too small for the DOTA relay (need at least 24)")
  return size


def sc2_dota_plain_blocks(image: bytes) -> "tuple[bytes, bytes]":
  """Plain 16-byte blocks of steps 1 and 3: (file size u32 LE, CRC-16/MODBUS u16 LE), zero padded."""
  first = struct.pack("<I", len(image)).ljust(DOTA_AES_BLOCK, b"\x00")
  last = struct.pack("<H", crc16_modbus(image)).ljust(DOTA_AES_BLOCK, b"\x00")
  return first, last


def sc2_dota_data_header(chunk: bytes, sequence: int) -> bytes:
  """Clear header of a step-2 write: [0x20][n][seq u16 LE][CRC-8 of the plain data]."""
  return bytes([DOTA_STEP_DATA, len(chunk)]) + struct.pack("<HB", sequence & 0xFFFF, crc8_maxim(chunk))


def sc2_dota_ack_ok(step: int, plain_block: bytes) -> bool:
  """Whether a decrypted ack block says 'ok' (first byte 1) for steps 2 and 3."""
  return bool(plain_block) and plain_block[0] == 1
