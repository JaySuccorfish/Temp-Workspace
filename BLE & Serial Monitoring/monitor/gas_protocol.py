"""
@file gas_protocol.py
@brief Succorfish Gas BLE protocol: frames, AES, record format 3 (legacy v2 readable), reply decoders
@version 2.0
@date 2026-10-23

Everything a client needs to talk to the device, without any Bluetooth import:
the challenge answer, AES-128-ECB payload encryption, frame building, the
decoders of every reply and the 16-byte record codec. The values mirror
ble_protocol.h and env_record.h of the firmware and Doc/PROTOCOL.md; the record
codec is an independent implementation that the tests check against vectors
produced by the firmware's own encoder (tests/vectors.txt).
"""

import datetime
import struct
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# GATT, advertising, framing (ble_protocol.h)
# ---------------------------------------------------------------------------

SERVICE_UUID = "0000ab00-0143-0800-0008-e5f9b34fb000"
COMMAND_CHAR_UUID = "0000ab05-0143-0800-0008-e5f9b34fb000"
ADVERTISED_NAME = "Succorfish Gas"

AES_BLOCK = 16
FRAME_HEADER = 2
PROTOCOL_VERSION = 2

# Shared system key of the Succorfish D&T family (also in the app and SC2).
AES_KEY = bytes([0x39, 0x99, 0xAB, 0x12, 0x13, 0x88, 0x98, 0x47,
                 0x33, 0x08, 0x49, 0x21, 0x12, 0xD4, 0x65, 0x79])

OP_READ_AUTH_KEY = 0x01
OP_SET_AUTH_VALUE = 0x02
OP_DEVICE_RESET = 0x03
OP_BATTERY_PERCENT = 0x04
OP_REC_MODE_SET = 0x07
OP_REC_MODE_GET = 0x08
OP_SANITY_TEST = 0x09
OP_RESET_DEFAULTS = 0xFE

OP_START = 0x20
OP_STOP = 0x21
OP_SET_INTERVAL = 0x22
OP_GET_INTERVAL = 0x23
OP_SET_RTC = 0x24
OP_GET_RTC = 0x25
OP_GET_STATS = 0x26
OP_RETRIEVE_DATA = 0x27
OP_CLEAR_DATA = 0x28
OP_FORMAT = 0x29
OP_GET_ERROR_LOG = 0x2A

OP_LIVE_DATA = 0x30
OP_LIVE_STREAM = 0x31
OP_GET_INFO = 0x34
OP_GET_LOG_STATUS = 0x35
OP_READ_RECORDS = 0x36
OP_ACK_RECORDS = 0x37

STATUS_SUCCESS = 0x00
STATUS_ERROR = 0x01
STATUS_INVALID_PARAM = 0x02
STATUS_NOT_READY = 0x03
STATUS_NO_DATA = 0x04

STATUS_NAMES = {
  STATUS_SUCCESS: "OK",
  STATUS_ERROR: "ERROR",
  STATUS_INVALID_PARAM: "INVALID_PARAM",
  STATUS_NOT_READY: "NOT_READY",
  STATUS_NO_DATA: "NO_DATA",
}

AUTH_SUCCESS = 0x01
PHASE_ALL = 0xFF
LOG_PHASE = 2

DEVICE_TYPE_DEPTH_TEMP = 0x01
DEVICE_TYPE_ENV_PROTOTYPE = 0x02
DEVICE_TYPE_GAS = 0x03
DEVICE_TYPE_NAMES = {
  DEVICE_TYPE_DEPTH_TEMP: "Succorfish D&T",
  DEVICE_TYPE_ENV_PROTOTYPE: "BME690 prototype",
  DEVICE_TYPE_GAS: "Succorfish Gas",
}

READ_RECORDS_MAX = 14
READ_RECORDS_HEADER = 6

# Logger limits (env_logger.h).
INTERVAL_MIN_S = 1
INTERVAL_MAX_S = 3600
DURATION_MIN_S = 600
DURATION_MAX_S = 864000
REC_MODE_INFINITE = 0x04
EPOCH_MIN_VALID = 1577836800      # 2020-01-01, the device refuses older times

