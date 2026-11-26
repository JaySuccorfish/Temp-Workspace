"""@file sc2_protocol.py
@brief BLE protocol of the GiGO Depth & Temperature logger (nRF52805), used by the Depth & Temp window
@version 2.1
@date 2026-11-22

The module and class keep their historic "SC2" names (older builds of the monitor called this logger "SC2 logger");
it is the GiGO Depth & Temp device, profile key 'gigo_dt' in monitor/device_profiles.py. Authoritative protocol:
gigo-depth-temp/Documents/Firmware_Reference/03_BLE_Protocol.md.

GIGO Depth & Temperature (SC2 / nRF52805) logger protocol.

Source of truth: `gigo-depth-temp/Documents/PROTOCOL.md`. Every layout below is taken from that
document -- not inferred -- so field offsets and units match the firmware.

The device is a **Nordic nRF52805 depth/temperature logger**, NOT an STM32WB Valtrack unit. It
shares the challenge/response formula and the 2-byte frame header with Valtrack, but nothing else:
different characteristic, different opcodes, different payloads. That is why the app exposes a
device-profile selector rather than auto-detecting.

Transport
    service 0xab00, ONE characteristic for both directions (phone writes, device notifies).
    There is no separate TX/RX characteristic pair.

Frame (both directions)
    [opcode:1][length:1][data:length]
    length is the POST-encryption (padded) length for post-auth frames.

Auth (§3.1 -- plaintext, always)
    0x01 (empty)                 -> 1 byte challenge
    0x02 + u32 LE f(challenge)   -> 1 byte status (1 = accepted)
    0xFE (empty)                 -> factory reset, accepted even PRE-auth

    f(challenge): the shared challenge transform, see derived_key_value() below (not repeated here)

Post-auth payloads are AES-128-ECB, zero-padded to 16 bytes. data[0] and the length byte are
never encrypted. data[0] of every response is a status byte (§4).

NOTE ON A KNOWN DOC/CLIENT DISAGREEMENT
    PROTOCOL.md §5 says every post-auth response begins with a status byte, including 0x04 and
    0x09. The reference client (`test_logger.py`) reads those two with a status-less path. Both
    are implemented here (`send` vs `send_raw`) and the UI reports which one produced a value,
    rather than guessing.
"""
import asyncio
import struct

from monitor.config_codecs import gigo_decode_config
from monitor.config_codecs import gigo_encode_config

from Crypto.Cipher import AES

# ---------------------------------------------------------------------------------------------
# GATT
# ---------------------------------------------------------------------------------------------
SC2_SERVICE_UUID = "0000ab00-0143-0800-0008-e5f9b34fb000"
SC2_CHAR_UUID    = "0000ab05-0143-0800-0008-e5f9b34fb000"   # write + notify

AES_BLOCK = 16

# Fallback key. main.c compiles its own CryptoAes128Key; the caller normally passes the real one.
AES_KEY_SC2 = bytes([
    0x2B, 0x7E, 0x15, 0x16, 0x28, 0xAE, 0xD2, 0xA6,
    0xAB, 0xF7, 0x15, 0x88, 0x09, 0xCF, 0x4F, 0x3C,
])

# ---------------------------------------------------------------------------------------------
# Opcodes (§5 device control, §6 logger/sensor)
# ---------------------------------------------------------------------------------------------
OP_READ_AUTH_KEY   = 0x01
OP_SET_AUTH_VALUE  = 0x02

OP_DEVICE_RESET      = 0x03
OP_BATTERY_PCT       = 0x04
OP_DEVICE_CONFIG_SET = 0x05
OP_DEVICE_CONFIG_GET = 0x06
OP_REC_MODE_SET      = 0x07
OP_REC_MODE_GET      = 0x08
OP_SANITY_TEST       = 0x09
OP_RESET_DEFAULT     = 0xFE

OP_KELLER_READ_PRESSURE = 0x10
OP_KELLER_READ_TEMP     = 0x11
OP_KELLER_READ_ALL      = 0x12
OP_KELLER_GET_INFO      = 0x13

