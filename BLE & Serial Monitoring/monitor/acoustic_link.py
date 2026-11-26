"""@file acoustic_link.py
@brief GiGO Delphis acoustic link: NM3 nano-modem commands and replies, text messages and image transfers over BLE
@version 1.0
@date 2026-11-22

Delphis (MyGearTag) relays its NM3 acoustic modem (Newcastle University NM3, 9600 8N1) over BLE: a command written as
0xA0 [length][bytes] goes to the modem UART unchanged, and every line the modem prints comes back as one 0xA0
notification (gigo-MGT-Delphis app_protocol.c / nanomodem.c). Limits that follow from the firmware:
  - Delphis drops BLE writes over 64 bytes (APP_PROTOCOL_MAX_FRAME_LEN), so a modem command is at most 48 bytes
    (2-byte header + 3 AES blocks): 41 data bytes for $U / $M, 44 for $B (the NM3 itself takes 2..64).
  - Delphis cuts received lines at '#' and '$' and ends them at CR LF, so text must not contain '#' or '$'; binary
    data (pictures) is byte-stuffed by the image protocol for that reason.
  - 0xA1 means the modem refused the command: switched off, or still busy with the previous one.
Pictures use the camera project's protocol (img_protocol.py, a verbatim copy): OFFER / DATA / DONE frames, each with
a CRC-16 and stuffed, sent as $M (unicast with acknowledgement) to the receiving modem, which prints them as #U lines.
AcousticLink is the per-session service: it listens to the Delphis notifications whether or not a window is open,
keeps the message log, reassembles received pictures and saves them, and runs commands one at a time.
The command builders and the parser are pure and unit tested (tests/test_acoustic.py).
"""
from . import img_protocol as img
from .gigo_imu_codecs import NANO_MODEM_DATA
from .gigo_imu_codecs import NANO_MODEM_STATUS
from .logbus import log_system
from datetime import datetime
from datetime import timezone
import asyncio
import collections
import math
import os
import re
import time


# ---------------------------------------------------------------------------------------------------------------------
# Limits (see the module header)
# ---------------------------------------------------------------------------------------------------------------------
DELPHIS_COMMAND_MAX = 48
NM3_DATA_MIN = 2
NM3_DATA_MAX = 64
ADDRESS_MIN = 0
ADDRESS_MAX = 255
UNICAST_HEADER_LEN = 7
BROADCAST_HEADER_LEN = 4
UNICAST_DATA_MAX = DELPHIS_COMMAND_MAX - UNICAST_HEADER_LEN
BROADCAST_DATA_MAX = DELPHIS_COMMAND_MAX - BROADCAST_HEADER_LEN
TEXT_FORBIDDEN = "#$"
TEXT_PAD = b" "
PRINTABLE_FIRST = 0x20
PRINTABLE_LAST = 0x7E

# NM3 conversions (NM3 serial command manual, firmware 1.6.0)
VOLTS_PER_COUNT = 15.0 / 65536.0
SOUND_SPEED_M_S = 1500.0
RANGE_SECONDS_PER_COUNT = 3.125e-5
NOISE_VOLTS_PER_COUNT = 2.5 / 65536.0
NOISE_REFERENCE_DB = 145.0
DOPPLER_DIVISOR = 100000.0

# Message modes
MODE_UNICAST = "U"
MODE_ACK = "M"
MODE_BROADCAST = "B"
MODE_NAMES = {MODE_ACK: "unicast with acknowledge ($M)", MODE_UNICAST: "unicast ($U)",
              MODE_BROADCAST: "broadcast ($B)"}

# Timing
LOCAL_REPLY_TIMEOUT_S = 5.0
REMOTE_REPLY_TIMEOUT_S = 20.0
IMAGE_ATTEMPTS = 7
IMAGE_PAUSE_S = 0.3
LOG_LENGTH = 500
LINE_BUFFER_LIMIT = 512