LIVE_FLAG_GAS_FRESH = 0x01
LIVE_FLAG_GAS_VALID = 0x02
LIVE_FLAG_THP_VALID = 0x04
LIVE_FLAG_IAQ_VALID = 0x08
LIVE_FLAG_TIME_SYNCED = 0x10
LIVE_FLAG_GAS_SETTLED = 0x20
LIVE_U16_INVALID = 0xFFFF

INFO_FLAG_NAMES = [
  (0x01, "sensor ready"),
  (0x02, "storage ready"),
  (0x04, "clock valid"),
  (0x08, "clock synced"),
  (0x10, "recording"),
  (0x20, "streaming"),
  (0x40, "BSEC ready"),
]

ERROR_FLAG_NAMES = [
  (0x01, "sensor communication"),
  (0x02, "BSEC processing"),
  (0x04, "gas reading invalid"),
  (0x08, "value out of range"),
  (0x10, "EEPROM write failed"),
  (0x20, "EEPROM record CRC error"),
  (0x40, "EEPROM bus read failed"),
  (0x80, "oldest records overwritten"),
]

RESET_REASON_NAMES = [
  (0x01, "reset pin"),
  (0x02, "watchdog"),
  (0x04, "software"),
  (0x08, "CPU lock-up"),
]

PHASE_NAMES = {0: "idle", 2: "logging", 4: "done"}

# ---------------------------------------------------------------------------
# Record format 3 (legacy format 2 readable; Doc/PROTOCOL.md section 6)
# ---------------------------------------------------------------------------

RECORD_SIZE = 16
RECORD_FORMAT_VERSION = 3
LEGACY_RECORD_FORMAT_VERSION = 2

OFFSET_EPOCH = 0
OFFSET_TEMPERATURE = 4
OFFSET_HUMIDITY = 6
OFFSET_BATTERY = 7
OFFSET_PRESSURE = 8
OFFSET_GAS = 10
OFFSET_STATUS = 12
OFFSET_SEQUENCE = 14
OFFSET_CRC = 15

STATUS_IAQ_MASK = 0x01FF
STATUS_ACCURACY_SHIFT = 9
STATUS_ACCURACY_MASK = 0x0600
STATUS_GAS_FRESH = 0x0800
STATUS_TIME_SYNCED = 0x1000
STATUS_GAS_SETTLED = 0x2000
STATUS_VERSION_SHIFT = 14

TEMPERATURE_INVALID = 0x8000
HUMIDITY_INVALID = 0xFF
LEGACY_HUMIDITY_INVALID = 0xFFFF
BATTERY_INVALID = 0xFF
PRESSURE_INVALID = 0xFFFF
GAS_INVALID = 0x0000
IAQ_INVALID = 0x01FF

TEMPERATURE_STEPS_PER_C = 100
HUMIDITY_STEPS_PER_PCT = 1
PRESSURE_PA_PER_STEP = 2

TEMPERATURE_RANGE_C = (-40.0, 85.0)
HUMIDITY_RANGE_PCT = (0.0, 100.0)
PRESSURE_RANGE_PA = (30000.0, 125000.0)
IAQ_RANGE = (0.0, 500.0)

GAS_MANTISSA_BITS = 11
GAS_MANTISSA_MASK = 0x7FF
GAS_IMPLICIT_ONE = 0x800
GAS_SIGNIFICAND_LIMIT = 0x1000
GAS_EXPONENT_MAX = 31
GAS_CODE_MAX = 0xFFFF
GAS_MIN_OHM = 2

BLANK_BYTE = 0xFF
UINT32_MASK = 0xFFFFFFFF
BYTE_MASK = 0xFF

CRC8_POLYNOMIAL = 0x31
CRC16_POLYNOMIAL = 0x1021
CRC16_INITIAL = 0xFFFF


class ProtocolError(Exception):
  """A frame or reply does not follow the protocol."""


class RecordError(ProtocolError):
  """A 16-byte record cannot be decoded; kind is blank, crc or format."""

  def __init__(self, kind, message):
    super().__init__(message)
    self.kind = kind