OP_START         = 0x20
OP_STOP          = 0x21
OP_SET_INTERVAL  = 0x22
OP_GET_INTERVAL  = 0x23
OP_SET_RTC       = 0x24
OP_GET_RTC       = 0x25
OP_GET_STATS     = 0x26
OP_RETRIEVE_DATA = 0x27
OP_CLEAR_DATA    = 0x28
OP_FORMAT        = 0x29
OP_GET_ERROR_LOG = 0x2A

OP_NAMES = {
    OP_DEVICE_RESET: "Device reset", OP_BATTERY_PCT: "Battery %",
    OP_DEVICE_CONFIG_SET: "Config set", OP_DEVICE_CONFIG_GET: "Config get",
    OP_REC_MODE_SET: "Rec mode set", OP_REC_MODE_GET: "Rec mode get",
    OP_SANITY_TEST: "Sanity test", OP_RESET_DEFAULT: "Factory reset",
    OP_KELLER_READ_PRESSURE: "Pressure", OP_KELLER_READ_TEMP: "Temperature",
    OP_KELLER_READ_ALL: "All sensors", OP_KELLER_GET_INFO: "Sensor info",
    OP_START: "Start", OP_STOP: "Stop",
    OP_SET_INTERVAL: "Set interval", OP_GET_INTERVAL: "Get interval",
    OP_SET_RTC: "Set RTC", OP_GET_RTC: "Get RTC", OP_GET_STATS: "Stats",
    OP_RETRIEVE_DATA: "Retrieve", OP_CLEAR_DATA: "Clear", OP_FORMAT: "Format",
    OP_GET_ERROR_LOG: "Error log",
}

# §4 response status codes
STATUS_SUCCESS     = 0x00
STATUS_ERROR       = 0x01
STATUS_INVALID     = 0x02
STATUS_NOT_READY   = 0x03
STATUS_NO_DATA     = 0x04

STATUS_NAMES = {
    STATUS_SUCCESS:   "SUCCESS",
    STATUS_ERROR:     "ERROR",
    STATUS_INVALID:   "INVALID_PARAM",
    STATUS_NOT_READY: "NOT_READY",
    STATUS_NO_DATA:   "NO_DATA",
}

# §6.1 SET_INTERVAL bounds
INTERVAL_MIN, INTERVAL_MAX = 1, 3600
DURATION_MIN, DURATION_MAX = 600, 864000        # 10 min .. 10 days; 0 is refused (INVALID_PARAM) by the logger
REC_MODE_INFINITE = 0x04                         # endless logging is recording-mode bit 2, not duration 0

# §6.3 Keller channel names
KELLER_PRESSURE_CHANNELS = {0: "P1", 1: "P2"}
KELLER_TEMP_CHANNELS     = {0: "T", 1: "TOB1", 2: "TOB2"}

# Depth as the logger's own state detector computes it (state_detector.h): the Keller P1 reads gauge pressure that
# sits at 0.061 bar at the surface, and the hydrostatic gradient is 0.09807 bar per metre (fresh water).
SURFACE_PRESSURE_BAR = 0.061
BAR_PER_METRE = 0.09807

# 0x12 KELLER_READ_ALL reply after the status byte (ble_commands.c BleCmdKellerReadAll): success, Keller return code,
# valid-flag byte, Keller status byte, P/T status byte, then six little-endian floats.
SNAPSHOT_HEADER_LEN = 5
SNAPSHOT_FLOATS = ("p1_bar", "p2_bar", "tob1_c", "tob2_c", "t_c", "ch0")
SNAPSHOT_VALID_BITS = {"p1_bar": 0x01, "p2_bar": 0x02, "tob1_c": 0x04, "tob2_c": 0x08, "t_c": 0x10, "ch0": 0x20}

# Record byte 12: the deployment state (data_logger_types.h LogState_t).
LOG_STATE_NAMES = {0: "idle", 1: "deploying", 2: "submerged", 3: "retrieving", 4: "recovered", 5: "error"}