DEFAULT_IMAGE_FOLDER = os.path.join(os.path.expanduser("~"), "Documents", "Succorfish BLE Monitor", "Delphis")


class AcousticError(ValueError):
  """A message or command that the modem or Delphis would refuse."""


# ---------------------------------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------------------------------
def check_address(address: int) -> int:
  """The modem address 0..255, else AcousticError."""
  if not isinstance(address, int) or not ADDRESS_MIN <= address <= ADDRESS_MAX:
    raise AcousticError(f"modem address must be {ADDRESS_MIN}..{ADDRESS_MAX}")
  return address


def cmd_status() -> bytes:
  """$? -> #A<addr>V<supply>R<release>B<build>."""
  return b"$?"


def cmd_set_address(address: int) -> bytes:
  """$A<xxx> -> #A<xxx> (stored in the modem)."""
  return b"$A%03d" % check_address(address)


def cmd_ping(address: int) -> bytes:
  """$P<xxx> -> #R<xxx>T<counts> (range) or #TO."""
  return b"$P%03d" % check_address(address)


def cmd_remote_battery(address: int) -> bytes:
  """$V<xxx> -> the remote modem broadcasts its battery voltage and noise (#B<xxx><yy>V..R..P..M..)."""
  return b"$V%03d" % check_address(address)


def cmd_noise() -> bytes:
  """$N -> #NR<rms>P<peak-peak>M<mean> over 1 s."""
  return b"$N"


def cmd_wake() -> bytes:
  """$W wakes the modem's receiver."""
  return b"$W"


def cmd_link_quality(enable: bool) -> bytes:
  """$XQE / $XQD: append quality and Doppler to received messages."""
  return b"$XQE" if enable else b"$XQD"


def cmd_test_message(address: int) -> bytes:
  """$T<xxx>: the remote modem broadcasts a test message."""
  return b"$T%03d" % check_address(address)


def cmd_message(mode: str, data: bytes, address: int = 0) -> bytes:
  """$U / $M <xxx><yy><data> or $B<yy><data>; data 2..64 bytes and the whole command at most 48 bytes (Delphis)."""
  if len(data) < NM3_DATA_MIN or len(data) > NM3_DATA_MAX:
    raise AcousticError(f"a modem message carries {NM3_DATA_MIN}..{NM3_DATA_MAX} bytes, got {len(data)}")
  if mode == MODE_BROADCAST:
    command = b"$B%02d" % len(data) + data
  elif mode in (MODE_UNICAST, MODE_ACK):
    command = b"$" + mode.encode("ascii") + b"%03d%02d" % (check_address(address), len(data)) + data
  else:
    raise AcousticError(f"unknown message mode {mode!r}")
  if len(command) > DELPHIS_COMMAND_MAX:
    raise AcousticError(f"command is {len(command)} bytes; Delphis passes at most {DELPHIS_COMMAND_MAX}")
  return command


def text_parts(text: str, mode: str) -> list:
  """Split a text message into modem payloads (41 bytes unicast, 44 broadcast; a 1-byte part is padded to 2).
  Printable ASCII only, without '#' and '$' (Delphis would cut the received line there).
  """
  if not text:
    raise AcousticError("empty message")
  bad = sorted({char for char in text if char in TEXT_FORBIDDEN or not PRINTABLE_FIRST <= ord(char) <= PRINTABLE_LAST})
  if bad:
    raise AcousticError(f"characters not allowed in a message: {''.join(bad)!r} (printable ASCII without # and $)")
  size = BROADCAST_DATA_MAX if mode == MODE_BROADCAST else UNICAST_DATA_MAX
  data = text.encode("ascii")
  parts = [data[start:start + size] for start in range(0, len(data), size)]
  return [part if len(part) >= NM3_DATA_MIN else part + TEXT_PAD for part in parts]