@dataclass
class GasRecord:
  """One decoded record. A field is None when the record marks it invalid."""
  epoch: int
  sequence8: int
  time_synced: bool
  temperature_c: Optional[float]
  humidity_pct: Optional[float]
  battery_pct: Optional[int]
  pressure_pa: Optional[float]
  gas_resistance_ohm: Optional[int]
  iaq: Optional[int]
  iaq_accuracy: int
  gas_fresh: bool
  gas_settled: bool
  raw: bytes = b""


# ---------------------------------------------------------------------------
# Checksums (env_record.c)
# ---------------------------------------------------------------------------

def crc8(data: bytes) -> int:
  """CRC-8, polynomial 0x31, initial value 0, MSB first, no final XOR."""
  crc = 0
  for byte in data:
    crc ^= byte
    for _ in range(8):
      crc = ((crc << 1) ^ CRC8_POLYNOMIAL) & BYTE_MASK if crc & 0x80 else (crc << 1) & BYTE_MASK
  return crc


def crc16(data: bytes) -> int:
  """CRC-16/CCITT-FALSE."""
  crc = CRC16_INITIAL
  for byte in data:
    crc ^= byte << 8
    for _ in range(8):
      crc = ((crc << 1) ^ CRC16_POLYNOMIAL) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
  return crc


# ---------------------------------------------------------------------------
# AES-128 (FIPS-197), single blocks, as used by the device
# ---------------------------------------------------------------------------

def _gf_multiply(left, right):
  product = 0
  for _ in range(8):
    if right & 1:
      product ^= left
    high = left & 0x80
    left = (left << 1) & BYTE_MASK
    if high:
      left ^= 0x1B
    right >>= 1
  return product


def _build_sboxes():
  """S-box and inverse from the GF(2^8) definition (no tables to mistype)."""
  sbox = [0] * 256
  for value in range(256):
    inverse = 0
    if value:
      inverse = next(candidate for candidate in range(1, 256) if _gf_multiply(value, candidate) == 1)
    result = inverse
    for shift in range(1, 5):
      result ^= ((inverse << shift) | (inverse >> (8 - shift))) & BYTE_MASK
    sbox[value] = result ^ 0x63
  inverse_sbox = [0] * 256
  for index, value in enumerate(sbox):
    inverse_sbox[value] = index
  return sbox, inverse_sbox


_SBOX, _INV_SBOX = _build_sboxes()
_MUL = {factor: [_gf_multiply(value, factor) for value in range(256)] for factor in (1, 2, 3, 9, 11, 13, 14)}


def _expand_key(key: bytes):
  words = [list(key[index:index + 4]) for index in range(0, 16, 4)]
  round_constant = 1
  for index in range(4, 44):
    word = list(words[index - 1])
    if index % 4 == 0:
      word = [_SBOX[byte] for byte in word[1:] + word[:1]]
      word[0] ^= round_constant
      round_constant = _gf_multiply(round_constant, 2)
    words.append([first ^ second for first, second in zip(words[index - 4], word)])
  return [sum(words[round_index * 4:round_index * 4 + 4], []) for round_index in range(11)]


_ROUND_KEYS = {}


def _round_keys(key: bytes):
  if key not in _ROUND_KEYS:
    _ROUND_KEYS[key] = _expand_key(key)
  return _ROUND_KEYS[key]


def _shift_rows(state, inverse=False):
  """State is column-major (index = column * 4 + row); row r moves by r."""
  result = [0] * 16
  for column in range(4):
    for row in range(4):
      source = (column - row) % 4 if inverse else (column + row) % 4
      result[column * 4 + row] = state[source * 4 + row]
  return result


def _mix_columns(state, inverse=False):
  matrix = [14, 11, 13, 9] if inverse else [2, 3, 1, 1]
  result = [0] * 16
  for column in range(4):
    values = state[column * 4:column * 4 + 4]
    for row in range(4):
      result[column * 4 + row] = (_MUL[matrix[(0 - row) % 4]][values[0]] ^ _MUL[matrix[(1 - row) % 4]][values[1]] ^
                                  _MUL[matrix[(2 - row) % 4]][values[2]] ^ _MUL[matrix[(3 - row) % 4]][values[3]])
  return result


