"""@file config_codecs.py
@brief Byte layouts of the configuration and status frames of every supported device, as pure encode/decode functions
@version 1.0
@date 2026-11-21

No BLE and no Tk in here, so every layout is unit tested offline (tests/test_codecs.py). Each layout was traced from the
firmware of its device; the source is named at each section. Values are range-checked BEFORE a frame is built: a value
that does not fit its field (a day, 86400 s, in a 16-bit field) is refused with a readable message instead of being cut
to its low bits or crashing the write.
"""
from dataclasses import dataclass
import re
import struct


# ---------------------------------------------------------------------------------------------------------------------
# Field descriptions and range checks
# ---------------------------------------------------------------------------------------------------------------------
U8_KEEP = 0xFF
U16_KEEP = 0xFFFF
U32_KEEP = 0xFFFFFFFF
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400


class ConfigValueError(ValueError):
  """A configuration value the device cannot take (not a number, or outside the field's range)."""


@dataclass(frozen=True)
class FieldSpec:
  """One editable configuration field: key, label, unit, accepted range and a short help text."""
  key: str
  label: str
  unit: str
  minimum: int
  maximum: int
  hint: str = ""
  zero_allowed: bool = False
  read_only: bool = False

  def check(self, value: int) -> int:
    """Return value when the device accepts it, else raise ConfigValueError naming the field and its range."""
    if not isinstance(value, int):
      raise ConfigValueError(f"{self.label}: '{value}' is not a whole number")
    if self.zero_allowed and value == 0:
      return value
    if value < self.minimum or value > self.maximum:
      zero_text = "0 (off) or " if self.zero_allowed else ""
      unit_text = f" {self.unit}" if self.unit else ""
      raise ConfigValueError(f"{self.label}: {value}{unit_text} is outside {zero_text}"
                             f"{self.minimum}..{self.maximum}{unit_text}")
    return value

  def range_text(self) -> str:
    """Human-readable accepted range, e.g. '0 (off) or 5..86400 s'."""
    zero_text = "0 (off) or " if self.zero_allowed else ""
    unit_text = f" {self.unit}" if self.unit else ""
    return f"{zero_text}{self.minimum}..{self.maximum}{unit_text}"


def parse_number(text: str, field: FieldSpec) -> "int | None":
  """Parse an entry box: '' or '--' means 'keep' (None); decimal, 0x-hex and h/m/s/d suffixes ('15m', '1d') are
  accepted for second fields. Raises ConfigValueError on anything else or when out of range.
  """
  raw = (text or "").strip().lower()
  if raw in ("", "--"):
    return None
  multiplier = 1
  if field.unit == "s" and raw[-1:] in ("s", "m", "h", "d") and not raw.startswith("0x"):
    multiplier = {"s": 1, "m": SECONDS_PER_MINUTE, "h": SECONDS_PER_HOUR, "d": SECONDS_PER_DAY}[raw[-1]]
    raw = raw[:-1].strip()
  try:
    value = int(raw, 0) * multiplier
  except ValueError:
    raise ConfigValueError(f"{field.label}: '{text}' is not a number") from None
  return field.check(value)


def format_seconds(seconds: "int | None") -> str:
  """Short human form of a duration: '86400 s (1d)', '900 s (15m)', '90 s (1m30s)', 'off' for 0, '--' for None."""
  if seconds is None:
    return "--"
  if seconds == 0:
    return "off"
  remaining = int(seconds)
  parts = []
  for unit_seconds, suffix in ((SECONDS_PER_DAY, "d"), (SECONDS_PER_HOUR, "h"), (SECONDS_PER_MINUTE, "m"), (1, "s")):
    if remaining >= unit_seconds:
      count, remaining = divmod(remaining, unit_seconds)
      parts.append(f"{count}{suffix}")
  return "".join(parts)


