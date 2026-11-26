"""@file config_panel.py
@brief Valtrack configuration window: system/diagnostics, device (0xC1), SIM (0xC3), server (0xC4), actions
       (0xC5 reset, 0xE2 token) and the BLE firmware update (DOTA)
@version 2.0
@date 2026-11-21

One window per device: it is bound to the session it was opened for (SessionView), so two open windows never
talk to the wrong device. Every button handler builds a small coroutine "go" and runs it on the BLE thread with
_run()/run_async(); results come back to the widgets with self.after(0, ...), because Tk is single-threaded.
"""
from .ble_engine import BleEngine
from .config_codecs import VALTRACK_C1_FIELDS
from .config_codecs import ConfigValueError
from .config_codecs import format_seconds
from .config_codecs import parse_number
from .ble_engine import SessionView
from .constants import STM_OTA_SERVICE_UUID
from .ble_engine import run_async
from .constants import APP_BASE_ADDRESS_FIELD
from .constants import APP_SECTOR_COUNT_MAX
from .constants import APP_START_SECTOR
from .constants import ATT_HEADER_BYTES
from .constants import BLE_READ_FIRMWARE_VERSION
from .constants import BRAND
from .constants import COMMAND_APPLICATION_UPLOAD
from .constants import COMMAND_CHAR_UUIDS
from .constants import COMMAND_UPLOAD_FINISHED
from .constants import CONFIRMATION_REBOOT
from .constants import DOUBLE_WORD_BYTES
from .constants import FLASH_SECTOR_BYTES
from .constants import RAW_DATA_MAX_BYTES
from .constants import REBOOT_CHAR_UUID
from .constants import REBOOT_MODE_BLE_OTA
from .constants import STATUS_OK
from .constants import STM_OTA_CONF_CHAR
from .constants import STM_OTA_CTRL_CHAR
from .constants import STM_OTA_DATA_CHAR
from .logbus import log_system
from .logbus import post_gui
from .ota_image import check_image
from .theme import BrandHeader
from .theme import apply_brand_theme
from .theme import apply_window_icon
from bleak import BleakClient
from bleak import BleakScanner
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
from datetime import datetime
from datetime import timezone
import asyncio
import math
import time
import os
import tkinter as tk


# Device clock difference to the PC that is still shown as in sync.
CLOCK_TOLERANCE_S = 2


def _related_ble_addresses(address: str) -> set[str]:
    """Return lowercase MAC address plus its +/-1 last-octet peers (Valtrack_Boot uses BD_ADDR[0] + 1)."""
    raw = (address or "").strip().lower()
    if not raw:
        return set()
    peers = {raw}
    parts = raw.split(":")
    if len(parts) == 6:
        try:
            last_octet = int(parts[5], 16)
            prefix = ":".join(parts[:5])
            peers.add(f"{prefix}:{(last_octet + 1) & 0xFF:02x}")
            peers.add(f"{prefix}:{(last_octet - 1) & 0xFF:02x}")
        except ValueError:
            pass
    return peers