def aes128_encrypt_block(block: bytes, key: bytes = AES_KEY) -> bytes:
  keys = _round_keys(key)
  state = [first ^ second for first, second in zip(block, keys[0])]
  for round_index in range(1, 11):
    state = _shift_rows([_SBOX[byte] for byte in state])
    if round_index < 10:
      state = _mix_columns(state)
    state = [first ^ second for first, second in zip(state, keys[round_index])]
  return bytes(state)


def aes128_decrypt_block(block: bytes, key: bytes = AES_KEY) -> bytes:
  keys = _round_keys(key)
  state = [first ^ second for first, second in zip(block, keys[10])]
  for round_index in range(9, -1, -1):
    state = [_INV_SBOX[byte] for byte in _shift_rows(state, inverse=True)]
    state = [first ^ second for first, second in zip(state, keys[round_index])]
    if round_index > 0:
      state = _mix_columns(state, inverse=True)
  return bytes(state)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

def auth_answer(challenge: int) -> int:
  """Answer to the one-byte challenge of READ_AUTH_KEY (32-bit result)."""
  return ((((challenge * 79) + 8294) * 19) - ((573 * challenge) + 989)) & UINT32_MASK


def encrypt_payload(payload: bytes) -> bytes:
  """Zero-pad to whole blocks and encrypt each block (ECB)."""
  if not payload:
    return b""
  padded = payload + bytes((-len(payload)) % AES_BLOCK)
  return b"".join(aes128_encrypt_block(padded[index:index + AES_BLOCK]) for index in range(0, len(padded), AES_BLOCK))


def decrypt_payload(payload: bytes) -> bytes:
  if len(payload) % AES_BLOCK:
    raise ProtocolError("encrypted payload is not a whole number of blocks")
  return b"".join(aes128_decrypt_block(payload[index:index + AES_BLOCK]) for index in range(0, len(payload), AES_BLOCK))


def build_plain_frame(opcode: int, payload: bytes = b"") -> bytes:
  return bytes([opcode, len(payload)]) + payload


def build_command(opcode: int, payload: bytes = b"") -> bytes:
  """Frame for an authenticated command: payload padded and encrypted."""
  encrypted = encrypt_payload(payload)
  return bytes([opcode, len(encrypted)]) + encrypted


def parse_frame(frame: bytes):
  """Split a received frame into (opcode, payload bytes as on the wire)."""
  if len(frame) < FRAME_HEADER:
    raise ProtocolError("frame shorter than its header")
  opcode, length = frame[0], frame[1]
  if len(frame) < FRAME_HEADER + length:
    raise ProtocolError("frame shorter than its declared length")
  return opcode, frame[FRAME_HEADER:FRAME_HEADER + length]


def parse_reply(frame: bytes):
  """(opcode, decrypted payload) of an authenticated reply."""
  opcode, payload = parse_frame(frame)
  return opcode, decrypt_payload(payload)


# ---------------------------------------------------------------------------
# Record codec
# ---------------------------------------------------------------------------

def decode_gas(code: int) -> int:
  """Gas resistance code -> ohm (0 = no reading)."""
  if code == GAS_INVALID:
    return 0
  exponent = code >> GAS_MANTISSA_BITS
  significand = GAS_IMPLICIT_ONE | (code & GAS_MANTISSA_MASK)
  if exponent >= GAS_MANTISSA_BITS:
    return significand << (exponent - GAS_MANTISSA_BITS)
  return significand >> (GAS_MANTISSA_BITS - exponent)


def encode_gas(resistance_ohm: int) -> int:
  """Ohm -> code, 12 significant bits, rounded half up, saturating."""
  if resistance_ohm < GAS_MIN_OHM:
    return GAS_INVALID
  exponent = resistance_ohm.bit_length() - 1
  if exponent > GAS_MANTISSA_BITS:
    drop = exponent - GAS_MANTISSA_BITS
    significand = resistance_ohm >> drop
    if (resistance_ohm >> (drop - 1)) & 1:
      significand += 1
    if significand >= GAS_SIGNIFICAND_LIMIT:
      significand >>= 1
      exponent += 1
    if exponent > GAS_EXPONENT_MAX:
      return GAS_CODE_MAX
  else:
    significand = resistance_ohm << (GAS_MANTISSA_BITS - exponent)
  return (exponent << GAS_MANTISSA_BITS) | (significand & GAS_MANTISSA_MASK)


