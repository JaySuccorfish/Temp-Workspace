"""@file sc2_panel.py
@brief Window for the SC2 depth & temperature logger: sensors, device settings, logging control and record download
@version 1.1
@date 2026-11-22

Opened from the main window for the GiGO Depth & Temp logger (profile 'gigo_dt'). Each button runs a small coroutine
("go") on the BLE thread through _run(); results are written to the window's output box with _say(). Tabs: single
sensor reads, a live chart (the device tab's DepthTempLiveView), logger control, the plot of the downloaded records (or
of a saved CSV), device configuration. The protocol itself is in sc2_protocol.py.
"""
from .ble_engine import run_async
from .constants import BRAND
from .live_views import DT_PLOT_GROUPS
from .live_views import DT_PLOT_SERIES
from .live_views import DepthTempLiveView
from .live_views import RecordPlot
from .live_views import dt_plot_row
from .live_views import dt_rows_from_csv
from .logbus import log_system
from .theme import BrandHeader
from .theme import apply_brand_theme
from .theme import apply_window_icon
from datetime import datetime
from sc2_protocol import CSV_HEADER
from sc2_protocol import KELLER_PRESSURE_CHANNELS
from sc2_protocol import KELLER_TEMP_CHANNELS
from sc2_protocol import discover_sc2
from sc2_protocol import record_to_csv
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
import tkinter as tk


