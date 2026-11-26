"""@file gas_panel.py
@brief Window of the GiGO Gas logger (BME690, "Succorfish Gas"): live air values, logging set-up, record download
@version 1.1
@date 2026-11-22

Bound to the connected logger (session.sc2, a GasLoggerDevice). Tabs: status (device information 0x34, battery 0x04,
self test 0x09, error log 0x2A, clock 0x25 / 0x24), live values (the device tab's GasLiveView: 0x30 read, shared 0x31
stream, IAQ band, chart, CSV), logging (interval 0x23 / 0x22, recording mode 0x08 / 0x07, start 0x20, stop 0x21,
statistics 0x26, log status 0x35), records (download by sequence 0x36 to CSV, acknowledge 0x37 only after the file is
written, plot of the records or of a saved CSV, clear 0x28, format 0x29) and maintenance (reset 0x03, defaults 0xFE).
Ranges are the firmware's (env_logger.h): interval 1..3600 s, duration 600..864000 s.
"""
from . import gas_protocol as gas
from .ble_engine import run_async
from .config_codecs import ConfigValueError
from .config_codecs import FieldSpec
from .config_codecs import format_seconds
from .config_codecs import parse_number
from .constants import BRAND
from .gas_device import gas_record_csv_header
from .gas_device import gas_record_csv_row
from .live_views import GAS_PLOT_GROUPS
from .live_views import GAS_PLOT_SERIES
from .live_views import GasLiveView
from .live_views import RecordPlot
from .live_views import gas_plot_row
from .live_views import gas_rows_from_csv
from .logbus import log_system
from .theme import BrandHeader
from .theme import apply_brand_theme
from .theme import apply_window_icon
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
import time
import tkinter as tk


WINDOW_WIDTH = 940
WINDOW_HEIGHT = 760
INTERVAL_FIELD = FieldSpec("interval", "Logging interval", "s", gas.INTERVAL_MIN_S, gas.INTERVAL_MAX_S,
                           "One record per interval")
DURATION_FIELD = FieldSpec("duration", "Logging duration", "s", gas.DURATION_MIN_S, gas.DURATION_MAX_S,
                           "Ignored when 'infinite' is set in the recording mode")
REC_MODE_BITS = ((0x01, "Phase 1"), (0x02, "Phase 3"), (gas.REC_MODE_INFINITE, "Infinite duration"))