def _round_half_up(value: float) -> int:
  """Same rounding as the firmware: away from zero at .5."""
  return int(value + 0.5) if value >= 0.0 else int(value - 0.5)


def _scaled(value, valid_range, scale):
  """Steps for a value inside its range, None outside or missing."""
  if value is None or value != value:
    return None
  if not valid_range[0] <= value <= valid_range[1]:
    return None
  return _round_half_up(value * scale)


def decode_record(record: bytes, absolute_sequence: Optional[int] = None) -> GasRecord:
  """Verify (blank, CRC, version, optional sequence) and unpack 16 bytes."""
  if len(record) != RECORD_SIZE:
    raise RecordError("format", "a record is %d bytes, got %d" % (RECORD_SIZE, len(record)))
  if all(byte == BLANK_BYTE for byte in record):
    raise RecordError("blank", "erased record")
  if crc8(record[:OFFSET_CRC]) != record[OFFSET_CRC]:
    raise RecordError("crc", "record CRC mismatch")
  epoch, temperature = struct.unpack_from("<IH", record, 0)
  pressure, gas, status = struct.unpack_from("<HHH", record, OFFSET_PRESSURE)
  version = status >> STATUS_VERSION_SHIFT
  if version not in (RECORD_FORMAT_VERSION, LEGACY_RECORD_FORMAT_VERSION):
    raise RecordError("format", "unsupported record format %d" % version)
  if version == LEGACY_RECORD_FORMAT_VERSION:
    humidity_code = struct.unpack_from("<H", record, OFFSET_HUMIDITY)[0]
    humidity = None if humidity_code == LEGACY_HUMIDITY_INVALID else humidity_code / 100.0
    battery = None
  else:
    humidity_code, battery_code = record[OFFSET_HUMIDITY], record[OFFSET_BATTERY]
    humidity = humidity_code if humidity_code <= 100 else None
    battery = battery_code if battery_code <= 100 else None
  sequence8 = record[OFFSET_SEQUENCE]
  if absolute_sequence is not None and (absolute_sequence & BYTE_MASK) != sequence8:
    raise RecordError("format", "sequence byte %d does not belong to sequence %d" % (sequence8, absolute_sequence))
  iaq_code = status & STATUS_IAQ_MASK
  signed_temperature = struct.unpack("<h", struct.pack("<H", temperature))[0]
  return GasRecord(
    epoch=epoch,
    sequence8=sequence8,
    time_synced=bool(status & STATUS_TIME_SYNCED),
    temperature_c=None if temperature == TEMPERATURE_INVALID else signed_temperature / TEMPERATURE_STEPS_PER_C,
    humidity_pct=humidity,
    battery_pct=battery,
    pressure_pa=None if pressure == PRESSURE_INVALID else float(pressure * PRESSURE_PA_PER_STEP),
    gas_resistance_ohm=None if gas == GAS_INVALID else decode_gas(gas),
    iaq=None if iaq_code == IAQ_INVALID else iaq_code,
    iaq_accuracy=(status & STATUS_ACCURACY_MASK) >> STATUS_ACCURACY_SHIFT,
    gas_fresh=bool(status & STATUS_GAS_FRESH),
    gas_settled=bool(status & STATUS_GAS_SETTLED),
    raw=bytes(record),
  )


