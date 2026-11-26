"""@file gigo_device.py
@brief Command channel to ONE connected Valtrack, SC2 hub or GiGO Delphis / Rotation-Jack device (one per connection)
@version 1.1
@date 2026-11-22

Wraps the command characteristic: authentication, then encrypted request/response commands. One class serves every
family that uses this framing (Valtrack V1/2, SC2 Gen2/Gen3, GiGO Delphis and beacon); self.profile selects the byte
layout of each command through config_codecs.py, so a field is never encoded with another device's layout.
Notifications are split three ways: the debug-log stream goes straight to the log pane line by line; frames a panel
listens to (IMU stream 0xB2, nano-modem lines 0xA0 / 0xA1, SC2 GPS fixes 0x12) go to every listener of that opcode
(the device tab's live view and a window can both listen) unless a command is waiting for the same opcode; every other
notification is queued for the command waiting on send_raw(). The IMU stream is shared through stream owners.
"""
from .config_codecs import VALTRACK_C1_READ_EXTENDED
from .config_codecs import beacon_decode_tuning
from .config_codecs import beacon_encode_tuning
from .config_codecs import decode_battery
from .config_codecs import decode_firmware_version
from .config_codecs import gigo_decode_config
from .config_codecs import gigo_encode_config
from .config_codecs import sc2_decode_c1
from .config_codecs import sc2_encode_c1
from .config_codecs import valtrack_decode_c1
from .config_codecs import valtrack_decode_live_position
from .config_codecs import valtrack_encode_c1
from .constants import AES_KEY_GIGO_4G
from .constants import BLE_READ_FIRMWARE_VERSION
from .constants import BLE_SEND_DEBUG_LOG
from .constants import OPCODE_CONFIG_DEVICE
from .constants import OPCODE_CONFIG_SERVER
from .constants import OPCODE_CONFIG_SIM
from .constants import OPCODE_DEVICE_RESET
from .constants import OPCODE_IMEI_REQ
from .constants import OPCODE_READ_AUTH_KEY
from .constants import OPCODE_SEND_AUTH_STATUS
from .constants import OPCODE_SEND_CONN_AUTH_KEY
from .constants import OPCODE_SET_AUTH_VALUE
from .constants import OPCODE_TOKEN_VALIDATION
from .constants import SERVER_HOST_LEN
from .constants import SERVER_HOST_OFFSET
from .device_profiles import PROBE_BEACON_OPCODE
from .device_profiles import PROBE_TIMEOUT_S
from .gigo_imu_codecs import IMU_STREAM_START
from .gigo_imu_codecs import IMU_STREAM_STOP
from .gigo_imu_codecs import NANO_MODEM_DATA
from .gigo_imu_codecs import NANO_MODEM_MAX_COMMAND
from .gigo_imu_codecs import decode_imu_full_scale
from .gigo_imu_codecs import decode_imu_register_read
from .gigo_imu_codecs import encode_imu_full_scale
from .gigo_imu_codecs import encode_imu_register_read
from .gigo_imu_codecs import encode_imu_register_write
from .gigo_imu_codecs import encode_nano_modem
from .logbus import log_device_line
from .logbus import log_system
from .ota_image import _strip_ansi
from .protocol import auth_key_value_gigo
from .protocol import build_debug_log_cmd
from .protocol import decrypt_response
from .protocol import encrypt_command
from .sc2_hub_codecs import SC2_ACK_SUCCESS
from .sc2_hub_codecs import SC2_DT_ACK_DONE
from .sc2_hub_codecs import SC2_LIVE_TRACK_START
from .sc2_hub_codecs import SC2_LIVE_TRACK_STOP
from .sc2_hub_codecs import sc2_decode_dt_addresses
from .sc2_hub_codecs import sc2_decode_dt_device
from .sc2_hub_codecs import sc2_decode_industry
from .sc2_hub_codecs import sc2_dt_get_request
from .sc2_hub_codecs import sc2_encode_dt_addresses
from .sc2_hub_codecs import sc2_encode_dt_device
from .sc2_hub_codecs import sc2_encode_industry
from bleak import BleakClient
from Crypto.Cipher import AES
import asyncio
import struct


