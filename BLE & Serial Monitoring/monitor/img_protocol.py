# ******************************************************************************
# @file img_protocol.py
# @brief Delphis IMG protocol (OFFER / DATA / DONE) of the STM32N657 sender: framing, CRCs, stuffing, reassembly
# @version 1.0.0
# @date 2026-11-06
#
# Python twin of the firmware's delphis_img_protocol.c / delphis_img_tx.c (and of gigo_image.py v1.5, proven on
# the bench): the inner frame OPCODE, PACKET_ID, LEN, DATA, CRC-16 is byte-stuffed and sent as "$M<aaa><yy>..."
# to the receiving modem, which hands it to its host as "#U<yy><stuffed frame>". The builder side exists for the
# simulator and the tests; it must produce exactly what the firmware sends.
# ******************************************************************************

import binascii
import enum
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

# ---- wire constants (delphis_img_protocol.h) -----------------------------------------------------------------------
OPCODE_OFFER = 0x21
OPCODE_DATA = 0x22
OPCODE_DONE = 0x24
FRAME_HEADER_BYTES = 3                # OPCODE + PACKET_ID + LEN
FRAME_CRC_BYTES = 2                   # CRC-16 big endian
MANIFEST_BYTES = 8                    # OFFER: length(2 BE) reserved(1) fragmentCount(1) crc32(4 BE)
DONE_BYTES = 5                        # DONE: crc32(4 BE) fragmentCount(1)
MIN_DATA = 3
MAX_DATA = 43
CHUNK_SHRINK_STEP = 8
MODEM_PAYLOAD_MAX = 64                # two ASCII digits <yy>: the Delphis hardware ceiling
MAX_FRAGMENTS = 255                   # the fragment count is one byte
RESERVED_BYTE = 0x00
STUFF_ESCAPE = 0x7D
STUFF_XOR_MASK = 0x20
STUFF_SPECIAL_BYTES = frozenset({0x23, 0x24, 0xA0, 0xA1, STUFF_ESCAPE})   # '#', '$' reset the Gigo's line buffer
COMMAND_PREFIX = b"$M"
ADDRESS_DIGITS = 3
LENGTH_DIGITS = 2
UNSOLICITED_TOKENS = (b"#U", b"#B")   # unicast / broadcast message received by the modem
LINE_ENDINGS = (b"\r", b"\n")
TEXT_BUFFER_LIMIT = 256             # bytes of text without a line end before they are dropped
CRC16_INITIAL = 0xFFFF
CRC16_POLYNOMIAL = 0x1021
CRC16_TOP_BIT = 0x8000
CRC16_MASK = 0xFFFF
CRC32_MASK = 0xFFFFFFFF
BITS_PER_BYTE = 8
BYTE_MASK = 0xFF


class ProtocolStatus(enum.IntEnum):
  OK = 0
  BAD_STUFFING = 1
  BAD_LENGTH = 2
  BAD_CRC = 3
  BAD_COMMAND = 4


class ProtocolError(Exception):
  """A frame or a transfer violated the protocol; status says which rule."""

  def __init__(self, status, message):
    super().__init__(message)
    self.status = status


# ---- checksums and stuffing ----------------------------------------------------------------------------------------
def crc16_ccitt_false(data: bytes) -> int:
  """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final XOR): "123456789" -> 0x29B1."""
  crc = CRC16_INITIAL
  for value in data:
    crc ^= value << BITS_PER_BYTE
    for _ in range(BITS_PER_BYTE):
      crc = ((crc << 1) ^ CRC16_POLYNOMIAL) & CRC16_MASK if crc & CRC16_TOP_BIT else (crc << 1) & CRC16_MASK
  return crc


def crc32(data: bytes) -> int:
  """CRC-32/ISO-HDLC (zlib): "123456789" -> 0xCBF43926; the payload checksum in OFFER and DONE."""
  return binascii.crc32(data) & CRC32_MASK


def stuff(data: bytes) -> bytes:
  """Escape '#', '$', 0xA0, 0xA1 and 0x7D as 0x7D, byte ^ 0x20 (DelphisImgStuffFrame)."""
  out = bytearray()
  for value in data:
    if value in STUFF_SPECIAL_BYTES:
      out += bytes((STUFF_ESCAPE, value ^ STUFF_XOR_MASK))
    else:
      out.append(value)
  return bytes(out)


