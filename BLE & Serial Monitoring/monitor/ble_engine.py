"""@file ble_engine.py
@brief BLE side of the monitor: one session per connected device, connect/reconnect loop, asyncio worker thread
@version 1.0
@date 2026-11-07

bleak is asyncio-based and Tk is not, so all BLE work runs on one asyncio loop in a background thread; the GUI
starts coroutines there with run_async() and gets results back through the GUI message bus. Several devices can
be connected at once: each has a BleSession, and the GUI works on the selected one (or a SessionView of one).
Which device a session talks to (Valtrack V1/2, SC2 Gen2/Gen3, GiGO Delphis / Rotation-Jack / Depth & Temp) is decided
from its GATT table after connecting, probing the GiGO families after authentication (device_profiles.py); an operator
choice under 'Device type' is used where the GATT table cannot decide (SC2 generation, Valtrack V1 vs 2).
"""
from .constants import AES_KEY_GIGO_4G
from .constants import AES_KEY_SC2_HUB
from .constants import BRAND
from .constants import SC2_HUB_CMD_CHAR_UUID
from .constants import STM_OTA_SERVICE_UUID
from .device_profiles import GIGO_IMU_CMD_CHAR
from .device_profiles import GIGO_LOGGER_CMD_CHAR
from .device_profiles import PROBE_GAS_OPCODE
from .device_profiles import PROBE_TIMEOUT_S
from .device_profiles import PROFILES
from .device_profiles import VALTRACK_CMD_CHAR
from .device_profiles import SC2_GEN3_MIN_FW_MAJOR
from .device_profiles import NORDIC_COMPANY_ID
from .device_profiles import classify_advertisement
from .device_profiles import classify_gatt
from .device_profiles import device_type_profile
from .device_profiles import gatt_family_profiles
from .device_profiles import normalise_profile
from .device_profiles import profile_label
from .gas_device import GasLoggerDevice
from .gas_protocol import DEVICE_TYPE_GAS
from .gas_protocol import STATUS_SUCCESS as GAS_STATUS_SUCCESS
from .gigo_device import Gigo4gDevice
from .logbus import _log_session_id
from .logbus import _selected_ble_session_id
from .logbus import log_system
from .logbus import post_gui
from .protocol import auth_key_value_gigo
from .protocol import auth_key_value_sc2_hub
from bleak import BleakClient
from bleak import BleakScanner
from sc2_protocol import Sc2Device
import asyncio
import sys
import threading


# BLE Engine
# --------------------------------------------------------------------------
class BleSession:
    def __init__(self, session_id: str, target: str, profile: str, device_type: str = "auto"):
        """Per-device state: target name/address, protocol profile (device_profiles.PROFILES key), the device type
        the operator chose ('auto' = detect), BLE client, the protocol object, connection flags and the event that
        ends its connect loop.
        """
        self.session_id = session_id
        self.target = target
        self.profile = normalise_profile(profile) or "valtrack"
        self.device_type = device_type or "auto"
        self.name = target
        self.address = target
        self.client: "BleakClient | None" = None
        self.device: "Gigo4gDevice | None" = None
        self.sc2: "Sc2Device | None" = None
        self.disconnect_evt = asyncio.Event()
        self.connected = False
        self.connecting = True
        self.ota_active = False   # True while a DOTA update is running for this device