# ---------------------------------------------------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------------------------------------------------
STATUS_RE = re.compile(rb"^#A(\d{3})V(\d{5})R(\d{3}\.\d{3}\.\d{3})B(\S+)")
ADDRESS_RE = re.compile(rb"^#A(\d{3})$")
ACK_RE = re.compile(rb"^#R(\d{3})T(\d{5})")
NOISE_RE = re.compile(rb"^#NR(\d{6})P(\d{6})M(\d{6})")
LQI_RE = re.compile(rb"^#XQ([ED])")
ECHO_RE = re.compile(rb"^\$([A-Z?])(.*)$")
STATS_RE = re.compile(rb"^Q(\d{2})D([+-]\d{3})")
BATTERY_RE = re.compile(rb"^V(\d{5})R(\d{6})P(\d{6})M(\d{6})")
UNICAST_HEADER = 4
BROADCAST_HEADER = 7


def range_metres(counts: int, sound_speed: float = SOUND_SPEED_M_S) -> float:
  """Range from the NM3 16 kHz propagation counter (manual: counts x c x 3.125e-5)."""
  return counts * sound_speed * RANGE_SECONDS_PER_COUNT


def supply_volts(counts: int) -> float:
  """Modem supply voltage from its 16-bit monitor value."""
  return counts * VOLTS_PER_COUNT


def noise_db(adc_units: int) -> float:
  """Noise in dB re 1 uPa from ADC units (receive sensitivity about -145 dB)."""
  volts = max(adc_units, 1) * NOISE_VOLTS_PER_COUNT
  return 20.0 * math.log10(volts) + NOISE_REFERENCE_DB


def _stats(tail: bytes) -> dict:
  """Quality (0..99) and relative speed (m/s, positive = moving apart) appended when link quality is on."""
  found = STATS_RE.match(tail)
  if not found:
    return {}
  doppler = int(found.group(2))
  return {"quality": int(found.group(1)), "speed_m_s": SOUND_SPEED_M_S * doppler / DOPPLER_DIVISOR}


def parse_modem_line(line: bytes) -> dict:
  """One modem line (without CR LF) -> {'kind', ...}. Kinds: status, address, unicast, broadcast, battery, ack,
  timeout, noise, lqi, echo, error, unique_id, text.
  """
  line = bytes(line)
  if line.startswith(b"#U") and len(line) >= UNICAST_HEADER and line[2:4].isdigit():
    length = int(line[2:4])
    data = line[UNICAST_HEADER:UNICAST_HEADER + length]
    event = {"kind": "unicast", "data": data}
    event.update(_stats(line[UNICAST_HEADER + length:]))
    return event
  if line.startswith(b"#B") and len(line) >= BROADCAST_HEADER and line[2:7].isdigit():
    length = int(line[5:7])
    data = line[BROADCAST_HEADER:BROADCAST_HEADER + length]
    event = {"kind": "broadcast", "source": int(line[2:5]), "data": data}
    event.update(_stats(line[BROADCAST_HEADER + length:]))
    battery = BATTERY_RE.match(data)
    if battery:
      event.update(kind="battery", volts=supply_volts(int(battery.group(1))),
                   noise_db=noise_db(int(battery.group(2))))
    return event
  found = STATUS_RE.match(line)
  if found:
    return {"kind": "status", "address": int(found.group(1)), "volts": supply_volts(int(found.group(2))),
            "release": found.group(3).decode("ascii"), "build": found.group(4).decode("ascii", "replace")}
  found = ADDRESS_RE.match(line)
  if found:
    return {"kind": "address", "address": int(found.group(1))}
  found = ACK_RE.match(line)
  if found:
    counts = int(found.group(2))
    return {"kind": "ack", "address": int(found.group(1)), "counts": counts, "range_m": range_metres(counts)}
  if line == b"#TO":
    return {"kind": "timeout"}
  found = NOISE_RE.match(line)
  if found:
    rms = int(found.group(1))
    return {"kind": "noise", "rms": rms, "peak": int(found.group(2)), "mean": int(found.group(3)),
            "noise_db": noise_db(rms)}
  found = LQI_RE.match(line)
  if found:
    return {"kind": "lqi", "enabled": found.group(1) == b"E"}
  if line.startswith(b"#DID:"):
    return {"kind": "unique_id", "text": line[1:].decode("ascii", "replace")}
  if line == b"E":
    return {"kind": "error"}
  found = ECHO_RE.match(line)
  if found:
    return {"kind": "echo", "command": found.group(1).decode("ascii"), "rest": found.group(2)}
  return {"kind": "text", "text": line.decode("latin-1")}