def unstuff(data: bytes) -> bytes:
  """Reverse stuff(); a dangling escape byte is a damaged frame."""
  out = bytearray()
  index = 0
  while index < len(data):
    if data[index] == STUFF_ESCAPE:
      if index + 1 >= len(data):
        raise ProtocolError(ProtocolStatus.BAD_STUFFING, "dangling escape byte")
      out.append(data[index + 1] ^ STUFF_XOR_MASK)
      index += 2
    else:
      out.append(data[index])
      index += 1
  return bytes(out)


# ---- frames --------------------------------------------------------------------------------------------------------
@dataclass
class Frame:
  opcode: int
  packet_id: int
  data: bytes
  address: int = 0


def parse_stuffed_frame(stuffed: bytes, address: int = 0) -> Frame:
  """The inner frame as the receiving modem delivers it: un-stuff, check LEN and the CRC-16."""
  frame = unstuff(stuffed)
  if len(frame) < FRAME_HEADER_BYTES + MIN_DATA + FRAME_CRC_BYTES:
    raise ProtocolError(ProtocolStatus.BAD_LENGTH, f"frame too short ({len(frame)} B)")
  opcode, packet_id, length = frame[0], frame[1], frame[2]
  if not (MIN_DATA <= length <= MAX_DATA) or len(frame) != FRAME_HEADER_BYTES + length + FRAME_CRC_BYTES:
    raise ProtocolError(ProtocolStatus.BAD_LENGTH, f"LEN field {length} does not match the frame size {len(frame)}")
  wire_crc = (frame[-2] << BITS_PER_BYTE) | frame[-1]
  computed = crc16_ccitt_false(frame[:-FRAME_CRC_BYTES])
  if wire_crc != computed:
    raise ProtocolError(ProtocolStatus.BAD_CRC, f"CRC-16 mismatch (wire {wire_crc:04X}, computed {computed:04X})")
  return Frame(opcode, packet_id, bytes(frame[FRAME_HEADER_BYTES:FRAME_HEADER_BYTES + length]), address)


def parse_command(command: bytes) -> Frame:
  """'$M' + 3 ASCII digits (address) + 2 ASCII digits (stuffed length) + stuffed frame (what the sender writes)."""
  header = len(COMMAND_PREFIX) + ADDRESS_DIGITS + LENGTH_DIGITS
  if len(command) < header or not command.startswith(COMMAND_PREFIX):
    raise ProtocolError(ProtocolStatus.BAD_COMMAND, f"not a $M command: {command[:header]!r}")
  address_text = command[len(COMMAND_PREFIX):len(COMMAND_PREFIX) + ADDRESS_DIGITS]
  length_text = command[len(COMMAND_PREFIX) + ADDRESS_DIGITS:header]
  if not (address_text.isdigit() and length_text.isdigit()):
    raise ProtocolError(ProtocolStatus.BAD_COMMAND, "address / length digits are not ASCII decimals")
  stuffed = command[header:]
  if len(stuffed) != int(length_text):
    raise ProtocolError(ProtocolStatus.BAD_COMMAND, f"<yy>={int(length_text)} but {len(stuffed)} bytes follow")
  return parse_stuffed_frame(stuffed, int(address_text))


def build_frame(opcode: int, packet_id: int, data: bytes) -> bytes:
  """Unstuffed inner frame with its CRC-16 (big endian)."""
  frame = bytes((opcode, packet_id, len(data))) + data
  crc = crc16_ccitt_false(frame)
  return frame + bytes((crc >> BITS_PER_BYTE, crc & BYTE_MASK))


def build_command(address: int, opcode: int, packet_id: int, data: bytes) -> Optional[bytes]:
  """'$M<aaa><yy><stuffed frame>' or None when the stuffed frame exceeds the 64-byte modem payload."""
  stuffed = stuff(build_frame(opcode, packet_id, data))
  if len(stuffed) > MODEM_PAYLOAD_MAX:
    return None
  return COMMAND_PREFIX + f"{address:0{ADDRESS_DIGITS}d}{len(stuffed):0{LENGTH_DIGITS}d}".encode("ascii") + stuffed


def pick_chunk_length(address: int, payload: bytes, offset: int, packet_id: int) -> int:
  """DelphisImgPickChunkLength(): 43 B, shrunk by 8 (floor 3) until the stuffed DATA command fits 64 B."""
  candidate = min(MAX_DATA, len(payload) - offset)
  while True:
    if build_command(address, OPCODE_DATA, packet_id, payload[offset:offset + candidate]) is not None:
      return candidate
    if candidate <= MIN_DATA:
      return MIN_DATA
    candidate = candidate - CHUNK_SHRINK_STEP if candidate > MIN_DATA + CHUNK_SHRINK_STEP else MIN_DATA