class BleEngine:
    """All BLE sessions plus the scanner; the GUI's single entry point for BLE work."""
    def __init__(self):
        """No devices yet, no session selected, profile 'valtrack', auto-reconnect on."""
        self.discovered_devices = {}
        self.sessions: dict[str, BleSession] = {}
        self.selected_session_id: str | None = None
        self.profile: str = "valtrack"
        self.device_type: str = "auto"
        self.loop: "asyncio.AbstractEventLoop | None" = None
        self.auto_reconnect: bool = True

    @property
    def selected_session(self) -> BleSession | None:
        """The session the current call works on: the one bound to this call's context, else the
        one selected in the GUI, else the only session when there is just one.
        """
        session_id = _selected_ble_session_id.get()
        if session_id is None:
            session_id = self.selected_session_id
        if session_id is None:
            if len(self.sessions) == 1:
                return next(iter(self.sessions.values()))
            return None
        sid = session_id.lower().strip()
        for k, v in self.sessions.items():
            if k.lower().strip() == sid:
                return v
        if len(self.sessions) == 1:
            return next(iter(self.sessions.values()))
        return None

    @property
    def active_device(self) -> "Gigo4gDevice | None":
        """Valtrack protocol object of the selected session, else of any connected session."""
        session = self.selected_session
        if session and session.device:
            return session.device
        for s in self.sessions.values():
            if s.connected and s.device:
                return s.device
        return None

    @property
    def active_sc2(self) -> "Sc2Device | None":
        """SC2 protocol object of the selected session, else of any connected session."""
        session = self.selected_session
        if session and session.sc2:
            return session.sc2
        for s in self.sessions.values():
            if s.connected and s.sc2:
                return s.sc2
        return None

    @property
    def active_client(self) -> "BleakClient | None":
        """BLE client of the selected session, else of any connected session."""
        session = self.selected_session
        if session and session.client:
            return session.client
        for s in self.sessions.values():
            if s.connected and s.client:
                return s.client
        return None

    def select_session(self, session_id: str | None):
        """Make session_id (case-insensitive) the selected session; None clears the selection."""
        if not session_id:
            self.selected_session_id = None
            return
        sid = session_id.lower().strip()
        for k in self.sessions:
            if k.lower().strip() == sid:
                self.selected_session_id = k
                return
        self.selected_session_id = sid

    async def scan(self, filter_text: str = "Succorfish Gigo", timeout: float = 6.0):
        """Scan for timeout seconds, keep the devices whose name matches filter_text (empty = all) and send
        the list to the GUI.
        """
        log_system(f"Scanning for BLE devices (Filter: '{filter_text or 'All'}')...", "#facc15")
        post_gui("status_update", status="Scanning...", color="#eab308")

        try:
            discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
            self.discovered_devices.clear()

            device_list = []
            succorfish_found = False

            for mac, (device, adv_data) in discovered.items():
                self.discovered_devices[mac] = (device, adv_data)
                name = adv_data.local_name or device.name or "Unknown Device"
                uuids = [str(u).lower() for u in (adv_data.service_uuids or [])]
                is_stm_ota = name.lower().startswith("stm_ota") or (STM_OTA_SERVICE_UUID.lower() in uuids)
                if is_stm_ota and name == "Unknown Device":
                    name = "STM_OTA"

                if filter_text and (filter_text.lower() not in name.lower()) and not is_stm_ota:
                    continue

                is_succorfish = (
                    "succorfish" in name.lower()
                    or "gigo" in name.lower()
                    or is_stm_ota
                )
                if is_succorfish:
                    succorfish_found = True

                # Device type from the advertisement (confirmed from GATT after connecting, see device_profiles).
                companies = adv_data.manufacturer_data or {}
                guess = classify_advertisement(name, uuids, companies.get(NORDIC_COMPANY_ID), tuple(companies))
                if guess["is_ota"]:
                    is_stm_ota = True
                suggested_profile = guess["profile"]
                fw_label = guess["fw"]

                device_list.append({
                    "mac": mac,
                    "name": name,
                    "rssi": adv_data.rssi,
                    "device": device,
                    "is_target": is_succorfish,
                    "is_ota": is_stm_ota,
                    "fw_version": fw_label,
                    "suggested_profile": suggested_profile,
                    "kind": guess["kind"],
                })

            device_list.sort(key=lambda x: (x["is_target"], x["rssi"]), reverse=True)
            post_gui("scan_results", devices=device_list, auto_select_target=succorfish_found)

            if not device_list:
                log_system("No matching BLE devices found.", "#ef4444")
                post_gui("status_update", status="No Devices", color="#ef4444")
            else:
                log_system(f"Found {len(device_list)} device(s). Ready to connect.", "#10b981")
                post_gui("status_update", status="Device Found", color="#10b981")

        except Exception as e:
            log_system(f"Scan error: {e}", "#ef4444")
            if sys.platform == "darwin":
                log_system(
                    "macOS hint: ensure Bluetooth is ON and grant Bluetooth permission to "
                    "Terminal / ValtrackDebugMonitor in System Settings -> Privacy & Security -> Bluetooth.",
                    BRAND["warn"],
                )
            post_gui("scan_results", devices=[], auto_select_target=False)
            post_gui("status_update", status="Scan Error", color="#ef4444")

    async def connect(self, target_identifier: str = "Succorfish Gigo",
                      profile: str = "valtrack", device_type: str = "auto"):
        """Create (or reuse) the session for target and start its connect loop; does nothing when that
        device is already connected or connecting.
        """
        target = target_identifier.strip()
        session_id = target.lower()
        existing = self.sessions.get(session_id)
        if existing and (existing.connected or existing.connecting):
            log_system(f"Already connected or connecting to {existing.name}.", BRAND["warn"],
                       session_id=session_id)
            return

        session = BleSession(session_id, target, profile, device_type)
        cached = self.discovered_devices.get(target)
        if cached:
            session.name = cached[1].local_name or cached[0].name or target
            session.address = cached[0].address or target
        self.sessions[session_id] = session
        self.selected_session_id = session_id
        post_gui("session_state", session_id=session_id, name=session.name,
                 address=session.address, profile=profile, status="connecting")
        await self.connect_loop(session)

    async def connect_loop(self, session: BleSession):
        """Keep one session connected until the user disconnects it.

        Flow: wait while a firmware update owns the radio -> connect (_connect_session returns when
        the link drops) -> on failure clean up and report -> stop if disconnect was requested or
        auto-reconnect is off -> wait 3 s and try again.
        """
        attempt = 1
        while not session.disconnect_evt.is_set():
            # A firmware update owns the radio: the app link is expected to drop and the device to
            # come back as STM_OTA. Do not try to reconnect to it until the update finishes.
            while session.ota_active and not session.disconnect_evt.is_set():
                await asyncio.sleep(0.25)
            if session.disconnect_evt.is_set():
                break
            try:
                await self._connect_session(session)
            except Exception as e:
                err_msg = str(e) or type(e).__name__
                failed_client = session.client
                if failed_client and failed_client.is_connected:
                    try:
                        await failed_client.disconnect()
                    except Exception:
                        pass
                session.client = None
                session.device = None
                session.sc2 = None
                session.connected = False
                session.connecting = False
                log_system(f"Connection failed: {err_msg}", "#ef4444",
                           session_id=session.session_id)
                post_gui("session_state", session_id=session.session_id, name=session.name,
                         address=session.address, profile=session.profile, status="disconnected")

            if session.disconnect_evt.is_set():
                break
            if not self.auto_reconnect:
                log_system("Auto-reconnect is disabled. Standing by.", "#9ca3af",
                           session_id=session.session_id)
                break

            wait_seconds = 3.0
            log_system(f"Connection lost. Auto-reconnecting in {int(wait_seconds)}s "
                       f"(Attempt #{attempt})...", "#f59e0b", session_id=session.session_id)
            post_gui("session_state", session_id=session.session_id, name=session.name,
                     address=session.address, profile=session.profile, status="reconnecting")
            for _ in range(int(wait_seconds * 4)):
                if session.disconnect_evt.is_set():
                    break
                await asyncio.sleep(0.25)
            if session.disconnect_evt.is_set():
                break
            attempt += 1

    async def _connect_session(self, session: BleSession):
        """Connect one session and stay until the link drops.

        Flow: find the device (rescan if it is not cached) -> GATT connect with retries -> find
        the command characteristic of the profile (Valtrack or SC2) -> create the protocol
        object, subscribe, authenticate -> report 'connected' -> poll the link every 0.5 s ->
        unsubscribe, disconnect, report 'disconnected'.
        """
        target = session.target
        cached = self.discovered_devices.get(target)
        if cached is None:
            cached = next((value for key, value in self.discovered_devices.items()
                           if key.lower() == target.lower() or
                           (value[0].name or "").lower() == target.lower()), None)
        if cached is None:
            try:
                log_system(f"Target [{target}] not in scan cache; scanning...", BRAND["warn"],
                           session_id=session.session_id)
                disc = await BleakScanner.discover(timeout=6.0, return_adv=True)
                for mac, (dev, adv) in disc.items():
                    self.discovered_devices[mac] = (dev, adv)
                cached = self.discovered_devices.get(target)
                if cached is None:
                    cached = next((value for key, value in self.discovered_devices.items()
                                   if key.lower() == target.lower() or
                                   (value[0].name or "").lower() == target.lower()), None)
            except Exception as scan_err:
                log_system(f"Direct scan error: {scan_err}", BRAND["err"],
                           session_id=session.session_id)
        target_obj = cached[0] if cached else target
        if cached:
            session.name = cached[1].local_name or target_obj.name or session.name
        session.address = getattr(target_obj, "address", None) or session.address
        session.connecting = True
        post_gui("session_state", session_id=session.session_id, name=session.name,
                 address=session.address, profile=session.profile, status="connecting")
        log_system(f"Connecting to {session.name} [{session.address}]...", "#38bdf8",
                   session_id=session.session_id)

        def handle_disconnect(_client):
            """bleak disconnect callback: log it and wake the protocol object."""
            log_system("BLE link disconnected.", "#ef4444", session_id=session.session_id)
            if session.device:
                session.device.disconnected_evt.set()

        client = None
        dev = None
        for attempt in range(1, 4):
            if session.disconnect_evt.is_set():
                break
            try:
                connect_target = target_obj
                if attempt > 1 or isinstance(connect_target, str):
                    try:
                        fresh = await BleakScanner.find_device_by_address(session.address, timeout=6.0)
                        if fresh is not None:
                            connect_target = fresh
                            target_obj = fresh
                    except Exception:
                        pass
                log_system(f"Connecting to GATT (Attempt {attempt}/3)...",
                           "#facc15", session_id=session.session_id)
                client = BleakClient(connect_target, timeout=16.0, disconnected_callback=handle_disconnect)
                session.client = client
                await client.connect()
                if client.is_connected:
                    break
            except Exception as e:
                err_txt = str(e) or type(e).__name__
                log_system(f"GATT connect attempt {attempt} failed: {err_txt}", "#f59e0b",
                           session_id=session.session_id)
                if attempt == 3:
                    raise
                await asyncio.sleep(1.0)

        if not client or not client.is_connected:
            raise RuntimeError("Connection could not be established.")

        device_addr = getattr(target_obj, "address", session.address)
        device_name = getattr(target_obj, "name", session.name) or session.name
        session.address = device_addr
        session.name = device_name
        log_system(f"Connected to {device_name} [{device_addr}]!", "#10b981",
                   session_id=session.session_id)

        try:
            # ---- device family from the GATT table; the GiGO nRF families are told apart after authentication ----
            all_chars = {
                str(char.uuid).lower()
                for service in client.services
                for char in service.characteristics
            }
            gatt_family = classify_gatt(all_chars)
            requested = device_type_profile(session.device_type)
            mfg_bytes = (cached[1].manufacturer_data or {}).get(0x0059) if cached else None
            if gatt_family is None:
                raise RuntimeError("No known command characteristic (0xFE41 Valtrack, 0xBA01 SC2, 0xAB01 GiGO "
                                   "Delphis / Rotation-Jack, 0xAB05 GiGO Depth & Temp / Gas) on this device.")

            candidates = gatt_family_profiles(gatt_family)
            profile = requested if requested in candidates else None
            name_lower = (session.name or "").lower()
            if requested and requested not in candidates:
                log_system(f"Device type '{profile_label(requested)}' was selected, but the device exposes the "
                           f"service of {' / '.join(profile_label(key) for key in candidates)}; detecting it from "
                           "the device instead.", BRAND["warn"], session_id=session.session_id)

            if gatt_family == "valtrack":
                profile = "valtrack"
                char_found = VALTRACK_CMD_CHAR
            elif gatt_family == "sc2":
                char_found = SC2_HUB_CMD_CHAR_UUID.lower()
                if profile is None and mfg_bytes and len(mfg_bytes) >= 8:
                    profile = "sc2_gen3" if mfg_bytes[6] >= SC2_GEN3_MIN_FW_MAJOR else "sc2_gen2"
                elif profile is None:
                    profile = "sc2_gen2"
                    log_system("SC2 generation unknown (no scan response data): assuming Gen2. Choose 'SC2 Gen3' "
                               "under Device type if this is a Gen3 unit.", BRAND["warn"],
                               session_id=session.session_id)
            elif gatt_family == "gigo_imu":
                char_found = GIGO_IMU_CMD_CHAR
            else:
                char_found = GIGO_LOGGER_CMD_CHAR
                if profile is None and "d&t" in name_lower:
                    profile = "gigo_dt"
                elif profile is None and "succorfish gas" in name_lower:
                    profile = "gigo_gas"

            if gatt_family == "gigo_logger":
                # D&T and gas share transport and opcodes 0x03..0x2A: the gas class serves both (it only adds 0x30..).
                dev = GasLoggerDevice(client, aes_key=AES_KEY_GIGO_4G, char_uuid=char_found)
                dev.session_id = session.session_id
                session.sc2 = dev
                await dev.start()
                log_system("GiGO logger: authenticating (challenge/response)...", BRAND["warn"],
                           session_id=session.session_id)
                if not await dev.authenticate():
                    raise RuntimeError("GiGO logger rejected authentication.")
                if profile is None:
                    status, data = await dev.send(PROBE_GAS_OPCODE, timeout=PROBE_TIMEOUT_S)
                    is_gas = status == GAS_STATUS_SUCCESS and bool(data) and data[0] == DEVICE_TYPE_GAS
                    profile = "gigo_gas" if is_gas else "gigo_dt"
                    log_system(f"GiGO logger identified by probing 0x{PROBE_GAS_OPCODE:02X}: "
                               f"{profile_label(profile)}.", BRAND["info"], session_id=session.session_id)
            else:
                is_sc2_hub = profile in ("sc2_gen2", "sc2_gen3")
                dev = Gigo4gDevice(
                    client,
                    cmd_char_uuid=char_found,
                    session_id=session.session_id,
                    aes_key=AES_KEY_SC2_HUB if is_sc2_hub else AES_KEY_GIGO_4G,
                    auth_fn=auth_key_value_sc2_hub if is_sc2_hub else auth_key_value_gigo,
                    profile=profile or "gigo_delphis",
                )
                session.device = dev
                await dev.start()
                log_system(f"Authenticating ({profile_label(profile) if profile else 'GiGO device'})...",
                           BRAND["warn"], session_id=session.session_id)
                if not await dev.authenticate():
                    raise RuntimeError("Device rejected authentication.")

                if profile is None:
                    # Delphis and the beacon share name, service and characteristic: only the beacon answers 0xB8.
                    profile = await dev.identify_gigo_family()
                    dev.profile = profile
                    log_system(f"GiGO device identified by probing: {profile_label(profile)}.", BRAND["info"],
                               session_id=session.session_id)

            session.profile = profile
            self.profile = profile
            label = profile_label(profile, session.device_type)
            log_system(f"Device type: {label} ({PROFILES[profile].hardware}). Authenticated.", BRAND["ok"],
                       session_id=session.session_id)
            post_gui("connection_state", connected=True, mac=device_addr, session_id=session.session_id,
                     name=device_name, char_uuid=char_found, profile=profile)
            session.connected = True
            session.connecting = False
            post_gui("session_state", session_id=session.session_id, name=session.name,
                     address=session.address, profile=session.profile, status="connected")

            while client.is_connected and not session.disconnect_evt.is_set() and (
                    not isinstance(dev, Gigo4gDevice) or not dev.disconnected_evt.is_set()):
                await asyncio.sleep(0.5)

        finally:
            if isinstance(dev, Gigo4gDevice):
                await dev.stop()
            elif isinstance(dev, Sc2Device):
                await dev.stop()
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            session.client = None
            session.device = None
            session.sc2 = None
            session.connected = False
            session.connecting = False
            post_gui("connection_state", connected=False, session_id=session.session_id,
                     profile=session.profile)
            post_gui("session_state", session_id=session.session_id, name=session.name,
                     address=session.address, profile=session.profile, status="disconnected")
            log_system("Disconnected.", "#facc15", session_id=session.session_id)

    async def disconnect_selected(self):
        """End the selected session's connect loop and drop its link."""
        session = self.selected_session
        if session is None:
            return
        session.disconnect_evt.set()
        if session.client and session.client.is_connected:
            await session.client.disconnect()

    async def disconnect_all(self):
        """End every session's connect loop and drop every link."""
        for session in list(self.sessions.values()):
            session.disconnect_evt.set()
            if session.client and session.client.is_connected:
                await session.client.disconnect()

    async def toggle_stream(self, enable: bool):
        """Switch the selected device's debug-log stream on or off and show the new state."""
        session = self.selected_session
        if session and session.device and session.client and session.client.is_connected:
            accepted = await session.device.set_debug_log(enable)
            post_gui("stream_state", streaming=session.device.streaming_enabled,
                     session_id=session.session_id)
            if not accepted:
                log_system(f"Device did not accept debug logging {'ON' if enable else 'OFF'}.",
                           BRAND["err"], session_id=session.session_id)