# ---------------------------------------------------------------------------------------------------------------------
# Valtrack V1 / V2 (STM32WB55, app_ble_p2p.c, docs 07_BLE_Interface.md section 5.1)
#
# 0xC1 frame offsets (byte 0 opcode, byte 1 length). Every multi-byte field is big-endian in BOTH directions.
#   2..3  GSM interval s (u16)     4..5 heartbeat min     6..7 GPS interval s (u16)   8..13 unused (0xFF)
#   14 low-power flag   15 motion enable   16 erase logs (write 1)   17 motion threshold %   18..19 battery event min
#   20 LED flag   21 BLE advertising in low power
#   22..25 GSM interval s (u32)   26..29 GPS interval s (u32)   <- extended frame (firmware 2026-11-21 and later)
# A read with selector VALTRACK_C1_READ_EXTENDED returns the 28-byte payload; older firmware answers 20 bytes and then
# a 16-bit interval field of 0xFFFF means "longer than 65534 s, unknown".
# ---------------------------------------------------------------------------------------------------------------------
VALTRACK_C1_PAYLOAD_LEN = 20
VALTRACK_C1_EXT_PAYLOAD_LEN = 28
VALTRACK_C1_READ_EXTENDED = 0xA5
VALTRACK_INTERVAL_MAX_S = SECONDS_PER_DAY
VALTRACK_FIELD16_INTERVAL_MAX_S = 0xFFFE

VALTRACK_C1_FIELDS = (
  FieldSpec("gsm_interval", "GSM report interval", "s", 5, VALTRACK_INTERVAL_MAX_S,
            "Seconds between reports (fixed rate). 0 = no timed reports. Up to 86400 s (1 day); "
            "suffixes 15m / 2h / 1d accepted.", zero_allowed=True),
  FieldSpec("gps_interval", "GPS interval", "s", 5, VALTRACK_INTERVAL_MAX_S,
            "Seconds between GPS fixes. 0 or equal to the GSM interval = one fix per report.", zero_allowed=True),
  FieldSpec("heartbeat", "Heartbeat", "min", 1, 0xFFFE, "Minutes between heartbeat reports (720 = 12 h)."),
  FieldSpec("battery_event", "Battery event interval", "min", 0, 0xFFFE,
            "Minutes between battery readings (0 = read at each report)."),
  FieldSpec("low_power_flag", "Low-power mode", "", 0, 1,
            "1 = STOP2 between reports; only in effect when motion is on or GSM interval >= 10 min."),
  FieldSpec("motion_enable", "Motion detection", "", 0, 1, "1 = report in trips started by movement."),
  FieldSpec("motion_thresh", "Motion threshold", "%", 1, 127, "Accelerometer threshold (85 typical)."),
  FieldSpec("led_all_flag", "LEDs", "", 0, 1, "0 = all LEDs off (they are always off in low power)."),
  FieldSpec("ble_adv_lp", "BLE advertising in low power", "", 0, 1, "1 = slow advertising, 0 = radio off."),
)

_VALTRACK_U16_OFFSETS = {"gsm_interval": 2, "heartbeat": 4, "gps_interval": 6, "battery_event": 18}
_VALTRACK_U8_OFFSETS = {"low_power_flag": 14, "motion_enable": 15, "motion_thresh": 17, "led_all_flag": 20,
                        "ble_adv_lp": 21}
_VALTRACK_U32_OFFSETS = {"gsm_interval": 22, "gps_interval": 26}
VALTRACK_ERASE_OFFSET = 16


def valtrack_decode_c1(packet: bytes) -> dict:
  """Decode a decrypted 0xC1 read reply (frame incl. opcode and length). None = not set / not known."""
  if len(packet) < 2 + VALTRACK_C1_PAYLOAD_LEN or packet[0] != 0xC1:
    raise ValueError(f"not a Valtrack 0xC1 reply: {packet.hex(' ')}")
  result = {}
  for key, offset in _VALTRACK_U16_OFFSETS.items():
    value = struct.unpack_from(">H", packet, offset)[0]
    result[key] = None if value == U16_KEEP else value
  for key, offset in _VALTRACK_U8_OFFSETS.items():
    result[key] = None if packet[offset] == U8_KEEP else packet[offset]
  extended = packet[1] >= VALTRACK_C1_EXT_PAYLOAD_LEN and len(packet) >= 2 + VALTRACK_C1_EXT_PAYLOAD_LEN
  for key, offset in _VALTRACK_U32_OFFSETS.items():
    if extended:
      value = struct.unpack_from(">I", packet, offset)[0]
      result[key] = None if value == U32_KEEP else value
  result["extended"] = extended
  return result