# --------------------------------------------------------------------------
# Main GUI Window
# --------------------------------------------------------------------------
class Sc2Panel(tk.Toplevel):
    """Depth, temperature and logger control for the SC2 device profile.

    Deliberately a separate window from ConfigPanel: the two protocols share no opcodes, and
    mixing them in one tab set is how the wrong command gets sent to the wrong device.
    """

    def __init__(self, master, eng):
        """Bind the window to the connected SC2 device and build the header, tabs and output box."""
        super().__init__(master)
        self.engine = eng
        self.dev = eng.active_sc2
        self.title(f"{BRAND['company']} - GiGO Depth & Temp logger")
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        target_w = min(780, int(screen_w * 0.85))
        target_h = min(700, int(screen_h * 0.85))
        pos_x = max(0, (screen_w - target_w) // 2)
        pos_y = max(0, (screen_h - target_h) // 2)
        self.geometry(f"{target_w}x{target_h}+{pos_x}+{pos_y}")
        self.minsize(min(640, screen_w - 40), min(450, screen_h - 60))
        self.configure(bg=BRAND["bg"])
        self.transient(master)
        apply_window_icon(self)
        apply_brand_theme(self)

        BrandHeader(self, subtitle="GiGO Depth & Temp logger").pack(fill=tk.X, side=tk.TOP)

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 8))
        self.nb = nb

        self._build_sensors_tab(nb)
        self._build_live_tab(nb)
        self._build_logger_tab(nb)
        self._build_plot_tab(nb)
        self._build_device_tab(nb)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.out = tk.Text(self, height=9, bg=BRAND["bg"], fg=BRAND["text_dim"],
                           font=("Consolas", 9), bd=0, relief=tk.FLAT,
                           selectbackground=BRAND["accent_dark"],
                           selectforeground=BRAND["accent_text"])
        self.out.pack(fill=tk.BOTH, expand=False, padx=10, pady=(0, 10))
        self._say("Ready. Depth and temperature read from the Keller sensor over 0x10/0x11.")

    # ---- live chart and record plot ---------------------------------------------------------
    def _build_live_tab(self, nb):
        """The device tab's live view: depth / pressure / temperature every few seconds, charted."""
        frame = tk.Frame(nb, bg=BRAND["surface"])
        nb.add(frame, text="Live chart")
        self.live_view = DepthTempLiveView(frame, getattr(self.dev, "session_id", None))
        self.live_view.pack(fill=tk.BOTH, expand=True)
        if self.dev is not None:
            self.live_view.attach()
        else:
            self.live_view.detach()

    def _build_plot_tab(self, nb):
        """Depth and temperature of the downloaded records (or of a saved CSV) against time."""
        frame = tk.Frame(nb, bg=BRAND["surface"])
        nb.add(frame, text="Records plot")
        self.plot_tab = frame
        self.record_plot = RecordPlot(frame, DT_PLOT_SERIES, DT_PLOT_GROUPS, dt_rows_from_csv, invert_left=True,
                                      title="RECORDS")
        self.record_plot.pack(fill=tk.BOTH, expand=True)

    def _close(self):
        """Stop the live chart's polling, close."""
        self.live_view.close()
        self.destroy()

    # ---- helpers -----------------------------------------------------------------------
    def _say(self, text):
        self.out.insert(tk.END, text.rstrip() + "\n")
        self.out.see(tk.END)
        log_system(f"[D&T] {text}", BRAND["info"])

    def _run(self, coro_fn, label):
        """Run coroutine function coro_fn on the BLE thread; any exception is shown as 'label: FAILED'."""
        if self.dev is None:
            messagebox.showwarning("Depth & Temp", "Not connected to a GiGO Depth & Temp logger.")
            return

        async def go():
            """Await coro_fn and report a failure in the output box."""
            try:
                await coro_fn()
            except Exception as exc:
                self._say(f"{label}: FAILED - {exc}")
        run_async(go(), session_id=getattr(self.dev, "session_id", None))

    def _tab(self, nb, title):
        """Add a tab called title and return its frame."""
        f = tk.Frame(nb, bg=BRAND["surface"], padx=12, pady=10)
        nb.add(f, text=title)
        f.grid_columnconfigure(0, weight=1)
        return f

    def _row(self, parent, r, text, cmd, colour=None):
        """Add an action button at row r of a tab."""
        b = tk.Button(parent, text=text, command=cmd, bg=colour or BRAND["accent_dark"],
                      fg=BRAND["accent_text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                      padx=12, pady=5, cursor="hand2", bd=0)
        b.grid(row=r, column=0, sticky="w", padx=4, pady=3)
        return b

    # ---- sensors -----------------------------------------------------------------------
    def _build_sensors_tab(self, nb):
        f = self._tab(nb, "Depth & Temperature")
        r = 0

        tk.Label(f, text="KELLER PRESSURE SENSOR", bg=BRAND["surface"], fg=BRAND["accent"],
                 font=BRAND["font_ui_b"]).grid(row=r, column=0, sticky="w", padx=4)
        r += 1
        tk.Label(f, text="0x10 - gauge pressure in bar; depth as the logger computes it: (P - 0.061) / 0.09807 m.",
                 bg=BRAND["surface"], fg=BRAND["text_muted"],
                 font=("Segoe UI", 8)).grid(row=r, column=0, sticky="w", padx=4)
        r += 1

        self.cmb_press = ttk.Combobox(f, values=[f"{k}  {v}" for k, v in
                                                 KELLER_PRESSURE_CHANNELS.items()],
                                      state="readonly", width=16)
        self.cmb_press.current(0)
        self.cmb_press.grid(row=r, column=0, sticky="w", padx=4, pady=4)
        r += 1
        self._row(f, r, "Read pressure / depth", self._read_pressure)
        r += 1

        tk.Label(f, text="KELLER TEMPERATURE SENSOR", bg=BRAND["surface"], fg=BRAND["accent"],
                 font=BRAND["font_ui_b"]).grid(row=r, column=0, sticky="w", padx=4, pady=(12, 0))
        r += 1
        self.cmb_temp = ttk.Combobox(f, values=[f"{k}  {v}" for k, v in
                                                KELLER_TEMP_CHANNELS.items()],
                                     state="readonly", width=16)
        self.cmb_temp.current(0)
        self.cmb_temp.grid(row=r, column=0, sticky="w", padx=4, pady=4)
        r += 1
        self._row(f, r, "Read temperature", self._read_temperature)
        r += 1

        self._row(f, r, "Read ALL sensors (0x12)", self._read_all, BRAND["accent"])
        r += 1
        self._row(f, r, "Sensor info (0x13)", self._sensor_info)
        r += 1

        self.lbl_sensor = tk.Label(f, text="-", bg=BRAND["surface"], fg=BRAND["ok"],
                                   font=("Consolas", 12, "bold"))
        self.lbl_sensor.grid(row=r, column=0, sticky="w", padx=4, pady=(12, 0))

    def _sel_channel(self, combo):
        """Channel number chosen in combo (its first word), 0 when unreadable."""
        try:
            return int(combo.get().split()[0])
        except Exception:
            return 0

    def _read_pressure(self):
        """Read the pressure of the chosen sensor channel."""
        ch = self._sel_channel(self.cmb_press)

        async def go():
            """Read and show the pressure (or 'no response')."""
            res = await self.dev.read_pressure(ch)
            if not res:
                self._say(f"Pressure ch{ch}: no response")
                return
            if not res.get("success"):
                self._say(f"Pressure ch{res['channel']} ({res['status']}): sensor error code "
                          f"{res.get('keller_code')}")
                return
            txt = (f"Pressure {res['channel']}: {res['pressure_bar']:.4f} bar  "
                   f"({res['pressure_mbar']:.1f} mbar)   depth {res['depth_m']:.2f} m")
            self.after(0, lambda t=txt: self.lbl_sensor.config(text=t))
            self._say(txt)
        self._run(go, "Pressure")

    def _read_temperature(self):
        """Read the temperature of the chosen sensor channel."""
        ch = self._sel_channel(self.cmb_temp)

        async def go():
            """Read and show the temperature (or 'no response')."""
            res = await self.dev.read_temperature(ch)
            if not res:
                self._say(f"Temperature ch{ch}: no response")
                return
            if not res.get("success"):
                self._say(f"Temperature ch{res['channel']} ({res['status']}): sensor error code "
                          f"{res.get('keller_code')}")
                return
            txt = f"Temperature {res['channel']}: {res['temp_c']:.3f} \u00b0C"
            self.after(0, lambda t=txt: self.lbl_sensor.config(text=t))
            self._say(txt)
        self._run(go, "Temperature")

    def _read_all(self):
        """Read all sensor values at once."""
        async def go():
            """Read and show all sensor values."""
            res = await self.dev.read_all_sensors()
            if not res:
                self._say("Read all: no response")
                return
            if not res.get("success") or "valid" not in res:
                self._say(f"Read all ({res.get('status')}): Keller error code {res.get('keller_code')}")
                return

            def show(value, unit, digits):
                return "--" if value is None else f"{value:.{digits}f} {unit}"
            txt = (f"P1 {show(res['p1_bar'], 'bar', 4)} (depth {show(res['depth_m'], 'm', 2)}), "
                   f"P2 {show(res['p2_bar'], 'bar', 4)}, TOB1 {show(res['tob1_c'], 'C', 2)}, "
                   f"TOB2 {show(res['tob2_c'], 'C', 2)}, T {show(res['t_c'], 'C', 2)}, "
                   f"Keller status 0x{res['keller_status']:02X}")
            self.after(0, lambda t=txt: self.lbl_sensor.config(text=t[:60]))
            self._say(txt)
        self._run(go, "Read all")

    def _sensor_info(self):
        """Read the sensor's identity and calibration information."""
        async def go():
            """Read and show the sensor information."""
            res = await self.dev.sensor_info()
            if not res:
                self._say("Sensor info: no response")
                return
            self._say(f"Sensor info raw: {res['raw']}")
            if res.get("ascii"):
                self._say(f"Sensor info text: {res['ascii']}")
        self._run(go, "Sensor info")

    # ---- logger ------------------------------------------------------------------------
    def _build_logger_tab(self, nb):
        f = self._tab(nb, "Logger")
        r = 0

        self._row(f, r, "Start recording (0x20)", self._start)
        r += 1
        self._row(f, r, "Stop recording (0x21)", self._stop, BRAND["err"])
        r += 1

        tk.Label(f, text="Interval 1..3600 s / duration 600..864000 s (endless: recording-mode bit 2)",
                 bg=BRAND["surface"], fg=BRAND["text_dim"],
                 font=("Segoe UI", 8)).grid(row=r, column=0, sticky="w", padx=4, pady=(10, 0))
        r += 1
        rowf = tk.Frame(f, bg=BRAND["surface"])
        rowf.grid(row=r, column=0, sticky="w", padx=4)
        self.ent_interval = tk.Entry(rowf, width=8, bg=BRAND["surface_alt"], fg=BRAND["text"],
                                     insertbackground=BRAND["text"], font=("Consolas", 9),
                                     relief=tk.FLAT, highlightthickness=1,
                                     highlightbackground=BRAND["border"])
        self.ent_interval.insert(0, "60")
        self.ent_interval.pack(side=tk.LEFT)
        self.ent_duration = tk.Entry(rowf, width=10, bg=BRAND["surface_alt"], fg=BRAND["text"],
                                     insertbackground=BRAND["text"], font=("Consolas", 9),
                                     relief=tk.FLAT, highlightthickness=1,
                                     highlightbackground=BRAND["border"])
        self.ent_duration.insert(0, "3600")
        self.ent_duration.pack(side=tk.LEFT, padx=6)
        tk.Button(rowf, text="Set", command=self._set_interval, bg=BRAND["accent_dark"],
                  fg=BRAND["accent_text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=10).pack(side=tk.LEFT)
        tk.Button(rowf, text="Get", command=self._get_interval, bg=BRAND["surface_alt"],
                  fg=BRAND["text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=10).pack(side=tk.LEFT, padx=(6, 0))
        r += 1

        self._row(f, r, "Get RTC (0x25)", self._get_rtc)
        r += 1
        self._row(f, r, "Set RTC to now (0x24)", self._set_rtc)
        r += 1
        self._row(f, r, "Stats (0x26)", self._stats)
        r += 1
        self._row(f, r, "Retrieve record 0 (0x27)", self._retrieve)
        r += 1
        self._row(f, r, "Error log (0x2A)", self._error_log)
        r += 1
        self._row(f, r, "Rec mode get (0x08)", self._rec_get)
        r += 1
        self._row(f, r, "Sanity (0x09)", self._sanity)
        r += 1
        self._row(f, r, "Clear data (0x28)", self._clear, BRAND["warn"])
        r += 1
        self._row(f, r, "Format EEPROM (0x29)", self._format, BRAND["err"])
        r += 1

        tk.Label(f, text="BULK EXPORT", bg=BRAND["surface"], fg=BRAND["accent"],
                 font=BRAND["font_ui_b"]).grid(row=r, column=0, sticky="w", padx=4,
                                               pady=(16, 2))
        r += 1
        tk.Label(f, text="Reads GET_STATS for the record count, then walks RETRIEVE_DATA across "
                         "phases 1-3 and writes a CSV.",
                 bg=BRAND["surface"], fg=BRAND["text_muted"], font=("Segoe UI", 8),
                 wraplength=680, justify=tk.LEFT).grid(row=r, column=0, sticky="w", padx=4)
        r += 1
        bar = tk.Frame(f, bg=BRAND["surface"])
        bar.grid(row=r, column=0, sticky="w", padx=4, pady=(4, 0))
        self.btn_dump = tk.Button(bar, text="Download all records - CSV",
                                  command=self._dump_all, bg=BRAND["accent"],
                                  fg=BRAND["accent_text"], relief=tk.FLAT,
                                  font=BRAND["font_ui_b"], padx=12, pady=5, cursor="hand2")
        self.btn_dump.pack(side=tk.LEFT)
        tk.Button(bar, text="Read advertisement", command=self._read_adv,
                  bg=BRAND["surface_alt"], fg=BRAND["text"], relief=tk.FLAT,
                  font=BRAND["font_ui_b"], padx=12, pady=5).pack(side=tk.LEFT, padx=8)

    # ---- device config (0x05 / 0x06 / 0x07 / 0x08) -------------------------------------
    def _build_device_tab(self, nb):
        f = self._tab(nb, "Device Config")
        r = 0

        tk.Label(f, text="DEVICE CONFIGURATION  (0x06 read / 0x05 write)",
                 bg=BRAND["surface"], fg=BRAND["accent"],
                 font=BRAND["font_ui_b"]).grid(row=r, column=0, sticky="w", padx=4)
        r += 1
        tk.Label(f, text="Name is 16 bytes; voltage thresholds are the battery-percentage "
                         "calibration. Writing triggers an FDS persist and a device reset.",
                 bg=BRAND["surface"], fg=BRAND["text_muted"], font=("Segoe UI", 8),
                 wraplength=680, justify=tk.LEFT).grid(row=r, column=0, sticky="w",
                                                       padx=4, pady=(0, 6))
        r += 1

        def field(label, default=""):
            """Add a labelled entry to the device tab and return it."""
            nonlocal r
            tk.Label(f, text=label, bg=BRAND["surface"], fg=BRAND["text"],
                     font=BRAND["font_ui"]).grid(row=r, column=0, sticky="w", padx=4, pady=2)
            e = tk.Entry(f, width=28, bg=BRAND["surface_alt"], fg=BRAND["text"],
                         insertbackground=BRAND["text"], font=("Consolas", 9),
                         relief=tk.FLAT, highlightthickness=1,
                         highlightbackground=BRAND["border"], highlightcolor=BRAND["accent"])
            e.insert(0, default)
            e.grid(row=r, column=1, sticky="w", padx=4, pady=2)
            r += 1
            return e

        self.ent_sc2_name = field("Advertising name")
        self.cmb_sc2_keller = tk.StringVar(value="(keep)")
        tk.Label(f, text="Keller sensor enable", bg=BRAND["surface"], fg=BRAND["text"],
                 font=BRAND["font_ui"]).grid(row=r, column=0, sticky="w", padx=4, pady=2)
        ttk.Combobox(f, textvariable=self.cmb_sc2_keller, state="readonly", width=25,
                     values=["(keep)", "Enabled", "Disabled"]).grid(row=r, column=1,
                                                                    sticky="w", padx=4)
        r += 1
        self.ent_sc2_flat = field("Battery flat (mV, e.g. 3300)")
        self.ent_sc2_full = field("Battery full (mV, e.g. 4200)")

        bar = tk.Frame(f, bg=BRAND["surface"])
        bar.grid(row=r, column=0, columnspan=2, sticky="w", padx=4, pady=(10, 0))
        tk.Button(bar, text="Read config", command=self._cfg_read, bg=BRAND["accent_dark"],
                  fg=BRAND["accent_text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=12, pady=4).pack(side=tk.LEFT)
        tk.Button(bar, text="Write config", command=self._cfg_write, bg=BRAND["ok"],
                  fg=BRAND["accent_text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=12, pady=4).pack(side=tk.LEFT, padx=8)
        tk.Button(bar, text="Rec mode get", command=self._rec_get, bg=BRAND["surface_alt"],
                  fg=BRAND["text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=12, pady=4).pack(side=tk.LEFT)
        r += 1

        self.ent_rec_mode = field("Recording mode flags 0..7 (bit 0 phase 1, bit 1 phase 3, bit 2 endless)")
        tk.Button(f, text="Rec mode set (0x07)", command=self._rec_set, bg=BRAND["accent_dark"],
                  fg=BRAND["accent_text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=12, pady=4).grid(row=r - 1, column=2, sticky="w", padx=4)

        tk.Label(f, text="MAINTENANCE", bg=BRAND["surface"], fg=BRAND["accent"],
                 font=BRAND["font_ui_b"]).grid(row=r, column=0, sticky="w", padx=4,
                                               pady=(16, 2))
        r += 1
        bar2 = tk.Frame(f, bg=BRAND["surface"])
        bar2.grid(row=r, column=0, columnspan=2, sticky="w", padx=4)
        tk.Button(bar2, text="Sanity test (0x09)", command=self._sanity, bg=BRAND["surface_alt"],
                  fg=BRAND["text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=12, pady=4).pack(side=tk.LEFT)
        tk.Button(bar2, text="Device reset (0x03)", command=self._reset, bg=BRAND["warn"],
                  fg=BRAND["accent_text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=12, pady=4).pack(side=tk.LEFT, padx=8)
        tk.Button(bar2, text="Factory reset (0xFE)", command=self._factory, bg=BRAND["err"],
                  fg=BRAND["accent_text"], relief=tk.FLAT, font=BRAND["font_ui_b"],
                  padx=12, pady=4).pack(side=tk.LEFT)

    def _cfg_read(self):
        """Read the logger's device settings into the device tab."""
        async def go():
            """Read the settings and fill the entries."""
            res = await self.dev.device_config_get()
            if not res:
                self._say("Config read: no response")
                return
            self._say(f"Config: {res}")
            if res.get("name") is not None:
                self.after(0, lambda: (self.ent_sc2_name.delete(0, tk.END),
                                       self.ent_sc2_name.insert(0, res["name"])))
            if res.get("battery_flat_mv"):
                self.after(0, lambda v=res["battery_flat_mv"]: (
                    self.ent_sc2_flat.delete(0, tk.END), self.ent_sc2_flat.insert(0, str(v))))
            if res.get("battery_full_mv"):
                self.after(0, lambda v=res["battery_full_mv"]: (
                    self.ent_sc2_full.delete(0, tk.END), self.ent_sc2_full.insert(0, str(v))))
        self._run(go, "Config read")

    def _cfg_write(self):
        """Write the device settings from the tab ('(keep)' / empty = unchanged); the device resets."""
        name = self.ent_sc2_name.get().strip() or None
        ksel = self.cmb_sc2_keller.get()
        keller = None if ksel.startswith("(keep)") else ksel == "Enabled"
        try:
            flat = int(self.ent_sc2_flat.get().strip() or 0) or None
            full = int(self.ent_sc2_full.get().strip() or 0) or None
        except ValueError:
            messagebox.showerror("Config", "Voltage thresholds must be whole millivolts.")
            return

        async def go():
            """Write the settings and report the result."""
            ok = await self.dev.device_config_set(name=name, keller_enable=keller,
                                                  battery_flat_mv=flat, battery_full_mv=full)
            self._say(f"Config write: {'OK (device will reset)' if ok else 'FAILED'}")
        self._run(go, "Config write")

    def _reset(self):
        """Ask for confirmation, then reset the logger."""
        if not messagebox.askyesno("Device reset", "Reset the device now?"):
            return

        async def go():
            """Send the reset command."""
            ok = await self.dev.device_reset()
            self._say(f"Device reset: {'sent' if ok else 'FAILED'}")
        self._run(go, "Reset")

    def _factory(self):
        """Ask for confirmation, then restore factory defaults (configuration and data are lost)."""
        if not messagebox.askyesno(
                "Factory reset",
                "Restore FACTORY DEFAULTS?\n\nAll configuration and stored data will be lost."):
            return

        async def go():
            """Send the factory-reset command."""
            ok = await self.dev.factory_reset()
            self._say(f"Factory reset: {'OK' if ok else 'FAILED'}")
        self._run(go, "Factory reset")

    def _start(self):
        """Start logging."""
        async def go():
            """Send the start command."""
            ok = await self.dev.start_recording()
            self._say(f"Start recording: {'OK' if ok else 'FAILED'}")
        self._run(go, "Start")

    def _stop(self):
        """Stop logging."""
        async def go():
            """Send the stop command."""
            ok = await self.dev.stop_recording()
            self._say(f"Stop recording: {'OK' if ok else 'FAILED'}")
        self._run(go, "Stop")

    def _set_interval(self):
        """Set the logging interval and duration from the entries (both must be numbers)."""
        try:
            iv = int(self.ent_interval.get().strip() or "0")
            du = int(self.ent_duration.get().strip() or "0")
        except ValueError:
            messagebox.showerror("Interval", "Interval and duration must be integers.")
            return

        async def go():
            """Send the interval and duration."""
            ok = await self.dev.set_interval(iv, du)
            self._say(f"Set interval {iv}s / {du}s: {'OK' if ok else 'FAILED'}")
        self._run(go, "Set interval")

    def _get_interval(self):
        """Read the logging interval and duration."""
        async def go():
            """Read and show the interval and duration."""
            res = await self.dev.get_interval()
            self._say(f"Interval: {res}" if res else "Get interval: no response")
        self._run(go, "Get interval")

    def _get_rtc(self):
        """Read the logger clock."""
        async def go():
            """Read and show the logger clock (epoch and PC local time)."""
            ts = await self.dev.get_rtc()
            if ts is None:
                self._say("RTC: no response")
                return
            self._say(f"RTC epoch {ts} - {datetime.fromtimestamp(ts)}")
        self._run(go, "Get RTC")

    def _set_rtc(self):
        """Set the logger clock to this PC's time."""
        async def go():
            """Send the PC time (Unix epoch seconds)."""
            import time as _t
            ok = await self.dev.set_rtc(int(_t.time()))
            self._say(f"Set RTC: {'OK' if ok else 'FAILED'}")
        self._run(go, "Set RTC")

    def _stats(self):
        """Read the record counters."""
        async def go():
            """Read and show the record counters."""
            res = await self.dev.get_stats()
            self._say(f"Stats: {res}" if res else "Stats: no response")
        self._run(go, "Stats")

    def _retrieve(self):
        """Read the first record of phase 1 (a quick check that records can be read)."""
        async def go():
            """Read and show one record."""
            res = await self.dev.retrieve(1, 0)
            self._say(f"Record: {res['raw']}" if res else "Retrieve: no record")
        self._run(go, "Retrieve")

    def _error_log(self):
        """Read the logger's error log."""
        async def go():
            """Read and show the error log."""
            res = await self.dev.get_error_log()
            if not res:
                self._say("Error log: no response")
                return
            counters = ", ".join(f"{name} {value}" for name, value in (res.get("counters") or {}).items())
            self._say(f"Error log ({res['status']}): {counters or '-'}; flags {res.get('current_flags')}")
        self._run(go, "Error log")

    def _rec_get(self):
        """Read the recording-mode flags."""
        async def go():
            """Read and show the recording-mode flags."""
            flags = await self.dev.rec_mode_get()
            self._say(f"Rec mode flags: {flags if flags is not None else 'no response'}")
        self._run(go, "Rec mode")

    def _rec_set(self):
        """Write the recording-mode flags from the entry (0..7)."""
        try:
            flags = int(self.ent_rec_mode.get().strip(), 0)
        except ValueError:
            messagebox.showerror("Recording mode", "Enter a number 0..7.")
            return
        if not 0 <= flags <= 7:
            messagebox.showerror("Recording mode", "Flags must be 0..7: only bits 0..2 have a meaning (the logger stores "
                                                   "any value without a check, then saves and resets).")
            return

        async def go():
            """Send the recording-mode flags."""
            ok = await self.dev.rec_mode_set(flags)
            self._say(f"Rec mode 0x{flags:02X}: {'OK' if ok else 'REFUSED'}")
        self._run(go, "Rec mode set")

    def _sanity(self):
        """Read the logger's sanity-check result."""
        async def go():
            """Read and show the sanity-check result."""
            res = await self.dev.get_sanity()
            self._say(f"Sanity: {res}" if res else "Sanity: no response")
        self._run(go, "Sanity")

    def _clear(self):
        """Ask for confirmation, then erase all stored records."""
        if not messagebox.askyesno("Clear data", "Erase all stored records?"):
            return

        async def go():
            """Send the clear-data command for all phases."""
            ok = await self.dev.clear_data(0xFF)
            self._say(f"Clear data: {'OK' if ok else 'FAILED'}")
        self._run(go, "Clear")

    def _dump_all(self):
        """Walk every record across phases 1-3 and write a CSV.

        Reaches parity with Valtrack's waypoint download: the SC2 device stores records locally
        rather than uploading them, so the tool has to pull them.
        """
        if not messagebox.askyesno(
                "Download all records",
                "Read every stored record and save them to a CSV file?\n\n"
                "A large log can take a while - the device is polled once per record."):
            return

        async def go():
            """Read the record counts, read every record of phases 1-3, then save them as a CSV file."""
            stats = await self.dev.get_stats()
            if not stats or stats.get("status") != "SUCCESS":
                self._say(f"Cannot read stats: {stats}")
                return

            total = stats.get("total_records", 0)
            phase_counts = {1: stats.get("phase1", 0), 2: stats.get("phase2", 0),
                            3: stats.get("phase3", 0)}
            self._say(f"Records: {total} total - phase1={phase_counts[1]} "
                      f"phase2={phase_counts[2]} phase3={phase_counts[3]}")
            if not total:
                self._say("Nothing to download.")
                return

            rows, failures, crc_bad = [], 0, 0
            done = 0
            for phase, count in phase_counts.items():
                for idx in range(count):
                    rec = await self.dev.retrieve(phase, idx)
                    done += 1
                    if done % 25 == 0:
                        self.after(0, lambda d=done, t=total: self.btn_dump.config(
                            text=f"Downloading- {d}/{t}"))
                    if not rec or "epoch" not in rec:
                        failures += 1
                        continue
                    if not rec.get("crc8_ok"):
                        crc_bad += 1
                    rows.append(rec)

            self.after(0, lambda: self.btn_dump.config(text="Download all records - CSV"))

            if not rows:
                self._say(f"No records read ({failures} retrieval failures).")
                return
            if failures:
                self._say(f"  {failures} record(s) could not be read")
            if crc_bad:
                self._say(f"  {crc_bad} record(s) failed CRC-8 check - possible flash corruption")
            # Dialogs and widgets belong to the Tk thread.
            self.after(0, lambda: self._save_and_plot(rows))
        self._run(go, "Download all")

    def _save_and_plot(self, records):
        """Plot the downloaded records, then offer to save them as CSV (Tk thread)."""
        self.record_plot.set_rows(sorted((dt_plot_row(rec) for rec in records), key=lambda row: row["epoch"]),
                                  "downloaded")
        self.nb.select(self.plot_tab)
        path = filedialog.asksaveasfilename(
            parent=self, title="Save Depth & Temp records", defaultextension=".csv",
            initialfile=f"depth_temp_records_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if not path:
            self._say(f"Read {len(records)} records (plotted) but no file was chosen.")
            return
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(CSV_HEADER + "\n")
            fh.write("\n".join(record_to_csv(rec) for rec in records) + "\n")
        self._say(f"Saved {len(records)} records - {path}")

    def _read_adv(self):
        """Read FW version / battery / charging straight from the advertisement (no connection)."""
        async def go():
            """Scan for SC2 advertisements and show firmware version, battery and charging state."""
            found = await discover_sc2(timeout=6.0)
            if not found:
                self._say("No SC2 advertisement seen.")
                return
            for name, addr, rssi, info in found:
                if info:
                    self._say(f"{name} [{addr}] rssi={rssi}  FW {info['fw_version']}  "
                              f"battery {info['battery_pct']}%  {info['charging_text']}")
                else:
                    self._say(f"{name} [{addr}] rssi={rssi}  (no manufacturer payload)")
        self._run(go, "Advertisement")

    def _format(self):
        """Ask for confirmation, then format the logger's EEPROM (all data is lost)."""
        if not messagebox.askyesno("Format EEPROM",
                                   "Format the entire EEPROM?\n\nAll stored data is lost."):
            return

        async def go():
            """Send the format command."""
            ok = await self.dev.format_eeprom()
            self._say(f"Format: {'OK' if ok else 'FAILED'}")
        self._run(go, "Format")