def encode_record(epoch, temperature_c, humidity_pct, pressure_pa, gas_resistance_ohm, iaq, iaq_accuracy,
                  time_synced, gas_fresh, gas_settled, sequence8=0, battery_pct=None) -> bytes:
  """Independent encoder (tests and tools); None means "no valid value"."""
  temperature = _scaled(temperature_c, TEMPERATURE_RANGE_C, TEMPERATURE_STEPS_PER_C)
  humidity = _scaled(humidity_pct, HUMIDITY_RANGE_PCT, HUMIDITY_STEPS_PER_PCT)
  battery = None
  if battery_pct is not None and 0 <= battery_pct <= 100 and int(battery_pct) == battery_pct:
    battery = int(battery_pct)
  pressure = _scaled(pressure_pa, PRESSURE_RANGE_PA, 1.0 / PRESSURE_PA_PER_STEP)
  iaq_steps = _scaled(iaq, IAQ_RANGE, 1)
  gas = GAS_INVALID
  if gas_resistance_ohm is not None and gas_resistance_ohm == gas_resistance_ohm and gas_resistance_ohm >= GAS_MIN_OHM:
    gas = encode_gas(min(int(gas_resistance_ohm + 0.5), UINT32_MASK))
  status = IAQ_INVALID if iaq_steps is None else iaq_steps | ((iaq_accuracy & 3) << STATUS_ACCURACY_SHIFT)
  status |= STATUS_GAS_FRESH if gas_fresh else 0
  status |= STATUS_TIME_SYNCED if time_synced else 0
  status |= STATUS_GAS_SETTLED if gas_settled else 0
  status |= RECORD_FORMAT_VERSION << STATUS_VERSION_SHIFT
  body = (struct.pack("<IHBBHHH", epoch & UINT32_MASK,
                      TEMPERATURE_INVALID if temperature is None else temperature & 0xFFFF,
                      HUMIDITY_INVALID if humidity is None else humidity,
                      BATTERY_INVALID if battery is None else battery,
                      PRESSURE_INVALID if pressure is None else pressure,
                      gas, status) + bytes([sequence8 & BYTE_MASK]))
  return body + bytes([crc8(body)])


# ---------------------------------------------------------------------------
# Reply decoders (all take the decrypted payload, status byte first)
# ---------------------------------------------------------------------------

def _require(payload: bytes, length: int, name: str):
  if len(payload) < length:
    raise ProtocolError("%s reply too short (%d < %d bytes)" % (name, len(payload), length))


def decode_stats(payload: bytes) -> dict:
  """GET_STATS (28 bytes, D&T layout)."""
  _require(payload, 28, "GET_STATS")
  total, phase1, phase2, phase3, free_bytes = struct.unpack_from("<IIIII", payload, 1)
  return {"status": payload[0], "total": total, "phase1": phase1, "phase2": phase2, "phase3": phase3,
          "free_bytes": free_bytes, "recording": bool(payload[21]), "phase": payload[22],
          "error_events": struct.unpack_from("<I", payload, 23)[0], "error_flags": payload[27]}


def decode_info(payload: bytes) -> dict:
  """GET_INFO: 22 bytes on Succorfish Gas, the first 13 on the prototype."""
  _require(payload, 13, "GET_INFO")
  info = {"status": payload[0], "device_type": payload[1],
          "device_name": DEVICE_TYPE_NAMES.get(payload[1], "unknown (%d)" % payload[1]),
          "firmware": "%d.%d" % (payload[2], payload[3]), "protocol_version": payload[4],
          "record_format": payload[5], "record_size": payload[6], "eeprom_chips": payload[7],
          "flags": payload[8], "eeprom_bytes": struct.unpack_from("<I", payload, 9)[0]}
  if len(payload) >= 22:
    info["bsec_version"] = "%d.%d.%d.%d" % tuple(payload[13:17])
    info["bsec_period_s"] = struct.unpack_from("<H", payload, 17)[0]
    info["reset_reason"] = payload[19]
    info["stack_unused_bytes"] = struct.unpack_from("<H", payload, 20)[0]
  return info


def decode_log_status(payload: bytes) -> dict:
  """GET_LOG_STATUS (24 bytes)."""
  _require(payload, 24, "GET_LOG_STATUS")
  count, first_sequence, capacity, overwritten, newest_epoch = struct.unpack_from("<IIIII", payload, 1)
  return {"status": payload[0], "count": count, "first_sequence": first_sequence,
          "next_sequence": (first_sequence + count) & UINT32_MASK, "capacity": capacity,
          "overwritten": overwritten, "newest_epoch": newest_epoch,
          "interval_s": struct.unpack_from("<H", payload, 21)[0], "batch_max": payload[23]}