# §5.1 sanity err_flags bits
SANITY_FLAG_TEXT = {
    0x01: "battery below critical threshold",
    0x02: "Keller not initialised",
    0x04: "fewer than 10 I2C devices responded",
}

# §7 I2C bitmap
I2C_DEVICES = {0: "Keller DPT (0x40)"}
I2C_DEVICES.update({b: f"24LC512 EEPROM #{b - 1} (0x{b + 0x4F:02X})" for b in range(1, 9)})
I2C_DEVICES[9] = "ICM-20948 IMU (0x68)"

# §6.2.2 error-log counter names, in wire order
ERROR_COUNTER_NAMES = [
    "Keller timeouts", "Keller CRC errors", "Keller sensor faults", "Keller out-of-range",
    "EEPROM write failures", "EEPROM CRC errors", "I2C bus errors", "Battery-critical events",
]


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
def derived_key_value(challenge: int) -> int:
    """§3.1 -- the firmware's exact formula, wrapped to uint32."""
    return (((challenge * 79 + 8294) * 19) - (573 * challenge + 989)) & 0xFFFFFFFF


def depth_from_pressure(pressure_bar: float) -> float:
    """Depth in metres from the logged gauge pressure, the way the logger's state detector converts it (never below
    the surface).
    """
    return max(0.0, (pressure_bar - SURFACE_PRESSURE_BAR) / BAR_PER_METRE)


def decode_keller_snapshot(data: bytes) -> "dict | None":
    """Decode the 0x12 reply data after the status byte: {'success', 'keller_code', 'valid', 'keller_status',
    'pt_status', 'p1_bar' ... 'ch0' (None when its valid bit is clear), 'depth_m'}; None when too short.
    """
    if not data or len(data) < 2:
        return None
    out = {"success": bool(data[0]), "keller_code": data[1]}
    if not out["success"] or len(data) < SNAPSHOT_HEADER_LEN + 4 * len(SNAPSHOT_FLOATS):
        return out
    valid = data[2]
    out.update(valid=valid, keller_status=data[3], pt_status=data[4])
    values = struct.unpack_from("<6f", data, SNAPSHOT_HEADER_LEN)
    for key, value in zip(SNAPSHOT_FLOATS, values):
        out[key] = value if valid & SNAPSHOT_VALID_BITS[key] else None
    out["depth_m"] = depth_from_pressure(out["p1_bar"]) if out["p1_bar"] is not None else None
    return out