class SessionView:
    """Read-only view of ONE BLE session, immune to which log tab is currently selected.

    Config / sensor windows hold one of these so a click always talks to the device the window
    was opened for, even if the operator switches to another device's tab afterwards.
    """

    def __init__(self, engine: "BleEngine", session_id: "str | None"):
        """View of the engine pinned to one session (used by windows opened for a given device)."""
        self._engine = engine
        self.session_id = session_id

    @property
    def selected_session_id(self):
        """The session this view is pinned to."""
        return self.session_id

    @property
    def selected_session(self):
        """The pinned session (never falls back to a different device when pinned)."""
        if not self.session_id:
            return self._engine.selected_session
        sid = self.session_id.lower().strip()
        for k, v in self._engine.sessions.items():
            if k.lower().strip() == sid:
                return v
        return None

    @property
    def active_device(self):
        """Valtrack protocol object of the pinned session while it is connected."""
        s = self.selected_session
        return s.device if s and s.connected else None

    @property
    def active_sc2(self):
        """SC2 protocol object of the pinned session while it is connected."""
        s = self.selected_session
        return s.sc2 if s and s.connected else None

    @property
    def active_client(self):
        """BLE client of the pinned session while it is connected."""
        s = self.selected_session
        return s.client if s and s.connected else None