def valtrack_encode_c1(fields: dict, erase_logs: bool = False) -> bytes:
  """Build the 28-byte 0xC1 write payload (without opcode and length). A key missing or None is sent as 'keep'.
  Intervals above 65534 s go in the u32 fields only (firmware before 2026-11-21 ignores those and keeps its value,
  which the read-back after a write reports).
  """
  payload = bytearray(b"\xFF" * VALTRACK_C1_EXT_PAYLOAD_LEN)
  specs = {spec.key: spec for spec in VALTRACK_C1_FIELDS}
  for key, value in fields.items():
    if value is None or key not in specs:
      continue
    specs[key].check(int(value))
  for key, offset in _VALTRACK_U8_OFFSETS.items():
    if fields.get(key) is not None:
      payload[offset - 2] = int(fields[key])
  for key, offset in _VALTRACK_U16_OFFSETS.items():
    value = fields.get(key)
    if value is None:
      continue
    if key in _VALTRACK_U32_OFFSETS and value > VALTRACK_FIELD16_INTERVAL_MAX_S:
      continue
    struct.pack_into(">H", payload, offset - 2, int(value))
  for key, offset in _VALTRACK_U32_OFFSETS.items():
    if fields.get(key) is not None:
      struct.pack_into(">I", payload, offset - 2, int(fields[key]))
  if erase_logs:
    payload[VALTRACK_ERASE_OFFSET - 2] = 1
  return bytes(payload)


def valtrack_decode_live_position(packet: bytes) -> "dict | None":
  """Decode the 0xE3 reply [valid, lat deg, lat min u32 BE, lat sign, lon deg, lon min u32 BE, lon sign]; minutes are
  scaled by 100 000 as in the server waypoint (doc 05). None when the frame is too short.
  """
  if len(packet) < 2 + 13 or packet[0] != 0xE3:
    return None
  valid, lat_deg = packet[2], packet[3]
  lat_min = struct.unpack_from(">I", packet, 4)[0] / 100000.0
  lat_sign = packet[8]
  lon_deg = packet[9]
  lon_min = struct.unpack_from(">I", packet, 10)[0] / 100000.0
  lon_sign = packet[14]
  latitude = (lat_deg + lat_min / 60.0) * (-1.0 if lat_sign else 1.0)
  longitude = (lon_deg + lon_min / 60.0) * (-1.0 if lon_sign else 1.0)
  return {"valid": bool(valid), "latitude": round(latitude, 6), "longitude": round(longitude, 6)}


# ---------------------------------------------------------------------------------------------------------------------
# SC2 Gen2 / Gen3 hub (STM32 + nRF; sc2-gen*-STM32CubeIDE docs 07_BLE_and_Mobile_App_Interface.md section 5.1)
#
# 0xC1 payload (frame offset = payload offset + 2). The GET reply is BIG-endian, a SET is read LITTLE-endian
# (memcpy into uint16_t); 0xFFFF / 0xFF = keep. All intervals are 16-bit on BLE although the device stores them as u32,
# so 65534 s (about 18.2 h) is the BLE maximum; longer intervals need the server (0xF2) or SMS. The firmware refuses an
# out-of-range value silently and still acknowledges, so the ranges below (doc 04) are checked here.
# ---------------------------------------------------------------------------------------------------------------------
SC2_C1_PAYLOAD_LEN = 20
SC2_BLE_INTERVAL_MAX_S = 0xFFFE

SC2_C1_FIELDS = (
  FieldSpec("gsm_interval", "GSM report interval", "s", 1, SC2_BLE_INTERVAL_MAX_S,
            "0 = GSM reporting off. BLE field is 16-bit: max 65534 s (use server/SMS for up to 86400 s).",
            zero_allowed=True),
  FieldSpec("gsm_timeout", "GSM registration timeout", "s", 60, 300, ""),
  FieldSpec("gps_interval", "GPS interval", "s", 1, SC2_BLE_INTERVAL_MAX_S,
            "0 = fix only when reporting. Max 65534 s over BLE.", zero_allowed=True),
  FieldSpec("gps_timeout", "GPS timeout", "s", 30, 120, ""),
  FieldSpec("iridium_interval", "Iridium report interval", "s", 1, SC2_BLE_INTERVAL_MAX_S,
            "0 = Iridium reporting off. Max 65534 s over BLE.", zero_allowed=True),
  FieldSpec("iridium_timeout", "Iridium timeout", "s", 60, 600, ""),
  FieldSpec("cheapest_multiplier", "Cheapest multiplier", "", 1, 60, "GSM attempts in cheapest mode."),
  FieldSpec("cheapest_mode", "Cheapest mode", "", 0, 1, "1 = retry GSM, then fall back to Iridium."),
  FieldSpec("iridium_always_on", "Iridium always on", "", 0, 1, "1 = never power Iridium down."),
  FieldSpec("iridium_events", "Iridium events", "", 0, 1, ""),
  FieldSpec("instant_tamper", "Instant tamper", "", 0, 1, "1 = report tamper immediately."),
)