def crc8_maxim(data: bytes) -> int:
    """CRC-8/MAXIM over a record's bytes 0..13 (§8)."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc >> 1) ^ 0x8C) if (crc & 0x01) else (crc >> 1)
    return crc & 0xFF


class Sc2Device:
    """Depth & temperature logger on an already-connected BleakClient."""

    def __init__(self, client, aes_key=None, char_uuid=SC2_CHAR_UUID):
        """Remember the BLE client and the single SC2 characteristic; not authenticated yet."""
        self.client = client
        self.char_uuid = char_uuid
        self.key = aes_key or AES_KEY_SC2
        self._pending = asyncio.Queue()
        self.authenticated = False
        self.last_status = None
        # One command at a time: the logger window and the live view in the device tab share this link, and a reply
        # carries no request id, so two overlapping commands would take each other's replies.
        self._cmd_lock = asyncio.Lock()
        # Opcodes the logger may send without being asked (the gas logger's live stream); never taken as a reply.
        self.unsolicited_opcodes = set()

    # ---- connection -------------------------------------------------------------------
    async def start(self):
        await self.client.start_notify(self.char_uuid, self._on_notify)

    async def stop(self):
        """Unsubscribe from notifications (errors ignored: the link may already be gone)."""
        try:
            await self.client.stop_notify(self.char_uuid)
        except Exception:
            pass

    def _on_notify(self, _handle, data: bytearray):
        """Notification callback: queue the frame for the command waiting on it."""
        self._pending.put_nowait(bytes(data))

    # ---- crypto (a fresh cipher per call -- reuse yields garbage) ----------------------
    def _encrypt(self, data: bytes) -> bytes:
        if not data:
            return b""
        pad = (-len(data)) % AES_BLOCK
        return AES.new(self.key, AES.MODE_ECB).encrypt(data + b"\x00" * pad)

    def _decrypt(self, data: bytes) -> bytes:
        """AES-decrypt a post-auth payload; empty when it is not whole 16-byte blocks."""
        if not data or len(data) % AES_BLOCK:
            return b""
        return AES.new(self.key, AES.MODE_ECB).decrypt(data)

    # ---- authentication (§3.1, plaintext) ---------------------------------------------
    async def authenticate(self, timeout: float = 5.0) -> bool:
        self._drain()
        await self.client.write_gatt_char(self.char_uuid,
                                          bytearray([OP_READ_AUTH_KEY, 0x00]),
                                          response=True)
        try:
            raw = await asyncio.wait_for(self._pending.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return False

        # §9 warns the length byte changes meaning between regimes; pre-auth the reply is
        # [0x01][0x01][challenge], so accept either 3 bytes or a 1-byte payload.
        if len(raw) >= 3 and raw[0] == OP_READ_AUTH_KEY:
            challenge = raw[2]
        elif len(raw) == 2 and raw[0] == OP_READ_AUTH_KEY:
            challenge = raw[1]
        else:
            return False

        packet = bytes([OP_SET_AUTH_VALUE, 0x04]) + struct.pack("<I", derived_key_value(challenge))
        await self.client.write_gatt_char(self.char_uuid, bytearray(packet), response=True)
        try:
            raw = await asyncio.wait_for(self._pending.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return False

        ok = len(raw) >= 2 and raw[0] == OP_SET_AUTH_VALUE and raw[-1] == 0x01
        self.authenticated = ok
        return ok

    # ---- transport -------------------------------------------------------------------
    def _drain(self):
        while not self._pending.empty():
            try:
                self._pending.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _exchange(self, opcode: int, payload: bytes, timeout: float):
        """Write one encrypted request and return the raw reply frame (None on timeout). The caller holds
        _cmd_lock. Unsolicited frames of another opcode are skipped, they are never a reply.
        """
        self._drain()
        enc = self._encrypt(payload)
        wire = bytes([opcode, len(enc)]) + enc
        await self.client.write_gatt_char(self.char_uuid, bytearray(wire), response=True)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(self._pending.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if raw and raw[0] != opcode and raw[0] in self.unsolicited_opcodes:
                continue
            return raw

    async def send(self, opcode: int, payload: bytes = b"", timeout: float = 6.0):
        """Post-auth send. Returns (status, data-after-status) or (None, None) on timeout.

        PROTOCOL.md §4: data[0] of every post-auth response is a status byte.
        """
        async with self._cmd_lock:
            raw = await self._exchange(opcode, payload, timeout)
        return self._parse_status_reply(raw)

    def _parse_status_reply(self, raw):
        """(status, data after the status byte) of a status-led reply frame, (None, None) when unusable."""
        if raw is None or len(raw) < 2:
            return None, None

        cipher = raw[2:]
        plain = self._decrypt(cipher) if cipher and len(cipher) % AES_BLOCK == 0 else cipher
        if not plain:
            return None, None

        self.last_status = plain[0]
        return plain[0], plain[1:]

    async def send_raw(self, opcode: int, payload: bytes = b"", timeout: float = 6.0):
        """Status-less send: returns the decrypted reply payload, zero-padded to whole AES blocks (the logger rewrites
        the length byte to the padded length, so the true length is not on the wire).
        """
        async with self._cmd_lock:
            raw = await self._exchange(opcode, payload, timeout)
        if raw is None or len(raw) < 2:
            return None
        cipher = raw[2:]
        if cipher and len(cipher) % AES_BLOCK == 0:
            plain = self._decrypt(cipher)
            if plain:
                return plain
        return cipher

    @staticmethod
    def status_text(code):
        """Readable name of a status byte ('no response' for None)."""
        if code is None:
            return "no response"
        return STATUS_NAMES.get(code, f"0x{code:02X}")

    # ---- Keller sensors (§6.3) --------------------------------------------------------
    async def read_pressure(self, sensor: int = 0):
        """0x10 -> success_flag, keller_error_code, pressure f32 on success."""
        status, data = await self.send(OP_KELLER_READ_PRESSURE, bytes([sensor & 0xFF]))
        if status is None:
            return None
        out = {"status": self.status_text(status),
               "channel": KELLER_PRESSURE_CHANNELS.get(sensor, str(sensor))}
        if status != STATUS_SUCCESS or not data:
            return out
        out["success"] = bool(data[0])
        out["keller_code"] = data[1] if len(data) >= 2 else None
        if out["success"] and len(data) >= 6:
            bar = struct.unpack_from("<f", data, 2)[0]
            out["pressure_bar"] = bar
            out["pressure_mbar"] = bar * 1000.0
            out["depth_m"] = depth_from_pressure(bar)
        return out

    async def read_temperature(self, sensor: int = 0):
        """0x11 -> success_flag, keller_error_code, temperature f32 on success."""
        status, data = await self.send(OP_KELLER_READ_TEMP, bytes([sensor & 0xFF]))
        if status is None:
            return None
        out = {"status": self.status_text(status),
               "channel": KELLER_TEMP_CHANNELS.get(sensor, str(sensor))}
        if status != STATUS_SUCCESS or not data:
            return out
        out["success"] = bool(data[0])
        out["keller_code"] = data[1] if len(data) >= 2 else None
        if out["success"] and len(data) >= 6:
            out["temp_c"] = struct.unpack_from("<f", data, 2)[0]
        return out

    async def read_all_sensors(self):
        """0x12 -> every Keller value in one read (decode_keller_snapshot) plus 'status' and 'raw'; None on
        timeout.
        """
        status, data = await self.send(OP_KELLER_READ_ALL)
        if status is None:
            return None
        out = decode_keller_snapshot(data) or {}
        out.update(status=self.status_text(status), raw=data.hex(" ").upper() if data else "")
        return out

    async def sensor_info(self):
        """0x13 -> serial, firmware version, calibration date, sensor type, ranges."""
        status, data = await self.send(OP_KELLER_GET_INFO)
        if status is None:
            return None
        text = ""
        if data:
            printable = bytes(b for b in data if 32 <= b < 127)
            if len(printable) >= 4:
                text = printable.decode("ascii", "replace")
        return {"status": self.status_text(status),
                "raw": data.hex(" ").upper() if data else "",
                "ascii": text}

    # ---- device control (§5) ----------------------------------------------------------
    # 0x03..0x09 are answered by device_config.c directly: NO status byte, payload zero-padded to 16 bytes and the
    # length byte rewritten to the padded length. send() would take the first data byte for a status (the first
    # letter of the name, the battery percent...), which shifted every field by one; send_control() returns the
    # decrypted payload as it is.
    async def send_control(self, opcode: int, payload: bytes = b"", timeout: float = 6.0):
        """Post-auth device-control command; returns the decrypted (padded) reply payload, or None."""
        return await self.send_raw(opcode, payload, timeout=timeout)

    async def get_battery(self):
        """0x04 -> battery percent (reply [pct], no status byte)."""
        data = await self.send_control(OP_BATTERY_PCT)
        return data[0] if data else None

    async def get_sanity(self):
        """0x09 -> decoded sanity result (§5.1): pass, error flags, I2C bitmap u16 LE, device count, battery mV LE."""
        data = await self.send_control(OP_SANITY_TEST)
        if not data or len(data) < 2:
            return None
        out = {"overall_pass": bool(data[0]), "err_flags": data[1]}
        out["err_text"] = [txt for bit, txt in SANITY_FLAG_TEXT.items() if data[1] & bit]
        if len(data) >= 4:
            out["i2c_bitmap"] = struct.unpack_from("<H", data, 2)[0]
            out["i2c_devices"] = [name for bit, name in I2C_DEVICES.items() if out["i2c_bitmap"] & (1 << bit)]
        if len(data) >= 5:
            out["i2c_count"] = data[4]
        if len(data) >= 7:
            out["battery_mv"] = struct.unpack_from("<H", data, 5)[0]
        return out

    async def device_config_get(self):
        """0x06 -> name(16) keller(1) battery 0 % mV LE(2) battery 100 % mV LE(2)."""
        data = await self.send_control(OP_DEVICE_CONFIG_GET)
        if not data or len(data) < 21:
            return None
        out = gigo_decode_config("gigo_dt", bytes([OP_DEVICE_CONFIG_GET, len(data)]) + bytes(data))
        out["keller_enabled"] = bool(out.get("keller_enable"))
        return out

    async def device_config_set(self, name=None, keller_enable=None,
                                battery_flat_mv=None, battery_full_mv=None):
        """0x05 -> writes name / Keller enable / battery range, then the device saves and resets.

        The Keller flag has no 'keep' value (whatever is sent is stored), so when it is not given the current value
        is read first; a voltage left empty is sent as 0, which the logger treats as 'keep' (0xFFFF would be stored as
        65.535 V).
        """
        current = {}
        if keller_enable is None:
            current = await self.device_config_get() or {}
        fields = {"name": name or "",
                  "keller_enable": None if keller_enable is None else int(bool(keller_enable)),
                  "battery_flat_mv": battery_flat_mv, "battery_full_mv": battery_full_mv}
        payload = gigo_encode_config("gigo_dt", fields, current)
        data = await self.send_control(OP_DEVICE_CONFIG_SET, payload)
        return bool(data) and data[0] == 1

    async def rec_mode_get(self):
        """Read the recording-mode flags (bit 0 phase 1, bit 1 phase 3, bit 2 infinite); None without an answer."""
        data = await self.send_control(OP_REC_MODE_GET)
        return data[0] if data else None

    async def rec_mode_set(self, flags: int) -> bool:
        """Write the recording-mode flags; True when the logger acknowledges."""
        data = await self.send_control(OP_REC_MODE_SET, bytes([flags & 0xFF]))
        return bool(data) and data[0] == 1

    async def device_reset(self) -> bool:
        """0x03 -> the logger resets after the ack, so a timeout is not an error."""
        try:
            data = await self.send_control(OP_DEVICE_RESET, timeout=2.5)
            return data is None or (len(data) >= 1 and data[0] == 1)
        except Exception:
            return True

    async def factory_reset(self) -> bool:
        """0xFE -> pre-auth reset to defaults. Sent plaintext."""
        self._drain()
        await self.client.write_gatt_char(self.char_uuid,
                                          bytearray([OP_RESET_DEFAULT, 0x00]), response=True)
        try:
            raw = await asyncio.wait_for(self._pending.get(), timeout=3.0)
        except asyncio.TimeoutError:
            return True
        return len(raw) >= 2 and raw[-1] == 0x01

    # ---- logger (§6.1 / §6.2) ---------------------------------------------------------
    async def start_recording(self) -> bool:
        status, _ = await self.send(OP_START)
        return status == STATUS_SUCCESS

    async def stop_recording(self) -> bool:
        """Stop logging; True on success."""
        status, _ = await self.send(OP_STOP)
        return status == STATUS_SUCCESS

    def validate_interval(self, interval_s: int, duration_s: int):
        """Return an error string, or None when the pair is in range (§6.1)."""
        if not (INTERVAL_MIN <= interval_s <= INTERVAL_MAX):
            return f"interval must be {INTERVAL_MIN}-{INTERVAL_MAX} s"
        if not (DURATION_MIN <= duration_s <= DURATION_MAX):
            return (f"duration must be {DURATION_MIN}-{DURATION_MAX} s (the logger refuses 0; for endless logging "
                    "set recording-mode bit 2)")
        return None

    async def set_interval(self, interval_s: int, duration_s: int = DURATION_MIN) -> bool:
        """Set the logging interval and duration (seconds) after checking the range (ble_commands.c refuses a
        duration outside 600..864000, 0 included); True on success.
        """
        err = self.validate_interval(interval_s, duration_s)
        if err:
            self.last_status = STATUS_INVALID
            return False
        payload = struct.pack("<HI", interval_s & 0xFFFF, duration_s & 0xFFFFFFFF)
        status, _ = await self.send(OP_SET_INTERVAL, payload)
        return status == STATUS_SUCCESS

    async def get_interval(self):
        """Read the logging interval and duration as a dict, or None."""
        status, data = await self.send(OP_GET_INTERVAL)
        if status != STATUS_SUCCESS or not data or len(data) < 6:
            return None
        interval, duration = struct.unpack_from("<HI", data, 0)
        return {"interval_s": interval, "duration_s": duration}

    async def set_rtc(self, epoch: int) -> bool:
        """Set the logger clock (Unix epoch seconds); True on success."""
        status, _ = await self.send(OP_SET_RTC, struct.pack("<I", epoch & 0xFFFFFFFF))
        return status == STATUS_SUCCESS

    async def get_rtc(self):
        """Read the logger clock (Unix epoch seconds), or None."""
        status, data = await self.send(OP_GET_RTC)
        if status != STATUS_SUCCESS or not data or len(data) < 4:
            return None
        return struct.unpack_from("<I", data, 0)[0]

    async def get_stats(self):
        """0x26 -> decoded 27-byte stats block (§6.2.1)."""
        status, data = await self.send(OP_GET_STATS)
        if status is None:
            return None
        out = {"status": self.status_text(status)}
        if status != STATUS_SUCCESS or not data:
            return out
        try:
            (out["total_records"], out["phase1"], out["phase2"], out["phase3"],
             out["free_bytes"]) = struct.unpack_from("<IIIII", data, 0)
            out["recording_active"] = bool(data[20])
            out["current_phase"] = data[21]
            out["error_record_count"] = struct.unpack_from("<I", data, 22)[0]
            out["current_error_flags"] = data[26] if len(data) > 26 else None
        except struct.error:
            out["raw"] = data.hex(" ").upper()
        return out

    async def retrieve(self, phase: int, index: int):
        """0x27 -> decoded 16-byte DataRecord_t (§8). phase 1-3, index u32 LE."""
        if phase not in (1, 2, 3):
            self.last_status = STATUS_INVALID
            return None
        payload = bytes([phase]) + struct.pack("<I", index & 0xFFFFFFFF)
        status, data = await self.send(OP_RETRIEVE_DATA, payload, timeout=10.0)
        if status != STATUS_SUCCESS or not data or len(data) < 16:
            return {"status": self.status_text(status)} if status is not None else None

        epoch = struct.unpack_from("<I", data, 0)[0]
        bar = struct.unpack_from("<f", data, 4)[0]
        temp_x10 = struct.unpack_from("<h", data, 8)[0]
        batt = struct.unpack_from("<H", data, 10)[0]
        state = data[12]
        err = data[13]
        crc = data[14]

        return {
            "status": STATUS_NAMES[STATUS_SUCCESS],
            "epoch": epoch,
            "pressure_bar": bar,
            "depth_m_est": depth_from_pressure(bar),
            "temp_c": temp_x10 / 10.0,
            "battery_pct": batt,
            "state": state,
            "error_flags": err,
            "crc8_ok": crc8_maxim(data[0:14]) == crc,
        }

    async def clear_data(self, phase: int = 0xFF) -> bool:
        """Erase the logged data of one phase (0xFF = all); True on success."""
        status, _ = await self.send(OP_CLEAR_DATA, bytes([phase & 0xFF]))
        return status == STATUS_SUCCESS

    async def format_eeprom(self) -> bool:
        """Format the logger's EEPROM (slow: 20 s timeout); True on success."""
        status, _ = await self.send(OP_FORMAT, timeout=20.0)
        return status == STATUS_SUCCESS

    async def get_error_log(self):
        """0x2A -> nine saturating since-boot counters + current flag byte (§6.2.2)."""
        status, data = await self.send(OP_GET_ERROR_LOG, timeout=10.0)
        if status is None:
            return None
        out = {"status": self.status_text(status)}
        if status != STATUS_SUCCESS or not data:
            return out
        out["counters"] = {
            name: (data[i] if i < len(data) else None)
            for i, name in enumerate(ERROR_COUNTER_NAMES)
        }
        out["current_flags"] = data[8] if len(data) > 8 else None
        return out