def describe_event(event: dict) -> str:
  """One readable line for the log."""
  kind = event["kind"]
  stats = ""
  if "quality" in event:
    stats = f" (quality {event['quality']}, {event['speed_m_s']:+.2f} m/s)"
  if kind == "unicast":
    return f"message: {printable(event['data'])}{stats}"
  if kind == "broadcast":
    return f"broadcast from {event['source']:03d}: {printable(event['data'])}{stats}"
  if kind == "battery":
    return f"modem {event['source']:03d}: battery {event['volts']:.2f} V, noise {event['noise_db']:.0f} dB"
  if kind == "status":
    return f"modem {event['address']:03d}, supply {event['volts']:.2f} V, firmware {event['release']}"
  if kind == "address":
    return f"modem address set to {event['address']:03d}"
  if kind == "ack":
    return f"acknowledged by {event['address']:03d}, range {event['range_m']:.1f} m"
  if kind == "timeout":
    return "no acknowledgement (timeout)"
  if kind == "noise":
    return f"noise {event['noise_db']:.1f} dB (rms {event['rms']}, peak-peak {event['peak']})"
  if kind == "lqi":
    return f"link quality report {'on' if event['enabled'] else 'off'}"
  if kind == "error":
    return "modem: command refused (E)"
  if kind == "echo":
    return f"modem accepted ${event['command']}{event['rest'].decode('latin-1')}"
  if kind == "refused":
    return event["text"]
  return event.get("text", str(event))


def printable(data: bytes) -> str:
  """Text of message bytes; non-printable bytes as \\xNN."""
  return "".join(chr(value) if PRINTABLE_FIRST <= value <= PRINTABLE_LAST else f"\\x{value:02x}" for value in data)


class LineParser:
  """Bytes of the Delphis 0xA0 notifications -> complete modem lines. #U / #B lines are cut by their length digits,
  so a CR LF inside binary data does not end them; other lines end at CR or LF.
  """

  def __init__(self):
    self.buffer = bytearray()

  def feed(self, chunk: bytes) -> list:
    """Complete lines (without CR LF) found after adding chunk."""
    self.buffer += chunk
    lines = []
    while True:
      while self.buffer[:1] in (b"\r", b"\n"):
        del self.buffer[:1]
      if not self.buffer:
        break
      header = UNICAST_HEADER if self.buffer.startswith(b"#U") else (
        BROADCAST_HEADER if self.buffer.startswith(b"#B") else 0)
      if header:
        if len(self.buffer) < header:
          break
        digits = bytes(self.buffer[header - 2:header])
        if digits.isdigit() and (header == UNICAST_HEADER or bytes(self.buffer[2:5]).isdigit()):
          body_end = header + int(digits)
          if len(self.buffer) < body_end:
            break
          line_end = self._line_end(body_end)
          if line_end < 0:
            break
          lines.append(bytes(self.buffer[:line_end]))
          del self.buffer[:line_end]
          continue
      line_end = self._line_end(0)
      if line_end < 0:
        if len(self.buffer) > LINE_BUFFER_LIMIT:
          lines.append(bytes(self.buffer))
          self.buffer.clear()
        break
      lines.append(bytes(self.buffer[:line_end]))
      del self.buffer[:line_end]
    return lines

  def _line_end(self, start: int) -> int:
    """Index of the first CR or LF at or after start, -1 when none yet."""
    positions = [position for position in (self.buffer.find(b"\r", start), self.buffer.find(b"\n", start))
                 if position >= 0]
    return min(positions) if positions else -1