engine = BleEngine()


_async_loop_ready = threading.Event()
_async_loop_start_lock = threading.Lock()


def _async_loop_worker():
    """Body of the BLE thread: create the asyncio loop, publish it and run it until stopped, then
    cancel what is left.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    engine.loop = loop
    _async_loop_ready.set()
    loop.run_forever()

    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()


def run_async(coro, session_id: str | None = None):
    """Run coroutine coro on the BLE loop (starting the thread on first use) and return its future.
    The call's log lines and session selection are bound to session_id; errors are logged.
    """
    if engine.loop is None:
        with _async_loop_start_lock:
            if engine.loop is None:
                threading.Thread(target=_async_loop_worker, daemon=True).start()
                if not _async_loop_ready.wait(timeout=5.0):
                    raise RuntimeError("BLE asyncio event loop did not start.")

    if session_id is None:
        session_id = engine.selected_session_id

    async def run_with_log_context():
        """Await coro, then restore the context variables set for it."""
        token = _log_session_id.set(session_id)
        selected_token = _selected_ble_session_id.set(session_id)
        try:
            return await coro
        finally:
            _selected_ble_session_id.reset(selected_token)
            _log_session_id.reset(token)

    fut = asyncio.run_coroutine_threadsafe(run_with_log_context(), engine.loop)
    def _log_bg_err(f):
        """Done-callback: log the exception of a background task, if any."""
        try:
            exc = f.exception()
            if exc:
                log_system(f"Background task error: {exc}", BRAND["err"], session_id=session_id)
        except Exception:
            pass
    fut.add_done_callback(_log_bg_err)
    return fut
