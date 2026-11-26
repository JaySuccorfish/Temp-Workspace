"""@file gigo_family_panel.py
@brief Configuration window of the GiGO nRF devices on service 0xAB00: Delphis (MyGearTag) and Rotation / Jack beacon
@version 1.2
@date 2026-11-22

One window per device, bound to its session like the Valtrack and SC2 windows. Tabs: configuration (0x06 read, 0x05
write: name, two flags, battery range), status and actions (battery 0x04, self test 0x09, IMU calibration 0x0C, reset
0x03, defaults 0x07), IMU (live stream 0xB1/0xB2, ranges 0xB5/0xB4, magnetometer 0xB6, recalibration 0xB3, register
access 0xB0), on Delphis the acoustic modem (messages, ping / range, pictures: acoustic_panel.py) and on the beacon
movement detection (mode 0xB8/0xB7, tuning 0xBA/0xB9). A configuration read also shows the device's role (what its
flags switch on: acoustic modem, IMU, rotation / jack, or BLE advertising only). Layouts and ranges come from config_codecs.py / gigo_imu_codecs.py. Every configuration write makes
the device save and reset, and the flags have no 'keep' value, so a write is only allowed after a read.
"""
from .ble_engine import BleEngine
from .ble_engine import SessionView
from .ble_engine import run_async
from .config_codecs import BEACON_IMU_MODES
from .config_codecs import BEACON_TUNING_FIELDS
from .config_codecs import ConfigValueError
from .config_codecs import gigo_config_fields
from .config_codecs import parse_number
from .constants import BRAND
from .acoustic_panel import AcousticMessagesTab
from .acoustic_panel import ImageTransferTab
from .device_profiles import gigo_roles
from .device_profiles import profile_label
from .device_profiles import roles_text
from .gigo_imu_codecs import IMU_ACC_SCALES
from .gigo_imu_codecs import IMU_GYRO_SCALES
from .gigo_imu_codecs import IMU_REGISTER_MAX_READ
from .gigo_imu_codecs import IMU_STREAM_KEYS
from .gigo_imu_codecs import decode_imu_stream
from .logbus import log_system
from .theme import BrandHeader
from .theme import apply_brand_theme
from .theme import apply_window_icon
from tkinter import messagebox
from tkinter import ttk
import time
import tkinter as tk


WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 660
SANITY_TESTS = {1: "1 - all (IMU and nano-modem)", 2: "2 - nano-modem only", 3: "3 - IMU only"}
SANITY_ERRORS = {0x00: "no error", 0x01: "IMU and nano-modem failed", 0x02: "nano-modem failed / not fitted",
                 0x03: "IMU failed"}
CHARGING_TEXT = {0: "not charging", 1: "charging", 2: "full", 3: "fault"}
IMU_GROUPS = (("Acceleration (g)", ("acc_x", "acc_y", "acc_z")), ("Angular rate (deg/s)", ("gyro_x", "gyro_y", "gyro_z")),
              ("Magnetometer (uT)", ("mag_x", "mag_y", "mag_z")), ("Roll / pitch / yaw (deg)", ("roll", "pitch", "yaw")))


