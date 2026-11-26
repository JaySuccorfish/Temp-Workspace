"""@file gas_device.py
@brief GiGO Gas logger (BME690, "Succorfish Gas") command channel on an already-connected BLE client
@version 1.1
@date 2026-11-22

The gas logger uses the Depth & Temp transport (service 0xAB00, characteristic 0xAB05, the GiGO AES key and the x79
challenge answer, status-led logger replies, raw device-control replies), so this class extends the D&T one and adds
the gas opcodes 0x30..0x37 (gigo-gas-sensors ble_protocol.h). Byte layouts are decoded by gas_protocol.py, a verbatim
copy of gigo-gas-sensors/tests/gas_protocol.py (kept identical by tests/test_hub_and_imu.py when the sibling repository
is present). Live-stream frames (0x30) arrive unsolicited and go to every stream callback, never to the reply queue.
The stream is shared: the device tab's live view and the gas window each acquire it, and it is switched off only when
the last owner releases it.
"""
from . import gas_protocol as gas
from sc2_protocol import DepthTempDevice
import asyncio
import struct


GAS_INFO_TIMEOUT_S = 4.0
GAS_READ_TIMEOUT_S = 8.0
GAS_FORMAT_TIMEOUT_S = 25.0
GAS_SANITY_FLAGS = {0x01: "battery low", 0x02: "BME690 sensor not ready", 0x04: "I2C devices missing"}


