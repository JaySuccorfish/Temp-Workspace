"""@file sc2_hub_panel.py
@brief Dedicated configuration window for SC2 Gen2 (nRF52840) and SC2 Gen3 (nRF5340) hubs (0xBA01)
@version 2.1
@date 2026-11-22

Kept separate from Valtrack's ConfigPanel so SC2 features never conflict with Valtrack-only opcodes. Every frame was
traced from the SC2 firmware (sc2-gen*-STM32CubeIDE ble_cmd_dispatch.c, the BLE co-processor relays) and lives in
config_codecs.py / sc2_hub_codecs.py. Tabs: 0xC1 device configuration (GET big-endian, SET little-endian, 16-bit
intervals), 0xC2 industry mode, 0xC3 SIM (read-modify-write: 0xFF in the URAT / profile bytes is NOT 'keep' on the
SC2) and 0xC4 server, 0xC8 / 0xC9 Depth & Temp loggers the hub collects from, 0x12 / 0xE3 GPS fix and live tracking,
system commands, an expert box limited to the opcodes the BLE co-processor forwards, and the STM32 DOTA relay
(CRC-8/MAXIM per slice, CRC-16/MODBUS of the image, image name checked before sending). Not offered, because the
firmware has no working handler: geofences (GEOFENCE_FEATURE_ENABLE is off on Gen2 and Gen3), Wi-Fi 0xC6 (Gen2 ignores
it, Gen3 answers an error) and app text messages 0xB1 (Gen2 drops every message, doc 07 D-04).
"""
from .ble_engine import BleEngine
from .ble_engine import SessionView
from .ble_engine import run_async
from .config_codecs import SC2_C1_FIELDS
from .config_codecs import ConfigValueError
from .config_codecs import format_seconds
from .config_codecs import parse_number
from .constants import AES_KEY_SC2_HUB
from .constants import BRAND
from .constants import SC2_HUB_DOTA_CHAR_UUID
from .logbus import log_system
from .logbus import post_gui
from .sc2_hub_codecs import DOTA_AES_BLOCK
from .sc2_hub_codecs import DOTA_STEP_CHECKSUM
from .sc2_hub_codecs import DOTA_STEP_FILE_LENGTH
from .sc2_hub_codecs import SC2_CHANNEL_NAMES
from .sc2_hub_codecs import SC2_DT_DURATION_FIELD
from .sc2_hub_codecs import SC2_DT_INTERVAL_FIELD
from .sc2_hub_codecs import SC2_DT_MODE_FIELD
from .sc2_hub_codecs import SC2_DT_PHASES
from .sc2_hub_codecs import SC2_DT_SLOTS
from .sc2_hub_codecs import SC2_FORWARDED_OPCODES
from .sc2_hub_codecs import SC2_GARAGE_NAMES
from .sc2_hub_codecs import SC2_INDUSTRY_FIELDS
from .sc2_hub_codecs import sc2_decode_gps
from .sc2_hub_codecs import sc2_dota_ack_ok
from .sc2_hub_codecs import sc2_dota_check_image
from .sc2_hub_codecs import sc2_dota_data_header
from .sc2_hub_codecs import sc2_dota_plain_blocks
from .sc2_hub_codecs import sc2_dota_slice_size
from .theme import BrandHeader
from .theme import apply_brand_theme
from .theme import apply_window_icon
from Crypto.Cipher import AES
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
import asyncio
import os
import time
import tkinter as tk


SC2_SIM_TYPES = {0: "eSIM", 1: "physical SIM"}
DOTA_ACK_TIMEOUT_S = 10.0
DOTA_SLICE_RETRIES = 3
DOTA_DEFAULT_MTU = 23
GPS_REQUEST_NOTE_S = 120