class ConfigPanel(tk.Toplevel):
    """Configuration window of one Valtrack device (see the module header)."""
    def __init__(self, master, engine: "BleEngine", initial_tab: "str | None" = None,
                 target_address: "str | None" = None, session_id: "str | None" = None):
        """Bind the window to the exact device session, size and place it, build the header and all tabs."""
        super().__init__(master)
        # Bind strictly to the requested device (never steal another connected device's session).
        explicit_sid = (session_id or target_address or "").lower().strip() or None
        self.session_id = explicit_sid or engine.selected_session_id
        self.target_address = (target_address or "").strip()
        self.engine = SessionView(engine, self.session_id)
        sess = self.engine.selected_session
        addr_tag = f" [{sess.address}]" if sess and getattr(sess, "address", None) else (
            f" [{self.target_address}]" if self.target_address else ""
        )
        self.title(f"{BRAND['company']} {BRAND['product']} - Configuration{addr_tag}")
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        target_w = min(1100, int(screen_w * 0.9))
        target_h = min(740, int(screen_h * 0.85))
        pos_x = max(0, (screen_w - target_w) // 2)
        pos_y = max(0, (screen_h - target_h) // 2)
        self.geometry(f"{target_w}x{target_h}+{pos_x}+{pos_y}")
        self.minsize(min(700, screen_w - 40), min(480, screen_h - 60))
        self.configure(bg=BRAND["bg"])
        self.transient(master)
        self.resizable(True, True)
        apply_window_icon(self)
        apply_brand_theme(self)

        self._widgets = {}
        self.var_hex_out = tk.BooleanVar(value=False)
        self.fw_filepath = tk.StringVar(value="")

        self.header = BrandHeader(self, subtitle="Configuration")
        self.header.pack(fill=tk.X, side=tk.TOP)
        sess_obj = self.engine.selected_session
        self.header.set_link(
            bool(self.engine.active_client and self.engine.active_client.is_connected),
            getattr(sess_obj, "name", "") or "",
        )

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))
        self._notebook = notebook

        self._build_device_tab(notebook)
        self._build_sim_tab(notebook)
        self._build_server_tab(notebook)
        self._build_system_tab(notebook)
        self._build_actions_tab(notebook)
        self._dota_tab = self._build_dota_tab(notebook)
        if initial_tab == "dota" and self._dota_tab is not None:
            notebook.select(self._dota_tab)

    def _field(self, parent, row, label, key, category, width=20):
        """Add one labelled entry at row of a tab and register it as (category, key) in self._widgets."""
        parent.grid_columnconfigure(0, weight=0)
        parent.grid_columnconfigure(1, weight=1)

        tk.Label(parent, text=label, bg=BRAND["surface"], fg=BRAND["text"],
                 font=BRAND["font_ui"], anchor="w").grid(row=row, column=0, sticky="ew",
                                                        padx=8, pady=3)
        ent = tk.Entry(parent, width=width, bg=BRAND["surface_alt"], fg=BRAND["text"],
                       insertbackground=BRAND["text"], font=BRAND["font_mono"],
                       relief=tk.FLAT, highlightthickness=1,
                       highlightbackground=BRAND["border"], highlightcolor=BRAND["accent"])
        ent.grid(row=row, column=1, sticky="ew", padx=8, pady=3)
        self._widgets[(category, key)] = ent
        return ent

    def _mk_tab(self, notebook, title):
        """Add a tab called title to notebook and return its frame."""
        frame = tk.Frame(notebook, bg=BRAND["surface"], padx=10, pady=10)
        notebook.add(frame, text=title)
        return frame

    def _accent_button(self, parent, text, command, colour=None, **pack_kw):
        """A tk.Button styled to match the brand (ttk buttons ignore bg on most themes)."""
        btn = tk.Button(parent, text=text, command=command,
                        bg=colour or BRAND["accent_dark"], fg=BRAND["accent_text"],
                        activebackground=BRAND["accent"], activeforeground=BRAND["accent_text"],
                        relief=tk.FLAT, font=BRAND["font_ui_b"], padx=14, pady=5,
                        cursor="hand2", bd=0)
        btn.pack(side=tk.LEFT, **pack_kw)
        return btn

    # -----------------------------
    # System & Diagnostics tab: the Valtrack status and action commands (doc 07), each with its opcode
    # -----------------------------
    def _build_system_tab(self, notebook):
        """System tab: firmware version, debug stream, motion / low power, LEDs, live position, log erase and
        factory reset. Motion, low power and log erase are partial 0xC1 writes (every other field 'keep').
        """
        f = self._mk_tab(notebook, "System & diagnostics")
        f.grid_columnconfigure(0, weight=1)

        def section(row, title, note=""):
            """Add a section title (and an optional grey note) at row of the system tab."""
            tk.Label(f, text=title, bg=BRAND["surface"], fg=BRAND["accent"],
                     font=BRAND["font_ui_b"]).grid(row=row, column=0, columnspan=2,
                                                   sticky="w", padx=6, pady=(12, 1))
            if note:
                tk.Label(f, text=note, bg=BRAND["surface"], fg=BRAND["text_muted"],
                         font=("Segoe UI", 8), justify=tk.LEFT, wraplength=760).grid(
                    row=row + 1, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 4))

        section(0, f"Firmware version  (0x{BLE_READ_FIRMWARE_VERSION:02X})  and device clock  (0x11)")
        bar = tk.Frame(f, bg=BRAND["surface"])
        bar.grid(row=2, column=0, columnspan=2, sticky="w", padx=6)
        self._accent_button(bar, "Read version", self._read_fw_version, BRAND["accent_dark"])
        self._accent_button(bar, "Read device clock", self._read_device_clock, BRAND["accent_dark"], padx=(6, 0))
        self.lbl_fw = tk.Label(f, text="-", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_mono_b"])
        self.lbl_fw.grid(row=3, column=0, sticky="w", padx=8, pady=(4, 0))

        section(4, "Live diagnostic stream  (0xFD)")
        bar2 = tk.Frame(f, bg=BRAND["surface"])
        bar2.grid(row=6, column=0, columnspan=2, sticky="w", padx=6)
        self._accent_button(bar2, "Stream ON", lambda: self._set_stream(True), BRAND["ok"])
        self._accent_button(bar2, "Stream OFF", lambda: self._set_stream(False), BRAND["err"], padx=(6, 0))

        section(7, "Motion & low power  (0xC1 fields 14, 15, 17)",
                "'(keep)' or an empty threshold leaves the device value unchanged. Low power only takes effect with "
                "motion on or a GSM interval of at least 10 minutes.")
        mrow = 9
        tk.Label(f, text="Motion detection", bg=BRAND["surface"], fg=BRAND["text"],
                 font=BRAND["font_ui"]).grid(row=mrow, column=0, sticky="w", padx=6)
        self.cmb_motion = ttk.Combobox(f, values=["(keep)", "Enabled", "Disabled"], state="readonly", width=14)
        self.cmb_motion.current(0)
        self.cmb_motion.grid(row=mrow, column=1, sticky="w", padx=6, pady=2)
        tk.Label(f, text="Threshold (1-127 %, 85 typical)", bg=BRAND["surface"], fg=BRAND["text"],
                 font=BRAND["font_ui"]).grid(row=mrow + 1, column=0, sticky="w", padx=6)
        self.ent_threshold = tk.Entry(f, width=16, bg=BRAND["surface_alt"], fg=BRAND["text"],
                                      insertbackground=BRAND["text"], font=BRAND["font_mono"], relief=tk.FLAT,
                                      highlightthickness=1, highlightbackground=BRAND["border"],
                                      highlightcolor=BRAND["accent"])
        self.ent_threshold.grid(row=mrow + 1, column=1, sticky="w", padx=6, pady=2)
        tk.Label(f, text="Low-power mode", bg=BRAND["surface"], fg=BRAND["text"],
                 font=BRAND["font_ui"]).grid(row=mrow + 2, column=0, sticky="w", padx=6)
        self.cmb_lowpower = ttk.Combobox(f, values=["(keep)", "Enabled", "Disabled"], state="readonly", width=14)
        self.cmb_lowpower.current(0)
        self.cmb_lowpower.grid(row=mrow + 2, column=1, sticky="w", padx=6, pady=2)
        bar3 = tk.Frame(f, bg=BRAND["surface"])
        bar3.grid(row=mrow + 3, column=0, columnspan=2, sticky="w", padx=6, pady=(6, 0))
        self._accent_button(bar3, "Apply motion / power", self._apply_motion, BRAND["accent_dark"])

        section(mrow + 4, "LEDs  (0xFC)  and live position  (0xE3)",
                "The LED flag is stored; in low power the LEDs stay off whatever it says. The live position is the "
                "last fix; the device starts a new one in the background.")
        bar4 = tk.Frame(f, bg=BRAND["surface"])
        bar4.grid(row=mrow + 6, column=0, columnspan=2, sticky="w", padx=6)
        self._accent_button(bar4, "LEDs ON", lambda: self._set_led(True), BRAND["ok"])
        self._accent_button(bar4, "LEDs OFF", lambda: self._set_led(False), BRAND["border"], padx=(6, 0))
        self._accent_button(bar4, "Read live position", self._read_live_position, BRAND["accent_dark"],
                            padx=(16, 0))
        self.lbl_position = tk.Label(f, text="-", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_mono_b"])
        self.lbl_position.grid(row=mrow + 7, column=0, columnspan=2, sticky="w", padx=8, pady=(4, 0))

        section(mrow + 8, "Stored data  (0xC1 field 16, 0xE6)",
                "DESTRUCTIVE. 'Erase logs' deletes every waypoint and event not yet sent. 'Factory reset' also "
                "restores the default configuration and clears the security token, then resets the device.")
        bar5 = tk.Frame(f, bg=BRAND["surface"])
        bar5.grid(row=mrow + 10, column=0, columnspan=2, sticky="w", padx=6, pady=(4, 0))
        self._accent_button(bar5, "Erase logs", self._erase_logs, BRAND["err"])
        self._accent_button(bar5, "Factory reset", self._factory_reset, BRAND["err"], padx=(6, 0))

    # -- System tab handlers ---------------------
    def _read_fw_version(self):
        """Read and show the firmware version."""
        async def go():
            """Ask the device for its firmware version and show it."""
            ver = await self.engine.active_device.read_firmware_version()
            text = f"Firmware {ver}" if ver else "no answer to 0xE5 / 0xD0"
            self.after(0, lambda: self.lbl_fw.config(text=text, fg=BRAND["ok"] if ver else BRAND["warn"]))
            log_system(f"Firmware version: {text}", BRAND["ok"] if ver else BRAND["warn"])
        self._run(go)

    def _read_device_clock(self):
        """Read the device RTC and show it next to the PC's UTC time."""
        async def go():
            """Read 0x11 and compare with the PC clock."""
            epoch = await self.engine.active_device.read_device_time()
            if not epoch:
                self.after(0, lambda: self.lbl_fw.config(text="no answer to 0x11", fg=BRAND["warn"]))
                return
            offset = int(epoch - time.time())
            device_text = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            text = f"Device clock {device_text} (PC {offset:+d} s)"
            colour = BRAND["ok"] if abs(offset) <= CLOCK_TOLERANCE_S else BRAND["warn"]
            self.after(0, lambda: self.lbl_fw.config(text=text, fg=colour))
            log_system(text, colour)
        self._run(go)

    def _set_stream(self, enable):
        """Switch the device's debug-log stream on or off."""
        async def go():
            """Send the debug-log command and log whether the device accepted it."""
            ok = await self.engine.active_device.set_debug_log(enable)
            log_system(f"Debug stream {'ON' if enable else 'OFF'} - {'OK' if ok else 'FAILED'}",
                       BRAND["ok"] if ok else BRAND["err"])
        self._run(go)

    def _apply_motion(self):
        """Send motion / threshold / low-power as a partial 0xC1 write ('(keep)' or empty = unchanged)."""
        motion = self.cmb_motion.get()
        motion_enable = None if motion.startswith("(keep)") else motion == "Enabled"
        lp = self.cmb_lowpower.get()
        low_power = None if lp.startswith("(keep)") else lp == "Enabled"
        try:
            threshold = parse_number(self.ent_threshold.get(), self._field_specs["motion_thresh"])
        except ConfigValueError as error:
            messagebox.showerror("Invalid threshold", str(error), parent=self)
            return

        async def go():
            """Send the partial 0xC1 write, then refresh the device tab and the live panel."""
            ok = await self.engine.active_device.set_motion_lowpower(
                motion_enable=motion_enable, threshold=threshold, low_power=low_power)
            log_system(f"Motion / low power - {'OK' if ok else 'REFUSED'}", BRAND["ok"] if ok else BRAND["err"])
            await self._refresh_device_config()
        self._run(go)

    def _set_led(self, enable):
        """Store the 'all LEDs' flag."""
        async def go():
            """Send 0xFC and log the flag the device reports back."""
            flag = await self.engine.active_device.set_led(enable)
            if flag is None:
                log_system("LED flag: no answer to 0xFC", BRAND["warn"])
            else:
                log_system(f"LED flag is now {'ON' if flag else 'OFF'}", BRAND["ok"])
                post_gui("device_config", session_id=self.session_id, config={"led_all_flag": int(flag)})
        self._run(go)

    def _read_live_position(self):
        """Show the device's last fix (0xE3)."""
        async def go():
            """Read 0xE3 and show latitude / longitude."""
            pos = await self.engine.active_device.read_live_position()
            if pos is None:
                text, colour = "no answer to 0xE3", BRAND["warn"]
            elif not pos["valid"]:
                text, colour = "no valid fix yet (a new fix was requested)", BRAND["warn"]
            else:
                text, colour = f"lat {pos['latitude']:.6f}, lon {pos['longitude']:.6f}", BRAND["ok"]
            self.after(0, lambda: self.lbl_position.config(text=text, fg=colour))
            log_system(f"Live position: {text}", colour)
        self._run(go)

    def _erase_logs(self):
        """Ask for confirmation, then ask the device to erase its waypoint and event logs."""
        if not messagebox.askyesno("Erase logs", "Erase every stored waypoint and event?\n\n"
                                   "Data not yet sent to the server is lost.", parent=self):
            return

        async def go():
            """Send the erase flag (0xC1 byte 16)."""
            ok = await self.engine.active_device.erase_logs()
            log_system(f"Erase logs - {'accepted (runs in the background)' if ok else 'FAILED'}",
                       BRAND["ok"] if ok else BRAND["err"])
        self._run(go)

    def _factory_reset(self):
        """Ask for confirmation, then send the factory reset."""
        if not messagebox.askyesno("Factory reset", "Restore the default configuration, erase all logs and the "
                                   "security token, and reset the device?", parent=self):
            return

        async def go():
            """Send 0xE6."""
            ok = await self.engine.active_device.factory_reset()
            log_system(f"Factory reset - {'sent' if ok else 'FAILED'}", BRAND["ok"] if ok else BRAND["err"])
        self._run(go)

    def _build_device_tab(self, notebook):
        """Device configuration tab: one row per VALTRACK_C1_FIELDS field (entry, accepted range, read value in
        words), Read / Write buttons. Intervals accept plain seconds or 15m / 2h / 1d.
        """
        f = self._mk_tab(notebook, "Config 0xC1")
        f.grid_columnconfigure(2, weight=1)
        self._field_specs = {spec.key: spec for spec in VALTRACK_C1_FIELDS}
        self._device_notes = {}
        self._last_device_cfg = {}
        for row, spec in enumerate(VALTRACK_C1_FIELDS):
            label = f"{spec.label} ({spec.unit})" if spec.unit else spec.label
            self._field(f, row, label, spec.key, "device", width=14)
            note = tk.Label(f, text=spec.range_text(), bg=BRAND["surface"], fg=BRAND["text_muted"],
                            font=("Segoe UI", 8), anchor="w")
            note.grid(row=row, column=2, sticky="w", padx=6)
            self._device_notes[spec.key] = note
            self._attach_tooltip(self._widgets[("device", spec.key)], spec.hint)

        btn_bar = tk.Frame(f, bg=BRAND["surface"])
        btn_bar.grid(row=len(VALTRACK_C1_FIELDS), column=0, columnspan=3, pady=12, sticky="ew", padx=6)
        tk.Button(btn_bar, text="Read", command=self._read_device, bg=BRAND["accent_dark"], fg="white",
                  relief=tk.FLAT, padx=12).pack(side=tk.LEFT)
        tk.Button(btn_bar, text="Write changed fields", command=self._write_device, bg=BRAND["ok"], fg="white",
                  relief=tk.FLAT, padx=12).pack(side=tk.LEFT, padx=6)
        tk.Checkbutton(btn_bar, text="Show hex (0x..)", variable=self.var_hex_out, bg=BRAND["surface"],
                       fg=BRAND["text"], selectcolor=BRAND["accent_dark"]).pack(side=tk.LEFT, padx=(20, 4))
        self.lbl_device_status = tk.Label(f, text="Read the configuration first; only fields you change are written.",
                                          bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 8),
                                          justify=tk.LEFT, wraplength=760, anchor="w")
        self.lbl_device_status.grid(row=len(VALTRACK_C1_FIELDS) + 1, column=0, columnspan=3, sticky="w", padx=6)

    def _attach_tooltip(self, widget, text):
        """Show text in the status line while the pointer is over widget."""
        if not text:
            return
        widget.bind("<Enter>", lambda _e: self.lbl_device_status.config(text=text, fg=BRAND["text_dim"])
                    if hasattr(self, "lbl_device_status") else None)

    def _build_sim_tab(self, notebook):
        """SIM tab: APN, user name, password, Read/Write buttons."""
        f = self._mk_tab(notebook, "SIM / APN 0xC3")
        self._field(f, 0, "APN", "apn", "sim", width=36)
        self._field(f, 1, "Username", "username", "sim", width=36)
        self._field(f, 2, "Password", "password", "sim", width=36)
        self._field(f, 3, "Network Mode (2/13/14/38)", "network_mode", "sim", width=36)

        btn_bar = tk.Frame(f, bg=BRAND["surface"])
        btn_bar.grid(row=4, column=0, columnspan=2, pady=12, sticky="ew", padx=6)
        tk.Button(btn_bar, text="Read", command=self._read_sim, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=12).pack(side=tk.LEFT)
        tk.Button(btn_bar, text="Write SIM / Network", command=self._write_sim, bg=BRAND["ok"], fg="white", relief=tk.FLAT, padx=12).pack(side=tk.LEFT, padx=6)

    def _build_server_tab(self, notebook):
        """Server tab: host, port, TCP keep-alive, Read/Write buttons."""
        f = self._mk_tab(notebook, "Server 0xC4")
        self._field(f, 0, "Host / IP", "host", "server", width=36)
        self._field(f, 1, "Port", "port", "server")
        self._field(f, 2, "TCP Keepalive (min)", "keepalive", "server")

        btn_bar = tk.Frame(f, bg=BRAND["surface"])
        btn_bar.grid(row=3, column=0, columnspan=2, pady=12, sticky="ew", padx=6)
        tk.Button(btn_bar, text="Read", command=self._read_server, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=12).pack(side=tk.LEFT)
        tk.Button(btn_bar, text="Write", command=self._write_server, bg=BRAND["ok"], fg="white", relief=tk.FLAT, padx=12).pack(side=tk.LEFT, padx=6)

    def _build_actions_tab(self, notebook):
        """Actions tab: device reset, identity/time queries, and token validation."""
        f = self._mk_tab(notebook, "Actions")
        f.grid_columnconfigure(0, weight=1)

        tk.Label(f, text="Device Reset (0xC5)", bg=BRAND["surface"], fg=BRAND["err"], font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=6, pady=(12, 2))
        tk.Button(f, text="Reset Device", command=self._reset_device, bg=BRAND["err"], fg="white", relief=tk.FLAT, padx=14, pady=3).grid(row=1, column=0, sticky="w", padx=6)

        tk.Label(f, text="Token Validation (0xE2) - Hex string (112 bytes)", bg=BRAND["surface"], fg=BRAND["text"], font=("Segoe UI", 10, "bold")).grid(row=2, column=0, sticky="w", padx=6, pady=(20, 2))
        self.token_entry = tk.Entry(f, width=70, bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground="white", font=("Consolas", 9))
        self.token_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=6)
        tk.Button(f, text="Validate Token", command=self._validate_token, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=14, pady=3).grid(row=4, column=0, sticky="w", padx=6, pady=4)

        tk.Label(f, text="Identity, Battery & Time (0xE1 / 0xE4 / 0x11)", bg=BRAND["surface"], fg=BRAND["info"], font=("Segoe UI", 10, "bold")).grid(row=5, column=0, sticky="w", padx=6, pady=(20, 4))
        util_bar = tk.Frame(f, bg=BRAND["surface"])
        util_bar.grid(row=6, column=0, columnspan=2, sticky="w", padx=6)
        tk.Button(util_bar, text="Read IMEI (0xE1)", command=self._read_imei, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT)
        tk.Button(util_bar, text="Read Battery (0xE4)", command=self._read_battery, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT, padx=6)
        tk.Button(util_bar, text="Sync PC UTC Time (0x11)", command=self._sync_pc_time, bg=BRAND["ok"], fg="white", relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT)
        self.lbl_action_info = tk.Label(f, text="-", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_mono_b"])
        self.lbl_action_info.grid(row=7, column=0, columnspan=2, sticky="w", padx=6, pady=(6, 0))

    def _read_imei(self):
        async def go():
            try:
                imei = await self.engine.active_device.read_imei()
                txt = f"IMEI: {imei}" if imei else "IMEI: no response"
                if imei:
                    post_gui("device_imei", session_id=self.session_id, imei=imei)
                self.after(0, lambda t=txt: self.lbl_action_info.config(text=t, fg=BRAND["ok"] if imei else BRAND["warn"]))
                log_system(txt, BRAND["ok"] if imei else BRAND["warn"], session_id=self.session_id)
            except Exception as e:
                log_system(f"Read IMEI failed: {e}", BRAND["err"], session_id=self.session_id)
        self._run(go)

    def _read_battery(self):
        async def go():
            try:
                b = await self.engine.active_device.read_battery()
                txt = f"Battery: {b['percent']}% ({b['voltage_mv']} mV)" if b else "Battery: no response"
                self.after(0, lambda t=txt: self.lbl_action_info.config(text=t, fg=BRAND["ok"] if b else BRAND["warn"]))
                log_system(txt, BRAND["ok"] if b else BRAND["warn"])
            except Exception as e:
                log_system(f"Read battery failed: {e}", BRAND["err"])
        self._run(go)

    def _sync_pc_time(self):
        import time as _t
        epoch = int(_t.time())
        async def go():
            try:
                ok = await self.engine.active_device.set_device_time(epoch)
                txt = f"UTC Time Sync ({epoch}): {'OK' if ok else 'FAILED / NO ACK'}"
                self.after(0, lambda t=txt: self.lbl_action_info.config(text=t, fg=BRAND["ok"] if ok else BRAND["warn"]))
                log_system(txt, BRAND["ok"] if ok else BRAND["warn"])
            except Exception as e:
                log_system(f"Time sync failed: {e}", BRAND["err"])
        self._run(go)

    def _build_dota_tab(self, notebook):
        """Firmware update tab: file chooser, start button, progress bar and status line."""
        f = self._mk_tab(notebook, "DOTA update")
        f.grid_columnconfigure(0, weight=1)

        tk.Label(f, text="BLE Over-The-Air (DOTA) Firmware Flashing", bg=BRAND["surface"], fg=BRAND["info"], font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=6, pady=(8, 4))
        tk.Label(f, text="Select compiled firmware DFU binary (.bin) to flash via STM_OTA bootloader.", bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", padx=6, pady=(0, 10))

        file_frame = tk.Frame(f, bg=BRAND["surface"])
        file_frame.grid(row=2, column=0, sticky="ew", padx=6, pady=4)
        file_frame.grid_columnconfigure(0, weight=1)

        self.ent_fw = tk.Entry(file_frame, textvariable=self.fw_filepath, width=54, bg=BRAND["surface_alt"], fg=BRAND["text"], font=("Consolas", 9), insertbackground="white")
        self.ent_fw.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        tk.Button(file_frame, text="Browse .bin", command=self._browse_fw, bg=BRAND["surface_alt"], fg="white", relief=tk.FLAT, padx=10).grid(row=0, column=1)

        self.dota_progress_lbl = tk.Label(f, text="Status: Ready to start DOTA", bg=BRAND["surface"], fg=BRAND["warn"], font=("Consolas", 9, "bold"))
        self.dota_progress_lbl.grid(row=3, column=0, sticky="w", padx=6, pady=(16, 4))

        self.dota_bar = ttk.Progressbar(f, orient="horizontal", length=500, mode="determinate")
        self.dota_bar.grid(row=4, column=0, sticky="ew", padx=6, pady=4)

        btn_dota_frame = tk.Frame(f, bg=BRAND["surface"])
        btn_dota_frame.grid(row=5, column=0, sticky="w", padx=6, pady=16)
        tk.Button(btn_dota_frame, text="Start DOTA Flash Sequence", command=self._start_dota_flash, bg=BRAND["ok"], fg="white", font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=14, pady=6).pack(side=tk.LEFT)
        return f

    def _browse_fw(self):
        """Let the user pick the firmware .bin file."""
        filename = filedialog.askopenfilename(title="Select Firmware Binary", filetypes=[("Binary Files", "*.bin"), ("All Files", "*.*")])
        if filename:
            self.fw_filepath.set(filename)

    def _start_dota_flash(self):
        """Check that a file is chosen, ask for confirmation, then run the update sequence on the BLE thread.
        Works both when connected to the application and when the device is already in STM_OTA bootloader mode.
        """
        path = self.fw_filepath.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("File Error", "Please select a valid firmware (.bin) file.")
            return

        app_connected = bool(self.engine.active_client and self.engine.active_client.is_connected)
        if app_connected:
            prompt = "This will trigger the bootloader and flash the selected binary over BLE. Continue?"
        else:
            prompt = (
                "No active application connection.\n\n"
                "Scan for a device in STM_OTA bootloader mode and flash the selected binary over BLE?"
            )
        if not messagebox.askyesno("Confirm DOTA Flash", prompt):
            return

        run_async(self._execute_dota_sequence(path), session_id=self.session_id)

    async def _execute_dota_sequence(self, bin_path: str):
        """BLE firmware update (DOTA) of the connected or bootloader-mode device.

        Flow:
          1. Pause auto-reconnect for this device (the link drops on purpose), read the file,
             check the image (check_image) and that its sector count fits the region.
          2. When connected to the application, write the reboot request (BLE update mode, start
             sector, sector count) to the reboot characteristic and disconnect the old GATT client
             so Windows WinRT releases the handle before STM_OTA starts advertising. When the
             device is already in STM_OTA mode, skip directly to scanning for STM_OTA.
          3. Up to 3 attempts (covering the bootloader's one-shot reset if an earlier interrupted
             upload left flash non-blank):
               - Scan for STM_OTA (preferring the target board's MAC / BD_ADDR[0]+1 peer).
               - Connect, subscribe to confirmation indications, send 'start application upload',
                 stream the image in 8-byte-aligned chunks (15 ms pacing), send 'upload finished',
                 wait for confirmation (0x01) and wait for the bootloader link to disconnect on reset.
          4. Scan (up to 7 attempts) until the application advertises again; still STM_OTA
             means the image was not accepted.
          5. Report the result and let auto-reconnect resume.
        """
        session = self.engine.selected_session
        try:
            # Pause this device's auto-reconnect: after the reboot request the app link drops, and
            # a reconnect attempt would fight the OTA client for the radio / the same address.
            if session is not None:
                session.ota_active = True
            with open(bin_path, "rb") as f:
                image = f.read()

            if check_image(image) != STATUS_OK:
                raise RuntimeError("Firmware image validation failed (check vector table/end tag).")

            file_size = len(image)
            sector_count = math.ceil(file_size / FLASH_SECTOR_BYTES)
            if not 1 <= sector_count <= APP_SECTOR_COUNT_MAX:
                raise RuntimeError(
                    f"Firmware needs {sector_count} sectors; the supported range is "
                    f"1..{APP_SECTOR_COUNT_MAX}."
                )

            app_addr = (
                (getattr(session, "address", "") or "")
                or self.target_address
                or (self.session_id or "")
            ).lower().strip()
            peer_addrs = _related_ble_addresses(app_addr)

            # Step 1: If connected to the application, send Reboot Request to REBOOT_CHAR_UUID
            # (0x01 = DOTA, 0x08 = Sector 8, Count) and close the old GATT connection cleanly.
            app_client = self.engine.active_client
            if app_client and app_client.is_connected:
                self.after(0, lambda: self.dota_progress_lbl.config(
                    text=f"Status: Triggering bootloader (Size: {file_size} bytes, {sector_count} sectors)..."
                ))
                payload = bytes([REBOOT_MODE_BLE_OTA, APP_START_SECTOR, sector_count])
                log_system(f"Sending DOTA reboot packet: {payload.hex()}...", "#38bdf8",
                           session_id=self.session_id)

                # REBOOT_CHAR_UUID (0000fe11-...) is CHAR_PROP_WRITE_WITHOUT_RESP on Valtrack_App,
                # so try response=False first and fall back to response=True only if needed.
                try:
                    await app_client.write_gatt_char(REBOOT_CHAR_UUID, payload, response=False)
                except Exception as no_resp_error:
                    try:
                        await app_client.write_gatt_char(REBOOT_CHAR_UUID, payload, response=True)
                    except Exception as command_error:
                        if app_client.is_connected:
                            raise RuntimeError(
                                f"Could not send the BLE OTA reboot request: {command_error}"
                            ) from no_resp_error

                # Explicitly disconnect the application GATT client so Windows WinRT does not keep
                # the old link open while STM_OTA boots and erases flash.
                try:
                    await app_client.disconnect()
                except Exception:
                    pass

                log_system("BLE OTA reboot request accepted; waiting for STM_OTA...", "#10b981",
                           session_id=self.session_id)
                self.after(0, lambda: self.dota_progress_lbl.config(
                    text="Status: Waiting for STM_OTA bootloader advertisement..."
                ))
                await asyncio.sleep(2.5)
            else:
                log_system("No active app link; scanning directly for STM_OTA bootloader...",
                           "#38bdf8", session_id=self.session_id)
                self.after(0, lambda: self.dota_progress_lbl.config(
                    text="Status: Scanning directly for STM_OTA bootloader..."
                ))

            # Step 2 & 3: Scan for STM_OTA, connect, and stream the image (up to 3 attempts so
            # the loader's one-shot dirty-flash reset or a transient BLE drop is automatically retried).
            ota_device = None
            upload_completed = False
            max_ota_attempts = 3

            for ota_attempt in range(1, max_ota_attempts + 1):
                ota_device = None
                for scan_attempt in range(1, 4):
                    self.after(0, lambda a=scan_attempt, oa=ota_attempt: self.dota_progress_lbl.config(
                        text=f"Status: Scanning for STM_OTA (Scan {a}/3, Attempt {oa}/{max_ota_attempts})..."
                    ))
                    discovered = await BleakScanner.discover(timeout=5.0, return_adv=True)
                    matches = []
                    for _, (dev, adv) in discovered.items():
                        name = (adv.local_name or dev.name or "").lower()
                        uuids = [u.lower() for u in (adv.service_uuids or [])]
                        if name.startswith("stm_ota") or STM_OTA_SERVICE_UUID in uuids:
                            matches.append(dev)
                    ota_device = next(
                        (d for d in matches if d.address.lower() in peer_addrs),
                        matches[0] if matches else None,
                    )
                    if ota_device:
                        break
                    await asyncio.sleep(1.5)

                if not ota_device:
                    if ota_attempt == max_ota_attempts:
                        raise RuntimeError(
                            "Device did not enter STM_OTA mode (No 'STM_OTA' advertisement found)."
                        )
                    continue

                peer_addrs |= _related_ble_addresses(ota_device.address)
                self.after(0, lambda addr=ota_device.address: self.dota_progress_lbl.config(
                    text=f"Status: Found bootloader {addr}, connecting..."
                ))
                log_system(f"Bootloader found: {ota_device.address} (attempt {ota_attempt}/{max_ota_attempts})",
                           BRAND["ok"], session_id=self.session_id)

                ota_disconnected = asyncio.Event()
                confirmed = asyncio.Event()
                confirmation: list[int] = []

                def _on_ota_disconnect(_client):
                    ota_disconnected.set()

                def _on_ota_notify(_sender, raw: bytearray):
                    """Loader confirmation notification: keep the bytes, wake the waiter."""
                    confirmation.extend(bytes(raw))
                    confirmed.set()

                try:
                    async with BleakClient(
                        ota_device, timeout=15.0, disconnected_callback=_on_ota_disconnect
                    ) as ota_client:
                        if not ota_client.is_connected:
                            raise RuntimeError("Failed to connect to STM_OTA bootloader service.")

                        await ota_client.start_notify(STM_OTA_CONF_CHAR, _on_ota_notify)

                        # Send start command (Base Address 0x08008000)
                        start_cmd = bytes([COMMAND_APPLICATION_UPLOAD]) + APP_BASE_ADDRESS_FIELD
                        await ota_client.write_gatt_char(STM_OTA_CTRL_CHAR, start_cmd, response=False)
                        await asyncio.sleep(0.3)

                        # If flash was not blank from an earlier interrupted attempt, Valtrack_Boot
                        # resets itself once here to erase all application sectors at boot.
                        if ota_disconnected.is_set() or not ota_client.is_connected:
                            log_system(
                                "Bootloader restarted to erase flash; reconnecting to STM_OTA...",
                                BRAND["warn"], session_id=self.session_id,
                            )
                            await asyncio.sleep(2.5)
                            continue

                        # Determine chunk size aligned to double words (8 bytes)
                        chunk_bytes = min(RAW_DATA_MAX_BYTES, max(ota_client.mtu_size - ATT_HEADER_BYTES, 20))
                        chunk_bytes = max(chunk_bytes - chunk_bytes % DOUBLE_WORD_BYTES, DOUBLE_WORD_BYTES)

                        total_chunks = math.ceil(file_size / chunk_bytes)
                        self.after(0, lambda tc=total_chunks: self.dota_bar.config(maximum=tc, value=0))

                        link_dropped = False
                        for packet_index in range(total_chunks):
                            if ota_disconnected.is_set() or not ota_client.is_connected:
                                link_dropped = True
                                break

                            chunk = image[packet_index * chunk_bytes:(packet_index + 1) * chunk_bytes]
                            if len(chunk) % DOUBLE_WORD_BYTES != 0:
                                chunk = chunk + b"\xFF" * (DOUBLE_WORD_BYTES - (len(chunk) % DOUBLE_WORD_BYTES))

                            await ota_client.write_gatt_char(STM_OTA_DATA_CHAR, chunk, response=False)
                            await asyncio.sleep(0.015)  # 15ms pacing delay for inline flash programming

                            current_chunk = packet_index + 1
                            pct = int((current_chunk / total_chunks) * 100)
                            self.after(0, lambda c=current_chunk, p=pct, tc=total_chunks: (
                                self.dota_bar.config(value=c),
                                self.dota_progress_lbl.config(text=f"Status: Flashing chunk {c}/{tc} ({p}%)")
                            ))

                        if link_dropped or ota_disconnected.is_set():
                            log_system(
                                f"BLE link dropped during upload (attempt {ota_attempt}/{max_ota_attempts}); retrying...",
                                BRAND["warn"], session_id=self.session_id,
                            )
                            await asyncio.sleep(2.0)
                            continue

                        # Allow the last data packet to finish programming before sending finish_cmd
                        await asyncio.sleep(0.1)

                        # Send upload finished command
                        finish_cmd = bytes([COMMAND_UPLOAD_FINISHED, 0x00, 0x00, 0x00])
                        confirmed.clear()
                        await ota_client.write_gatt_char(STM_OTA_CTRL_CHAR, finish_cmd, response=False)

                        try:
                            await asyncio.wait_for(confirmed.wait(), timeout=10.0)
                        except asyncio.TimeoutError:
                            log_system(
                                f"Finish indication timeout on attempt {ota_attempt}/{max_ota_attempts}.",
                                BRAND["warn"], session_id=self.session_id,
                            )
                            if ota_attempt < max_ota_attempts:
                                await asyncio.sleep(2.0)
                                continue
                        else:
                            if bytes(confirmation) != bytes([CONFIRMATION_REBOOT]):
                                raise RuntimeError(
                                    f"Bootloader rejected upload (confirmation: {bytes(confirmation).hex() or 'empty'})."
                                )
                            log_system(
                                "Finish indication (0x01) received; waiting for bootloader to reset into application...",
                                BRAND["ok"], session_id=self.session_id,
                            )
                            try:
                                await asyncio.wait_for(ota_disconnected.wait(), timeout=10.0)
                            except asyncio.TimeoutError:
                                pass
                            upload_completed = True
                            break
                except Exception as ota_err:
                    if ota_attempt == max_ota_attempts:
                        raise
                    log_system(
                        f"OTA attempt {ota_attempt}/{max_ota_attempts} interrupted ({ota_err}); retrying...",
                        BRAND["warn"], session_id=self.session_id,
                    )
                    await asyncio.sleep(2.0)

            # Give the MCU and Windows BLE stack time to reset and clean up handles
            await asyncio.sleep(2.5)

            self.after(0, lambda: self.dota_progress_lbl.config(
                text="Status: Waiting for application to start..."
            ))
            app_verified = False
            device_still_in_loader = False
            expected_name = (getattr(session, "name", "") or "").lower()
            if expected_name.startswith("stm_ota"):
                expected_name = ""

            for attempt in range(1, 8):
                self.after(0, lambda a=attempt: self.dota_progress_lbl.config(
                    text=f"Status: Checking application startup (Attempt {a}/7)..."
                ))
                discovered = await BleakScanner.discover(timeout=5.0, return_adv=True)
                candidate = None
                candidate_adv = None
                device_still_in_loader = False

                for dev, adv in discovered.values():
                    dev_addr = dev.address.lower()
                    dev_name = (adv.local_name or dev.name or "").lower()
                    uuids = [str(u).lower() for u in (adv.service_uuids or [])]

                    is_loader = dev_name.startswith("stm_ota") or (STM_OTA_SERVICE_UUID.lower() in uuids)
                    addr_match = dev_addr in peer_addrs
                    name_match = bool(expected_name and expected_name in dev_name) or ("succorfish" in dev_name) or ("gigo" in dev_name)

                    if addr_match or (not peer_addrs and name_match):
                        if is_loader:
                            device_still_in_loader = True
                            log_system(f"Device still advertising as STM_OTA (attempt {attempt}/7)",
                                       BRAND["warn"], session_id=self.session_id)
                        else:
                            candidate = dev
                            candidate_adv = adv
                            log_system(f"Application advertisement detected: '{adv.local_name or dev.name}' ({dev.address})",
                                       BRAND["ok"], session_id=self.session_id)
                            break

                if candidate is not None:
                    app_verified = True
                    if candidate_adv is not None:
                        self.engine._engine.discovered_devices[candidate.address] = (candidate, candidate_adv)
                    try:
                        async with BleakClient(candidate, timeout=6.0) as verify_client:
                            if any(
                                str(characteristic.uuid).lower() in COMMAND_CHAR_UUIDS
                                for service in verify_client.services
                                for characteristic in service.characteristics
                            ):
                                log_system("Application GATT service successfully verified.",
                                           BRAND["ok"], session_id=self.session_id)
                    except Exception as exc:
                        log_system(f"Application connect check ({exc}); advertisement confirmed.",
                                   BRAND["info"], session_id=self.session_id)
                    break

                await asyncio.sleep(1.5)

            if not app_verified:
                if device_still_in_loader:
                    raise RuntimeError(
                        "Upload ended but the device is still running the STM_OTA bootloader. "
                        "Check the image end tag, SFSA boundary, or reset the device."
                    )
                if not upload_completed:
                    raise RuntimeError("BLE OTA upload did not receive reboot confirmation from STM_OTA.")
                raise RuntimeError(
                    "Upload finished (100%), but could not verify application GATT service responding. "
                    "Please power cycle or reset the board."
                )
            else:
                self.after(0, lambda: self.dota_progress_lbl.config(
                    text="Status: Application verified; DOTA completed successfully!"
                ))
                self.after(0, lambda: messagebox.showinfo(
                    "DOTA Success",
                    "Firmware upload completed and the application GATT service is responding."
                ))

        except Exception as e:
            log_system(f"DOTA Flash failed: {e}", "#ef4444", session_id=self.session_id)
            self.after(0, lambda err=str(e): self.dota_progress_lbl.config(text=f"Status: Failed - {err}"))
            self.after(0, lambda err=str(e): messagebox.showerror(
                "DOTA Error", f"Firmware flashing failed:\n{err}"))
        finally:
            # Let the normal auto-reconnect resume for this device.
            if session is not None:
                session.ota_active = False

    def _run(self, coro_fn):
        """Run coro_fn() on the BLE thread for this window's device; warn when it is not connected."""
        if not self.engine.active_device or not self.engine.active_client or not self.engine.active_client.is_connected:
            messagebox.showwarning("Not connected", "Connect to a device first.")
            return
        run_async(coro_fn(), session_id=self.session_id)

    def _read_device(self):
        """Read the device configuration into the device tab (and the live panel of the main window)."""
        async def go():
            """Read the configuration and fill the entries."""
            await self._refresh_device_config()
        self._run(go)

    async def _refresh_device_config(self):
        """Read 0xC1, show it in the device tab and post it to the main window's live panel."""
        try:
            cfg = await self.engine.active_device.read_device_config()
        except Exception as error:
            log_system(f"Read device config failed: {error}", BRAND["err"])
            return None
        self._last_device_cfg = dict(cfg)
        self.after(0, lambda: self._fill_device(cfg))
        post_gui("device_config", session_id=self.session_id, config=cfg)
        if not cfg.get("extended"):
            log_system("Device config read (20-byte layout: this firmware has no 32-bit interval fields, intervals "
                       "above 65534 s show as '--').", BRAND["warn"])
        else:
            log_system("Device config read.", BRAND["ok"])
        return cfg

    def _fill_device(self, cfg):
        """Show a read configuration ('--' = not set or not known) with intervals also in words."""
        for spec in VALTRACK_C1_FIELDS:
            ent = self._widgets[("device", spec.key)]
            ent.delete(0, tk.END)
            val = cfg.get(spec.key)
            if val is None:
                ent.insert(0, "--")
            elif self.var_hex_out.get():
                ent.insert(0, f"0x{val:X}")
            else:
                ent.insert(0, str(val))
            note = spec.range_text()
            if val is not None and spec.unit == "s":
                note = f"= {format_seconds(val)}   ({note})"
            elif val is not None and spec.unit == "min":
                note = f"= {format_seconds(val * 60)}   ({note})"
            self._device_notes[spec.key].config(text=note)
        self.lbl_device_status.config(text="Configuration read. Change fields and press 'Write changed fields'.",
                                      fg=BRAND["text_dim"])

    def _collect_device_changes(self):
        """Fields whose entry differs from the last read ({} when nothing changed). Raises ConfigValueError."""
        changes = {}
        for spec in VALTRACK_C1_FIELDS:
            value = parse_number(self._widgets[("device", spec.key)].get(), spec)
            if value is None:
                continue
            if self._last_device_cfg and self._last_device_cfg.get(spec.key) == value:
                continue
            changes[spec.key] = value
        return changes

    def _write_device(self):
        """Validate the changed fields (range per field) and write them; verify by reading back.

        A changed GSM or GPS interval makes the firmware restart 250 ms after its reply, so then the read-back is
        skipped: the values are posted to the live panel and re-read when the link comes back.
        """
        try:
            changes = self._collect_device_changes()
        except ConfigValueError as error:
            messagebox.showerror("Invalid value", str(error), parent=self)
            return
        if not changes:
            self.lbl_device_status.config(text="Nothing changed since the last read.", fg=BRAND["warn"])
            return
        restarts = any(key in changes for key in ("gsm_interval", "gps_interval"))
        summary = ", ".join(f"{self._field_specs[key].label} = {value}" for key, value in changes.items())

        async def go():
            """Write, then verify (or announce the restart) and update the live panel."""
            try:
                if restarts:
                    acked = await self.engine.active_device.write_device_config(changes)
                    read_back, mismatches = {}, []
                else:
                    acked, read_back, mismatches = await self.engine.active_device.write_and_verify_device_config(
                        changes)
            except ConfigValueError as error:
                message = str(error)
                self.after(0, lambda: messagebox.showerror("Invalid value", message, parent=self))
                return
            except Exception as error:
                log_system(f"Write device config failed: {error}", BRAND["err"])
                return
            if not acked:
                text, colour = f"Device REFUSED the write ({summary}); nothing was guaranteed applied.", BRAND["err"]
            elif mismatches:
                details = "; ".join(f"{key}: sent {sent}, device has {got}" for key, sent, got in mismatches)
                text, colour = f"Written but not applied: {details}. Older firmware cannot take intervals above " \
                               f"65534 s over BLE.", BRAND["err"]
            elif restarts:
                text, colour = (f"Written ({summary}). The device restarts now to apply the new interval; the "
                                f"configuration is read again when it reconnects.", BRAND["ok"])
                merged = dict(self._last_device_cfg)
                merged.update(changes)
                self._last_device_cfg = merged
                post_gui("device_config", session_id=self.session_id, config=merged)
            else:
                text, colour = f"Written and verified ({summary}).", BRAND["ok"]
                self._last_device_cfg = dict(read_back)
                self.after(0, lambda: self._fill_device(read_back))
                post_gui("device_config", session_id=self.session_id, config=read_back)
            log_system(f"Write device config: {text}", colour)
            self.after(0, lambda: self.lbl_device_status.config(text=text, fg=colour))
        self._run(go)

    def _read_sim(self):
        """Read the SIM settings into the SIM tab."""
        async def go():
            """Read the SIM settings and fill the entries."""
            try:
                cfg = await self.engine.active_device.read_sim_config()
                self.after(0, lambda: self._fill_sim(cfg))
                log_system("SIM config read successfully.", "#10b981")
            except Exception as e:
                log_system(f"Read SIM failed: {e}", "#ef4444")
        self._run(go)

    def _fill_sim(self, cfg):
        """Show the read SIM settings ('--' when unset)."""
        for key in ("apn", "username", "password", "network_mode"):
            ent = self._widgets[("sim", key)]
            ent.delete(0, tk.END)
            val = cfg.get(key)
            ent.insert(0, str(val) if val is not None else "--")

    def _write_sim(self):
        """Write the SIM settings (empty entries are left unchanged on the device)."""
        apn = self._widgets[("sim", "apn")].get().strip() or None
        username = self._widgets[("sim", "username")].get().strip() or None
        password = self._widgets[("sim", "password")].get().strip() or None
        mode_text = self._widgets[("sim", "network_mode")].get().strip()
        network_mode = None
        if mode_text and mode_text != "--":
            try:
                network_mode = int(mode_text, 0)
            except ValueError:
                log_system(f"Network mode '{mode_text}' is not a number (2, 13, 14 or 38)", "#ef4444")
                return
        async def go():
            """Write the SIM settings and log the result."""
            try:
                ok = await self.engine.active_device.write_sim_config(apn=apn, username=username, password=password,
                                                                     network_mode=network_mode)
                log_system(f"Write SIM config - {'OK' if ok else 'FAILED'}", "#10b981" if ok else "#ef4444")
            except Exception as e:
                log_system(f"Write SIM failed: {e}", "#ef4444")
        self._run(go)

    def _read_server(self):
        """Read the server settings into the server tab."""
        async def go():
            """Read the server settings and fill the entries."""
            try:
                cfg = await self.engine.active_device.read_server_config()
                self.after(0, lambda: self._fill_server(cfg))
                log_system("Server config read successfully.", "#10b981")
            except Exception as e:
                log_system(f"Read server failed: {e}", "#ef4444")
        self._run(go)

    def _fill_server(self, cfg):
        """Show the read server settings ('--' when unset)."""
        for key in ("host", "port", "keepalive"):
            ent = self._widgets[("server", key)]
            ent.delete(0, tk.END)
            val = cfg.get(key)
            ent.insert(0, str(val) if val is not None else "--")

    def _write_server(self):
        """Write the server settings (empty entries are left unchanged on the device)."""
        host = self._widgets[("server", "host")].get().strip() or None
        port = self._widgets[("server", "port")].get().strip() or None
        ka = self._widgets[("server", "keepalive")].get().strip() or None
        async def go():
            """Write the server settings (port and keep-alive accept decimal or 0x hex) and log the result."""
            try:
                ok = await self.engine.active_device.write_server_config(
                    host=host, port=int(port, 0) if port and port != "--" else None,
                    keepalive=int(ka, 0) if ka and ka != "--" else None)
                log_system(f"Write server config - {'OK' if ok else 'FAILED'}", "#10b981" if ok else "#ef4444")
            except Exception as e:
                log_system(f"Write server failed: {e}", "#ef4444")
        self._run(go)

    def _reset_device(self):
        """Ask for confirmation, then send the reset command."""
        if not messagebox.askyesno("Reset Device", "Send reset command to the device?"):
            return
        async def go():
            """Send the reset command and log whether it was acknowledged."""
            try:
                ok = await self.engine.active_device.reset_device()
                log_system(f"Reset command - {'ACK' if ok else 'FAILED'}", "#10b981" if ok else "#ef4444")
            except Exception as e:
                log_system(f"Reset failed: {e}", "#ef4444")
        self._run(go)

    def _validate_token(self):
        """Send the hex token typed in the entry to the device for validation."""
        raw = self.token_entry.get().strip()
        if not raw:
            return
        token = bytes.fromhex(raw.replace(" ", ""))
        async def go():
            """Send the token and log whether the device reports it valid."""
            try:
                ok = await self.engine.active_device.validate_token(token)
                log_system(f"Token validation - {'VALID' if ok else 'INVALID'}", "#10b981" if ok else "#ef4444")
            except Exception as e:
                log_system(f"Token validation failed: {e}", "#ef4444")
        self._run(go)