# ---------------------------------------------------------------------------------------------------------------------
# Pictures (image protocol of succorfish-Camera, see img_protocol.py)
# ---------------------------------------------------------------------------------------------------------------------
IMAGE_OPCODES = (img.OPCODE_OFFER, img.OPCODE_DATA, img.OPCODE_DONE)


def image_frame(data: bytes) -> "img.Frame | None":
  """The image-protocol frame carried by a received message, None when the message is not one (plain text)."""
  # DONE is 0x24 ('$'), so its stuffed form starts with the escape byte: decide on the un-stuffed frame.
  if not data or (data[0] not in IMAGE_OPCODES and data[0] != img.STUFF_ESCAPE):
    return None
  try:
    frame = img.parse_stuffed_frame(data)
  except img.ProtocolError:
    return None
  return frame if frame.opcode in IMAGE_OPCODES else None


def _command(address: int, opcode: int, packet_id: int, data: bytes) -> "bytes | None":
  """$M command of one frame, None when it is longer than Delphis passes."""
  stuffed = img.stuff(img.build_frame(opcode, packet_id, data))
  command = img.COMMAND_PREFIX + b"%03d%02d" % (address, len(stuffed)) + stuffed
  return command if len(command) <= DELPHIS_COMMAND_MAX else None


def image_commands(address: int, payload: bytes) -> list:
  """Every $M command of one picture transfer (OFFER, DATA..., DONE) sized for Delphis (48-byte commands): DATA
  chunks shrink until the stuffed command fits, and no chunk is shorter than the protocol's 3 bytes.
  """
  check_address(address)
  if len(payload) < img.MIN_DATA:
    raise AcousticError(f"a transfer carries at least {img.MIN_DATA} bytes")
  chunks = []
  offset = 0
  while offset < len(payload):
    remaining = len(payload) - offset
    size = min(img.MAX_DATA, remaining)
    while size > img.MIN_DATA and (_command(address, img.OPCODE_DATA, len(chunks), payload[offset:offset + size])
                                   is None or 0 < remaining - size < img.MIN_DATA):
      size -= 1
    chunks.append(payload[offset:offset + size])
    offset += size
  if len(chunks) > img.MAX_FRAGMENTS:
    raise AcousticError(f"{len(payload)} bytes need {len(chunks)} fragments; the protocol allows "
                        f"{img.MAX_FRAGMENTS}")
  checksum = img.crc32(payload)
  manifest = len(payload).to_bytes(2, "big") + bytes((img.RESERVED_BYTE, len(chunks))) + checksum.to_bytes(4, "big")
  commands = [_command(address, img.OPCODE_OFFER, 0, manifest)]
  commands += [_command(address, img.OPCODE_DATA, index, chunk) for index, chunk in enumerate(chunks)]
  commands.append(_command(address, img.OPCODE_DONE, len(chunks), checksum.to_bytes(4, "big") +
                           bytes((len(chunks),))))
  if any(command is None for command in commands):
    raise AcousticError("a frame does not fit in a Delphis command")
  return commands


def utc_stamp(epoch: "float | None" = None) -> str:
  """'YYYYmmdd_HHMMSS' in UTC for file names."""
  return datetime.fromtimestamp(epoch or time.time(), tz=timezone.utc).strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------------------------------------------------