class GasLoggerDevice(DepthTempDevice):
  """GiGO Gas logger: D&T transport plus live data, device info and sequence-numbered record sync."""

  def __init__(self, client, aes_key=None, char_uuid=gas.COMMAND_CHAR_UUID):
    """Same set-up as the D&T logger; no stream callbacks and no stream owners yet."""
    super().__init__(client, aes_key=aes_key, char_uuid=char_uuid)
    self._stream_callbacks = []
    self._stream_owners = set()
    self._live_request_pending = False
    self.unsolicited_opcodes = {gas.OP_LIVE_DATA}

  def add_stream_callback(self, callback):
    """callback(dict) receives every decoded 0x30 stream frame (called on the BLE thread)."""
    if callback not in self._stream_callbacks:
      self._stream_callbacks.append(callback)

  def remove_stream_callback(self, callback):
    """Stop sending stream frames to callback."""
    if callback in self._stream_callbacks:
      self._stream_callbacks.remove(callback)

  def _on_notify(self, _handle, data: bytearray):
    """A 0x30 frame nobody asked for is a stream sample: it goes to the stream callbacks (dropped when there are
    none), never to the reply queue, where the next command would take it as its reply. Everything else is a reply.
    """
    raw = bytes(data)
    if raw and raw[0] == gas.OP_LIVE_DATA and not self._live_request_pending:
      plain = self._decrypt(raw[2:]) if len(raw) > 2 else b""
      try:
        live = gas.decode_live(plain)
      except gas.ProtocolError:
        return
      for callback in list(self._stream_callbacks):
        try:
          callback(live)
        except Exception:
          pass
      return
    self._pending.put_nowait(raw)

  async def stream_acquire(self, owner) -> bool:
    """Join the live stream; the first owner switches it on (0x31). False when the logger refused."""
    if owner in self._stream_owners:
      return True
    if not self._stream_owners and not await self.set_stream(True):
      return False
    self._stream_owners.add(owner)
    return True

  async def stream_release(self, owner) -> bool:
    """Leave the live stream; the last owner switches it off. True when nothing was left to do or the logger
    accepted.
    """
    if owner not in self._stream_owners:
      return True
    self._stream_owners.discard(owner)
    if self._stream_owners:
      return True
    return await self.set_stream(False)

  # ---- device information and live values --------------------------------------------------------------------------
  async def get_info(self) -> "dict | None":
    """0x34 -> decoded device information (type, firmware, record format, flags, BSEC version ...)."""
    status, data = await self.send(gas.OP_GET_INFO, timeout=GAS_INFO_TIMEOUT_S)
    if status is None:
      return None
    try:
      info = gas.decode_info(bytes([status]) + data)
    except gas.ProtocolError:
      return {"status": self.status_text(status)}
    info["flags_text"] = gas.describe_flags(info["flags"], gas.INFO_FLAG_NAMES)
    return info

  async def read_live(self) -> "dict | None":
    """0x30 -> one decoded live sample (temperature, humidity, pressure, gas resistance, IAQ, CO2 eq., battery)."""
    async with self._cmd_lock:
      # Only while this request waits is a 0x30 frame a reply (a stream frame arriving now is just as fresh).
      self._live_request_pending = True
      try:
        raw = await self._exchange(gas.OP_LIVE_DATA, b"", GAS_INFO_TIMEOUT_S)
      finally:
        self._live_request_pending = False
    status, data = self._parse_status_reply(raw)
    if status is None:
      return None
    try:
      return gas.decode_live(bytes([status]) + data)
    except gas.ProtocolError:
      return {"status": status}

  async def set_stream(self, enable: bool) -> bool:
    """0x31: start / stop a 0x30 frame per new sample; True on success."""
    status, _ = await self.send(gas.OP_LIVE_STREAM, bytes([1 if enable else 0]))
    return status == gas.STATUS_SUCCESS

  # ---- logger status ------------------------------------------------------------------------------------------------
  async def gas_stats(self) -> "dict | None":
    """0x26 decoded with the gas layout (counts per partition, free bytes, recording, phase, errors)."""
    status, data = await self.send(gas.OP_GET_STATS)
    if status is None:
      return None
    try:
      stats = gas.decode_stats(bytes([status]) + data)
    except gas.ProtocolError:
      return {"status": status}
    stats["error_flags_text"] = gas.describe_error_flags(stats["error_flags"])
    stats["phase_text"] = gas.PHASE_NAMES.get(stats["phase"], str(stats["phase"]))
    return stats

  async def log_status(self) -> "dict | None":
    """0x35 -> stored count, first / next sequence, capacity, overwritten, newest epoch, interval, batch size."""
    status, data = await self.send(gas.OP_GET_LOG_STATUS)
    if status is None:
      return None
    try:
      return gas.decode_log_status(bytes([status]) + data)
    except gas.ProtocolError:
      return {"status": status}

  async def gas_error_log(self) -> "dict | None":
    """0x2A decoded with the gas counter names."""
    status, data = await self.send(gas.OP_GET_ERROR_LOG)
    if status is None:
      return None
    try:
      log = gas.decode_error_log(bytes([status]) + data)
    except gas.ProtocolError:
      return {"status": status}
    log["error_flags_text"] = gas.describe_error_flags(log["error_flags"])
    return log

  async def gas_sanity(self) -> "dict | None":
    """0x09 (raw reply): pass, error flags, I2C bitmap, device count, battery mV."""
    data = await self.send_control(gas.OP_SANITY_TEST)
    if not data or len(data) < 7:
      return None
    flags = data[1]
    return {"passed": bool(data[0]), "error_flags": flags,
            "errors": [text for bit, text in GAS_SANITY_FLAGS.items() if flags & bit],
            "i2c_bitmap": struct.unpack_from("<H", data, 2)[0], "i2c_count": data[4],
            "battery_mv": struct.unpack_from("<H", data, 5)[0]}

  # ---- record synchronisation (sequence numbers, at-least-once) ------------------------------------------------------
  async def read_records(self, first_sequence: int, max_count: int = gas.READ_RECORDS_MAX):
    """0x36 -> (status, first sequence actually sent, [16-byte records]); (None, None, []) on timeout."""
    payload = struct.pack("<IB", first_sequence & gas.UINT32_MASK, max(1, min(max_count, gas.READ_RECORDS_MAX)))
    status, data = await self.send(gas.OP_READ_RECORDS, payload, timeout=GAS_READ_TIMEOUT_S)
    if status is None:
      return None, None, []
    try:
      return gas.decode_read_records(bytes([status]) + data)
    except gas.ProtocolError:
      return status, None, []

  async def ack_records(self, up_to_sequence: int) -> "dict | None":
    """0x37: the device may delete every record before up_to_sequence (call only after they are stored safely)."""
    status, data = await self.send(gas.OP_ACK_RECORDS, struct.pack("<I", up_to_sequence & gas.UINT32_MASK))
    if status is None:
      return None
    try:
      return gas.decode_ack(bytes([status]) + data)
    except gas.ProtocolError:
      return {"status": status}

  async def download_all(self, progress=None) -> "tuple[list, int | None]":
    """Read every stored record by sequence number. Returns ([(sequence, GasRecord or error text)], next sequence);
    nothing is acknowledged here. progress(done, total) is called after each batch.
    """
    log = await self.log_status()
    if not log or log.get("status") != gas.STATUS_SUCCESS:
      return [], None
    total = log["count"]
    sequence = log["first_sequence"]
    end = log["next_sequence"]
    rows = []
    while total and sequence != end and len(rows) < total:
      status, first, records = await self.read_records(sequence)
      if status != gas.STATUS_SUCCESS or first is None or not records:
        break
      for index, record in enumerate(records):
        absolute = (first + index) & gas.UINT32_MASK
        try:
          rows.append((absolute, gas.decode_record(record, absolute)))
        except gas.RecordError as error:
          rows.append((absolute, f"{error.kind}: {error}"))
      sequence = (first + len(records)) & gas.UINT32_MASK
      if progress is not None:
        progress(len(rows), total)
      await asyncio.sleep(0)
    return rows, sequence


def gas_record_csv_header() -> str:
  """CSV header of a downloaded record row."""
  return ("sequence,utc,epoch,temperature_c,humidity_pct,pressure_pa,gas_resistance_ohm,iaq,iaq_accuracy,battery_pct,"
          "time_synced,gas_fresh,gas_settled")


def gas_record_csv_row(sequence: int, record) -> str:
  """One CSV row; an undecodable record becomes a row with its error text."""
  if isinstance(record, str):
    return f"{sequence},,,,,,,,,,,,{record}"

  def text(value):
    return "" if value is None else str(value)

  return ",".join([str(sequence), gas.format_epoch(record.epoch), str(record.epoch), text(record.temperature_c),
                   text(record.humidity_pct), text(record.pressure_pa), text(record.gas_resistance_ohm),
                   text(record.iaq), str(record.iaq_accuracy), text(record.battery_pct), str(int(record.time_synced)),
                   str(int(record.gas_fresh)), str(int(record.gas_settled))])