def build_transfer(address: int, payload: bytes) -> List[bytes]:
  """Every $M command of one transfer in order (OFFER, DATA..., DONE), as DelphisImgTxSend() writes them."""
  chunks = []
  offset = 0
  while offset < len(payload):
    length = pick_chunk_length(address, payload, offset, len(chunks))
    chunks.append(payload[offset:offset + length])
    offset += length
  if not chunks or len(chunks) > MAX_FRAGMENTS:
    raise ProtocolError(ProtocolStatus.BAD_LENGTH, f"{len(payload)} B cannot be sent in 1..255 fragments")
  checksum = crc32(payload)
  manifest = (len(payload).to_bytes(2, "big") + bytes((RESERVED_BYTE, len(chunks))) + checksum.to_bytes(4, "big"))
  commands = [build_command(address, OPCODE_OFFER, 0, manifest)]
  commands += [build_command(address, OPCODE_DATA, index, chunk) for index, chunk in enumerate(chunks)]
  commands.append(build_command(address, OPCODE_DONE, len(chunks), checksum.to_bytes(4, "big") + bytes((len(chunks),))))
  return commands


def command_to_unsolicited(command: bytes) -> bytes:
  """What the receiving modem prints for a $M it received: '#U<yy><stuffed frame>' plus CR LF."""
  stuffed = command[len(COMMAND_PREFIX) + ADDRESS_DIGITS + LENGTH_DIGITS:]
  return UNSOLICITED_TOKENS[0] + f"{len(stuffed):0{LENGTH_DIGITS}d}".encode("ascii") + stuffed + b"\r\n"


# ---- reassembly ----------------------------------------------------------------------------------------------------
@dataclass
class Transfer:
  declared_length: int = 0
  fragment_count: int = 0
  declared_crc32: int = 0
  reserved: int = 0
  fragments: Dict[int, bytes] = field(default_factory=dict)
  payload: Optional[bytes] = None
  duplicates: int = 0
  frames: int = 0
  started_at: float = 0.0
  completed_at: float = 0.0


class Assembler:
  """Feed frames in arrival order; on_complete(transfer) is called once per transfer whose CRC-32 checks out.

  A lost acknowledgement makes the sender repeat a frame, so duplicates (same bytes) are counted and ignored; a
  repeated OFFER of the running transfer keeps the fragments already received. A new OFFER abandons an incomplete
  transfer: the acoustic link drops whole packets, and the sender then starts the next picture.
  """

  def __init__(self, on_complete: Optional[Callable[[Transfer], None]] = None,
               on_problem: Optional[Callable[[str], None]] = None) -> None:
    self.current: Optional[Transfer] = None
    self.completed: List[Transfer] = []
    self.problems: List[str] = []
    self.abandoned = 0
    self._on_complete = on_complete
    self._on_problem = on_problem

  def feed(self, frame: Frame) -> Optional[Transfer]:
    """Process one frame; returns the transfer it completed, if any."""
    if frame.opcode == OPCODE_OFFER:
      self._offer(frame)
      return None
    if self.current is None:
      self._problem(f"{'DATA' if frame.opcode == OPCODE_DATA else 'DONE'} id {frame.packet_id} without an OFFER")
      return None
    self.current.frames += 1
    if frame.opcode == OPCODE_DATA:
      self._data(frame)
      return None
    if frame.opcode == OPCODE_DONE:
      return self._done(frame)
    self._problem(f"unknown opcode 0x{frame.opcode:02X}")
    return None

  def progress(self) -> str:
    """Short text of the running transfer for a status line."""
    if self.current is None:
      return "idle"
    return f"{len(self.current.fragments)}/{self.current.fragment_count} fragments of {self.current.declared_length} B"

  def _offer(self, frame: Frame) -> None:
    if len(frame.data) != MANIFEST_BYTES or frame.packet_id != 0:
      self._problem("malformed OFFER")
      return
    transfer = Transfer(declared_length=int.from_bytes(frame.data[0:2], "big"), fragment_count=frame.data[3],
                        declared_crc32=int.from_bytes(frame.data[4:8], "big"), reserved=frame.data[2],
                        started_at=time.time())
    if self.current is not None and self.current.declared_crc32 == transfer.declared_crc32:
      self.current.frames += 1                    # the same OFFER again (its acknowledgement was lost)
      return
    if self.current is not None:
      self.abandoned += 1
      self._problem(f"new OFFER while {self.progress()} were pending: previous transfer abandoned")
    transfer.frames = 1
    self.current = transfer

  def _data(self, frame: Frame) -> None:
    known = self.current.fragments.get(frame.packet_id)
    if known is not None:
      if known != frame.data:
        self._problem(f"fragment {frame.packet_id} repeated with different bytes (kept the first)")
      self.current.duplicates += 1
      return
    if frame.packet_id >= self.current.fragment_count:
      self._problem(f"fragment {frame.packet_id} outside 0..{self.current.fragment_count - 1}")
      return
    self.current.fragments[frame.packet_id] = frame.data

  def _done(self, frame: Frame) -> Optional[Transfer]:
    transfer = self.current
    if len(frame.data) != DONE_BYTES:
      self._problem("malformed DONE")
      return None
    if (frame.packet_id != transfer.fragment_count or frame.data[4] != transfer.fragment_count or
        int.from_bytes(frame.data[:4], "big") != transfer.declared_crc32):
      self._problem("DONE does not match its OFFER (count or CRC-32)")
      return None
    missing = [index for index in range(transfer.fragment_count) if index not in transfer.fragments]
    if missing:
      self._problem(f"DONE with missing fragments {missing}: transfer dropped")
      self.current = None
      return None
    payload = b"".join(transfer.fragments[index] for index in range(transfer.fragment_count))
    if len(payload) != transfer.declared_length or crc32(payload) != transfer.declared_crc32:
      self._problem(f"reassembled {len(payload)} B / crc32 {crc32(payload):08X} do not match OFFER "
                    f"{transfer.declared_length} B / {transfer.declared_crc32:08X}")
      self.current = None
      return None
    transfer.payload = payload
    transfer.completed_at = time.time()
    self.completed.append(transfer)
    self.current = None
    if self._on_complete is not None:
      self._on_complete(transfer)
    return transfer

  def _problem(self, text: str) -> None:
    self.problems.append(text)
    if self._on_problem is not None:
      self._on_problem(text)