_SC2_U16_OFFSETS = {"gsm_interval": 0, "gsm_timeout": 2, "gps_interval": 4, "gps_timeout": 6,
                    "iridium_interval": 8, "iridium_timeout": 10}
_SC2_U8_OFFSETS = {"cheapest_multiplier": 12, "cheapest_mode": 13, "iridium_always_on": 16, "iridium_events": 17,
                   "instant_tamper": 18}


def sc2_decode_c1(packet: bytes) -> dict:
  """Decode a decrypted SC2 0xC1 GET reply (frame incl. opcode and length; 16-bit fields big-endian)."""
  if len(packet) < 2 + SC2_C1_PAYLOAD_LEN or packet[0] != 0xC1:
    raise ValueError(f"not an SC2 0xC1 reply: {packet.hex(' ')}")
  payload = packet[2:]
  result = {}
  for key, offset in _SC2_U16_OFFSETS.items():
    value = struct.unpack_from(">H", payload, offset)[0]
    result[key] = None if value == U16_KEEP else value
  for key, offset in _SC2_U8_OFFSETS.items():
    result[key] = None if payload[offset] == U8_KEEP else payload[offset]
  return result


def sc2_encode_c1(fields: dict) -> bytes:
  """Build the 20-byte SC2 0xC1 SET payload (16-bit fields LITTLE-endian); missing/None = keep."""
  payload = bytearray(b"\xFF" * SC2_C1_PAYLOAD_LEN)
  specs = {spec.key: spec for spec in SC2_C1_FIELDS}
  for key, value in fields.items():
    if value is not None and key in specs:
      specs[key].check(int(value))
  for key, offset in _SC2_U16_OFFSETS.items():
    if fields.get(key) is not None:
      struct.pack_into("<H", payload, offset, int(fields[key]))
  for key, offset in _SC2_U8_OFFSETS.items():
    if fields.get(key) is not None:
      payload[offset] = int(fields[key])
  return bytes(payload)


# ---------------------------------------------------------------------------------------------------------------------
# GiGO nRF family on service 0xAB00 / characteristic 0xAB05 (gigo-MGT-Delphis, gigo-beacon-rotation-jack-VSCode,
# gigo-depth-temp; their docs 03_BLE_Protocol.md). Config GET 0x06 / SET 0x05, frame offsets:
#   2..17 name (byte 2 = 0 keeps it)   18 flag A   19 flag B   20..21 battery 0 % mV   22..23 battery 100 % mV
#   Delphis:  A = nano-modem enable, B = IMU enable;   voltages written LE, read back BE
#   Beacon:   A = beacon continuous advertising (1) / limited (0), B = IMU enable;  voltages written LE, read BE
#   D&T:      2..17 name, 18 Keller enable, 19..20 battery 0 % mV, 21..22 battery 100 % mV, LE both ways
# The flags have NO 'keep' value (whatever is sent is stored) and a voltage of 0 means keep, so a write must carry the
# flags read just before; 0xFFFF in a voltage field would be stored as 65.535 V.
# ---------------------------------------------------------------------------------------------------------------------
GIGO_NAME_LEN = 16
GIGO_VOLTAGE_KEEP = 0
# Plausible single-cell range; the firmware itself accepts any non-zero value and divides by 1000.
GIGO_BATTERY_MIN_MV = 1000
GIGO_BATTERY_MAX_MV = 5000

GIGO_CONFIG_FLAGS = {
  "gigo_delphis": (("nano_modem_enable", "Nano-modem enabled"), ("imu_enable", "IMU enabled")),
  "gigo_beacon": (("beacon_continuous", "Beacon continuous advertising"), ("imu_enable", "IMU enabled")),
  "gigo_dt": (("keller_enable", "Keller sensor enabled"),),
}


def gigo_config_fields(family: str) -> tuple:
  """Editable fields of the 0x05/0x06 configuration of one GiGO family."""
  fields = [FieldSpec("name", "Advertised name", "", 0, GIGO_NAME_LEN,
                      "Up to 16 characters; empty = keep. A renamed beacon is no longer seen by SC2 scanners.")]
  for key, label in GIGO_CONFIG_FLAGS.get(family, ()):
    fields.append(FieldSpec(key, label, "", 0, 1, ""))
  fields.append(FieldSpec("battery_flat_mv", "Battery 0 % voltage", "mV", GIGO_BATTERY_MIN_MV, GIGO_BATTERY_MAX_MV,
                          "0 or empty = keep."))
  fields.append(FieldSpec("battery_full_mv", "Battery 100 % voltage", "mV", GIGO_BATTERY_MIN_MV, GIGO_BATTERY_MAX_MV,
                          "0 or empty = keep."))
  return tuple(fields)