def decode_read_records(payload: bytes):
  """READ_RECORDS -> (status, first sequence, [16-byte records])."""
  _require(payload, 1, "READ_RECORDS")
  if payload[0] != STATUS_SUCCESS:
    return payload[0], None, []
  _require(payload, READ_RECORDS_HEADER, "READ_RECORDS")
  first_sequence = struct.unpack_from("<I", payload, 1)[0]
  count = payload[5]
  _require(payload, READ_RECORDS_HEADER + count * RECORD_SIZE, "READ_RECORDS")
  records = [bytes(payload[READ_RECORDS_HEADER + index * RECORD_SIZE:READ_RECORDS_HEADER + (index + 1) * RECORD_SIZE])
             for index in range(count)]
  return payload[0], first_sequence, records


def decode_ack(payload: bytes) -> dict:
  """ACK_RECORDS (9 bytes): status, first sequence and count after removal."""
  _require(payload, 9, "ACK_RECORDS")
  first_sequence, count = struct.unpack_from("<II", payload, 1)
  return {"status": payload[0], "first_sequence": first_sequence, "count": count}


def decode_live(payload: bytes) -> dict:
  """LIVE_DATA (32 bytes in the battery-enabled firmware)."""
  _require(payload, 31, "LIVE_DATA")
  epoch, temperature, humidity, pressure, gas, iaq = struct.unpack_from("<Ifffff", payload, 1)
  accuracy, flags = payload[25], payload[26]
  static_iaq, co2 = struct.unpack_from("<HH", payload, 27)
  thp_valid = bool(flags & LIVE_FLAG_THP_VALID)
  iaq_valid = bool(flags & LIVE_FLAG_IAQ_VALID)
  return {"status": payload[0], "epoch": epoch,
          "temperature_c": temperature if thp_valid else None,
          "humidity_pct": humidity if thp_valid else None,
          "pressure_pa": pressure if thp_valid else None,
          "gas_resistance_ohm": gas if flags & LIVE_FLAG_GAS_VALID else None,
          "iaq": iaq if iaq_valid else None, "iaq_accuracy": accuracy,
          "static_iaq": None if static_iaq == LIVE_U16_INVALID else static_iaq,
          "co2_equivalent_ppm": None if co2 == LIVE_U16_INVALID else co2,
          "gas_fresh": bool(flags & LIVE_FLAG_GAS_FRESH), "gas_settled": bool(flags & LIVE_FLAG_GAS_SETTLED),
          "time_synced": bool(flags & LIVE_FLAG_TIME_SYNCED),
          "battery_pct": (payload[31] if len(payload) >= 32 and payload[31] <= 100 else None)}


def decode_error_log(payload: bytes) -> dict:
  """GET_ERROR_LOG (10 bytes of saturating counters and the flags)."""
  _require(payload, 10, "GET_ERROR_LOG")
  names = ("sensor_comm", "bsec", "gas_invalid", "range", "eeprom_write", "eeprom_crc", "eeprom_read", "skipped")
  result = {name: payload[index + 1] for index, name in enumerate(names)}
  result["status"] = payload[0]
  result["error_flags"] = payload[9]
  return result


def decode_interval(payload: bytes) -> dict:
  _require(payload, 7, "GET_INTERVAL")
  interval, duration = struct.unpack_from("<HI", payload, 1)
  return {"status": payload[0], "interval_s": interval, "duration_s": duration}


def decode_rtc(payload: bytes) -> int:
  _require(payload, 5, "GET_RTC")
  return struct.unpack_from("<I", payload, 1)[0]


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

def describe_flags(flags: int, names) -> str:
  parts = [name for bit, name in names if flags & bit]
  return ", ".join(parts) if parts else "none"


def describe_error_flags(flags: int) -> str:
  return describe_flags(flags, ERROR_FLAG_NAMES)


def format_epoch(epoch: int) -> str:
  """UTC time of a record, or a marker for "clock not set"."""
  if epoch < EPOCH_MIN_VALID:
    return "not set"
  return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