class GigoFamilyPanel(tk.Toplevel):
  """Configuration window of one GiGO Delphis or Rotation / Jack beacon."""

  def __init__(self, master, engine: "BleEngine", target_address: "str | None" = None,
               session_id: "str | None" = None):
    """Bind to the exact device session and build the tabs that the device's family supports."""
    super().__init__(master)
    explicit_sid = (session_id or target_address or "").lower().strip() or None
    self.session_id = explicit_sid or engine.selected_session_id
    self.engine = SessionView(engine, self.session_id)
    session = self.engine.selected_session
    self.profile = getattr(session, "profile", "gigo_delphis") or "gigo_delphis"
    self.label = profile_label(self.profile)
    addr_tag = f" [{session.address}]" if session and getattr(session, "address", None) else (
        f" [{target_address}]" if target_address else ""
    )
    self.title(f"{BRAND['company']} {self.label} - Configuration{addr_tag}")
    self.geometry(f"{min(WINDOW_WIDTH, self.winfo_screenwidth() - 40)}x"
                  f"{min(WINDOW_HEIGHT, self.winfo_screenheight() - 60)}")
    self.configure(bg=BRAND["bg"])
    self.transient(master)
    apply_window_icon(self)
    apply_brand_theme(self)

    self._entries = {}
    self._current_config = {}
    self._config_specs = gigo_config_fields(self.profile)
    self._imu_labels = {}
    self._imu_samples = 0
    self._imu_streaming = False

    header = BrandHeader(self, subtitle=f"{self.label} - Config")
    header.pack(fill=tk.X, side=tk.TOP)
    header.set_link(bool(self.engine.active_client and self.engine.active_client.is_connected),
                    getattr(session, "name", "") or "")

    notebook = ttk.Notebook(self)
    notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 4))
    self._build_config_tab(notebook)
    self._build_actions_tab(notebook)
    self._build_imu_tab(notebook)
    if self.profile == "gigo_beacon":
      self._build_movement_tab(notebook)
    else:
      self._build_acoustic_tabs(notebook)

    self.txt_out = tk.Text(self, height=6, bg=BRAND["surface_alt"], fg=BRAND["text"], font=BRAND["font_mono"],
                           relief=tk.FLAT, wrap=tk.WORD)
    self.txt_out.pack(fill=tk.X, padx=10, pady=(0, 10))
    self.protocol("WM_DELETE_WINDOW", self._close)
    self._attach_listeners()

  # ---- helpers -----------------------------------------------------------------------------------------------------
  def _tab(self, notebook, title):
    """Add a tab and return its frame."""
    frame = tk.Frame(notebook, bg=BRAND["surface"], padx=12, pady=10)
    notebook.add(frame, text=title)
    frame.grid_columnconfigure(2, weight=1)
    return frame

  def _row(self, parent, row, spec, width=18):
    """Label, entry and range note for one field; the entry is registered under spec.key."""
    label = f"{spec.label} ({spec.unit})" if spec.unit else spec.label
    tk.Label(parent, text=label, bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_ui"],
             anchor="w").grid(row=row, column=0, sticky="w", padx=6, pady=3)
    entry = tk.Entry(parent, width=width, bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground=BRAND["text"],
                     font=BRAND["font_mono"], relief=tk.FLAT, highlightthickness=1,
                     highlightbackground=BRAND["border"], highlightcolor=BRAND["accent"])
    entry.grid(row=row, column=1, sticky="w", padx=6, pady=3)
    note = spec.hint or (spec.range_text() if spec.key != "name" else "")
    tk.Label(parent, text=note, bg=BRAND["surface"], fg=BRAND["text_muted"], font=("Segoe UI", 8),
             anchor="w", wraplength=380, justify=tk.LEFT).grid(row=row, column=2, sticky="w", padx=6)
    self._entries[spec.key] = entry
    return entry

  def _button(self, parent, text, command, colour=None):
    """Brand-styled button packed to the left of parent."""
    button = tk.Button(parent, text=text, command=command, bg=colour or BRAND["accent_dark"], fg="white",
                       relief=tk.FLAT, padx=12, pady=3, font=BRAND["font_ui_b"])
    button.pack(side=tk.LEFT, padx=(0, 6))
    return button

  def _say(self, text, colour=None):
    """Show text in the output box (from any thread) and in the device log."""
    def update():
      self.txt_out.delete("1.0", tk.END)
      self.txt_out.insert(tk.END, text)
    self.after(0, update)
    log_system(f"[{self.label}] {text}", colour or BRAND["info"], session_id=self.session_id)

  def _set_entry(self, key, value):
    """Fill one entry (thread-safe)."""
    def update():
      entry = self._entries.get(key)
      if entry is not None:
        entry.delete(0, tk.END)
        entry.insert(0, "" if value is None else str(value))
    self.after(0, update)

  def _run(self, coroutine_function, what):
    """Run a coroutine on the BLE thread for this window's device; errors are shown, never swallowed."""
    device = self.engine.active_device
    if device is None or not self.engine.active_client or not self.engine.active_client.is_connected:
      messagebox.showwarning("Not connected", f"Connect to the {self.label} first.", parent=self)
      return

    async def guarded():
      try:
        await coroutine_function(device)
      except ConfigValueError as error:
        message = str(error)
        self.after(0, lambda: messagebox.showerror("Invalid value", message, parent=self))
      except Exception as error:
        self._say(f"{what} failed: {error}", BRAND["err"])
    run_async(guarded(), session_id=self.session_id)

  # ---- configuration ----------------------------------------------------------------------------------------------
  def _build_config_tab(self, notebook):
    """Name, flags and battery range (0x06 / 0x05)."""
    frame = self._tab(notebook, "Configuration (0x06 / 0x05)")
    for row, spec in enumerate(self._config_specs):
      self._row(frame, row, spec, width=24 if spec.key == "name" else 12)
    bar = tk.Frame(frame, bg=BRAND["surface"])
    bar.grid(row=len(self._config_specs), column=0, columnspan=3, sticky="w", padx=6, pady=10)
    self._button(bar, "Read", self._read_config)
    self._button(bar, "Write (device saves and resets)", self._write_config, BRAND["ok"])
    tk.Label(frame, text="The flags have no 'keep' value on these devices, so Write is enabled only after a Read. "
                         "Battery voltages left empty or 0 are kept. The flags decide the device's role: Delphis "
                         "nano-modem -> acoustic messages and pictures, IMU -> attitude; beacon IMU + mode -> rotation "
                         "or jack detection; nothing enabled -> the device only advertises.", bg=BRAND["surface"],
             fg=BRAND["text_dim"], font=("Segoe UI", 8), wraplength=740, justify=tk.LEFT).grid(
      row=len(self._config_specs) + 1, column=0, columnspan=3, sticky="w", padx=6)
    self.lbl_role = tk.Label(frame, text="Role: read the configuration", bg=BRAND["surface"], fg=BRAND["accent"],
                             font=BRAND["font_ui_b"])
    self.lbl_role.grid(row=len(self._config_specs) + 2, column=0, columnspan=3, sticky="w", padx=6, pady=(8, 0))

  def _read_config(self):
    """Read the configuration into the entries."""
    async def go(device):
      config = await device.read_gigo_config()
      self._current_config = dict(config)
      for key, value in config.items():
        self._set_entry(key, value)
      mode = await device.beacon_read_mode() if self.profile == "gigo_beacon" else None
      role = roles_text(gigo_roles(self.profile, config, mode))
      self.after(0, lambda: self.lbl_role.config(text=f"Role: {role}"))
      self._say(f"Configuration read: {config}; role: {role}", BRAND["ok"])
    self._run(go, "Configuration read")

  def _write_config(self):
    """Validate and write the configuration (needs a previous read for the flags)."""
    if not self._current_config:
      messagebox.showinfo("Read first", "Read the configuration first: the device has no 'keep' value for its "
                          "flags, so they must be sent as they are.", parent=self)
      return
    fields = {"name": self._entries["name"].get().strip()}
    if fields["name"] == self._current_config.get("name"):
      fields["name"] = ""
    try:
      for spec in self._config_specs:
        if spec.key != "name":
          fields[spec.key] = parse_number(self._entries[spec.key].get(), spec)
    except ConfigValueError as error:
      messagebox.showerror("Invalid value", str(error), parent=self)
      return
    if not messagebox.askyesno("Write configuration", "Write the configuration? The device saves it and resets.",
                               parent=self):
      return

    async def go(device):
      ok = await device.write_gigo_config(fields, self._current_config)
      self._say(f"Configuration write: {'accepted, the device resets' if ok else 'REFUSED'}",
                BRAND["ok"] if ok else BRAND["err"])
    self._run(go, "Configuration write")

  # ---- status and actions -----------------------------------------------------------------------------------------
  def _build_actions_tab(self, notebook):
    """Battery, self test, IMU calibration and reset."""
    frame = self._tab(notebook, "Status & actions")
    bar = tk.Frame(frame, bg=BRAND["surface"])
    bar.grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(4, 10))
    self._button(bar, "Read battery (0x04)", self._read_battery)
    self._button(bar, "Save IMU calibration (0x0C)", self._save_calibration)
    self._button(bar, "Reset (0x03)", self._reset, BRAND["err"])
    self._button(bar, "Defaults (0x07)", self._reset_defaults, BRAND["err"])

    tk.Label(frame, text="Self test (0x09) - the device closes the link 0.5 s after the result",
             bg=BRAND["surface"], fg=BRAND["accent"], font=BRAND["font_ui_b"]).grid(
      row=1, column=0, columnspan=3, sticky="w", padx=6, pady=(8, 2))
    self.cmb_sanity = ttk.Combobox(frame, values=list(SANITY_TESTS.values()), state="readonly", width=30)
    self.cmb_sanity.current(2 if self.profile == "gigo_beacon" else 0)
    self.cmb_sanity.grid(row=2, column=0, sticky="w", padx=6)
    bar2 = tk.Frame(frame, bg=BRAND["surface"])
    bar2.grid(row=2, column=1, sticky="w", padx=6)
    self._button(bar2, "Run self test", self._sanity)

  def _read_battery(self):
    """Show battery percent, charging state and voltage."""
    async def go(device):
      battery = await device.read_battery()
      if not battery:
        self._say("Battery: no answer", BRAND["warn"])
        return
      charging = CHARGING_TEXT.get(battery.get("charging"), "?")
      voltage = f", {battery['voltage_mv']} mV" if battery.get("voltage_mv") else ""
      self._say(f"Battery: {battery['percent']} % ({charging}{voltage})", BRAND["ok"])
    self._run(go, "Battery read")

  def _save_calibration(self):
    """Store the current IMU offsets."""
    async def go(device):
      ok = await device.gigo_save_calibration()
      self._say(f"IMU calibration save: {'OK' if ok else 'no acknowledge'}", BRAND["ok"] if ok else BRAND["warn"])
    self._run(go, "Calibration save")

  def _reset(self):
    """Ask, then reset the device."""
    if not messagebox.askyesno("Reset", f"Reset the {self.label} now?", parent=self):
      return

    async def go(device):
      ok = await device.gigo_reset()
      self._say(f"Reset: {'sent' if ok else 'no acknowledge'}", BRAND["ok"] if ok else BRAND["warn"])
    self._run(go, "Reset")

  def _reset_defaults(self):
    """Ask, then restore the factory configuration (name, flags, battery range, IMU calibration) and reset."""
    if not messagebox.askyesno("Reset to defaults", f"Restore the {self.label} factory configuration? The name, the "
                               "flags, the battery range and the IMU calibration are lost, then it resets.",
                               icon="warning", parent=self):
      return

    async def go(device):
      ok = await device.gigo_reset_defaults()
      self._say(f"Defaults: {'restored, the device resets' if ok else 'no acknowledge'}",
                BRAND["ok"] if ok else BRAND["warn"])
    self._run(go, "Reset to defaults")

  def _sanity(self):
    """Run the selected self test and decode the result (Delphis adds the nano-modem address)."""
    test = next(key for key, text in SANITY_TESTS.items() if text == self.cmb_sanity.get())

    async def go(device):
      result = await device.gigo_sanity_test(test)
      if result is None:
        self._say("Self test: no answer", BRAND["warn"])
        return
      error_text = SANITY_ERRORS.get(result["error"], f"error 0x{result['error']:02X}")
      raw = bytes.fromhex(result["raw"].replace(" ", "")) if result["raw"] else b""
      modem = f", nano-modem address {raw[2]}" if len(raw) >= 3 and raw[2] else ""
      self._say(f"Self test {result['test']}: {error_text}{modem} (raw {result['raw']})",
                BRAND["ok"] if result["error"] == 0 else BRAND["err"])
    self._run(go, "Self test")

  # ---- listeners for unsolicited frames ---------------------------------------------------------------------------
  def _attach_listeners(self):
    """Route the IMU stream of this device to this window (the modem lines go to the session's acoustic link)."""
    device = self.engine.active_device
    if device is None or not hasattr(device, "add_listener"):
      return
    device.add_listener(0xB2, self._on_imu_frame)

  def _close(self):
    """Leave the IMU stream (it stops when the device tab does not use it either), drop this window's listeners,
    close.
    """
    device = self.engine.active_device
    if device is not None and hasattr(device, "remove_listener"):
      device.remove_listener(0xB2, self._on_imu_frame)
      if self._imu_streaming and self.engine.active_client and self.engine.active_client.is_connected:
        async def stop():
          try:
            await device.imu_stream_release(self)
          except Exception:
            pass
        run_async(stop(), session_id=self.session_id)
    self.destroy()

  # ---- IMU ----------------------------------------------------------------------------------------------------------
  def _build_imu_tab(self, notebook):
    """Live IMU values, measuring ranges, magnetometer, recalibration and raw register access."""
    frame = self._tab(notebook, "IMU (0xB0-0xB6)")
    bar = tk.Frame(frame, bg=BRAND["surface"])
    bar.grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(4, 6))
    self._button(bar, "Start stream (0xB1)", lambda: self._imu_stream(True), BRAND["ok"])
    self._button(bar, "Stop stream", lambda: self._imu_stream(False))
    self._button(bar, "Magnetometer init (0xB6)", self._imu_mag_init)
    self._button(bar, "Recalibrate (0xB3)", self._imu_recalibrate)

    grid = tk.Frame(frame, bg=BRAND["surface"])
    grid.grid(row=1, column=0, columnspan=4, sticky="w", padx=6, pady=4)
    for row, (title, keys) in enumerate(IMU_GROUPS):
      tk.Label(grid, text=title, bg=BRAND["surface"], fg=BRAND["text_dim"], font=BRAND["font_ui"], width=24,
               anchor="w").grid(row=row, column=0, sticky="w")
      for column, key in enumerate(keys, start=1):
        label = tk.Label(grid, text="--", bg=BRAND["surface_alt"], fg=BRAND["text"], font=BRAND["font_mono"],
                         width=11, anchor="e")
        label.grid(row=row, column=column, padx=3, pady=2)
        self._imu_labels[key] = label
    self.lbl_imu_state = tk.Label(frame, text="Stream off. The IMU must be enabled in the configuration.",
                                  bg=BRAND["surface"], fg=BRAND["text_muted"], font=("Segoe UI", 8))
    self.lbl_imu_state.grid(row=2, column=0, columnspan=4, sticky="w", padx=6)

    tk.Label(frame, text="Measuring ranges (0xB5 / 0xB4, kept until the next configuration save)",
             bg=BRAND["surface"], fg=BRAND["accent"], font=BRAND["font_ui_b"]).grid(
      row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(10, 2))
    self.cmb_acc = ttk.Combobox(frame, values=[f"{key} - {text}" for key, text in IMU_ACC_SCALES.items()],
                                state="readonly", width=16)
    self.cmb_acc.grid(row=4, column=0, sticky="w", padx=6)
    self.cmb_gyro = ttk.Combobox(frame, values=[f"{key} - {text}" for key, text in IMU_GYRO_SCALES.items()],
                                 state="readonly", width=20)
    self.cmb_gyro.grid(row=4, column=1, sticky="w", padx=6)
    bar2 = tk.Frame(frame, bg=BRAND["surface"])
    bar2.grid(row=4, column=2, columnspan=2, sticky="w", padx=6)
    self._button(bar2, "Read ranges", self._imu_read_ranges)
    self._button(bar2, "Write ranges", self._imu_write_ranges, BRAND["ok"])

    tk.Label(frame, text="ICM-20948 register access (0xB0) - for diagnosis; a wrong write can stop the IMU",
             bg=BRAND["surface"], fg=BRAND["accent"], font=BRAND["font_ui_b"]).grid(
      row=5, column=0, columnspan=4, sticky="w", padx=6, pady=(10, 2))
    regs = tk.Frame(frame, bg=BRAND["surface"])
    regs.grid(row=6, column=0, columnspan=4, sticky="w", padx=6)
    self.ent_bank = self._small_entry(regs, "Bank 0-3", "0")
    self.ent_register = self._small_entry(regs, "Register", "0x00")
    self.ent_count = self._small_entry(regs, f"Count 1-{IMU_REGISTER_MAX_READ}", "1")
    self.ent_value = self._small_entry(regs, "Write value", "")
    self._button(regs, "Read", self._imu_register_read)
    self._button(regs, "Write", self._imu_register_write, BRAND["err"])

  def _small_entry(self, parent, caption, default):
    """Caption plus a short entry packed left; returns the entry."""
    tk.Label(parent, text=caption, bg=BRAND["surface"], fg=BRAND["text_dim"], font=("Segoe UI", 8)).pack(side=tk.LEFT)
    entry = tk.Entry(parent, width=6, bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground=BRAND["text"],
                     font=BRAND["font_mono"], relief=tk.FLAT)
    entry.insert(0, default)
    entry.pack(side=tk.LEFT, padx=(2, 10))
    return entry

  def _on_imu_frame(self, packet: bytes):
    """BLE thread: decode a 0xB2 frame and show it."""
    values = decode_imu_stream(packet)
    if values is None:
      return

    def update():
      if not self.winfo_exists():
        return
      self._imu_samples += 1
      for key in IMU_STREAM_KEYS:
        self._imu_labels[key].config(text=f"{values[key]:.3f}")
      self.lbl_imu_state.config(text=f"Streaming: {self._imu_samples} samples, last {time.strftime('%H:%M:%S')} "
                                     f"({values['byte_order']}-endian floats)", fg=BRAND["ok"])
    self.after(0, update)

  def _imu_stream(self, enable: bool):
    """Start or stop the IMU stream."""
    async def go(device):
      ok = await (device.imu_stream_acquire(self) if enable else device.imu_stream_release(self))
      self._imu_streaming = enable and ok
      if not ok:
        self._say("IMU stream: no acknowledge (is the IMU enabled in the configuration? The beacon streams only in "
                  "net-drum or jack mode)", BRAND["warn"])
        return
      self._say(f"IMU stream {'started' if enable else 'released (it keeps running while the device tab uses it)'}",
                BRAND["ok"])
    self._run(go, "IMU stream")

  def _imu_mag_init(self):
    """Reset and start the magnetometer."""
    async def go(device):
      ok = await device.imu_mag_init()
      self._say(f"Magnetometer init: {'OK' if ok else 'failed / not found'}", BRAND["ok"] if ok else BRAND["err"])
    self._run(go, "Magnetometer init")

  def _imu_recalibrate(self):
    """Restart the IMU calibration (the device must lie still), then offer to store the result."""
    if not messagebox.askokcancel("Recalibrate", "Place the device flat and still, then press OK. Store the new "
                                  "offsets afterwards with 'Save IMU calibration (0x0C)'.", parent=self):
      return

    async def go(device):
      ok = await device.imu_recalibrate()
      self._say(f"IMU recalibration: {'started' if ok else 'no acknowledge'}", BRAND["ok"] if ok else BRAND["warn"])
    self._run(go, "IMU recalibration")

  def _imu_read_ranges(self):
    """Read the IMU init status and the measuring ranges."""
    async def go(device):
      ranges = await device.imu_read_full_scale()
      if ranges is None:
        self._say("Ranges: no answer (IMU disabled?)", BRAND["warn"])
        return
      acc, gyro = ranges["acc_index"], ranges["gyro_index"]

      def update():
        if acc in IMU_ACC_SCALES:
          self.cmb_acc.set(f"{acc} - {IMU_ACC_SCALES[acc]}")
        if gyro in IMU_GYRO_SCALES:
          self.cmb_gyro.set(f"{gyro} - {IMU_GYRO_SCALES[gyro]}")
      self.after(0, update)
      self._say(f"IMU init status {ranges['init_status']}, accelerometer {IMU_ACC_SCALES.get(acc, acc)}, "
                f"gyroscope {IMU_GYRO_SCALES.get(gyro, gyro)}", BRAND["ok"])
    self._run(go, "Range read")

  def _imu_write_ranges(self):
    """Write the selected ranges (an empty choice keeps that range)."""
    acc = int(self.cmb_acc.get().split(" ")[0]) if self.cmb_acc.get() else None
    gyro = int(self.cmb_gyro.get().split(" ")[0]) if self.cmb_gyro.get() else None

    async def go(device):
      ok = await device.imu_write_full_scale(acc, gyro)
      self._say(f"Ranges write: {'OK' if ok else 'no acknowledge'}", BRAND["ok"] if ok else BRAND["warn"])
    self._run(go, "Range write")

  def _register_inputs(self):
    """(bank, register) from the entries; raises ValueError with a readable text."""
    try:
      return int(self.ent_bank.get(), 0), int(self.ent_register.get(), 0)
    except ValueError:
      raise ValueError("bank and register must be numbers (0x.. for hex)") from None

  def _imu_register_read(self):
    """Read registers and show them in hex."""
    try:
      bank, register = self._register_inputs()
      count = int(self.ent_count.get(), 0)
    except ValueError as error:
      messagebox.showerror("Register read", str(error), parent=self)
      return

    async def go(device):
      data = await device.imu_register_read(bank, register, count)
      if data is None:
        self._say("Register read: no answer", BRAND["warn"])
        return
      self._say(f"Bank {bank} register 0x{register:02X}..: {data.hex(' ').upper()}", BRAND["ok"])
    self._run(go, "Register read")

  def _imu_register_write(self):
    """Ask, then write one register."""
    try:
      bank, register = self._register_inputs()
      value = int(self.ent_value.get(), 0)
    except ValueError as error:
      messagebox.showerror("Register write", str(error), parent=self)
      return
    if not messagebox.askyesno("Register write", f"Write 0x{value:02X} to bank {bank} register 0x{register:02X}?",
                               icon="warning", parent=self):
      return

    async def go(device):
      ok = await device.imu_register_write(bank, register, value)
      self._say(f"Register write: {'OK' if ok else 'no acknowledge'}", BRAND["ok"] if ok else BRAND["warn"])
    self._run(go, "Register write")

  # ---- Delphis nano-modem console ---------------------------------------------------------------------------------
  def _build_acoustic_tabs(self, notebook):
    """Delphis: acoustic messages and picture transfer over the nano-modem (acoustic_panel.py)."""
    messages = AcousticMessagesTab(notebook, self.session_id)
    notebook.add(messages, text="Acoustic messages")
    pictures = ImageTransferTab(notebook, self.session_id)
    notebook.add(pictures, text="Picture transfer")

  # ---- movement (beacon) -------------------------------------------------------------------------------------------
  def _build_movement_tab(self, notebook):
    """Beacon IMU operation mode (0xB8 / 0xB7) and movement tuning (0xBA / 0xB9)."""
    frame = self._tab(notebook, "Movement (0xB7-0xBA)")
    tk.Label(frame, text="Operation mode", bg=BRAND["surface"], fg=BRAND["text"], font=BRAND["font_ui"]).grid(
      row=0, column=0, sticky="w", padx=6, pady=3)
    self.cmb_mode = ttk.Combobox(frame, values=[f"{key} - {text}" for key, text in BEACON_IMU_MODES.items()],
                                 state="readonly", width=26)
    self.cmb_mode.grid(row=0, column=1, sticky="w", padx=6)
    bar = tk.Frame(frame, bg=BRAND["surface"])
    bar.grid(row=0, column=2, sticky="w", padx=6)
    self._button(bar, "Read mode", self._read_mode)
    self._button(bar, "Write mode (resets)", self._write_mode, BRAND["ok"])

    for row, spec in enumerate(BEACON_TUNING_FIELDS, start=1):
      self._row(frame, row, spec, width=12)
    bar2 = tk.Frame(frame, bg=BRAND["surface"])
    bar2.grid(row=len(BEACON_TUNING_FIELDS) + 1, column=0, columnspan=3, sticky="w", padx=6, pady=10)
    self._button(bar2, "Read tuning", self._read_tuning)
    self._button(bar2, "Write tuning (resets)", self._write_tuning, BRAND["ok"])

  def _read_mode(self):
    """Read the IMU operation mode."""
    async def go(device):
      mode = await device.beacon_read_mode()
      if mode is None:
        self._say("Operation mode: no answer", BRAND["warn"])
        return
      self.after(0, lambda: self.cmb_mode.set(f"{mode} - {BEACON_IMU_MODES.get(mode, '?')}"))
      self._say(f"Operation mode: {mode} ({BEACON_IMU_MODES.get(mode, 'unknown')})", BRAND["ok"])
    self._run(go, "Mode read")

  def _write_mode(self):
    """Write the selected IMU operation mode."""
    choice = self.cmb_mode.get()
    if not choice:
      messagebox.showinfo("Mode", "Choose a mode first.", parent=self)
      return
    mode = int(choice.split(" ")[0])

    async def go(device):
      ok = await device.beacon_write_mode(mode)
      self._say(f"Operation mode {mode}: {'accepted, the beacon resets' if ok else 'REFUSED'}",
                BRAND["ok"] if ok else BRAND["err"])
    self._run(go, "Mode write")

  def _read_tuning(self):
    """Read the movement tuning into the entries."""
    async def go(device):
      tuning = await device.beacon_read_tuning()
      if tuning is None:
        self._say("Tuning: no answer", BRAND["warn"])
        return
      for key, value in tuning.items():
        self._set_entry(key, value)
      self._say(f"Tuning read: {tuning}", BRAND["ok"])
    self._run(go, "Tuning read")

  def _write_tuning(self):
    """Validate and write the movement tuning (every field is required)."""
    try:
      fields = {spec.key: parse_number(self._entries[spec.key].get(), spec) for spec in BEACON_TUNING_FIELDS}
    except ConfigValueError as error:
      messagebox.showerror("Invalid value", str(error), parent=self)
      return

    async def go(device):
      ok = await device.beacon_write_tuning(fields)
      self._say(f"Tuning write: {'accepted, the beacon resets' if ok else 'REFUSED'}",
                BRAND["ok"] if ok else BRAND["err"])
    self._run(go, "Tuning write")