class Sc2HubConfigPanel(tk.Toplevel):
    """Dedicated configuration window for one SC2 Gen2 or SC2 Gen3 device."""

    def __init__(self, master, engine: "BleEngine", target_address: "str | None" = None,
                 session_id: "str | None" = None):
        super().__init__(master)
        explicit_sid = (session_id or target_address or "").lower().strip() or None
        self.session_id = explicit_sid or engine.selected_session_id
        self.engine = SessionView(engine, self.session_id)
        sess = self.engine.selected_session
        self.profile = getattr(sess, "profile", "sc2_gen2") or "sc2_gen2"
        self.hub_label = "SC2 Gen3 (nRF5340)" if self.profile == "sc2_gen3" else "SC2 Gen2 (nRF52840)"
        addr_tag = f" [{sess.address}]" if sess and getattr(sess, "address", None) else (
            f" [{target_address}]" if target_address else ""
        )

        self.title(f"{BRAND['company']} {self.hub_label} - Configuration{addr_tag}")
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        w = min(1180, int(screen_w * 0.9))
        h = min(740, int(screen_h * 0.85))
        x = max(0, (screen_w - w) // 2)
        y = max(0, (screen_h - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(min(700, screen_w - 40), min(480, screen_h - 60))
        self.configure(bg=BRAND["bg"])
        self.transient(master)
        self.resizable(True, True)
        apply_window_icon(self)
        apply_brand_theme(self)

        self._widgets = {}
        self.var_hex_out = tk.BooleanVar(value=False)
        self.fw_filepath = tk.StringVar(value="")

        self.header = BrandHeader(self, subtitle=f"{self.hub_label} - Config")
        self.header.pack(fill=tk.X, side=tk.TOP)
        self.header.set_link(
            bool(self.engine.active_client and self.engine.active_client.is_connected),
            getattr(sess, "name", "") or "",
        )

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 10))

        self._industry = {}
        self._industry_vars = {}
        self._dt_address_entries = []
        self._dt_phase_entries = []
        self._sim_current = {}

        self._build_device_tab(nb)
        self._build_industry_tab(nb)
        self._build_sim_server_tab(nb)
        self._build_relay_tab(nb)
        self._build_gps_tab(nb)
        self._build_system_actions_tab(nb)
        self._build_advanced_tab(nb)
        self._build_dota_tab(nb)

        self.protocol("WM_DELETE_WINDOW", self._close)
        device = self.engine.active_device
        if device is not None and hasattr(device, "add_listener"):
            device.add_listener(0x12, self._on_gps_frame)

    def _mk_tab(self, nb, title):
        f = tk.Frame(nb, bg=BRAND["surface"], padx=14, pady=12)
        nb.add(f, text=title)
        return f

    def _field(self, parent, row, label, key, category, width=24):
        parent.grid_columnconfigure(0, weight=0)
        parent.grid_columnconfigure(1, weight=1)
        tk.Label(parent, text=label, bg=BRAND["surface"], fg=BRAND["text"],
                 font=BRAND["font_ui"], anchor="w").grid(row=row, column=0, sticky="ew", padx=8, pady=3)
        ent = tk.Entry(parent, width=width, bg=BRAND["surface_alt"], fg=BRAND["text"],
                       insertbackground=BRAND["text"], font=BRAND["font_mono"],
                       relief=tk.FLAT, highlightthickness=1,
                       highlightbackground=BRAND["border"], highlightcolor=BRAND["accent"])
        ent.grid(row=row, column=1, sticky="ew", padx=8, pady=3)
        self._widgets[(category, key)] = ent
        return ent

    def _run(self, coro_fn):
        if not self.engine.active_device or not self.engine.active_client or not self.engine.active_client.is_connected:
            messagebox.showwarning("Not connected", f"Connect to an {self.hub_label} device first.")
            return
        run_async(coro_fn(), session_id=self.session_id)

    # ------------------------------------------------------------------
    # Tab 1: Device Config (0xC1)
    # ------------------------------------------------------------------
    def _build_device_tab(self, nb):
        """Device configuration tab with the SC2 0xC1 layout (config_codecs.SC2_C1_FIELDS): entry, accepted range
        and the read value in words per field.
        """
        f = self._mk_tab(nb, "Config 0xC1")
        f.grid_columnconfigure(2, weight=1)
        self._device_notes = {}
        self._last_device_cfg = {}
        for row, spec in enumerate(SC2_C1_FIELDS):
            self._field(f, row, f"{spec.label} ({spec.unit})" if spec.unit else spec.label, spec.key, "device",
                        width=14)
            note = tk.Label(f, text=spec.range_text(), bg=BRAND["surface"], fg=BRAND["text_muted"],
                            font=("Segoe UI", 8), anchor="w")
            note.grid(row=row, column=2, sticky="w", padx=6)
            self._device_notes[spec.key] = note

        bar = tk.Frame(f, bg=BRAND["surface"])
        bar.grid(row=len(SC2_C1_FIELDS), column=0, columnspan=3, pady=10, sticky="ew", padx=6)
        tk.Button(bar, text="Read (0xC1)", command=self._read_device, bg=BRAND["accent_dark"], fg="white",
                  relief=tk.FLAT, padx=12).pack(side=tk.LEFT)
        tk.Button(bar, text="Write changed fields (0xC1)", command=self._write_device, bg=BRAND["ok"], fg="white",
                  relief=tk.FLAT, padx=12).pack(side=tk.LEFT, padx=6)
        tk.Checkbutton(bar, text="Show hex (0x..)", variable=self.var_hex_out,
                       bg=BRAND["surface"], fg=BRAND["text"], selectcolor=BRAND["accent_dark"]).pack(side=tk.LEFT,
                                                                                                 padx=(16, 4))
        self.lbl_device_status = tk.Label(
            f, text="SC2 0xC1 intervals are 16-bit on BLE (max 65534 s); a value set above that by the server reads "
                    "back as its low 16 bits. SC2 acknowledges every write but silently drops out-of-range values, so "
                    "the ranges are checked here. The device resets after a write.",
            bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 8), justify=tk.LEFT, wraplength=760)
        self.lbl_device_status.grid(row=len(SC2_C1_FIELDS) + 1, column=0, columnspan=3, sticky="w", padx=6)

    def _fill_device(self, cfg: dict):
        """Show a read configuration ('--' = not set) with intervals also in words."""
        as_hex = self.var_hex_out.get()
        for spec in SC2_C1_FIELDS:
            ent = self._widgets[("device", spec.key)]
            ent.delete(0, tk.END)
            val = cfg.get(spec.key)
            if val is None:
                ent.insert(0, "--")
            elif as_hex:
                ent.insert(0, f"0x{val:X}")
            else:
                ent.insert(0, str(val))
            note = spec.range_text()
            if val is not None and spec.unit == "s":
                note = f"= {format_seconds(val)}   ({note})"
            self._device_notes[spec.key].config(text=note)

    def _read_device(self):
        """Read 0xC1 into the device tab."""
        async def go():
            """Read, fill the entries and remember the values (only changed fields are written)."""
            try:
                cfg = await self.engine.active_device.read_device_config()
                self._last_device_cfg = dict(cfg)
                self.after(0, lambda c=cfg: self._fill_device(c))
                log_system(f"[{self.hub_label}] Device config read OK.", BRAND["ok"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Device config read failed: {e}", BRAND["err"])
        self._run(go)

    def _write_device(self):
        """Validate the changed fields against the SC2 ranges and write them (little-endian SET frame)."""
        fields = {}
        try:
            for spec in SC2_C1_FIELDS:
                value = parse_number(self._widgets[("device", spec.key)].get(), spec)
                if value is not None and self._last_device_cfg.get(spec.key) != value:
                    fields[spec.key] = value
        except ConfigValueError as error:
            messagebox.showerror("Invalid value", str(error), parent=self)
            return
        if not fields:
            self.lbl_device_status.config(text="Nothing changed since the last read.", fg=BRAND["warn"])
            return
        summary = ", ".join(f"{key} = {value}" for key, value in fields.items())

        async def go():
            """Write the changed fields; the hub resets after acknowledging."""
            try:
                ok = await self.engine.active_device.write_device_config(fields)
            except Exception as e:
                log_system(f"[{self.hub_label}] Write 0xC1 failed: {e}", BRAND["err"])
                return
            text = (f"Written ({summary}); the hub resets to apply it. Read again after it reconnects to verify."
                    if ok else f"Write refused ({summary}).")
            log_system(f"[{self.hub_label}] {text}", BRAND["ok"] if ok else BRAND["err"])
            self.after(0, lambda: self.lbl_device_status.config(text=text, fg=BRAND["ok"] if ok else BRAND["err"]))
        self._run(go)

    # ------------------------------------------------------------------
    # Tab 2: Industry Mode (0xC2)
    # ------------------------------------------------------------------
    def _build_industry_tab(self, nb):
        """Garage mode and the report channels of GIGO beacon and Depth & Temp events (0xC2)."""
        f = self._mk_tab(nb, "Industry 0xC2")
        choices = {"garage_mode": SC2_GARAGE_NAMES, "gigo_channels": SC2_CHANNEL_NAMES,
                   "dt_channels": SC2_CHANNEL_NAMES}
        for row, spec in enumerate(SC2_INDUSTRY_FIELDS):
            tk.Label(f, text=spec.label, bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_ui"]).grid(
                row=row, column=0, sticky="w", padx=8, pady=4)
            variable = tk.StringVar(value="")
            ttk.Combobox(f, textvariable=variable, state="readonly", width=22,
                         values=[f"{key} - {text}" for key, text in choices[spec.key].items()]).grid(
                row=row, column=1, sticky="w", padx=8, pady=4)
            tk.Label(f, text=spec.hint, bg=BRAND["surface"], fg=BRAND["text_muted"], font=("Segoe UI", 8)).grid(
                row=row, column=2, sticky="w", padx=8)
            self._industry_vars[spec.key] = variable
        bar = tk.Frame(f, bg=BRAND["surface"])
        bar.grid(row=len(SC2_INDUSTRY_FIELDS), column=0, columnspan=3, sticky="w", padx=6, pady=10)
        tk.Button(bar, text="Read (0xC2)", command=self._read_industry, bg=BRAND["accent_dark"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT)
        tk.Button(bar, text="Write (hub resets)", command=self._write_industry, bg=BRAND["ok"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT, padx=6)
        self.lbl_industry = tk.Label(f, text="Read first: these fields have no 'keep' value. A refused value is "
                                             "answered without a reset.", bg=BRAND["surface"],
                                     fg=BRAND["text_dim"], font=("Segoe UI", 8), wraplength=700, justify=tk.LEFT)
        self.lbl_industry.grid(row=len(SC2_INDUSTRY_FIELDS) + 1, column=0, columnspan=3, sticky="w", padx=8)

    def _read_industry(self):
        """Read 0xC2 into the selectors."""
        async def go():
            try:
                industry = await self.engine.active_device.sc2_read_industry()
            except Exception as error:
                log_system(f"[{self.hub_label}] Industry read failed: {error}", BRAND["err"])
                return
            self._industry = industry
            names = {"garage_mode": SC2_GARAGE_NAMES, "gigo_channels": SC2_CHANNEL_NAMES,
                     "dt_channels": SC2_CHANNEL_NAMES}

            def fill():
                for key, variable in self._industry_vars.items():
                    value = industry.get(key)
                    variable.set(f"{value} - {names[key].get(value, '?')}" if value is not None else "")
                self.lbl_industry.config(text=f"Read: {industry}", fg=BRAND["ok"])
            self.after(0, fill)
            log_system(f"[{self.hub_label}] Industry mode: {industry}", BRAND["ok"])
        self._run(go)

    def _write_industry(self):
        """Write the selected industry fields (needs a previous read)."""
        if not self._industry:
            messagebox.showinfo("Industry mode", "Read the industry configuration first.", parent=self)
            return
        fields = {key: int(variable.get().split(" ")[0]) for key, variable in self._industry_vars.items()
                  if variable.get()}
        if not messagebox.askyesno("Industry mode", "Write the industry configuration? The hub resets.", parent=self):
            return

        async def go():
            try:
                ok = await self.engine.active_device.sc2_write_industry(fields, self._industry)
            except Exception as error:
                log_system(f"[{self.hub_label}] Industry write failed: {error}", BRAND["err"])
                return
            text = "Industry mode written, the hub resets." if ok else "Industry mode REFUSED (no reset)."
            self.after(0, lambda: self.lbl_industry.config(text=text, fg=BRAND["ok"] if ok else BRAND["err"]))
            log_system(f"[{self.hub_label}] {text}", BRAND["ok"] if ok else BRAND["err"])
        self._run(go)

    # ------------------------------------------------------------------
    # Tab 3: SIM (0xC3) & Server (0xC4)
    # ------------------------------------------------------------------
    def _build_sim_server_tab(self, nb):
        f = self._mk_tab(nb, "SIM / Server")
        self._field(f, 0, "APN", "apn", "sim", width=36)
        self._field(f, 1, "Username", "username", "sim", width=36)
        self._field(f, 2, "Password", "password", "sim", width=36)
        tk.Label(f, text="SIM", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_ui"]).grid(
            row=3, column=0, sticky="w", padx=8, pady=3)
        self.cmb_sim_type = ttk.Combobox(f, state="readonly", width=20,
                                         values=[f"{key} - {text}" for key, text in SC2_SIM_TYPES.items()])
        self.cmb_sim_type.grid(row=3, column=1, sticky="w", padx=8, pady=3)
        tk.Label(f, text="Read first: the hub stores the modem mode (URAT) and profile from the same frame, so they "
                         "are written back as read. Every write resets the hub.", bg=BRAND["surface"],
                 fg=BRAND["text_dim"], font=("Segoe UI", 8), wraplength=700, justify=tk.LEFT).grid(
            row=4, column=0, columnspan=2, sticky="w", padx=8)
        bar_sim = tk.Frame(f, bg=BRAND["surface"])
        bar_sim.grid(row=5, column=0, columnspan=2, pady=(6, 16), sticky="ew", padx=6)
        tk.Button(bar_sim, text="Read SIM (0xC3)", command=self._read_sim, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=12).pack(side=tk.LEFT)
        tk.Button(bar_sim, text="Write SIM (0xC3)", command=self._write_sim, bg=BRAND["ok"], fg="white", relief=tk.FLAT, padx=12).pack(side=tk.LEFT, padx=6)

        self._field(f, 6, "Server Host / IP", "host", "server", width=36)
        self._field(f, 7, "Port", "port", "server", width=16)
        self._field(f, 8, "TCP Keepalive (min)", "keepalive", "server", width=16)
        bar_srv = tk.Frame(f, bg=BRAND["surface"])
        bar_srv.grid(row=9, column=0, columnspan=2, pady=6, sticky="ew", padx=6)
        tk.Button(bar_srv, text="Read Server (0xC4)", command=self._read_server, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=12).pack(side=tk.LEFT)
        tk.Button(bar_srv, text="Write Server (0xC4)", command=self._write_server, bg=BRAND["ok"], fg="white", relief=tk.FLAT, padx=12).pack(side=tk.LEFT, padx=6)

    def _read_sim(self):
        """Read 0xC3: APN, user, password and the SIM type; URAT, band and profile are kept for the write."""
        async def go():
            try:
                cfg = await self.engine.active_device.read_sim_config()
            except Exception as e:
                log_system(f"[{self.hub_label}] SIM config read failed: {e}", BRAND["err"])
                return
            self._sim_current = dict(cfg)

            def fill():
                for k in ("apn", "username", "password"):
                    e = self._widgets[("sim", k)]
                    e.delete(0, tk.END)
                    e.insert(0, cfg.get(k) or "")
                sim_type = cfg.get("sim_type")
                if sim_type in SC2_SIM_TYPES:
                    self.cmb_sim_type.set(f"{sim_type} - {SC2_SIM_TYPES[sim_type]}")
            self.after(0, fill)
            log_system(f"[{self.hub_label}] SIM config read OK: SIM type {cfg.get('sim_type')}, URAT "
                       f"{cfg.get('network_mode')}, band mask {cfg.get('band_mask')}, profile {cfg.get('profile')}",
                       BRAND["ok"])
        self._run(go)

    def _write_sim(self):
        """Write 0xC3 (read first, so URAT and profile go back unchanged)."""
        if not self._sim_current:
            messagebox.showinfo("SIM", "Read the SIM configuration first: on the SC2, 0xFF in the modem-mode and "
                                "profile bytes is stored as a value, not 'keep'.", parent=self)
            return
        apn = self._widgets[("sim", "apn")].get().strip() or None
        user = self._widgets[("sim", "username")].get().strip() or None
        pwd = self._widgets[("sim", "password")].get().strip() or None
        sim_text = self.cmb_sim_type.get()
        sim_type = int(sim_text.split(" ")[0]) if sim_text else None
        if not messagebox.askyesno("SIM", "Write the SIM configuration? The hub resets.", parent=self):
            return

        async def go():
            try:
                ok = await self.engine.active_device.write_sim_config(apn=apn, username=user, password=pwd,
                                                                      sim_type=sim_type, current=self._sim_current)
                log_system(f"[{self.hub_label}] Write SIM - {'OK, the hub resets' if ok else 'FAILED'}",
                           BRAND["ok"] if ok else BRAND["err"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Write SIM failed: {e}", BRAND["err"])
        self._run(go)

    def _read_server(self):
        async def go():
            try:
                cfg = await self.engine.active_device.read_server_config()
                def fill():
                    for k in ("host", "port", "keepalive"):
                        e = self._widgets[("server", k)]
                        e.delete(0, tk.END)
                        v = cfg.get(k)
                        e.insert(0, "" if v is None else str(v))
                self.after(0, fill)
                log_system(f"[{self.hub_label}] Server config read OK.", BRAND["ok"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Server config read failed: {e}", BRAND["err"])
        self._run(go)

    def _write_server(self):
        host = self._widgets[("server", "host")].get().strip() or None
        port_s = self._widgets[("server", "port")].get().strip()
        ka_s = self._widgets[("server", "keepalive")].get().strip()
        try:
            port = int(port_s, 0) if port_s and port_s != "--" else None
            ka = int(ka_s, 0) if ka_s and ka_s != "--" else None
        except ValueError as e:
            messagebox.showerror("Invalid Server Field", str(e))
            return
        async def go():
            try:
                ok = await self.engine.active_device.write_server_config(host=host, port=port, keepalive=ka)
                log_system(f"[{self.hub_label}] Write Server - {'OK' if ok else 'FAILED'}",
                           BRAND["ok"] if ok else BRAND["err"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Write Server failed: {e}", BRAND["err"])
        self._run(go)

    # ------------------------------------------------------------------
    # Tab 4: Depth & Temp loggers the hub collects from (0xC8 / 0xC9)
    # ------------------------------------------------------------------
    def _build_relay_tab(self, nb):
        """Logger address list (0xC8) and the recording plan the hub sends to one logger (0xC9)."""
        f = self._mk_tab(nb, "D&T loggers")
        tk.Label(f, text="Depth & Temp loggers (BLE address, as printed: AA:BB:CC:DD:EE:FF)", bg=BRAND["surface"],
                 fg=BRAND["accent"], font=BRAND["font_ui_b"]).grid(row=0, column=0, columnspan=4, sticky="w", padx=6)
        for slot in range(SC2_DT_SLOTS):
            tk.Label(f, text=f"Slot {slot + 1}", bg=BRAND["surface"], fg=BRAND["text"],
                     font=BRAND["font_ui"]).grid(row=1 + slot, column=0, sticky="w", padx=8, pady=2)
            entry = tk.Entry(f, width=22, bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground=BRAND["text"],
                             font=BRAND["font_mono"], relief=tk.FLAT)
            entry.grid(row=1 + slot, column=1, sticky="w", padx=8, pady=2)
            tk.Button(f, text="Plan >", command=lambda index=slot: self._pick_logger(index),
                      bg=BRAND["surface_alt"], fg=BRAND["text"], relief=tk.FLAT, padx=6).grid(
                row=1 + slot, column=2, sticky="w")
            self._dt_address_entries.append(entry)
        bar = tk.Frame(f, bg=BRAND["surface"])
        bar.grid(row=1 + SC2_DT_SLOTS, column=0, columnspan=4, sticky="w", padx=6, pady=6)
        tk.Button(bar, text="Read list (0xC8)", command=self._read_dt_list, bg=BRAND["accent_dark"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT)
        tk.Button(bar, text="Write list (hub resets)", command=self._write_dt_list, bg=BRAND["ok"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT, padx=6)

        row = 2 + SC2_DT_SLOTS
        tk.Label(f, text="Recording plan of one logger (0xC9; empty = keep)", bg=BRAND["surface"], fg=BRAND["accent"],
                 font=BRAND["font_ui_b"]).grid(row=row, column=0, columnspan=4, sticky="w", padx=6, pady=(10, 2))
        tk.Label(f, text="Logger address", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_ui"]).grid(
            row=row + 1, column=0, sticky="w", padx=8)
        self.ent_dt_address = tk.Entry(f, width=22, bg=BRAND["surface_alt"], fg=BRAND["text"],
                                       insertbackground=BRAND["text"], font=BRAND["font_mono"], relief=tk.FLAT)
        self.ent_dt_address.grid(row=row + 1, column=1, sticky="w", padx=8, pady=2)
        tk.Label(f, text=f"{SC2_DT_MODE_FIELD.label}", bg=BRAND["surface"], fg=BRAND["text"],
                 font=BRAND["font_ui"]).grid(row=row + 2, column=0, sticky="w", padx=8)
        self.ent_dt_mode = tk.Entry(f, width=8, bg=BRAND["surface_alt"], fg=BRAND["text"],
                                    insertbackground=BRAND["text"], font=BRAND["font_mono"], relief=tk.FLAT)
        self.ent_dt_mode.grid(row=row + 2, column=1, sticky="w", padx=8, pady=2)
        tk.Label(f, text=SC2_DT_MODE_FIELD.hint, bg=BRAND["surface"], fg=BRAND["text_muted"],
                 font=("Segoe UI", 8)).grid(row=row + 2, column=2, columnspan=2, sticky="w", padx=8)
        for phase in range(SC2_DT_PHASES):
            tk.Label(f, text=f"Phase {phase + 1}: interval / duration (s)", bg=BRAND["surface"], fg=BRAND["text"],
                     font=BRAND["font_ui"]).grid(row=row + 3 + phase, column=0, sticky="w", padx=8)
            interval = tk.Entry(f, width=10, bg=BRAND["surface_alt"], fg=BRAND["text"],
                                insertbackground=BRAND["text"], font=BRAND["font_mono"], relief=tk.FLAT)
            interval.grid(row=row + 3 + phase, column=1, sticky="w", padx=8, pady=2)
            duration = tk.Entry(f, width=10, bg=BRAND["surface_alt"], fg=BRAND["text"],
                                insertbackground=BRAND["text"], font=BRAND["font_mono"], relief=tk.FLAT)
            duration.grid(row=row + 3 + phase, column=2, sticky="w", padx=8, pady=2)
            self._dt_phase_entries.append((interval, duration))
        tk.Label(f, text=f"Interval {SC2_DT_INTERVAL_FIELD.range_text()}, duration "
                         f"{SC2_DT_DURATION_FIELD.range_text()} (15m, 2h, 1d accepted)", bg=BRAND["surface"],
                 fg=BRAND["text_muted"], font=("Segoe UI", 8)).grid(row=row + 6, column=0, columnspan=4, sticky="w",
                                                                    padx=8)
        bar2 = tk.Frame(f, bg=BRAND["surface"])
        bar2.grid(row=row + 7, column=0, columnspan=4, sticky="w", padx=6, pady=6)
        tk.Button(bar2, text="Read plan (0xC9)", command=self._read_dt_plan, bg=BRAND["accent_dark"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT)
        tk.Button(bar2, text="Write plan (hub resets)", command=self._write_dt_plan, bg=BRAND["ok"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT, padx=6)
        self.lbl_relay = tk.Label(f, text="", bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 8),
                                  wraplength=720, justify=tk.LEFT)
        self.lbl_relay.grid(row=row + 8, column=0, columnspan=4, sticky="w", padx=8)

    def _relay_status(self, text, colour):
        """Show a relay result below the tab and in the log."""
        self.after(0, lambda: self.lbl_relay.config(text=text, fg=colour))
        log_system(f"[{self.hub_label}] {text}", colour)

    def _pick_logger(self, index):
        """Copy a slot's address into the plan section."""
        address = self._dt_address_entries[index].get().strip()
        self.ent_dt_address.delete(0, tk.END)
        self.ent_dt_address.insert(0, address)

    def _read_dt_list(self):
        """Read the five logger slots."""
        async def go():
            try:
                addresses = await self.engine.active_device.sc2_read_dt_addresses()
            except Exception as error:
                self._relay_status(f"Logger list read failed: {error}", BRAND["err"])
                return

            def fill():
                for entry, address in zip(self._dt_address_entries, addresses):
                    entry.delete(0, tk.END)
                    entry.insert(0, address)
            self.after(0, fill)
            self._relay_status(f"Logger list: {[a or '-' for a in addresses]}", BRAND["ok"])
        self._run(go)

    def _write_dt_list(self):
        """Write the logger slots (blank entries are dropped; order kept)."""
        addresses = [entry.get().strip() for entry in self._dt_address_entries]
        if not messagebox.askyesno("Depth & Temp loggers", "Write the logger list? The hub resets.", parent=self):
            return

        async def go():
            try:
                ok = await self.engine.active_device.sc2_write_dt_addresses(addresses)
            except ConfigValueError as error:
                message = str(error)
                self.after(0, lambda: messagebox.showerror("Logger list", message, parent=self))
                return
            except Exception as error:
                self._relay_status(f"Logger list write failed: {error}", BRAND["err"])
                return
            self._relay_status("Logger list stored, the hub resets." if ok else "Logger list REFUSED.",
                               BRAND["ok"] if ok else BRAND["err"])
        self._run(go)

    def _read_dt_plan(self):
        """Read the recording plan of the logger in the address field."""
        address = self.ent_dt_address.get().strip()

        async def go():
            try:
                plan = await self.engine.active_device.sc2_read_dt_device(address)
            except Exception as error:
                self._relay_status(f"Plan read failed: {error}", BRAND["err"])
                return
            if not plan["known"]:
                self._relay_status(f"{address} is not in the hub's logger list", BRAND["warn"])
                return

            def fill():
                self.ent_dt_mode.delete(0, tk.END)
                self.ent_dt_mode.insert(0, str(plan["mode"]))
                for (interval_entry, duration_entry), (interval, duration) in zip(self._dt_phase_entries,
                                                                                  plan["phases"]):
                    interval_entry.delete(0, tk.END)
                    interval_entry.insert(0, str(interval))
                    duration_entry.delete(0, tk.END)
                    duration_entry.insert(0, str(duration))
            self.after(0, fill)
            phases = ", ".join(f"{format_seconds(i)} for {format_seconds(d)}" for i, d in plan["phases"])
            self._relay_status(f"Plan of {plan['address']}: mode {plan['mode']}, {phases}", BRAND["ok"])
        self._run(go)

    def _write_dt_plan(self):
        """Validate and write the recording plan (empty fields keep the stored value)."""
        address = self.ent_dt_address.get().strip()
        try:
            mode = parse_number(self.ent_dt_mode.get(), SC2_DT_MODE_FIELD)
            phases = [(parse_number(interval.get(), SC2_DT_INTERVAL_FIELD),
                       parse_number(duration.get(), SC2_DT_DURATION_FIELD))
                      for interval, duration in self._dt_phase_entries]
        except ConfigValueError as error:
            messagebox.showerror("Invalid value", str(error), parent=self)
            return
        if not messagebox.askyesno("Recording plan", f"Write the plan of {address}? The hub resets (also when the "
                                   "address is not in its list).", parent=self):
            return

        async def go():
            try:
                stored = await self.engine.active_device.sc2_write_dt_device(address, mode, phases)
            except ConfigValueError as error:
                message = str(error)
                self.after(0, lambda: messagebox.showerror("Recording plan", message, parent=self))
                return
            except Exception as error:
                self._relay_status(f"Plan write failed: {error}", BRAND["err"])
                return
            if stored is None:
                self._relay_status("Plan write: no acknowledge", BRAND["warn"])
            elif stored:
                self._relay_status("Plan stored, the hub resets.", BRAND["ok"])
            else:
                self._relay_status("Address not in the hub's logger list: nothing stored (the hub still resets).",
                                   BRAND["err"])
        self._run(go)

    # ------------------------------------------------------------------
    # Tab 5: GPS fix (0x12) and live tracking (0xE3)
    # ------------------------------------------------------------------
    def _build_gps_tab(self, nb):
        """Ask for one fix or switch live tracking on; fixes arrive as 0x12 frames."""
        f = self._mk_tab(nb, "GPS")
        bar = tk.Frame(f, bg=BRAND["surface"])
        bar.grid(row=0, column=0, sticky="w", padx=6, pady=(4, 8))
        tk.Button(bar, text="Get a fix (0x12)", command=self._request_gps, bg=BRAND["accent_dark"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT)
        tk.Button(bar, text="Live tracking ON (0xE3)", command=lambda: self._live_track(True), bg=BRAND["ok"],
                  fg="white", relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT, padx=6)
        tk.Button(bar, text="Live tracking OFF", command=lambda: self._live_track(False), bg=BRAND["warn"],
                  fg="#000000", relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT)
        tk.Label(f, text=f"The hub answers when its GPS task has a fix (up to the GPS time-out, about "
                         f"{GPS_REQUEST_NOTE_S} s). Live tracking sends every later fix while connected and stops on "
                         "disconnect.", bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 8),
                 wraplength=720, justify=tk.LEFT).grid(row=1, column=0, sticky="w", padx=6)
        self.txt_gps = tk.Text(f, height=14, bg=BRAND["surface_alt"], fg=BRAND["text"], font=BRAND["font_mono"],
                               relief=tk.FLAT, wrap=tk.WORD)
        self.txt_gps.grid(row=2, column=0, sticky="nsew", padx=6, pady=6)
        f.grid_rowconfigure(2, weight=1)
        f.grid_columnconfigure(0, weight=1)

    def _on_gps_frame(self, packet: bytes):
        """BLE thread: a 0x12 fix frame."""
        fix = sc2_decode_gps(packet)
        if fix is None:
            return
        stamp = time.strftime("%H:%M:%S")
        if fix["valid"]:
            line = (f"{stamp} fix {fix['latitude']:.6f}, {fix['longitude']:.6f}  "
                    f"https://maps.google.com/?q={fix['latitude']},{fix['longitude']}")
        else:
            line = f"{stamp} no fix: {fix['error']}"

        def append():
            if self.winfo_exists():
                self.txt_gps.insert(tk.END, line + "\n")
                self.txt_gps.see(tk.END)
        self.after(0, append)
        log_system(f"[{self.hub_label}] GPS: {line}", BRAND["ok"] if fix["valid"] else BRAND["warn"])

    def _request_gps(self):
        """Ask the hub for a fix."""
        async def go():
            await self.engine.active_device.sc2_request_gps()
            log_system(f"[{self.hub_label}] GPS fix requested; waiting for the 0x12 frame", BRAND["info"])
        self._run(go)

    def _live_track(self, start: bool):
        """Switch live tracking on or off."""
        async def go():
            await self.engine.active_device.sc2_live_track(start)
            log_system(f"[{self.hub_label}] Live tracking {'on' if start else 'off'}", BRAND["info"])
        self._run(go)

    def _close(self):
        """Stop routing GPS frames to this window, then close it."""
        device = self.engine.active_device
        if device is not None and hasattr(device, "remove_listener"):
            device.remove_listener(0x12, self._on_gps_frame)
        self.destroy()

    # ------------------------------------------------------------------
    # Tab 5: System, Identity & Actions
    # ------------------------------------------------------------------
    def _build_system_actions_tab(self, nb):
        f = self._mk_tab(nb, "System")
        f.grid_columnconfigure(0, weight=1)

        tk.Label(f, text=f"Hub Info & Identity ({self.hub_label})", bg=BRAND["surface"], fg=BRAND["accent"],
                 font=BRAND["font_ui_b"]).grid(row=0, column=0, sticky="w", padx=6, pady=(4, 4))
        bar1 = tk.Frame(f, bg=BRAND["surface"])
        bar1.grid(row=1, column=0, sticky="w", padx=6, pady=(0, 8))
        tk.Button(bar1, text="Read FW (0xE5)", command=self._read_fw, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=10, pady=4).pack(side=tk.LEFT)
        tk.Button(bar1, text="Read IMEI (0xE1)", command=self._read_imei, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=10, pady=4).pack(side=tk.LEFT, padx=6)
        tk.Button(bar1, text="Read Battery (0xE4)", command=self._read_battery, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=10, pady=4).pack(side=tk.LEFT)
        tk.Button(bar1, text="Sync PC UTC Time (0x11)", command=self._sync_pc_time, bg=BRAND["ok"], fg="white", relief=tk.FLAT, padx=10, pady=4).pack(side=tk.LEFT, padx=6)

        self.lbl_sys_info = tk.Label(f, text="-", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_mono_b"])
        self.lbl_sys_info.grid(row=2, column=0, sticky="w", padx=6, pady=(2, 12))

        tk.Label(f, text="Debug Log Stream (0xFD) & Device Reset (0xC5)", bg=BRAND["surface"], fg=BRAND["accent"],
                 font=BRAND["font_ui_b"]).grid(row=3, column=0, sticky="w", padx=6, pady=(4, 4))
        bar2 = tk.Frame(f, bg=BRAND["surface"])
        bar2.grid(row=4, column=0, sticky="w", padx=6, pady=(0, 12))
        tk.Button(bar2, text="Stream ON", command=lambda: self._set_stream(True), bg=BRAND["ok"], fg="white", relief=tk.FLAT, padx=10, pady=4).pack(side=tk.LEFT)
        tk.Button(bar2, text="Stream OFF", command=lambda: self._set_stream(False), bg=BRAND["warn"], fg="#000000", relief=tk.FLAT, padx=10, pady=4).pack(side=tk.LEFT, padx=6)
        tk.Button(bar2, text="Reset Device (0xC5)", command=self._reset_device, bg=BRAND["err"], fg="white", relief=tk.FLAT, padx=10, pady=4).pack(side=tk.LEFT, padx=(16, 0))

        tk.Label(f, text="Token Validation (0xE2) - hex, 112 bytes. A token the hub does not accept makes the BLE "
                         "co-processor drop the link 0.5 s later.", bg=BRAND["surface"], fg=BRAND["text"],
                 font=BRAND["font_ui_b"]).grid(row=5, column=0, sticky="w", padx=6, pady=(8, 2))
        self.token_entry = tk.Entry(f, width=70, bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground="white", font=("Consolas", 9))
        self.token_entry.grid(row=6, column=0, sticky="ew", padx=6)
        tk.Button(f, text="Validate Token", command=self._validate_token, bg=BRAND["accent_dark"], fg="white", relief=tk.FLAT, padx=12, pady=3).grid(row=7, column=0, sticky="w", padx=6, pady=6)

        dfu_note = (
            "DFU: SC2 Gen2 (nRF52840) uses Nordic Buttonless DFU for BLE and DOTA (0xBA02) for STM32."
            if self.profile == "sc2_gen2"
            else "DFU: SC2 Gen3 (nRF5340) uses MCUmgr SMP for BLE and DOTA (0xBA02) for STM32."
        )
        tk.Label(f, text=dfu_note, bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 8, "italic")).grid(row=8, column=0, sticky="w", padx=6, pady=(12, 0))

    # ------------------------------------------------------------------
    # Tab 8: SC2 STM32 DOTA Relay (0xBA02)
    # ------------------------------------------------------------------
    def _build_dota_tab(self, nb):
        f = self._mk_tab(nb, "DOTA update")
        f.grid_columnconfigure(0, weight=1)

        tk.Label(f, text=f"{self.hub_label} STM32 DOTA Relay (0xBA02)", bg=BRAND["surface"], fg=BRAND["info"],
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=6, pady=(4, 2))
        tk.Label(
            f,
            text="Streams the STM32 application image (.bin linked at 0x08010000) through the BLE co-processor. The "
                 "image is checked first: it must carry the device name of this hub generation at offset 0x188 "
                 "('Succorfish SC2 GEN2' / 'GEN3'), otherwise the hub would refuse it after the whole transfer.",
            bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 9), wraplength=760, justify=tk.LEFT,
        ).grid(row=1, column=0, sticky="w", padx=6, pady=(0, 8))

        file_row = tk.Frame(f, bg=BRAND["surface"])
        file_row.grid(row=2, column=0, sticky="ew", padx=6, pady=4)
        file_row.grid_columnconfigure(0, weight=1)
        tk.Entry(file_row, textvariable=self.fw_filepath, bg=BRAND["surface_alt"], fg=BRAND["text"],
                 font=("Consolas", 9), insertbackground="white", relief=tk.FLAT).grid(row=0, column=0, sticky="ew",
                                                                                      padx=(0, 8))
        tk.Button(file_row, text="Browse .bin", command=self._browse_fw, bg=BRAND["surface_alt"],
                  fg="white", relief=tk.FLAT, padx=10).grid(row=0, column=1)

        self.dota_status_lbl = tk.Label(f, text="Status: Ready (0xBA02)", bg=BRAND["surface"],
                                        fg=BRAND["warn"], font=("Consolas", 9, "bold"))
        self.dota_status_lbl.grid(row=3, column=0, sticky="w", padx=6, pady=(14, 4))
        self.dota_bar = ttk.Progressbar(f, orient="horizontal", length=500, mode="determinate")
        self.dota_bar.grid(row=4, column=0, sticky="ew", padx=6, pady=4)

        btn_row = tk.Frame(f, bg=BRAND["surface"])
        btn_row.grid(row=5, column=0, sticky="w", padx=6, pady=12)
        tk.Button(btn_row, text="Start SC2 DOTA (0xBA02)", command=self._start_sc2_dota,
                  bg=BRAND["ok"], fg="white", font=("Segoe UI", 9, "bold"), relief=tk.FLAT, padx=14,
                  pady=6).pack(side=tk.LEFT)

        ble_dfu_note = (
            "Note: Updating the nRF BLE co-processor itself uses Nordic Buttonless DFU (Gen2)."
            if self.profile == "sc2_gen2"
            else "Note: Updating the nRF5340 co-processor itself uses MCUmgr SMP (Gen3)."
        )
        tk.Label(f, text=ble_dfu_note, bg=BRAND["surface"], fg=BRAND["text_dim"],
                 font=("Segoe UI", 8, "italic")).grid(row=6, column=0, sticky="w", padx=6)

    def _browse_fw(self):
        path = filedialog.askopenfilename(title="Select STM32 Firmware Binary",
                                          filetypes=[("Binary Files", "*.bin"), ("All Files", "*.*")])
        if path:
            self.fw_filepath.set(path)

    def _start_sc2_dota(self):
        """Check the image, confirm, then run the three DOTA steps with an acknowledge for every write."""
        path = self.fw_filepath.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showerror("File Error", "Please select a valid firmware (.bin) file.", parent=self)
            return
        if not self.engine.active_client or not self.engine.active_client.is_connected:
            messagebox.showwarning("Not connected", f"Connect to {self.hub_label} first.", parent=self)
            return
        with open(path, "rb") as handle:
            image = handle.read()
        try:
            image_name = sc2_dota_check_image(image, self.profile)
        except ValueError as error:
            messagebox.showerror("Wrong image", str(error), parent=self)
            return
        if not messagebox.askyesno(
            "Confirm SC2 DOTA",
            f"Flash {len(image):,} bytes ('{image_name}') to {self.hub_label} over 0xBA02?\n\n"
            "Do not disconnect during the transfer. The hub resets when the image is accepted.", parent=self,
        ):
            return

        client = self.engine.active_client
        mtu = getattr(client, "mtu_size", DOTA_DEFAULT_MTU) or DOTA_DEFAULT_MTU
        try:
            slice_size = sc2_dota_slice_size(mtu)
        except ValueError as error:
            messagebox.showerror("DOTA", str(error), parent=self)
            return

        def encrypt(block: bytes) -> bytes:
            """AES-128-ECB with the hub key, zero padded to whole blocks."""
            padded = block + bytes((-len(block)) % DOTA_AES_BLOCK)
            return AES.new(AES_KEY_SC2_HUB, AES.MODE_ECB).encrypt(padded)

        def decrypt_block(data: bytes) -> bytes:
            """Decrypt one 16-byte block of an acknowledge (b'' when too short)."""
            if len(data) < DOTA_AES_BLOCK:
                return b""
            return AES.new(AES_KEY_SC2_HUB, AES.MODE_ECB).decrypt(bytes(data[:DOTA_AES_BLOCK]))

        async def go():
            ack_q: "asyncio.Queue[bytes]" = asyncio.Queue()

            def on_dota_notify(_h, data: bytearray):
                ack_q.put_nowait(bytes(data))

            def set_ui(msg, pct=None, col=BRAND["info"]):
                def _u():
                    self.dota_status_lbl.config(text=f"Status: {msg}", fg=col)
                    if pct is not None:
                        self.dota_bar["value"] = pct
                self.after(0, _u)

            async def ack(step: int, timeout: float) -> bytes:
                """Next acknowledge of this step (others are logged and skipped)."""
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while True:
                    frame = await asyncio.wait_for(ack_q.get(), timeout=max(0.1, deadline - loop.time()))
                    if frame and (frame[0] >> 4) == (step >> 4):
                        return frame
                    log_system(f"[SC2-DOTA] unexpected frame {frame.hex(' ')}", BRAND["warn"])

            first_block, last_block = sc2_dota_plain_blocks(image)
            total = (len(image) + slice_size - 1) // slice_size
            try:
                await client.start_notify(SC2_HUB_DOTA_CHAR_UUID, on_dota_notify)
                set_ui(f"Step 1: file size {len(image)} B, erasing the download area...", 0)
                await client.write_gatt_char(SC2_HUB_DOTA_CHAR_UUID,
                                             bytes([DOTA_STEP_FILE_LENGTH]) + encrypt(first_block), response=True)
                await ack(DOTA_STEP_FILE_LENGTH, 4 * DOTA_ACK_TIMEOUT_S)

                for sequence in range(total):
                    chunk = image[sequence * slice_size:(sequence + 1) * slice_size]
                    frame = sc2_dota_data_header(chunk, sequence) + encrypt(chunk)
                    for attempt in range(1, DOTA_SLICE_RETRIES + 1):
                        await client.write_gatt_char(SC2_HUB_DOTA_CHAR_UUID, frame, response=True)
                        reply = await ack(0x20, DOTA_ACK_TIMEOUT_S)
                        if sc2_dota_ack_ok(2, decrypt_block(reply[5:])):
                            break
                        log_system(f"[SC2-DOTA] slice {sequence}: CRC refused (attempt {attempt})", BRAND["warn"])
                    else:
                        raise RuntimeError(f"slice {sequence} refused {DOTA_SLICE_RETRIES} times (CRC-8)")
                    if sequence % 10 == 0 or sequence + 1 == total:
                        pct = int(((sequence + 1) / total) * 100)
                        set_ui(f"Step 2: slice {sequence + 1}/{total} ({pct}%)", pct)

                set_ui("Step 3: image CRC-16 and name check...", 100)
                await client.write_gatt_char(SC2_HUB_DOTA_CHAR_UUID,
                                             bytes([DOTA_STEP_CHECKSUM]) + encrypt(last_block), response=True)
                reply = await ack(DOTA_STEP_CHECKSUM, 4 * DOTA_ACK_TIMEOUT_S)
                if sc2_dota_ack_ok(3, decrypt_block(reply[1:])):
                    log_system("[SC2-DOTA] Image accepted; the hub installs it and restarts.", BRAND["ok"])
                    set_ui("Image accepted - the hub restarts and installs it.", 100, BRAND["ok"])
                else:
                    log_system("[SC2-DOTA] Image REFUSED (CRC-16 or device name check failed).", BRAND["err"])
                    set_ui("Image refused by the hub (CRC-16 / device name).", None, BRAND["err"])
            except asyncio.TimeoutError:
                log_system("[SC2-DOTA] Failed: no acknowledge from the hub.", BRAND["err"])
                set_ui("Error: no acknowledge from the hub", None, BRAND["err"])
            except Exception as exc:
                log_system(f"[SC2-DOTA] Failed: {exc}", BRAND["err"])
                set_ui(f"Error: {exc}", None, BRAND["err"])
            finally:
                try:
                    await client.stop_notify(SC2_HUB_DOTA_CHAR_UUID)
                except Exception:
                    pass

        run_async(go(), session_id=self.session_id)

    # ------------------------------------------------------------------
    # Shared SC2 command & system handlers
    # ------------------------------------------------------------------
    def _sc2_show_reply(self, label: str, pkt: bytes):
        body = pkt[2:] if len(pkt) >= 2 else b""
        ascii_txt = "".join(chr(b) if 32 <= b < 127 else "." for b in body)
        msg = (
            f"[{label}] opcode=0x{pkt[0]:02X} len={pkt[1] if len(pkt) > 1 else 0}\n"
            f"  HEX  : {body.hex(' ')}\n"
            f"  ASCII: {ascii_txt}\n"
        )
        def _update():
            for box in (getattr(self, "txt_ind_out", None), getattr(self, "txt_sc2_out", None)):
                if box is not None:
                    box.delete("1.0", tk.END)
                    box.insert(tk.END, msg)
        self.after(0, _update)
        log_system(f"[{self.hub_label}] {label}: {pkt.hex(' ')}", BRAND["ok"])

    def _build_advanced_tab(self, nb):
        """Expert box: any opcode the BLE co-processor forwards, read (length 1) or encrypted write."""
        f = self._mk_tab(nb, "Advanced")
        f.grid_columnconfigure(1, weight=1)
        tk.Label(f, text="Raw command (experts). Only the opcodes the BLE co-processor forwards are offered; writes "
                         "of configuration opcodes make the hub save and reset.", bg=BRAND["surface"],
                 fg=BRAND["text_dim"], font=("Segoe UI", 8), wraplength=720, justify=tk.LEFT).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))
        tk.Label(f, text="Opcode", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_ui"]).grid(
            row=1, column=0, sticky="w", padx=6)
        self.cmb_sc2_op = ttk.Combobox(f, state="readonly", width=12,
                                       values=[f"0x{opcode:02X}" for opcode in SC2_FORWARDED_OPCODES])
        self.cmb_sc2_op.set("0xC1")
        self.cmb_sc2_op.grid(row=1, column=1, sticky="w", padx=6, pady=2)
        tk.Label(f, text="Payload", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_ui"]).grid(
            row=2, column=0, sticky="w", padx=6)
        prow = tk.Frame(f, bg=BRAND["surface"])
        prow.grid(row=2, column=1, sticky="ew", padx=6, pady=2)
        prow.grid_columnconfigure(0, weight=1)
        self.ent_sc2_payload = tk.Entry(prow, bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground="white",
                                        font=BRAND["font_mono"], relief=tk.FLAT)
        self.ent_sc2_payload.grid(row=0, column=0, sticky="ew")
        self.var_sc2_utf8 = tk.BooleanVar(value=False)
        tk.Checkbutton(prow, text="UTF-8 text", variable=self.var_sc2_utf8, bg=BRAND["surface"], fg=BRAND["text"],
                       selectcolor=BRAND["accent_dark"]).grid(row=0, column=1, padx=(6, 0))
        bar = tk.Frame(f, bg=BRAND["surface"])
        bar.grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=6)
        tk.Button(bar, text="Read (len=1)", command=self._sc2_custom_read, bg=BRAND["accent_dark"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT)
        tk.Button(bar, text="Send encrypted write", command=self._sc2_custom_write, bg=BRAND["err"], fg="white",
                  relief=tk.FLAT, padx=12, pady=3).pack(side=tk.LEFT, padx=6)
        self.txt_sc2_out = tk.Text(f, height=12, bg=BRAND["surface_alt"], fg=BRAND["text"], font=BRAND["font_mono"],
                                   relief=tk.FLAT, wrap=tk.WORD, highlightthickness=1,
                                   highlightbackground=BRAND["border"])
        self.txt_sc2_out.grid(row=4, column=0, columnspan=2, sticky="nsew", padx=6, pady=(6, 0))
        f.grid_rowconfigure(4, weight=1)

    def _sc2_read(self, opcode: int, label: str):
        """Raw read of opcode; the decrypted reply is shown in hex and ASCII."""
        async def go():
            try:
                pkt = await self.engine.active_device.send_sc2_command(opcode, None, label=label)
                self._sc2_show_reply(label, pkt)
            except asyncio.TimeoutError:
                log_system(f"[{self.hub_label}] {label}: no reply (some opcodes answer later or not at all)",
                           BRAND["warn"])
            except Exception as e:
                log_system(f"[{self.hub_label}] {label} read failed: {e}", BRAND["err"])
        self._run(go)

    def _selected_sc2_opcode(self) -> int:
        """Opcode chosen in the expert box."""
        return int(self.cmb_sc2_op.get(), 16)

    def _sc2_custom_read(self):
        """Raw read of the chosen opcode."""
        op = self._selected_sc2_opcode()
        self._sc2_read(op, f"SC2 0x{op:02X}")

    def _sc2_custom_write(self):
        """Raw encrypted write of the chosen opcode (after a confirmation)."""
        op = self._selected_sc2_opcode()
        raw = self.ent_sc2_payload.get().strip()
        try:
            payload = raw.encode("utf-8") if self.var_sc2_utf8.get() else bytes.fromhex(raw.replace(" ", ""))
        except ValueError as e:
            messagebox.showerror("Invalid Payload", f"Hex parse error: {e}", parent=self)
            return
        if not messagebox.askyesno("Raw write", f"Send 0x{op:02X} with {len(payload)} payload byte(s)?",
                                   icon="warning", parent=self):
            return

        async def go():
            try:
                pkt = await self.engine.active_device.send_sc2_command(op, payload, label=f"SC2-0x{op:02X}")
                self._sc2_show_reply(f"SC2 0x{op:02X} Write", pkt)
            except Exception as e:
                log_system(f"[{self.hub_label}] SC2 0x{op:02X} write failed: {e}", BRAND["err"])
        self._run(go)

    def _read_fw(self):
        async def go():
            try:
                ver = await self.engine.active_device.read_firmware_version()
                txt = f"Firmware: {ver}" if ver else "Firmware: no response"
                self.after(0, lambda t=txt: self.lbl_sys_info.config(text=t, fg=BRAND["ok"] if ver else BRAND["warn"]))
                log_system(f"[{self.hub_label}] {txt}", BRAND["ok"] if ver else BRAND["warn"])
            except Exception as e:
                log_system(f"[{self.hub_label}] FW read failed: {e}", BRAND["err"])
        self._run(go)

    def _read_imei(self):
        async def go():
            try:
                imei = await self.engine.active_device.read_imei()
                txt = f"IMEI: {imei}" if imei else "IMEI: no response"
                if imei:
                    post_gui("device_imei", session_id=self.session_id, imei=imei)
                self.after(0, lambda t=txt: self.lbl_sys_info.config(text=t, fg=BRAND["ok"] if imei else BRAND["warn"]))
                log_system(f"[{self.hub_label}] {txt}", BRAND["ok"] if imei else BRAND["warn"], session_id=self.session_id)
            except Exception as e:
                log_system(f"[{self.hub_label}] IMEI read failed: {e}", BRAND["err"], session_id=self.session_id)
        self._run(go)

    def _read_battery(self):
        async def go():
            try:
                b = await self.engine.active_device.read_battery()
                txt = f"Battery: {b['percent']}%" if b else "Battery: no response"
                self.after(0, lambda t=txt: self.lbl_sys_info.config(text=t, fg=BRAND["ok"] if b else BRAND["warn"]))
                log_system(f"[{self.hub_label}] {txt}", BRAND["ok"] if b else BRAND["warn"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Battery read failed: {e}", BRAND["err"])
        self._run(go)

    def _sync_pc_time(self):
        epoch = int(time.time())
        async def go():
            try:
                ok = await self.engine.active_device.set_device_time(epoch)
                txt = f"UTC Time Sync ({epoch}): {'OK' if ok else 'FAILED / NO ACK'}"
                self.after(0, lambda t=txt: self.lbl_sys_info.config(text=t, fg=BRAND["ok"] if ok else BRAND["warn"]))
                log_system(f"[{self.hub_label}] {txt}", BRAND["ok"] if ok else BRAND["warn"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Time sync failed: {e}", BRAND["err"])
        self._run(go)

    def _set_stream(self, enable: bool):
        async def go():
            try:
                ok = await self.engine.active_device.set_debug_log(enable)
                log_system(f"[{self.hub_label}] Debug stream {'ON' if enable else 'OFF'} - {'OK' if ok else 'FAILED'}",
                           BRAND["ok"] if ok else BRAND["err"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Debug stream failed: {e}", BRAND["err"])
        self._run(go)

    def _reset_device(self):
        if not messagebox.askyesno("Confirm Reset", f"Reset {self.hub_label} device?"):
            return
        async def go():
            try:
                ok = await self.engine.active_device.reset_device()
                log_system(f"[{self.hub_label}] Reset command - {'ACK' if ok else 'FAILED'}",
                           BRAND["ok"] if ok else BRAND["err"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Reset failed: {e}", BRAND["err"])
        self._run(go)

    def _validate_token(self):
        raw = self.token_entry.get().strip().replace(" ", "")
        try:
            token = bytes.fromhex(raw)
        except ValueError as e:
            messagebox.showerror("Invalid Hex", str(e))
            return
        if len(token) != 112:
            messagebox.showerror("Invalid Length", f"Token must be 112 bytes (224 hex chars), got {len(token)} bytes.")
            return
        async def go():
            try:
                ok = await self.engine.active_device.validate_token(token)
                log_system(f"[{self.hub_label}] Token validation - {'VALID' if ok else 'INVALID'}",
                           BRAND["ok"] if ok else BRAND["err"])
            except Exception as e:
                log_system(f"[{self.hub_label}] Token validation failed: {e}", BRAND["err"])
        self._run(go)