async def discover_sc2(timeout: float = 6.0):
    """Scan for the GIGO service. Returns [(name, address, rssi, adv_info)]."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for addr, (dev, adv) in found.items():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if SC2_SERVICE_UUID in uuids or (dev.name and "gigo" in dev.name.lower()):
            out.append((dev.name or "(unnamed)", addr, adv.rssi,
                        parse_manufacturer_data(adv.manufacturer_data)))
    return out


# ---------------------------------------------------------------------------------------------
# Advertising payload (PROTOCOL.md §1)
#
# The device broadcasts its identity passively -- no connection, no auth, no command needed:
#     6 bytes MAC (reversed)  |  1 byte battery %  |  1 byte charging  |  2 bytes FW (major, minor)
#
# This is the SC2 equivalent of Valtrack's 0x90 identification header and 0xD2 version query, and
# it is strictly better: readable before connecting.
# ---------------------------------------------------------------------------------------------
CHARGING_TEXT = {0: "not charging", 1: "charging", 2: "full", 3: "fault"}


def parse_manufacturer_data(manufacturer_data) -> dict:
    """Decode the SC2 advertisement. Returns {} when the payload is short or absent."""
    if not manufacturer_data:
        return {}
    # {company_id: payload}; 0x0059 is the Nordic example ID the firmware currently uses.
    payload = None
    for cid in (0x0059, *manufacturer_data.keys()):
        if cid in manufacturer_data:
            payload = bytes(manufacturer_data[cid])
            break
    if not payload or len(payload) < 10:
        return {}

    mac_bytes = payload[0:6][::-1]                 # stored reversed on air
    info = {
        "mac": ":".join(f"{b:02X}" for b in mac_bytes),
        "battery_pct": payload[6],
        "charging": payload[7],
        "charging_text": CHARGING_TEXT.get(payload[7], "unknown"),
        "fw_major": payload[8],
        "fw_minor": payload[9],
        "fw_version": f"{payload[8]}.{payload[9]}",
    }
    return info


def decode_record(data: bytes):
    """Decode a raw 16-byte DataRecord_t (§8). Shared by retrieve and bulk export."""
    if not data or len(data) < 16:
        return None
    epoch = struct.unpack_from("<I", data, 0)[0]
    bar = struct.unpack_from("<f", data, 4)[0]
    temp_x10 = struct.unpack_from("<h", data, 8)[0]
    batt = struct.unpack_from("<H", data, 10)[0]
    return {
        "epoch": epoch,
        "pressure_bar": bar,
        "depth_m_est": depth_from_pressure(bar),
        "temp_c": temp_x10 / 10.0,
        "battery_pct": batt,
        "state": data[12],
        "error_flags": data[13],
        "crc8_ok": crc8_maxim(data[0:14]) == data[14],
    }


def record_to_csv(rec: dict) -> str:
    """One CSV row. Depth is marked as an estimate because the firmware logs bar, not metres (converted like the
    logger's state detector, depth_from_pressure).
    """
    return (f"{rec['epoch']},{rec['pressure_bar']:.4f},{rec['depth_m_est']:.2f},"
            f"{rec['temp_c']:.1f},{rec['battery_pct']},{rec['state']},"
            f"{rec['error_flags']},{1 if rec['crc8_ok'] else 0}")


CSV_HEADER = "epoch,pressure_bar,depth_m_est,temp_c,battery_pct,state,error_flags,crc8_ok"



# The logger is the GiGO Depth & Temp device; the historic class name is kept for existing imports.
DepthTempDevice = Sc2Device