class AcousticLink:
  """The acoustic link of one Delphis session (see the module header). Subscribers get (kind, data) on the BLE
  thread: 'line' (event dict), 'message' (log entry), 'progress' (text), 'image' (received picture record),
  'state' (modem status dict).
  """

  def __init__(self, session_id: str, image_folder: str = DEFAULT_IMAGE_FOLDER):
    self.session_id = session_id
    self.image_folder = image_folder
    self.device = None
    self.parser = LineParser()
    self.assembler = img.Assembler(on_problem=self._image_problem)
    self.log = collections.deque(maxlen=LOG_LENGTH)
    self.images = []
    self.modem = {}
    self._subscribers = []
    self._waiters = []
    self._lock = None

  # ---- wiring ------------------------------------------------------------------------------------------------------
  def attach(self, device) -> None:
    """Listen to a connected Delphis (Gigo4gDevice); the previous device, if any, is forgotten."""
    if device is self.device:
      return
    self.detach()
    self.device = device
    device.add_listener(NANO_MODEM_DATA, self._on_frame)
    device.add_listener(NANO_MODEM_STATUS, self._on_frame)

  def detach(self) -> None:
    """Stop listening; commands waiting for a reply end with None."""
    if self.device is not None:
      self.device.remove_listener(NANO_MODEM_DATA, self._on_frame)
      self.device.remove_listener(NANO_MODEM_STATUS, self._on_frame)
    self.device = None
    self.parser = LineParser()
    for _predicate, future in list(self._waiters):
      self._resolve(future, None)
    self._waiters.clear()

  def subscribe(self, callback) -> None:
    """callback(kind, data) for every event (BLE thread)."""
    if callback not in self._subscribers:
      self._subscribers.append(callback)

  def unsubscribe(self, callback) -> None:
    """Stop the callbacks to callback."""
    if callback in self._subscribers:
      self._subscribers.remove(callback)

  def _notify(self, kind: str, data) -> None:
    for callback in list(self._subscribers):
      try:
        callback(kind, data)
      except Exception as error:
        log_system(f"Acoustic link subscriber failed: {error}", "#64748b", self.session_id)

  # ---- receive -----------------------------------------------------------------------------------------------------
  def _on_frame(self, packet: bytes) -> None:
    """Device listener: a decrypted 0xA0 line or an 0xA1 refusal."""
    if not packet:
      return
    if packet[0] == NANO_MODEM_STATUS:
      self._handle({"kind": "refused", "text": "nano-modem refused the command: switched off or still busy"})
      return
    length = packet[1] if len(packet) > 1 else 0
    for line in self.parser.feed(bytes(packet[2:2 + length])):
      self._handle(parse_modem_line(line))

  def feed_line(self, line: bytes) -> None:
    """Handle one modem line (tests and replays)."""
    self._handle(parse_modem_line(line))

  def _handle(self, event: dict) -> None:
    """Log, reassemble pictures, update the modem state, wake the command waiting for this reply."""
    kind = event["kind"]
    if kind == "unicast":
      frame = image_frame(event["data"])
      if frame is not None:
        self._image_frame(frame, event)
      else:
        self._add_log("in", None, MODE_UNICAST, printable(event["data"]), event)
    elif kind == "broadcast":
      self._add_log("in", event["source"], MODE_BROADCAST, printable(event["data"]), event)
    elif kind == "status":
      self.modem.update(address=event["address"], volts=event["volts"], release=event["release"])
      self._notify("state", dict(self.modem))
    elif kind == "address":
      self.modem["address"] = event["address"]
      self._notify("state", dict(self.modem))
    elif kind == "lqi":
      self.modem["link_quality"] = event["enabled"]
      self._notify("state", dict(self.modem))
    self._notify("line", event)
    for waiter in list(self._waiters):
      predicate, future = waiter
      if predicate(event):
        self._waiters.remove(waiter)
        self._resolve(future, event)

  def _add_log(self, direction: str, peer, mode: str, text: str, event: "dict | None" = None,
               status: str = "") -> dict:
    """Append a message to the log and tell the subscribers."""
    entry = {"time": time.time(), "direction": direction, "peer": peer, "mode": mode, "text": text,
             "quality": (event or {}).get("quality"), "status": status}
    self.log.append(entry)
    self._notify("message", entry)
    return entry

  def _image_frame(self, frame, event: dict) -> None:
    """Feed a picture frame; a completed transfer is saved and announced."""
    completed = self.assembler.feed(frame)
    if completed is None:
      self._notify("progress", f"receiving picture: {self.assembler.progress()}")
      return
    record = {"time": completed.completed_at, "bytes": len(completed.payload), "crc32": completed.declared_crc32,
              "fragments": completed.fragment_count, "duplicates": completed.duplicates,
              "payload": completed.payload, "quality": event.get("quality"), "path": None}
    try:
      folder = os.path.join(self.image_folder, utc_stamp()[:8])
      os.makedirs(folder, exist_ok=True)
      record["path"] = os.path.join(folder, f"{utc_stamp(record['time'])}_{record['crc32']:08x}.latent")
      with open(record["path"], "wb") as handle:
        handle.write(completed.payload)
    except OSError as error:
      log_system(f"Picture not saved: {error}", "#f59e0b", self.session_id)
    self.images.append(record)
    self._notify("progress", f"picture received: {record['bytes']} B, CRC-32 {record['crc32']:08X}")
    self._notify("image", record)

  def _image_problem(self, text: str) -> None:
    """Assembler complaint (lost fragment, mismatch) to the log."""
    self._notify("progress", f"picture: {text}")

  # ---- commands ------------------------------------------------------------------------------------------------------
  @staticmethod
  def _resolve(future, value) -> None:
    """Complete a waiter's future on its own loop (the notification may come from another thread)."""
    def complete():
      if not future.done():
        future.set_result(value)
    try:
      future.get_loop().call_soon_threadsafe(complete)
    except RuntimeError:
      pass

  async def request(self, command: bytes, predicate, timeout: float) -> "dict | None":
    """Send one modem command and wait for the event predicate accepts (or an error / refusal); None on timeout or
    when not connected. Commands run one at a time.
    """
    if self._lock is None:
      self._lock = asyncio.Lock()
    async with self._lock:
      return await self._request_unlocked(command, predicate, timeout)

  async def _request_unlocked(self, command: bytes, predicate, timeout: float) -> "dict | None":
    device = self.device
    if device is None:
      return None
    future = asyncio.get_running_loop().create_future()
    waiter = (lambda event: event["kind"] in ("error", "refused") or predicate(event), future)
    self._waiters.append(waiter)
    try:
      await device.nano_modem_send_bytes(command)
      return await asyncio.wait_for(future, timeout)
    except asyncio.TimeoutError:
      return None
    finally:
      if waiter in self._waiters:
        self._waiters.remove(waiter)

  async def query_status(self) -> "dict | None":
    """$? -> status event."""
    return await self.request(cmd_status(), lambda event: event["kind"] == "status", LOCAL_REPLY_TIMEOUT_S)

  async def set_address(self, address: int) -> "dict | None":
    """$A -> address event."""
    return await self.request(cmd_set_address(address), lambda event: event["kind"] == "address",
                              LOCAL_REPLY_TIMEOUT_S)

  async def set_link_quality(self, enable: bool) -> "dict | None":
    """$XQE / $XQD -> lqi event."""
    return await self.request(cmd_link_quality(enable), lambda event: event["kind"] == "lqi", LOCAL_REPLY_TIMEOUT_S)

  async def wake(self) -> "dict | None":
    """$W -> echo."""
    return await self.request(cmd_wake(), lambda event: event["kind"] == "echo" and event["command"] == "W",
                              LOCAL_REPLY_TIMEOUT_S)

  async def noise(self) -> "dict | None":
    """$N -> noise event."""
    return await self.request(cmd_noise(), lambda event: event["kind"] == "noise", LOCAL_REPLY_TIMEOUT_S)

  async def ping(self, address: int) -> "dict | None":
    """$P -> ack (with range) or timeout event."""
    return await self.request(cmd_ping(address), lambda event: event["kind"] in ("ack", "timeout"),
                              REMOTE_REPLY_TIMEOUT_S)

  async def remote_battery(self, address: int) -> "dict | None":
    """$V -> battery event from that modem (None when it does not answer)."""
    return await self.request(cmd_remote_battery(address),
                              lambda event: event["kind"] == "battery" and event["source"] == address,
                              REMOTE_REPLY_TIMEOUT_S)

  async def send_raw(self, command: bytes) -> None:
    """A console line as typed (its replies only go to the log)."""
    device = self.device
    if device is None:
      raise AcousticError("not connected")
    if self._lock is None:
      self._lock = asyncio.Lock()
    async with self._lock:
      await device.nano_modem_send_bytes(command)

  async def send_message(self, text: str, mode: str, address: int = 0) -> list:
    """Send a text in as many parts as needed. Returns one result per part: 'delivered (x m)', 'sent', 'no
    acknowledgement', 'refused' or 'no answer'. The message is logged with the overall result.
    """
    parts = text_parts(text, mode)
    commands = [cmd_message(mode, part, address) for part in parts]
    results = []
    for command in commands:
      if mode == MODE_ACK:
        event = await self.request(command, lambda event: event["kind"] in ("ack", "timeout"), REMOTE_REPLY_TIMEOUT_S)
      else:
        expected = mode
        event = await self.request(command, lambda event: event["kind"] == "echo" and event["command"] == expected,
                                   LOCAL_REPLY_TIMEOUT_S)
      results.append(self._result_text(event))
      if event is None or event["kind"] in ("error", "refused", "timeout"):
        break
    status = results[-1] if len(results) == len(commands) else f"stopped after part {len(results)}: {results[-1]}"
    self._add_log("out", None if mode == MODE_BROADCAST else address, mode, text, status=status)
    return results

  @staticmethod
  def _result_text(event: "dict | None") -> str:
    """Outcome of one sent part."""
    if event is None:
      return "no answer"
    if event["kind"] == "ack":
      return f"delivered ({event['range_m']:.1f} m)"
    if event["kind"] == "timeout":
      return "no acknowledgement"
    if event["kind"] in ("error", "refused"):
      return "refused"
    return "sent"

  async def send_payload(self, address: int, payload: bytes, progress=None, cancel=None) -> dict:
    """Send a picture (or any 3..~9000 bytes) with the image protocol: every $M is retried up to IMAGE_ATTEMPTS times
    until the receiving modem acknowledges it. progress(done, total, text) after each frame; cancel() -> True stops.
    Returns {'result': 'DELIVERED' | 'FAILED' | 'CANCELLED', 'frames', 'retries', 'seconds', 'crc32'}.
    """
    commands = image_commands(address, payload)
    started = time.monotonic()
    retries = 0
    result = "DELIVERED"
    for index, command in enumerate(commands):
      delivered = False
      for attempt in range(IMAGE_ATTEMPTS):
        if cancel is not None and cancel():
          result = "CANCELLED"
          break
        event = await self.request(command, lambda event: event["kind"] in ("ack", "timeout"), REMOTE_REPLY_TIMEOUT_S)
        if event is not None and event["kind"] == "ack":
          delivered = True
          break
        retries += 1
        await asyncio.sleep(IMAGE_PAUSE_S)
      if result == "CANCELLED":
        break
      if not delivered:
        result = "FAILED"
        break
      if progress is not None:
        progress(index + 1, len(commands), f"frame {index + 1}/{len(commands)} delivered")
    summary = {"result": result, "frames": len(commands), "retries": retries,
               "seconds": time.monotonic() - started, "crc32": img.crc32(payload), "bytes": len(payload)}
    self._add_log("out", address, MODE_ACK, f"[picture {len(payload)} B, CRC-32 {summary['crc32']:08X}]",
                  status=f"{result}, {summary['seconds']:.0f} s, {retries} retries")
    return summary


_LINKS = {}


def link_for(session_id: str) -> AcousticLink:
  """The acoustic link of a session (created on first use)."""
  if session_id not in _LINKS:
    _LINKS[session_id] = AcousticLink(session_id)
  return _LINKS[session_id]


def drop_link(session_id: str) -> None:
  """Forget a session's link (its tab closed)."""
  link = _LINKS.pop(session_id, None)
  if link is not None:
    link.detach()