def gigo_decode_config(family: str, packet: bytes) -> dict:
  """Decode a decrypted 0x06 reply (frame incl. opcode and length) of a GiGO family device."""
  if len(packet) < 2 + GIGO_NAME_LEN + 3 or packet[0] != 0x06:
    raise ValueError(f"not a GiGO 0x06 reply: {packet.hex(' ')}")
  result = {"name": packet[2:2 + GIGO_NAME_LEN].split(b"\x00", 1)[0].decode("utf-8", "replace")}
  if family == "gigo_dt":
    result["keller_enable"] = packet[18]
    result["battery_flat_mv"] = struct.unpack_from("<H", packet, 19)[0] if len(packet) >= 21 else None
    result["battery_full_mv"] = struct.unpack_from("<H", packet, 21)[0] if len(packet) >= 23 else None
    return result
  flags = GIGO_CONFIG_FLAGS.get(family, ())
  for index, (key, _label) in enumerate(flags):
    result[key] = packet[18 + index]
  result["battery_flat_mv"] = struct.unpack_from(">H", packet, 20)[0] if len(packet) >= 22 else None
  result["battery_full_mv"] = struct.unpack_from(">H", packet, 22)[0] if len(packet) >= 24 else None
  return result


def gigo_encode_config(family: str, fields: dict, current: dict) -> bytes:
  """Build the 0x05 payload (without opcode and length). Flags missing from fields are taken from current (the last
  read), because the device has no 'keep' value for them; a voltage missing or None is sent as 0 (keep).
  """
  specs = {spec.key: spec for spec in gigo_config_fields(family)}
  name = fields.get("name") or ""
  encoded_name = name.encode("utf-8")
  if len(encoded_name) > GIGO_NAME_LEN:
    raise ConfigValueError(f"Advertised name: '{name}' is longer than {GIGO_NAME_LEN} bytes")
  for key in ("battery_flat_mv", "battery_full_mv"):
    if fields.get(key):
      specs[key].check(int(fields[key]))
  flag_values = []
  for key, label in GIGO_CONFIG_FLAGS.get(family, ()):
    value = fields.get(key)
    if value is None:
      value = current.get(key)
    if value is None:
      raise ConfigValueError(f"{label}: unknown - read the configuration first (the device has no 'keep' value)")
    flag_values.append(specs[key].check(int(value)))
  flat = int(fields.get("battery_flat_mv") or GIGO_VOLTAGE_KEEP)
  full = int(fields.get("battery_full_mv") or GIGO_VOLTAGE_KEEP)
  payload = bytearray(encoded_name.ljust(GIGO_NAME_LEN, b"\x00"))
  payload.extend(flag_values)
  payload.extend(struct.pack("<HH", flat, full))
  return bytes(payload)


BEACON_IMU_MODES = {0: "disabled", 1: "net drum (rotation)", 2: "hydraulic jack"}
BEACON_JACK_SENSITIVITY = {0: "low", 1: "medium", 2: "high"}
BEACON_TUNING_FIELDS = (
  FieldSpec("angle_threshold", "Angle threshold", "deg", 0, 255, ""),
  FieldSpec("dead_zone", "Dead zone", "deg", 0, 255, ""),
  FieldSpec("debounce_ms", "Debounce", "ms", 0, 0xFFFF, ""),
  FieldSpec("rotation_count", "Total rotation count", "", 0, 255, ""),
  FieldSpec("jack_sensitivity", "Jack sensitivity", "", 0, 2, "0 low, 1 medium, 2 high"),
)


def beacon_decode_tuning(packet: bytes) -> dict:
  """Decode the beacon 0xBA reply [angle, dead zone, debounce u16 BE, rotation count, jack sensitivity]."""
  if len(packet) < 8 or packet[0] != 0xBA:
    raise ValueError(f"not a beacon 0xBA reply: {packet.hex(' ')}")
  return {"angle_threshold": packet[2], "dead_zone": packet[3], "debounce_ms": struct.unpack_from(">H", packet, 4)[0],
          "rotation_count": packet[6], "jack_sensitivity": packet[7]}