class GasPanel(tk.Toplevel):
  """Window of one connected GiGO Gas logger."""

  def __init__(self, master, engine):
    """Bind to the selected session's logger and build the tabs."""
    super().__init__(master)
    self.engine = engine
    self.dev = engine.active_sc2
    self.session_id = getattr(self.dev, "session_id", None)
    self.title(f"{BRAND['company']} GiGO Gas logger")
    self.geometry(f"{min(WINDOW_WIDTH, self.winfo_screenwidth() - 40)}x"
                  f"{min(WINDOW_HEIGHT, self.winfo_screenheight() - 60)}")
    self.configure(bg=BRAND["bg"])
    self.transient(master)
    apply_window_icon(self)
    apply_brand_theme(self)
    BrandHeader(self, subtitle="GiGO Gas logger (BME690)").pack(fill=tk.X, side=tk.TOP)

    self._entries = {}
    self._mode_vars = {}

    notebook = ttk.Notebook(self)
    notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 4))
    self._build_status_tab(notebook)
    self._build_live_tab(notebook)
    self._build_logging_tab(notebook)
    self._build_records_tab(notebook)

    self.txt_out = tk.Text(self, height=8, bg=BRAND["surface_alt"], fg=BRAND["text"], font=BRAND["font_mono"],
                           relief=tk.FLAT, wrap=tk.WORD)
    self.txt_out.pack(fill=tk.X, padx=10, pady=(0, 10))
    self.protocol("WM_DELETE_WINDOW", self._close)

  # ---- helpers -----------------------------------------------------------------------------------------------------
  def _tab(self, notebook, title):
    """Add a tab and return its frame."""
    frame = tk.Frame(notebook, bg=BRAND["surface"], padx=12, pady=10)
    notebook.add(frame, text=title)
    frame.grid_columnconfigure(2, weight=1)
    return frame

  def _bar(self, parent, row, buttons):
    """A row of (text, command, colour) buttons."""
    bar = tk.Frame(parent, bg=BRAND["surface"])
    bar.grid(row=row, column=0, columnspan=3, sticky="w", padx=6, pady=4)
    for text, command, colour in buttons:
      tk.Button(bar, text=text, command=command, bg=colour or BRAND["accent_dark"], fg="white", relief=tk.FLAT,
                padx=10, pady=3, font=BRAND["font_ui_b"]).pack(side=tk.LEFT, padx=(0, 6))
    return bar

  def _say(self, text, colour=None):
    """Append text to the output box (from any thread) and to the device log."""
    def update():
      if self.winfo_exists():
        self.txt_out.insert(tk.END, text.rstrip() + "\n")
        self.txt_out.see(tk.END)
    self.after(0, update)
    log_system(f"[GiGO Gas] {text}", colour or BRAND["info"], session_id=self.session_id)

  def _run(self, coroutine_function, what):
    """Run a coroutine on the BLE thread; errors are reported, never swallowed."""
    if self.dev is None:
      messagebox.showwarning("Not connected", "Connect to a GiGO Gas logger first.", parent=self)
      return

    async def guarded():
      try:
        await coroutine_function(self.dev)
      except Exception as error:
        self._say(f"{what} failed: {error}", BRAND["err"])
    run_async(guarded(), session_id=self.session_id)

  def _close(self):
    """Leave the live stream (it keeps running while the device tab uses it), close."""
    self.live_view.close()
    self.destroy()

  # ---- status ------------------------------------------------------------------------------------------------------
  def _build_status_tab(self, notebook):
    """Device information, battery, self test, error log and clock."""
    frame = self._tab(notebook, "Status")
    self._bar(frame, 0, (("Device info (0x34)", self._info, None), ("Battery (0x04)", self._battery, None),
                         ("Self test (0x09)", self._sanity, None), ("Error log (0x2A)", self._error_log, None)))
    self._bar(frame, 1, (("Read clock (0x25)", self._read_clock, None),
                         ("Set clock to PC UTC (0x24)", self._sync_clock, BRAND["ok"])))
    self._bar(frame, 2, (("Reset (0x03)", self._reset, BRAND["err"]),
                         ("Factory defaults (0xFE)", self._defaults, BRAND["err"])))
    tk.Label(frame, text="The clock must be set before logging can start (the logger refuses times before 2020).",
             bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 8)).grid(row=3, column=0, columnspan=3,
                                                                                    sticky="w", padx=6, pady=6)

  def _info(self):
    """Show the decoded device information."""
    async def go(device):
      info = await device.get_info()
      if not info or "device_type" not in info:
        self._say(f"Device info: {info or 'no answer'}", BRAND["warn"])
        return
      text = (f"{info['device_name']} firmware {info['firmware']}, protocol {info['protocol_version']}, record "
              f"format {info['record_format']} ({info['record_size']} B), {info['eeprom_chips']} EEPROM chip(s) "
              f"{info['eeprom_bytes']} B; {info['flags_text']}")
      if "bsec_version" in info:
        text += (f"; BSEC {info['bsec_version']} every {info['bsec_period_s']} s; reset reason "
                 f"{gas.describe_flags(info['reset_reason'], gas.RESET_REASON_NAMES)}; stack never used "
                 f"{info['stack_unused_bytes']} B")
      self._say(text, BRAND["ok"])
    self._run(go, "Device info")

  def _battery(self):
    """Battery percent (the logger stays silent when its ADC read fails)."""
    async def go(device):
      percent = await device.get_battery()
      self._say(f"Battery: {percent} %" if percent is not None else "Battery: no answer (ADC read failed?)",
                BRAND["ok"] if percent is not None else BRAND["warn"])
    self._run(go, "Battery")

  def _sanity(self):
    """Run the self test."""
    async def go(device):
      result = await device.gas_sanity()
      if result is None:
        self._say("Self test: no answer", BRAND["warn"])
        return
      errors = ", ".join(result["errors"]) or "none"
      self._say(f"Self test {'PASSED' if result['passed'] else 'FAILED'}: errors {errors}; {result['i2c_count']} I2C "
                f"device(s) (bitmap 0x{result['i2c_bitmap']:04X}); battery {result['battery_mv']} mV",
                BRAND["ok"] if result["passed"] else BRAND["err"])
    self._run(go, "Self test")

  def _error_log(self):
    """Show the saturating error counters."""
    async def go(device):
      log = await device.gas_error_log()
      if not log or "error_flags" not in log:
        self._say(f"Error log: {log or 'no answer'}", BRAND["warn"])
        return
      counters = ", ".join(f"{key} {value}" for key, value in log.items()
                           if key not in ("status", "error_flags", "error_flags_text"))
      self._say(f"Error log: {counters}; flags: {log['error_flags_text']}", BRAND["info"])
    self._run(go, "Error log")

  def _read_clock(self):
    """Compare the logger clock with the PC clock."""
    async def go(device):
      epoch = await device.get_rtc()
      if epoch is None:
        self._say("Clock: no answer", BRAND["warn"])
        return
      offset = epoch - int(time.time())
      self._say(f"Logger clock {gas.format_epoch(epoch)} ({offset:+d} s against the PC)",
                BRAND["ok"] if abs(offset) <= 2 else BRAND["warn"])
    self._run(go, "Clock read")

  def _sync_clock(self):
    """Set the logger clock to the PC's UTC time."""
    async def go(device):
      ok = await device.set_rtc(int(time.time()))
      self._say(f"Clock set to PC UTC: {'OK' if ok else 'REFUSED'}", BRAND["ok"] if ok else BRAND["err"])
    self._run(go, "Clock set")

  def _reset(self):
    """Ask, then reset the logger."""
    if messagebox.askyesno("Reset", "Reset the gas logger now?", parent=self):
      async def go(device):
        await device.device_reset()
        self._say("Reset sent", BRAND["ok"])
      self._run(go, "Reset")

  def _defaults(self):
    """Ask, then restore the factory configuration."""
    if messagebox.askyesno("Factory defaults", "Restore the factory configuration of the gas logger? Interval, "
                           "duration and recording mode are lost.", icon="warning", parent=self):
      async def go(device):
        ok = await device.factory_reset()
        self._say(f"Factory defaults: {'done, the logger resets' if ok else 'REFUSED'}",
                  BRAND["ok"] if ok else BRAND["err"])
      self._run(go, "Factory defaults")

  # ---- live values -------------------------------------------------------------------------------------------------
  def _build_live_tab(self, notebook):
    """Live air values: the same view as in the device tab (one read, or the shared stream: a frame per new sample,
    about every 3 s), with the IAQ band and a chart.
    """
    frame = tk.Frame(notebook, bg=BRAND["surface"])
    notebook.add(frame, text="Live values")
    self.live_view = GasLiveView(frame, self.session_id)
    self.live_view.pack(fill=tk.BOTH, expand=True)
    if self.dev is not None:
      self.live_view.attach()
    else:
      self.live_view.detach()

  # ---- logging -----------------------------------------------------------------------------------------------------
  def _build_logging_tab(self, notebook):
    """Interval, duration, recording mode, start / stop and the logger state."""
    frame = self._tab(notebook, "Logging")
    for row, spec in enumerate((INTERVAL_FIELD, DURATION_FIELD)):
      tk.Label(frame, text=f"{spec.label} ({spec.unit})", bg=BRAND["surface"], fg=BRAND["text"],
               font=BRAND["font_ui"]).grid(row=row, column=0, sticky="w", padx=6, pady=3)
      entry = tk.Entry(frame, width=14, bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground=BRAND["text"],
                       font=BRAND["font_mono"], relief=tk.FLAT)
      entry.grid(row=row, column=1, sticky="w", padx=6, pady=3)
      tk.Label(frame, text=f"{spec.range_text()} ({spec.hint}); 15m, 2h, 1d accepted", bg=BRAND["surface"],
               fg=BRAND["text_muted"], font=("Segoe UI", 8)).grid(row=row, column=2, sticky="w", padx=6)
      self._entries[spec.key] = entry
    modes = tk.Frame(frame, bg=BRAND["surface"])
    modes.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=4)
    tk.Label(modes, text="Recording mode:", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_ui"]).pack(
      side=tk.LEFT)
    for bit, text in REC_MODE_BITS:
      variable = tk.IntVar(value=0)
      tk.Checkbutton(modes, text=text, variable=variable, bg=BRAND["surface"], fg=BRAND["text"],
                     selectcolor=BRAND["surface_alt"], activebackground=BRAND["surface"]).pack(side=tk.LEFT, padx=6)
      self._mode_vars[bit] = variable
    self._bar(frame, 3, (("Read settings", self._read_settings, None),
                         ("Write settings", self._write_settings, BRAND["ok"])))
    self._bar(frame, 4, (("Start logging (0x20)", self._start, BRAND["ok"]), ("Stop logging (0x21)", self._stop,
                                                                              BRAND["err"]),
                         ("Statistics (0x26)", self._stats, None), ("Log status (0x35)", self._log_status, None)))
    tk.Label(frame, text="Start is refused (NOT_READY) while the clock is not set or the storage is not ready.",
             bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 8)).grid(row=5, column=0, columnspan=3,
                                                                                    sticky="w", padx=6)

  def _read_settings(self):
    """Read interval, duration and recording mode."""
    async def go(device):
      interval = await device.get_interval()
      mode = await device.rec_mode_get()

      def update():
        if interval:
          for key, value in (("interval", interval.get("interval_s")), ("duration", interval.get("duration_s"))):
            self._entries[key].delete(0, tk.END)
            self._entries[key].insert(0, "" if value is None else str(value))
        if mode is not None:
          for bit, variable in self._mode_vars.items():
            variable.set(1 if mode & bit else 0)
      self.after(0, update)
      self._say(f"Settings: {interval}, recording mode 0x{mode:02X}" if mode is not None else f"Settings: {interval}",
                BRAND["ok"])
    self._run(go, "Settings read")

  def _write_settings(self):
    """Validate and write interval, duration and recording mode."""
    try:
      interval = parse_number(self._entries["interval"].get(), INTERVAL_FIELD)
      duration = parse_number(self._entries["duration"].get(), DURATION_FIELD)
    except ConfigValueError as error:
      messagebox.showerror("Invalid value", str(error), parent=self)
      return
    if interval is None or duration is None:
      messagebox.showinfo("Settings", "Enter both the interval and the duration (read them first).", parent=self)
      return
    mode = sum(bit for bit, variable in self._mode_vars.items() if variable.get())

    async def go(device):
      ok_interval = await device.set_interval(interval, duration)
      ok_mode = await device.rec_mode_set(mode)
      self._say(f"Interval {format_seconds(interval)}, duration {format_seconds(duration)}: "
                f"{'OK' if ok_interval else 'REFUSED'}; recording mode 0x{mode:02X}: {'OK' if ok_mode else 'REFUSED'}",
                BRAND["ok"] if ok_interval and ok_mode else BRAND["err"])
    self._run(go, "Settings write")

  def _start(self):
    """Start logging."""
    async def go(device):
      ok = await device.start_recording()
      self._say(f"Start logging: {'OK' if ok else 'REFUSED (' + device.status_text(device.last_status) + ')'}",
                BRAND["ok"] if ok else BRAND["err"])
    self._run(go, "Start")

  def _stop(self):
    """Stop logging."""
    async def go(device):
      ok = await device.stop_recording()
      self._say(f"Stop logging: {'OK' if ok else 'REFUSED'}", BRAND["ok"] if ok else BRAND["err"])
    self._run(go, "Stop")

  def _stats(self):
    """Show the logger statistics."""
    async def go(device):
      stats = await device.gas_stats()
      if not stats or "total" not in stats:
        self._say(f"Statistics: {stats or 'no answer'}", BRAND["warn"])
        return
      self._say(f"Statistics: {stats['total']} records (phase 1 {stats['phase1']}, 2 {stats['phase2']}, 3 "
                f"{stats['phase3']}), {stats['free_bytes']} B free, {'recording' if stats['recording'] else 'idle'} "
                f"({stats['phase_text']}), {stats['error_events']} error events, flags {stats['error_flags_text']}",
                BRAND["info"])
    self._run(go, "Statistics")

  def _log_status(self):
    """Show the record store status (sequence numbers)."""
    async def go(device):
      log = await device.log_status()
      if not log or "count" not in log:
        self._say(f"Log status: {log or 'no answer'}", BRAND["warn"])
        return
      self._say(f"Log: {log['count']} records, sequence {log['first_sequence']}..{log['next_sequence'] - 1}, "
                f"capacity {log['capacity']}, {log['overwritten']} overwritten, newest "
                f"{gas.format_epoch(log['newest_epoch'])}, interval {log['interval_s']} s", BRAND["info"])
    self._run(go, "Log status")

  # ---- records -----------------------------------------------------------------------------------------------------
  def _build_records_tab(self, notebook):
    """Download to CSV (acknowledge afterwards only when asked), clear and format."""
    frame = self._tab(notebook, "Records")
    self.var_ack = tk.IntVar(value=0)
    tk.Checkbutton(frame, text="After the CSV file is written, acknowledge the records (0x37): the logger may then "
                               "delete them", variable=self.var_ack, bg=BRAND["surface"], fg=BRAND["text"],
                   selectcolor=BRAND["surface_alt"], activebackground=BRAND["surface"]).grid(
      row=0, column=0, columnspan=3, sticky="w", padx=6, pady=4)
    self._bar(frame, 1, (("Download records to CSV (0x36)", self._download, BRAND["ok"]),))
    self.progress = ttk.Progressbar(frame, orient="horizontal", length=420, mode="determinate")
    self.progress.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=6)
    tk.Label(frame, text="Destructive (save a CSV first):", bg=BRAND["surface"], fg=BRAND["err"],
             font=BRAND["font_ui_b"]).grid(row=3, column=0, columnspan=3, sticky="w", padx=6, pady=(12, 2))
    self._bar(frame, 4, (("Clear all records (0x28)", self._clear, BRAND["err"]),
                         ("Format storage (0x29)", self._format, BRAND["err"])))
    frame.grid_rowconfigure(5, weight=1)
    self.record_plot = RecordPlot(frame, GAS_PLOT_SERIES, GAS_PLOT_GROUPS, gas_rows_from_csv, title="RECORD PLOT")
    self.record_plot.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(10, 0))

  def _download(self):
    """Read every record, write the CSV, then acknowledge if asked (never before the file is safely written)."""
    path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                        initialfile=f"gas_records_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    if not path:
      return
    acknowledge = bool(self.var_ack.get())

    def progress(done, total):
      self.after(0, lambda: self.progress.configure(value=100.0 * done / max(total, 1)))

    async def go(device):
      rows, next_sequence = await device.download_all(progress)
      if not rows:
        self._say("Download: no records", BRAND["warn"])
        return
      with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(gas_record_csv_header() + "\n")
        for sequence, record in rows:
          handle.write(gas_record_csv_row(sequence, record) + "\n")
      bad = sum(1 for _sequence, record in rows if isinstance(record, str))
      self._say(f"Download: {len(rows)} records written to {path} ({bad} with errors)",
                BRAND["ok"] if not bad else BRAND["warn"])
      plot_rows = [gas_plot_row(record) for _sequence, record in rows if not isinstance(record, str)]
      self.after(0, lambda: self.record_plot.set_rows(plot_rows, "downloaded"))
      if acknowledge and next_sequence is not None and not bad:
        result = await device.ack_records(next_sequence)
        self._say(f"Acknowledged up to sequence {next_sequence}: {result}", BRAND["info"])
      elif acknowledge and bad:
        self._say("Not acknowledged: some records had errors, they stay on the logger", BRAND["warn"])
    self._run(go, "Download")

  def _clear(self):
    """Ask, then clear all records."""
    if messagebox.askyesno("Clear records", "Delete ALL records on the gas logger?", icon="warning", parent=self):
      async def go(device):
        ok = await device.clear_data(gas.PHASE_ALL)
        self._say(f"Clear: {'OK' if ok else 'REFUSED'}", BRAND["ok"] if ok else BRAND["err"])
      self._run(go, "Clear")

  def _format(self):
    """Ask, then format the storage."""
    if messagebox.askyesno("Format storage", "Format the whole record storage? Every record is lost.",
                           icon="warning", parent=self):
      async def go(device):
        ok = await device.format_eeprom()
        self._say(f"Format: {'OK' if ok else 'REFUSED'}", BRAND["ok"] if ok else BRAND["err"])
      self._run(go, "Format")