# ---- byte streams of a receiving modem -----------------------------------------------------------------------------
class ModemStreamParser:
  """Raw bytes from a receiving Delphis modem (serial, or decrypted Gigo notifications) -> stuffed frames.

  "#U<yy><data>": two ASCII digits give the exact number of raw bytes that follow, so binary payloads are safe.
  Everything else the modem prints (#R, #A, #TO, echoes) is returned as text lines for the log.
  """

  def __init__(self) -> None:
    self.buffer = bytearray()
    self.messages = 0
    self.skipped_bytes = 0

  def feed(self, chunk: bytes):
    """Returns (stuffed frames, text lines) completed by chunk, each in arrival order."""
    self.buffer += chunk
    frames: List[bytes] = []
    lines: List[bytes] = []
    header = len(UNSOLICITED_TOKENS[0]) + LENGTH_DIGITS
    while True:
      start = self._find_token()
      if start < 0:
        self._take_text(len(self.buffer), lines, whole=False)
        break
      if start > 0:
        self._take_text(start, lines, whole=True)
      if len(self.buffer) < header:
        break
      digits = bytes(self.buffer[len(UNSOLICITED_TOKENS[0]):header])
      if not digits.isdigit() or int(digits) > MODEM_PAYLOAD_MAX:
        self.skipped_bytes += 1
        del self.buffer[:1]                         # not a real message: resynchronise one byte on
        continue
      length = int(digits)
      if len(self.buffer) < header + length:
        break                                       # payload still arriving
      frames.append(bytes(self.buffer[header:header + length]))
      self.messages += 1
      del self.buffer[:header + length]
      while self.buffer[:1] in LINE_ENDINGS:
        del self.buffer[:1]
    return frames, lines

  def _find_token(self) -> int:
    positions = [self.buffer.find(token) for token in UNSOLICITED_TOKENS]
    found = [position for position in positions if position >= 0]
    return min(found) if found else -1

  def _take_text(self, limit: int, lines: List[bytes], whole: bool) -> None:
    """Move text from buffer[:limit] to lines: all of it when whole, else only complete lines (a partial line may
    still become a "#U" message)."""
    text = bytes(self.buffer[:limit])
    end = len(text)
    if not whole:
      end = max(text.rfind(b"\n"), text.rfind(b"\r")) + 1
      if end == 0 and len(text) > TEXT_BUFFER_LIMIT:
        end = len(text) - 1                         # no line end in sight: drop it, keep a possible '#'
    for line in re.split(rb"[\r\n]+", text[:end]):
      if line.strip():
        lines.append(line.strip())
    del self.buffer[:end]