def beacon_encode_tuning(fields: dict) -> bytes:
  """Build the beacon 0xB9 payload; every field is required (the device has no 'keep' value)."""
  values = []
  for spec in BEACON_TUNING_FIELDS:
    if fields.get(spec.key) is None:
      raise ConfigValueError(f"{spec.label}: required (read the tuning first)")
    values.append(spec.check(int(fields[spec.key])))
  angle, dead_zone, debounce, rotations, sensitivity = values
  return bytes([angle, dead_zone]) + struct.pack(">H", debounce) + bytes([rotations, sensitivity])


# ---------------------------------------------------------------------------------------------------------------------
# Status replies shared by several families
# ---------------------------------------------------------------------------------------------------------------------
def decode_battery(family: str, packet: bytes) -> "dict | None":
  """Battery reply: Valtrack 0xE4 [pct, mV BE]; SC2 0xE4 [pct]; Delphis/beacon 0x04 [pct, charging, mV BE];
  D&T 0x04 [pct]. Returns {'percent', 'voltage_mv', 'charging'} (None where the device does not send it).
  """
  if len(packet) < 3:
    return None
  result = {"percent": packet[2], "voltage_mv": None, "charging": None}
  if family == "valtrack" and packet[0] == 0xE4 and len(packet) >= 5:
    result["voltage_mv"] = struct.unpack_from(">H", packet, 3)[0]
  elif family in ("gigo_delphis", "gigo_beacon") and packet[0] == 0x04 and len(packet) >= 6:
    result["charging"] = packet[3]
    result["voltage_mv"] = struct.unpack_from(">H", packet, 4)[0]
  return result


def decode_firmware_version(packet: bytes) -> "str | None":
  """0xE5 / 0xD0 reply [major u16 BE][minor u16 BE] (Valtrack and SC2) -> 'major.minor'; None when malformed or a
  negative acknowledge.
  """
  if len(packet) < 6 or packet[1] < 4:
    return None
  major, minor = struct.unpack_from(">HH", packet, 2)
  return f"{major}.{minor}"


def decode_manufacturer_data(family: str, data: "bytes | None") -> dict:
  """Scan-response manufacturer data (company 0x0059, id stripped by bleak). SC2: address(6) FW major, minor.
  GiGO nRF family: address(6) battery %, charging, FW major, minor. Valtrack: not advertised (doc 07 B-06).
  """
  if not data or len(data) < 8:
    return {}
  if family in ("sc2_gen2", "sc2_gen3", "sc2"):
    return {"fw": f"{data[6]}.{data[7]}", "fw_major": data[6]}
  if len(data) >= 10:
    return {"battery_pct": data[6], "charging": data[7], "fw": f"{data[8]}.{data[9]}", "fw_major": data[8]}
  return {}


# ---------------------------------------------------------------------------------------------------------------------
# Valtrack debug-log lines that carry configuration or schedule state (app_sm.c, configuration.c)
# ---------------------------------------------------------------------------------------------------------------------
_CONFIG_PRINT_RE = re.compile(
  r"(GSM Interval|GPS Interval|Heartbeat|Low Power Flag|Motion Enable|Motion Thresh|Led All Flag|BLE Adv LowPwr|"
  r"Power Mode|Battery Event)\s*:\s*(\d+)")
_CONFIG_PRINT_KEYS = {
  "GSM Interval": "gsm_interval", "GPS Interval": "gps_interval", "Heartbeat": "heartbeat",
  "Low Power Flag": "low_power_flag", "Motion Enable": "motion_enable", "Motion Thresh": "motion_thresh",
  "Led All Flag": "led_all_flag", "BLE Adv LowPwr": "ble_adv_lp", "Power Mode": "power_mode",
  "Battery Event": "battery_event",
}
_NEXT_REPORT_RE = re.compile(r"Report timer: next report in (\d+) s(?: \(cycle took (\d+) s, interval (\d+) s\))?")


def parse_config_print(line: str) -> "tuple[str, int] | None":
  """('gsm_interval', 900) for a line of the per-cycle configuration print ('GSM Interval   : 900 s'), else None."""
  match = _CONFIG_PRINT_RE.search(line or "")
  if not match:
    return None
  return _CONFIG_PRINT_KEYS[match.group(1)], int(match.group(2))


def parse_next_report(line: str) -> "dict | None":
  """{'next_s', 'cycle_s', 'interval_s'} from 'Report timer: next report in N s (...)', else None."""
  match = _NEXT_REPORT_RE.search(line or "")
  if not match:
    return None
  return {"next_s": int(match.group(1)),
          "cycle_s": int(match.group(2)) if match.group(2) else None,
          "interval_s": int(match.group(3)) if match.group(3) else None}