# --------------------------------------------------------------------------
# Device Handler
# --------------------------------------------------------------------------
class Gigo4gDevice:
    def __init__(self, client: BleakClient, cmd_char_uuid: str, session_id: str | None = None,
                 aes_key: bytes = AES_KEY_GIGO_4G, auth_fn=auth_key_value_gigo,
                 profile: str = "valtrack"):
        """Remember the BLE client and command characteristic; empty reply queue, not authenticated yet."""
        self.client = client
        self.cmd_char_uuid = cmd_char_uuid
        self.session_id = session_id
        self.aes_key = aes_key
        self.auth_fn = auth_fn
        self.profile = profile
        self._notify_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        self.disconnected_evt = asyncio.Event()
        self.streaming_enabled = False
        self._cmd_lock = asyncio.Lock()
        self._line_buffer = ""
        self._listeners = {}
        self._pending_opcode = None
        self._imu_stream_owners = set()

    def _on_notify(self, _handle, data: bytearray):
        """Notification callback (BLE thread).

        Flow: a debug-log packet is decrypted and its text appended to the line buffer, and every
        complete line is logged (a debug-log ON/OFF ack is queued instead); any other packet is a
        command reply and goes to the reply queue.
        """
        raw = bytes(data)

        # 1. Debug Log Stream (0xFD)
        if len(raw) >= 1 and raw[0] == BLE_SEND_DEBUG_LOG:
            try:
                pkt = decrypt_response(raw, aes_key=self.aes_key)
                if (len(pkt) >= 3 and pkt[0] == BLE_SEND_DEBUG_LOG and pkt[1] == 1
                        and pkt[2] in (0, 1)):
                    self._notify_queue.put_nowait(raw)
                    return
                length = pkt[1]
                raw_text = pkt[2:2 + length].decode("utf-8", errors="replace")
                text = "".join(
                    ch for ch in raw_text if ch in "\r\n\t" or (32 <= ord(ch) < 127)
                )
                clean_text = _strip_ansi(text)
                self._line_buffer += clean_text

                while "\n" in self._line_buffer:
                    line, self._line_buffer = self._line_buffer.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.strip():
                        log_device_line(line, self.session_id)
            except Exception as e:
                log_device_line(f"<debug-log decode error: {e}>", self.session_id)
            return

        # 2. Unsolicited frames the device tab or a window listens to, unless a command is waiting for that opcode
        listeners = list(self._listeners.get(raw[0], ())) if raw else []
        if listeners and raw[0] != self._pending_opcode:
            packet = decrypt_response(raw, self.aes_key)
            for listener in listeners:
                try:
                    listener(packet)
                except Exception as error:
                    log_system(f"Listener for 0x{raw[0]:02X} failed: {error}", "#64748b", self.session_id)
            return

        # 3. Command / Config Response Payload
        self._notify_queue.put_nowait(raw)

    def add_listener(self, opcode: int, callback) -> None:
        """callback(decrypted frame) receives unsolicited frames of opcode, next to any other listener of it (called
        on the BLE thread: a Tk panel must hand the data over with after()).
        """
        callbacks = self._listeners.setdefault(opcode, [])
        if callback not in callbacks:
            callbacks.append(callback)

    def remove_listener(self, opcode: int, callback=None) -> None:
        """Stop routing opcode to callback (to every listener of it when callback is None)."""
        callbacks = self._listeners.get(opcode, [])
        if callback is None:
            callbacks.clear()
        elif callback in callbacks:
            callbacks.remove(callback)
        if not callbacks:
            self._listeners.pop(opcode, None)

    async def start(self):
        """Subscribe to the command characteristic's notifications."""
        self._line_buffer = ""
        await self.client.start_notify(self.cmd_char_uuid, self._on_notify)

    async def stop(self):
        """Log any half line still buffered, then unsubscribe (errors ignored: the link may be gone)."""
        try:
            if self._line_buffer.strip():
                log_device_line(self._line_buffer.strip(), self.session_id)
                self._line_buffer = ""
            await self.client.stop_notify(self.cmd_char_uuid)
        except Exception:
            pass

    async def send_raw(self, payload: bytes, timeout: float = 5.0) -> bytes:
        """Write one command and return the reply carrying the same opcode.

        Commands are serialised per device (one in flight at a time) and any stale notification
        left over from an earlier timeout is discarded first, so a late reply can never be
        mistaken for the answer to the next command. The firmware echoes the request opcode in
        its reply, which is what is matched here.
        """
        expected = payload[0]
        async with self._cmd_lock:
            while not self._notify_queue.empty():
                stale = self._notify_queue.get_nowait()
                log_system(f"Discarded stale reply: {stale.hex(' ')}", "#64748b", self.session_id)

            self._pending_opcode = expected
            try:
                await self.client.write_gatt_char(self.cmd_char_uuid, payload, response=True)

                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    reply = await asyncio.wait_for(self._notify_queue.get(), timeout=remaining)
                    if reply and reply[0] == expected:
                        return reply
                    log_system(f"Ignoring unrelated notification (waiting for 0x{expected:02X}): "
                               f"{reply.hex(' ')}", "#64748b", self.session_id)
            finally:
                self._pending_opcode = None

    async def send_no_reply(self, payload: bytes) -> None:
        """Write one command that the device does not answer directly (its data arrives later through a listener)."""
        async with self._cmd_lock:
            await self.client.write_gatt_char(self.cmd_char_uuid, payload, response=True)

    def _decrypt_full(self, wire: bytes) -> bytes:
        """Decrypt every AES block of a reply WITHOUT cutting it to the length byte (some GiGO replies keep the
        request's length byte, e.g. the 0xB0 register read).
        """
        body = wire[2:]
        if body and len(body) % 16 == 0:
            body = AES.new(self.aes_key, AES.MODE_ECB).decrypt(body)
        return bytes(wire[:2]) + body

    async def authenticate(self, timeout: float = 5.0) -> bool:
        """Two-step challenge/response (firmware: app_ble_p2p.c, opcodes 0x01 / 0x02).

        1. Send ``01 00``            -> device answers ``01 01 <key>`` (random byte)
        2. Send ``02 04 <value LE32>`` -> device answers ``02 01 <1|0>``
        where ``value = ((key*79 + 8294) * 19) - (573*key + 989)``. Every step is logged to this
        device's own log tab so a failure can be pinpointed.
        """
        sid = self.session_id
        req1 = bytes([OPCODE_READ_AUTH_KEY, 0x00])
        log_system(f"[AUTH] -> request key   {req1.hex(' ')}", "#94a3b8", sid)
        resp1 = await self.send_raw(req1, timeout=timeout)
        log_system(f"[AUTH] <- key response  {resp1.hex(' ')}", "#94a3b8", sid)

        if resp1[0] != OPCODE_SEND_CONN_AUTH_KEY or len(resp1) < 3:
            raise RuntimeError(f"Unexpected auth-key response: {resp1.hex()}")

        auth_key = resp1[2]
        key_value_gen = self.auth_fn(auth_key) & 0xFFFFFFFF
        log_system(f"[AUTH] key=0x{auth_key:02X} ({auth_key}) -> value=0x{key_value_gen:08X}",
                   "#94a3b8", sid)

        req2 = bytes([OPCODE_SET_AUTH_VALUE, 0x04]) + struct.pack("<I", key_value_gen)
        log_system(f"[AUTH] -> send value    {req2.hex(' ')}", "#94a3b8", sid)
        resp2 = await self.send_raw(req2, timeout=timeout)
        log_system(f"[AUTH] <- status        {resp2.hex(' ')}", "#94a3b8", sid)

        if resp2[0] != OPCODE_SEND_AUTH_STATUS or len(resp2) < 3:
            raise RuntimeError(f"Unexpected auth-status response: {resp2.hex()}")

        return resp2[2] == 1

    async def set_debug_log(self, enable: bool) -> bool:
        """Switch the device's debug-log stream on or off; returns True when the device acked it."""
        plaintext_packet = build_debug_log_cmd(enable)
        encrypted_packet = encrypt_command(plaintext_packet, self.aes_key)

        response = await self.send_raw(encrypted_packet, timeout=5.0)
        packet = decrypt_response(response, aes_key=self.aes_key)
        if len(packet) < 3 or packet[0] != BLE_SEND_DEBUG_LOG or packet[1] != 1:
            raise RuntimeError(f"Unexpected debug-log response: {packet.hex()}")

        still_connected = self.client.is_connected and not self.disconnected_evt.is_set()
        accepted = packet[2] == 1
        log_system(f"Debug log {'ON' if enable else 'OFF'} -> device "
                   f"{'accepted' if accepted else 'REFUSED'}", "#94a3b8", self.session_id)
        self.streaming_enabled = enable and accepted and still_connected
        return accepted and still_connected

    def _build_config_read(self, opcode: int) -> bytes:
        """Plain read request for a configuration opcode (opcode, length 1)."""
        return bytes([opcode, 0x01])

    def _build_config_write(self, opcode: int, declared_len: int, data: bytes) -> bytes:
        """Encrypted write request: opcode, declared length, data (length must match)."""
        if len(data) != declared_len:
            raise ValueError(f"data length {len(data)} != declared_len {declared_len}")
        plaintext = bytes([opcode, declared_len]) + data
        return encrypt_command(plaintext, self.aes_key)

    def _decode_response(self, wire: bytes, label: str = "") -> bytes:
        """Decrypt a reply; with a label, log both the wire bytes and the plain bytes."""
        pkt = decrypt_response(wire, self.aes_key)
        if label:
            log_system(f"[{label}] WIRE  ({len(wire):>3}B): {wire.hex(' ')}", "#64748b")
            log_system(f"[{label}] PLAIN ({len(pkt):>3}B): {pkt.hex(' ')}", "#94a3b8")
        return pkt

    def _is_ack(self, pkt: bytes, expected_opcode: int) -> bool:
        """Whether pkt is the one-byte ack (0 or 1) of expected_opcode."""
        return (len(pkt) >= 3 and pkt[0] == expected_opcode
                and pkt[1] == 1 and pkt[2] in (0, 1))

    # ---------- Device Config (0xC1) ----------
    @property
    def family(self) -> str:
        """Protocol family of this link ('valtrack', 'sc2_gen2', 'sc2_gen3', 'gigo_delphis', 'gigo_beacon')."""
        return self.profile

    def _is_sc2(self) -> bool:
        """True on an SC2 Gen2 / Gen3 hub (little-endian SET frames, SC2 field layout)."""
        return self.profile in ("sc2_gen2", "sc2_gen3")

    async def read_device_config(self, timeout: float = 5.0) -> dict:
        """Read the 0xC1 configuration as a dict of the profile's field keys (None = not set / unknown).

        Valtrack: extended read (selector 0xA5) so intervals above 65535 s arrive as u32; older firmware answers the
        20-byte layout. SC2: 16-bit fields big-endian in the reply.
        """
        if self._is_sc2():
            wire = await self.send_raw(self._build_config_read(OPCODE_CONFIG_DEVICE), timeout=timeout)
            return sc2_decode_c1(self._decode_response(wire, label="CFG-DEV-RD"))
        if self.profile != "valtrack":
            raise RuntimeError(f"0xC1 is not supported by profile '{self.profile}'")
        request = self._build_config_write(OPCODE_CONFIG_DEVICE, 1, bytes([VALTRACK_C1_READ_EXTENDED]))
        wire = await self.send_raw(request, timeout=timeout)
        return valtrack_decode_c1(self._decode_response(wire, label="CFG-DEV-RD"))

    async def write_device_config(self, fields: dict, erase_logs: bool = False, timeout: float = 5.0) -> bool:
        """Write the 0xC1 configuration; keys missing or None are sent as 'keep'. Values are range-checked first
        (ConfigValueError). Valtrack: extended 28-byte frame (u32 intervals); SC2: 20 bytes little-endian.
        Returns True when the device acknowledged with success.
        """
        if self._is_sc2():
            payload = sc2_encode_c1(fields)
        elif self.profile == "valtrack":
            payload = valtrack_encode_c1(fields, erase_logs=erase_logs)
        else:
            raise RuntimeError(f"0xC1 is not supported by profile '{self.profile}'")
        wire = self._build_config_write(OPCODE_CONFIG_DEVICE, len(payload), payload)
        resp = await self.send_raw(wire, timeout=timeout)
        pkt = self._decode_response(resp, label="CFG-DEV-WR-ACK")
        return self._is_ack(pkt, OPCODE_CONFIG_DEVICE) and pkt[2] == 1

    async def write_and_verify_device_config(self, fields: dict, timeout: float = 5.0) -> "tuple[bool, dict, list]":
        """Write, then read back and compare every written field. Returns (acked, read_back, mismatches) where each
        mismatch is (key, sent, read). SC2 acknowledges even refused values and resets after a write, so its read-back
        may time out; that is reported as an empty read_back rather than an error.
        """
        acked = await self.write_device_config(fields, timeout=timeout)
        await asyncio.sleep(0.4)
        try:
            read_back = await self.read_device_config(timeout=timeout)
        except Exception:
            return acked, {}, []
        mismatches = [(key, value, read_back.get(key)) for key, value in fields.items()
                      if value is not None and key in read_back and read_back.get(key) != value]
        return acked, read_back, mismatches

    # ---------- SIM Config (0xC3) ----------
    async def read_sim_config(self, timeout: float = 5.0) -> dict:
        wire = await self.send_raw(self._build_config_read(OPCODE_CONFIG_SIM), timeout=timeout)
        pkt = self._decode_response(wire, label="CFG-SIM-RD")
        
        def _read_cstr(start: int, size: int):
            """NUL-terminated string at start, or None when absent or erased (0xFF)."""
            if start + size > len(pkt):
                return None
            raw_str = pkt[start:start + size].split(b"\x00", 1)[0]
            if not raw_str or raw_str[0] == 0xFF:
                return None
            return raw_str.decode("utf-8", errors="replace")

        # Byte 3 (URAT) carries the modem network mode (AT+CNMP 2/13/14/38) since firmware 2026-11-21; 0xFF before.
        network_mode = pkt[3] if (len(pkt) > 3 and pkt[3] != 0xFF) else None
        result = {
            "network_mode": network_mode,
            "apn":      _read_cstr(9, 33),
            "username": _read_cstr(42, 33),
            "password": _read_cstr(75, 33),
        }
        if self._is_sc2() and len(pkt) >= 9:
            # SC2 reply: [2] SIM type (0 eSIM, 1 physical), [3] URAT, [4..7] band mask big-endian, [8] profile.
            result.update(sim_type=pkt[2], urat=pkt[3], band_mask=int.from_bytes(pkt[4:8], "big"), profile=pkt[8])
        return result

    async def write_sim_config(self, apn=None, username=None, password=None, network_mode=None,
                               sim_type=None, current: "dict | None" = None, timeout: float = 5.0) -> bool:
        """Write APN, user name and password (33 bytes each, NUL-terminated) and the network mode (byte 3:
        2 automatic, 13 GSM, 14 WCDMA, 38 LTE); a value left None is sent as 0xFF bytes ('keep'). Returns True on
        ack.

        SC2: 0xFF is NOT 'keep' for URAT (reset to the default) and profile (stored as 0xFF), so the SIM type, URAT
        and profile are written back from current (a read_sim_config() result, read here when not given); the band
        mask is sent as 0xFFFFFFFF, the one real 'keep' value.
        """
        data = bytearray(b"\xFF" * 106)
        if self._is_sc2():
            if current is None or current.get("urat") is None:
                current = await self.read_sim_config(timeout=timeout)
            if current.get("urat") is None or current.get("profile") is None:
                raise RuntimeError("SC2 SIM write needs the current URAT and profile (read failed)")
            data[0] = (int(sim_type) if sim_type is not None else int(current.get("sim_type", 0xFF))) & 0xFF
            data[1] = int(current["urat"]) & 0xFF
            data[6] = int(current["profile"]) & 0xFF
        elif network_mode is not None:
            data[3 - 2] = int(network_mode) & 0xFF
        def _put(offset: int, text):
            """Copy text (cut to 32 bytes, NUL-terminated) into its field of the payload."""
            if text is not None:
                raw = text.encode("utf-8")[:32] + b"\x00"
                data[offset - 2:offset - 2 + 33] = raw.ljust(33, b"\x00")
        _put(9, apn)
        _put(42, username)
        _put(75, password)
        wire = self._build_config_write(OPCODE_CONFIG_SIM, 106, bytes(data))
        resp = await self.send_raw(wire, timeout=timeout)
        pkt = self._decode_response(resp, label="CFG-SIM-WR-ACK")
        return self._is_ack(pkt, OPCODE_CONFIG_SIM) and pkt[2] == 1

    # ---------- Server Config (0xC4) ----------
    async def read_server_config(self, timeout: float = 5.0) -> dict:
        wire = await self.send_raw(self._build_config_read(OPCODE_CONFIG_SERVER), timeout=timeout)
        pkt = self._decode_response(wire, label="CFG-SRV-RD")
        
        host_bytes = pkt[SERVER_HOST_OFFSET:SERVER_HOST_OFFSET + SERVER_HOST_LEN].split(b"\x00", 1)[0] if len(pkt) >= 35 else b""
        host = host_bytes.decode("utf-8", errors="replace") if host_bytes and host_bytes[0] != 0xFF else None
        
        port_val = int.from_bytes(pkt[35:37], "big") if len(pkt) >= 37 else 0xFFFF
        ka_val = pkt[37] if len(pkt) >= 38 else 0xFF
        
        return {
            "host": host,
            "port": port_val if port_val != 0xFFFF else None,
            "keepalive": ka_val if ka_val != 0xFF else None,
        }

    async def write_server_config(self, host=None, port=None, keepalive=None, timeout: float = 5.0) -> bool:
        """Write server host (33 bytes), port (big-endian) and keep-alive; None fields are sent
        as 0xFF ('keep'). Returns True on ack.
        """
        data = bytearray(b"\xFF" * 36)
        if host is not None:
            raw = host.encode("utf-8")[:32] + b"\x00"
            data[0:33] = raw.ljust(33, b"\x00")
        if port is not None:
            # Valtrack reads the port big-endian; SC2 copies it with memcpy (little-endian) in a SET frame.
            data[33:35] = int(port).to_bytes(2, "little" if self._is_sc2() else "big")
        if keepalive is not None:
            data[35] = int(keepalive) & 0xFF
        wire = self._build_config_write(OPCODE_CONFIG_SERVER, 36, bytes(data))
        resp = await self.send_raw(wire, timeout=timeout)
        pkt = self._decode_response(resp, label="CFG-SRV-WR-ACK")
        return self._is_ack(pkt, OPCODE_CONFIG_SERVER) and pkt[2] == 1

    # ---------- Device Reset (0xC5) ----------
    async def reset_device(self, timeout: float = 5.0) -> bool:
        wire = bytes([OPCODE_DEVICE_RESET, 0x01, 0x01])
        try:
            resp = await self.send_raw(wire, timeout=timeout)
            pkt = self._decode_response(resp, label="DEV-RST-ACK")
            return self._is_ack(pkt, OPCODE_DEVICE_RESET) and pkt[2] == 1
        except asyncio.TimeoutError:
            return True

    # ---------- Token Validation (0xE2) ----------
    async def validate_token(self, token: bytes, timeout: float = 5.0) -> bool:
        body = token.ljust(112, b"\x00")
        wire = self._build_config_write(OPCODE_TOKEN_VALIDATION, len(body), body)
        resp = await self.send_raw(wire, timeout=timeout)
        pkt = self._decode_response(resp, label="TOKEN-VAL-ACK")
        return self._is_ack(pkt, OPCODE_TOKEN_VALIDATION) and pkt[2] == 1

    # -----------------------------------------------------------------------------------------
    # Status and actions. Each returns None on "no answer" instead of raising: a firmware that does not implement an
    # opcode ignores it, and the UI reports that as "not supported", not as a failure.
    # -----------------------------------------------------------------------------------------
    async def read_firmware_version(self, timeout: float = 4.0) -> "str | None":
        """Firmware version 'major.minor' from 0xE5 (Valtrack also answers the 0xD0 alias), or None."""
        if self.profile not in ("valtrack", "sc2_gen2", "sc2_gen3"):
            return None
        ops = (BLE_READ_FIRMWARE_VERSION,) if self._is_sc2() else (BLE_READ_FIRMWARE_VERSION, 0xD0)
        for op in ops:
            try:
                resp = await self.send_raw(self._build_config_read(op), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            version = decode_firmware_version(self._decode_response(resp, label="FW-VER"))
            if version:
                return version
        return None

    async def read_battery(self, timeout: float = 4.0) -> "dict | None":
        """Battery {'percent', 'voltage_mv', 'charging'}: 0xE4 on Valtrack/SC2, 0x04 on the GiGO nRF family."""
        opcode = 0x04 if self.profile.startswith("gigo") else 0xE4
        try:
            resp = await self.send_raw(self._build_config_read(opcode), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return decode_battery(self.profile, self._decode_response(resp, label=""))

    async def read_device_time(self, timeout: float = 4.0) -> "int | None":
        """Query the device RTC epoch time in seconds (0x11, Valtrack only; SC2 answers 0x10 on the nRF side)."""
        if self.profile != "valtrack":
            return None
        wire = self._build_config_read(0x11)
        try:
            resp = await self.send_raw(wire, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        pkt = self._decode_response(resp, label="")
        if len(pkt) >= 6 and pkt[0] == 0x11 and pkt[1] == 4:
            return int.from_bytes(pkt[2:6], "big")
        return None

    async def set_device_time(self, epoch_seconds: int, timeout: float = 4.0) -> "bool | None":
        """Set the device RTC (0x11): epoch big-endian on Valtrack, little-endian (memcpy) on SC2."""
        body = int(epoch_seconds).to_bytes(4, "little" if self._is_sc2() else "big")
        try:
            resp = await self.send_raw(self._build_config_write(0x11, 4, body), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        pkt = self._decode_response(resp, label="TIME-WR")
        if not self._is_ack(pkt, 0x11):
            return None
        return bool(pkt[2])

    async def read_imei(self, timeout: float = 4.0) -> "str | None":
        """Query modem IMEI (0xE1): 7-byte little-endian integer -> 15-digit string."""
        wire = self._build_config_read(OPCODE_IMEI_REQ)
        try:
            resp = await self.send_raw(wire, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        pkt = self._decode_response(resp, label="IMEI-RD")
        if len(pkt) >= 9 and pkt[0] == OPCODE_IMEI_REQ and pkt[1] >= 7:
            val = int.from_bytes(pkt[2:9], "little")
            return f"{val:015d}"
        return None

    # ---------- Valtrack-only actions ----------
    async def set_motion_lowpower(self, motion_enable: "bool | None" = None,
                                  threshold: "int | None" = None,
                                  low_power: "bool | None" = None,
                                  timeout: float = 5.0) -> "bool | None":
        """Change motion detection, threshold and low-power flag through a partial 0xC1 write (every other field
        'keep'). Returns the ack, or None on a profile without these settings.
        """
        if self.profile != "valtrack":
            return None
        fields = {
            "motion_enable": None if motion_enable is None else int(bool(motion_enable)),
            "motion_thresh": threshold,
            "low_power_flag": None if low_power is None else int(bool(low_power)),
        }
        return await self.write_device_config(fields, timeout=timeout)

    async def read_live_position(self, timeout: float = 5.0) -> "dict | None":
        """Last fix from 0xE3 ({'valid', 'latitude', 'longitude'}); the device also starts a new fix."""
        if self.profile != "valtrack":
            return None
        try:
            resp = await self.send_raw(self._build_config_read(0xE3), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return valtrack_decode_live_position(self._decode_response(resp, label="LIVE-POS"))

    async def erase_logs(self, timeout: float = 5.0) -> "bool | None":
        """Erase the stored waypoints and events (0xC1 byte 16 = 1; the device does it in the background)."""
        if self.profile != "valtrack":
            return None
        return await self.write_device_config({}, erase_logs=True, timeout=timeout)

    async def set_led(self, enable: bool, timeout: float = 4.0) -> "bool | None":
        """Store the 'all LEDs' flag (0xFC); returns the flag the device reports, or None."""
        if self.profile != "valtrack":
            return None
        try:
            resp = await self.send_raw(self._build_config_write(0xFC, 1, bytes([1 if enable else 0])),
                                       timeout=timeout)
        except asyncio.TimeoutError:
            return None
        pkt = self._decode_response(resp, label="LED")
        return bool(pkt[2]) if len(pkt) >= 3 and pkt[0] == 0xFC else None

    async def factory_reset(self, timeout: float = 4.0) -> "bool | None":
        """Valtrack 0xE6: defaults, logs and token cleared, then reset. A reply lost to the reset counts as done."""
        if self.profile != "valtrack":
            return None
        try:
            resp = await self.send_raw(self._build_config_read(0xE6), timeout=timeout)
        except asyncio.TimeoutError:
            return True
        pkt = self._decode_response(resp, label="FACTORY-RST")
        return self._is_ack(pkt, 0xE6) and pkt[2] == 1

    # ---------- GiGO nRF family (Delphis, Rotation/Jack beacon) ----------
    async def probe(self, opcode: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
        """True when the device answers a read of opcode within timeout (used to tell the GiGO families apart)."""
        try:
            await self.send_raw(self._build_config_read(opcode), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def identify_gigo_family(self) -> str:
        """'gigo_beacon' or 'gigo_delphis' (both on characteristic 0xAB01): only the beacon answers 0xB8."""
        if await self.probe(PROBE_BEACON_OPCODE):
            return "gigo_beacon"
        return "gigo_delphis"

    async def read_gigo_config(self, timeout: float = 5.0) -> dict:
        """0x06 configuration of a Delphis / beacon (name, flags, battery range)."""
        wire = await self.send_raw(self._build_config_read(0x06), timeout=timeout)
        return gigo_decode_config(self.profile, self._decode_response(wire, label="GIGO-CFG-RD"))

    async def write_gigo_config(self, fields: dict, current: dict, timeout: float = 5.0) -> bool:
        """0x05 write (the device saves and resets). Flags not given are taken from current, see config_codecs."""
        payload = gigo_encode_config(self.profile, fields, current)
        resp = await self.send_raw(self._build_config_write(0x05, len(payload), payload), timeout=timeout)
        pkt = self._decode_response(resp, label="GIGO-CFG-WR")
        return self._is_ack(pkt, 0x05) and pkt[2] == 1

    async def gigo_reset(self, timeout: float = 3.0) -> bool:
        """0x03 reset; a reply lost to the reset counts as done."""
        try:
            resp = await self.send_raw(self._build_config_read(0x03), timeout=timeout)
        except asyncio.TimeoutError:
            return True
        return self._is_ack(self._decode_response(resp, label="GIGO-RST"), 0x03)

    async def gigo_sanity_test(self, test: int = 1, timeout: float = 8.0) -> "dict | None":
        """0x09 self test (1 all, 2 nano-modem, 3 IMU): {'test', 'error', 'raw'}; the device closes the link after."""
        try:
            resp = await self.send_raw(self._build_config_write(0x09, 1, bytes([test & 0xFF])), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        pkt = self._decode_response(resp, label="GIGO-SANITY")
        if len(pkt) < 4:
            return None
        return {"test": pkt[2], "error": pkt[3], "raw": pkt[2:2 + pkt[1]].hex(" ")}

    async def gigo_save_calibration(self, timeout: float = 4.0) -> bool:
        """0x0C: copy the IMU offsets into the stored configuration."""
        resp = await self.send_raw(self._build_config_read(0x0C), timeout=timeout)
        return self._is_ack(self._decode_response(resp, label="GIGO-CAL"), 0x0C)

    async def beacon_read_mode(self, timeout: float = 4.0) -> "int | None":
        """0xB8: IMU operation mode (0 disabled, 1 net drum, 2 hydraulic jack)."""
        try:
            resp = await self.send_raw(self._build_config_read(0xB8), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        pkt = self._decode_response(resp, label="BCN-MODE-RD")
        return pkt[2] if len(pkt) >= 3 else None

    async def beacon_write_mode(self, mode: int, timeout: float = 4.0) -> bool:
        """0xB7: set the IMU operation mode (the beacon saves and resets)."""
        resp = await self.send_raw(self._build_config_write(0xB7, 1, bytes([mode & 0xFF])), timeout=timeout)
        pkt = self._decode_response(resp, label="BCN-MODE-WR")
        return self._is_ack(pkt, 0xB7) and pkt[2] == 1

    async def beacon_read_tuning(self, timeout: float = 4.0) -> "dict | None":
        """0xBA: movement tuning (angle, dead zone, debounce ms, rotation count, jack sensitivity)."""
        try:
            resp = await self.send_raw(self._build_config_read(0xBA), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return beacon_decode_tuning(self._decode_response(resp, label="BCN-TUNE-RD"))

    async def beacon_write_tuning(self, fields: dict, timeout: float = 4.0) -> bool:
        """0xB9: write the movement tuning (all fields required; the beacon saves and resets)."""
        payload = beacon_encode_tuning(fields)
        resp = await self.send_raw(self._build_config_write(0xB9, len(payload), payload), timeout=timeout)
        pkt = self._decode_response(resp, label="BCN-TUNE-WR")
        return self._is_ack(pkt, 0xB9) and pkt[2] == 1

    # ---------- GiGO Delphis / beacon: defaults, IMU, nano-modem ----------
    async def gigo_reset_defaults(self, timeout: float = 3.0) -> bool:
        """0x07: configuration and IMU calibration back to defaults, then reset (a reply lost to the reset counts)."""
        try:
            resp = await self.send_raw(self._build_config_read(0x07), timeout=timeout)
        except asyncio.TimeoutError:
            return True
        return self._is_ack(self._decode_response(resp, label="GIGO-DEFAULTS"), 0x07)

    async def imu_set_stream(self, enable: bool, timeout: float = 4.0) -> bool:
        """0xB1: start / stop the 0xB2 IMU stream (every 500 ms; IMU must be enabled in the configuration)."""
        flag = IMU_STREAM_START if enable else IMU_STREAM_STOP
        resp = await self.send_raw(self._build_config_write(0xB1, 1, bytes([flag])), timeout=timeout)
        return self._is_ack(self._decode_response(resp, label="IMU-STREAM"), 0xB1)

    async def imu_stream_acquire(self, owner) -> bool:
        """Join the IMU stream; the first owner switches it on (0xB1). False when the device refused."""
        if owner in self._imu_stream_owners:
            return True
        if not self._imu_stream_owners and not await self.imu_set_stream(True):
            return False
        self._imu_stream_owners.add(owner)
        return True

    async def imu_stream_release(self, owner) -> bool:
        """Leave the IMU stream; the last owner switches it off. True when nothing was left to do or the device
        accepted.
        """
        if owner not in self._imu_stream_owners:
            return True
        self._imu_stream_owners.discard(owner)
        if self._imu_stream_owners:
            return True
        return await self.imu_set_stream(False)

    async def imu_read_full_scale(self, timeout: float = 4.0) -> "dict | None":
        """0xB5: IMU init status and the accelerometer / gyroscope range indices."""
        resp = await self.send_raw(self._build_config_read(0xB5), timeout=timeout)
        return decode_imu_full_scale(self._decode_response(resp, label="IMU-FS-RD"))

    async def imu_write_full_scale(self, acc_index: "int | None", gyro_index: "int | None",
                                   timeout: float = 4.0) -> bool:
        """0xB4: set the ranges (None keeps one); kept in RAM, stored with the next configuration save."""
        payload = encode_imu_full_scale(acc_index, gyro_index)
        resp = await self.send_raw(self._build_config_write(0xB4, len(payload), payload), timeout=timeout)
        return self._is_ack(self._decode_response(resp, label="IMU-FS-WR"), 0xB4)

    async def imu_mag_init(self, timeout: float = 4.0) -> bool:
        """0xB6: reset the AK09916 magnetometer and set its mode; True when it answered."""
        resp = await self.send_raw(self._build_config_read(0xB6), timeout=timeout)
        pkt = self._decode_response(resp, label="IMU-MAG")
        return self._is_ack(pkt, 0xB6) and pkt[2] == 1

    async def imu_recalibrate(self, timeout: float = 4.0) -> bool:
        """0xB3: restart the IMU calibration (keep the device still); 0x0C stores the offsets afterwards."""
        resp = await self.send_raw(self._build_config_read(0xB3), timeout=timeout)
        return self._is_ack(self._decode_response(resp, label="IMU-CAL"), 0xB3)

    async def imu_register_read(self, bank: int, register: int, count: int, timeout: float = 4.0) -> "bytes | None":
        """0xB0 read of count ICM-20948 registers from bank / register."""
        payload = encode_imu_register_read(bank, register, count)
        resp = await self.send_raw(self._build_config_write(0xB0, len(payload), payload), timeout=timeout)
        return decode_imu_register_read(self._decrypt_full(resp), count)

    async def imu_register_write(self, bank: int, register: int, value: int, timeout: float = 4.0) -> bool:
        """0xB0 write of one ICM-20948 register."""
        payload = encode_imu_register_write(bank, register, value)
        resp = await self.send_raw(self._build_config_write(0xB0, len(payload), payload), timeout=timeout)
        return self._is_ack(self._decode_response(resp, label="IMU-REG-WR"), 0xB0)

    async def nano_modem_send(self, command: str) -> None:
        """0xA0: pass a console line to the Delphis nano-modem UART; its answers arrive as 0xA0 / 0xA1 frames."""
        await self.nano_modem_send_bytes(encode_nano_modem(command))

    async def nano_modem_send_bytes(self, data: bytes) -> None:
        """0xA0 with any bytes (binary picture frames included), 1..48: Delphis drops BLE writes over 64 bytes."""
        if not data or len(data) > NANO_MODEM_MAX_COMMAND:
            raise ValueError(f"a nano-modem command is 1..{NANO_MODEM_MAX_COMMAND} bytes, got {len(data)}")
        await self.send_no_reply(self._build_config_write(NANO_MODEM_DATA, len(data), bytes(data)))

    # ---------- SC2 hub: industry mode, D&T relay, GPS ----------
    async def sc2_read_industry(self, timeout: float = 5.0) -> dict:
        """0xC2 get: garage mode and the GIGO / D&T report channels."""
        return sc2_decode_industry(await self.send_sc2_command(0xC2, label="SC2-IND-RD", timeout=timeout))

    async def sc2_write_industry(self, fields: dict, current: dict, timeout: float = 5.0) -> bool:
        """0xC2 set (the hub resets on success; a refused value is answered 0 without a reset)."""
        payload = sc2_encode_industry(fields, current)
        pkt = await self.send_sc2_command(0xC2, payload, label="SC2-IND-WR", timeout=timeout)
        return self._is_ack(pkt, 0xC2) and pkt[2] == SC2_ACK_SUCCESS

    async def sc2_read_dt_addresses(self, timeout: float = 5.0) -> list:
        """0xC8 get (length 0): the five logger slots as address strings ('' = empty)."""
        resp = await self.send_raw(bytes([0xC8, 0x00]), timeout=timeout)
        return sc2_decode_dt_addresses(self._decode_response(resp, label="SC2-DT-ADDR-RD"))

    async def sc2_write_dt_addresses(self, addresses: list, timeout: float = 5.0) -> bool:
        """0xC8 set: store up to five logger addresses (the hub resets)."""
        payload = sc2_encode_dt_addresses(addresses)
        pkt = await self.send_sc2_command(0xC8, payload, label="SC2-DT-ADDR-WR", timeout=timeout)
        return self._is_ack(pkt, 0xC8) and pkt[2] == SC2_DT_ACK_DONE

    async def sc2_read_dt_device(self, address: str, timeout: float = 5.0) -> dict:
        """0xC9 get: recording mode and phase intervals / durations the hub holds for one logger."""
        pkt = await self.send_sc2_command(0xC9, sc2_dt_get_request(address), label="SC2-DT-CFG-RD", timeout=timeout)
        return sc2_decode_dt_device(pkt)

    async def sc2_write_dt_device(self, address: str, mode, phases: list, timeout: float = 5.0) -> "bool | None":
        """0xC9 set: True stored, False address not in the list (the hub resets either way), None no answer."""
        payload = sc2_encode_dt_device(address, mode, phases)
        pkt = await self.send_sc2_command(0xC9, payload, label="SC2-DT-CFG-WR", timeout=timeout)
        if not self._is_ack(pkt, 0xC9):
            return None
        return pkt[2] == SC2_DT_ACK_DONE

    async def sc2_request_gps(self) -> None:
        """0x12: ask for a fix; the 0x12 frame arrives through a listener when the GPS task has one."""
        await self.send_no_reply(self._build_config_read(0x12))

    async def sc2_live_track(self, start: bool) -> None:
        """0xE3 start / stop: while on, the hub sends a 0x12 frame after every fix (stops on disconnect)."""
        flag = SC2_LIVE_TRACK_START if start else SC2_LIVE_TRACK_STOP
        await self.send_no_reply(self._build_config_write(0xE3, 1, bytes([flag])))

    async def send_sc2_command(self, opcode: int, payload: "bytes | None" = None,
                               label: str = "SC2-CMD", timeout: float = 5.0) -> bytes:
        """Send a read (payload is None) or AES-encrypted write for any SC2 hub opcode and return the decrypted reply.
        The BLE co-processor forwards only sc2_hub_codecs.SC2_FORWARDED_OPCODES; others are dropped without a reply.
        """
        if payload is None:
            wire = self._build_config_read(opcode)
        else:
            wire = self._build_config_write(opcode, len(payload), payload)
        resp = await self.send_raw(wire, timeout=timeout)
        return self._decode_response(resp, label=label)
