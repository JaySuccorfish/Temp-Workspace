"""@file live_views.py
@brief Per-application live view of a device tab: motion (Delphis, Rotation / Jack), depth & temperature, gas, SC2 GPS
@version 1.0
@date 2026-11-22

Every device type gets the live display its application needs, under its log in the device tab (create_live_view):
    GiGO Delphis            attitude (roll, pitch, heading), IMU values, nano-modem lines, attitude chart
    GiGO Rotation / Jack    attitude plus, by the beacon's mode (0xB8): drum dial with the beacon's own rotation count
                            (net drum) or moving / stationary gauge against the jack thresholds (jack); see
                            motion_analysis.py - the beacon sends neither result, the tool replays its algorithm
    GiGO Depth & Temp       depth / pressure / temperature read every few seconds (0x12), depth + temperature chart
    GiGO Gas                temperature, humidity, pressure, IAQ (Bosch band), CO2 eq., gas resistance from the live
                            stream (0x31 / 0x30), chart of a chosen pair
RecordPlot charts the records a logger window downloaded (or a CSV it saved): depth & temperature for the D&T
logger, the air values for the gas logger. Valtrack and the SC2 hubs keep the GPS map; Sc2GpsFeed puts SC2 GPS fixes (0x12) on it. Nothing starts on connect:
streams and polling cost battery, the operator presses Start. Streams are shared with the device windows through the
stream owners of the device objects, so closing one never stops the other. Data arrives on the BLE thread and is handed
to Tk with after().
"""
from .ble_engine import engine
from .ble_engine import run_async
from .charts import StripChart
from .config_codecs import BEACON_IMU_MODES
from .config_codecs import BEACON_JACK_SENSITIVITY
from .constants import BRAND
from .acoustic_link import describe_event
from .acoustic_link import link_for
from .device_profiles import gigo_roles
from .device_profiles import roles_text
from .gigo_imu_codecs import decode_imu_stream
from .logbus import log_system
from .motion_analysis import ACTION_NAMES
from .motion_analysis import ACTION_NONE
from .motion_analysis import IMU_MODE_DISABLED
from .motion_analysis import IMU_MODE_HYDRAULIC_JACK
from .motion_analysis import IMU_MODE_NET_DRUM
from .motion_analysis import JackDetector
from .motion_analysis import RotationCounter
from .motion_analysis import jack_thresholds
from .sc2_hub_codecs import sc2_decode_gps
from datetime import datetime
from datetime import timezone
from sc2_protocol import depth_from_pressure
from tkinter import filedialog
from tkinter import messagebox
from tkinter import ttk
import asyncio
import csv
import math
import time
import tkinter as tk


IMU_STREAM_OPCODE = 0xB2
SC2_GPS_OPCODE = 0x12
MS_PER_S = 1000.0
LIVE_VIEW_HEIGHT = 380
CHART_HEIGHT = 130
DIAL_SIZE = 150
PITCH_FULL_SCALE_DEG = 60.0
DRUM_SPOKES = 6
MODEM_LINES_SHOWN = 4
IMU_CHART_WINDOW_S = 120
LOGGER_POLL_CHOICES_S = (1, 2, 5, 10, 30)
LOGGER_POLL_DEFAULT_S = 2
CHART_WINDOW_CHOICES = (("5 min", 300), ("30 min", 1800), ("2 h", 7200), ("all", None))
CHART_WINDOW_DEFAULT = "30 min"
PASCAL_PER_HECTOPASCAL = 100.0
EPOCH_MIN_VALID = 1577836800  # 2020-01-01: older sample times mean the logger clock is not set
OHM_PER_KILOOHM = 1000.0
TILE_FONT = ("Consolas", 15, "bold")
TILE_FONT_COMPACT = ("Consolas", 12, "bold")
ATTITUDE_CAPTION_MARGIN = 80
TILE_MIN_WIDTH = 160
TILE_MIN_WIDTH_COMPACT = 128
SMALL_FONT = ("Segoe UI", 8)
SKY_COLOUR = "#1e6fa8"
GROUND_COLOUR = "#7a5230"
DIAL_FACE = "#17222b"

# Bosch BSEC IAQ bands, as the gas project's dashboard shows them (gigo-gas-sensors/tests/quality_assessment.py).
IAQ_BANDS = ((50, "excellent", "#37dc99"), (100, "good", "#37dc99"), (150, "lightly polluted", "#ffcf70"),
             (200, "moderately polluted", "#ffb367"), (250, "heavily polluted", "#ff8b70"),
             (350, "severely polluted", "#ff6988"), (math.inf, "extremely polluted", "#ff426d"))
IAQ_ACCURACY_NAMES = {0: "warming up", 1: "low accuracy", 2: "medium accuracy", 3: "calibrated"}


def iaq_band(iaq: "float | None") -> tuple:
  """(name, colour) of the Bosch IAQ band of a value; ('--', muted) for None."""
  if iaq is None:
    return ("--", BRAND["text_muted"])
  for upper, name, colour in IAQ_BANDS:
    if iaq <= upper:
      return (name, colour)
  return IAQ_BANDS[-1][1:]


def utc_text(epoch: "float | None") -> str:
  """'HH:MM:SS UTC' of an epoch ('--' for None)."""
  if not epoch:
    return "--"
  return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%S UTC")


def horizon_polygon(centre_x: float, centre_y: float, radius: float, roll_deg: float, pitch_deg: float) -> list:
  """Flat [x, y, ...] polygon of the ground part of an attitude-indicator disc: the horizon is turned by -roll and
  moved down by a nose-up pitch (PITCH_FULL_SCALE_DEG reaches the rim). Empty when the whole disc is sky.
  """
  roll = math.radians(-roll_deg)
  offset = max(-radius, min(radius, pitch_deg / PITCH_FULL_SCALE_DEG * radius))
  along = (math.cos(roll), math.sin(roll))
  normal = (-math.sin(roll), math.cos(roll))
  base = (centre_x + offset * normal[0], centre_y + offset * normal[1])
  half_chord = math.sqrt(max(radius * radius - offset * offset, 0.0))
  points = []
  ground_found = False
  for step in range(72):
    angle = step * math.tau / 72
    x = centre_x + radius * math.cos(angle)
    y = centre_y + radius * math.sin(angle)
    side = (x - base[0]) * normal[0] + (y - base[1]) * normal[1]
    if side >= 0.0:
      ground_found = True
      points.extend((x, y))
      continue
    foot_x, foot_y = x - side * normal[0], y - side * normal[1]
    distance = (foot_x - base[0]) * along[0] + (foot_y - base[1]) * along[1]
    distance = max(-half_chord, min(half_chord, distance))
    points.extend((base[0] + distance * along[0], base[1] + distance * along[1]))
  return points if ground_found else []


class Tile(tk.Frame):
  """A titled value box."""

  def __init__(self, parent, title: str, width: int = 13, compact: bool = False):
    super().__init__(parent, bg=BRAND["surface_alt"], padx=6 if compact else 8, pady=2 if compact else 4,
                     highlightthickness=1, highlightbackground=BRAND["border"])
    tk.Label(self, text=title, bg=BRAND["surface_alt"], fg=BRAND["text_dim"], font=SMALL_FONT, anchor="w").pack(
      fill=tk.X)
    self.value = tk.Label(self, text="--", bg=BRAND["surface_alt"], fg=BRAND["text"],
                          font=TILE_FONT_COMPACT if compact else TILE_FONT, width=width, anchor="w")
    self.value.pack(fill=tk.X)
    self.note = tk.Label(self, text="", bg=BRAND["surface_alt"], fg=BRAND["text_muted"], font=SMALL_FONT, anchor="w")
    self.note.pack(fill=tk.X)

  def show(self, text: str, note: str = "", colour: "str | None" = None) -> None:
    """Set the value (and optional note / colour)."""
    self.value.config(text=text, fg=colour or BRAND["text"])
    self.note.config(text=note)


class TileRow(tk.Frame):
  """Tiles in as many columns as the width allows (wrapping into more rows in a narrow window)."""

  def __init__(self, parent, min_width: int = TILE_MIN_WIDTH):
    super().__init__(parent, bg=BRAND["surface"])
    self.min_width = min_width
    self.tiles = {}
    self._columns = 0
    self.bind("<Configure>", lambda event: self._regrid(event.width))

  def add(self, key: str, title: str, width: int = 10, compact: bool = True) -> Tile:
    """Append a tile."""
    tile = Tile(self, title, width=width, compact=compact)
    self.tiles[key] = tile
    self._columns = 0
    self._regrid(self.winfo_width() if self.winfo_width() > 1 else len(self.tiles) * self.min_width)
    return tile

  def _regrid(self, width: int) -> None:
    """Lay the tiles out for width pixels."""
    columns = max(1, min(len(self.tiles), width // self.min_width))
    if columns == self._columns:
      return
    self._columns = columns
    for column in range(len(self.tiles)):
      self.grid_columnconfigure(column, weight=1 if column < columns else 0, uniform="tile" if column < columns
                                else "")
    for index, tile in enumerate(self.tiles.values()):
      tile.grid(row=index // columns, column=index % columns, sticky="nsew", padx=(0, 4), pady=(0, 4))


class AttitudeIndicator(tk.Canvas):
  """Artificial horizon (roll, pitch) with a heading pointer on the rim (yaw)."""

  def __init__(self, parent, size: int = DIAL_SIZE):
    super().__init__(parent, width=size + ATTITUDE_CAPTION_MARGIN, height=size + 18, bg=BRAND["surface"],
                     highlightthickness=0)
    self.size = size
    self.show(0.0, 0.0, 0.0, active=False)

  def show(self, roll: float, pitch: float, yaw: float, active: bool = True) -> None:
    """Redraw for the given angles in degrees."""
    self.delete("all")
    centre_x = (self.size + ATTITUDE_CAPTION_MARGIN) / 2
    centre = self.size / 2
    radius = centre - 6
    self.create_oval(centre_x - radius, centre - radius, centre_x + radius, centre + radius,
                     fill=SKY_COLOUR if active else DIAL_FACE, outline=BRAND["border"], width=2)
    ground = horizon_polygon(centre_x, centre, radius, roll, pitch) if active else []
    if len(ground) >= 6:
      self.create_polygon(*ground, fill=GROUND_COLOUR, outline="")
    self.create_line(centre_x - 30, centre, centre_x - 10, centre, fill="#facc15", width=3)
    self.create_line(centre_x + 10, centre, centre_x + 30, centre, fill="#facc15", width=3)
    self.create_oval(centre_x - 3, centre - 3, centre_x + 3, centre + 3, fill="#facc15", outline="")
    heading = math.radians(yaw)
    tip = (centre_x + radius * math.sin(heading), centre - radius * math.cos(heading))
    self.create_line(centre_x, centre, *tip, fill="#f8fafc", width=1, dash=(3, 3))
    self.create_oval(tip[0] - 5, tip[1] - 5, tip[0] + 5, tip[1] + 5, fill="#f8fafc", outline=BRAND["bg"])
    self.create_text(centre_x, 10, text="N", fill=BRAND["text_dim"], font=SMALL_FONT)
    text = f"roll {roll:+.0f}°  pitch {pitch:+.0f}°  hdg {yaw % 360:.0f}°" if active else "no data"
    self.create_text(centre_x, self.size + 8, text=text, fill=BRAND["text"], font=SMALL_FONT)


class DrumDial(tk.Canvas):
  """Net drum: spokes turned by the integrated angle, the direction and the beacon's counter state."""

  def __init__(self, parent, size: int = DIAL_SIZE):
    super().__init__(parent, width=size + ATTITUDE_CAPTION_MARGIN, height=size + 18, bg=BRAND["surface"],
                     highlightthickness=0)
    self.size = size
    self.show(0.0, "still", False, active=False)

  def show(self, angle_deg: float, direction_text: str, suppressed: bool, active: bool = True) -> None:
    """Redraw the drum at angle_deg (the canvas is wider than the drum so the caption fits)."""
    self.delete("all")
    centre_x = (self.size + ATTITUDE_CAPTION_MARGIN) / 2
    centre = self.size / 2
    radius = centre - 8
    rim = BRAND["warn"] if suppressed else BRAND["accent"]
    self.create_oval(centre_x - radius, centre - radius, centre_x + radius, centre + radius, fill=DIAL_FACE,
                     outline=rim, width=4)
    for spoke in range(DRUM_SPOKES):
      angle = math.radians(angle_deg + spoke * 360.0 / DRUM_SPOKES)
      self.create_line(centre_x, centre, centre_x + (radius - 6) * math.sin(angle),
                       centre - (radius - 6) * math.cos(angle), fill=BRAND["text_dim"], width=3)
    self.create_oval(centre_x - 14, centre - 14, centre_x + 14, centre + 14, fill=BRAND["surface_alt"], outline=rim)
    if active and direction_text in ("clockwise", "counter-clockwise"):
      extent = -120 if direction_text == "clockwise" else 120
      self.create_arc(centre_x - radius - 2, centre - radius - 2, centre_x + radius + 2, centre + radius + 2,
                      start=60, extent=extent, style="arc", outline=BRAND["ok"], width=3)
    turns = angle_deg / 360.0
    label = f"{turns:+.2f} turns  {direction_text}" if active else "no data"
    self.create_text(centre_x, self.size + 8, text=label, fill=BRAND["text"], font=SMALL_FONT)


class JackGauge(tk.Canvas):
  """Hydraulic jack: a ram that moves while the beacon would judge the jack as moving, and two bars against the
  jack.c thresholds.
  """

  WIDTH = 360
  HEIGHT = DIAL_SIZE + 18

  def __init__(self, parent):
    super().__init__(parent, width=self.WIDTH, height=self.HEIGHT, bg=BRAND["surface"], highlightthickness=0)
    self._phase = 0.0
    self.show(None)

  def _bar(self, top: float, label: str, value: float, threshold: float, unit: str) -> None:
    """Horizontal bar scaled to three times the threshold, with the threshold marked."""
    left, right = 96, self.WIDTH - 8
    full_scale = threshold * 3.0
    fraction = min(value / full_scale, 1.0) if full_scale > 0 else 0.0
    over = value > threshold
    self.create_text(left, top - 2, text=f"{label} {value:.3f} {unit}  (limit {threshold:g} {unit})", anchor="sw",
                     fill=BRAND["text_dim"], font=SMALL_FONT)
    self.create_rectangle(left, top, right, top + 12, fill=DIAL_FACE, outline=BRAND["border"])
    self.create_rectangle(left, top, left + (right - left) * fraction, top + 12,
                          fill=BRAND["warn"] if over else BRAND["ok"], outline="")
    marker = left + (right - left) / 3.0
    self.create_line(marker, top - 3, marker, top + 15, fill=BRAND["err"], width=2)

  def show(self, measure: "dict | None") -> None:
    """Redraw from a JackDetector.update() result (None = no data)."""
    self.delete("all")
    moving = bool(measure and measure["moving"])
    if moving:
      self._phase = (self._phase + 0.35) % math.tau
    extension = 0.5 + 0.4 * math.sin(self._phase)
    barrel_left, barrel_top, barrel_bottom = 20, 70, 160
    self.create_rectangle(barrel_left, barrel_top, barrel_left + 44, barrel_bottom, fill=BRAND["surface_alt"],
                          outline=BRAND["text_dim"], width=2)
    rod_top = barrel_top - 10 - 50 * extension
    self.create_rectangle(barrel_left + 16, rod_top, barrel_left + 28, barrel_top, fill="#cbd5e1", outline="")
    self.create_rectangle(barrel_left + 4, rod_top - 8, barrel_left + 40, rod_top, fill="#94a3b8", outline="")
    state = "MOVING" if moving else ("STATIONARY" if measure else "no data")
    colour = BRAND["warn"] if moving else (BRAND["ok"] if measure else BRAND["text_muted"])
    self.create_text(96, 14, text=state, anchor="w", fill=colour, font=("Segoe UI", 13, "bold"))
    if measure:
      self._bar(52, "accel. motion", measure["acc_motion_g"], measure["acc_threshold_g"], "g")
      self._bar(94, "turn rate", measure["gyro_dps"], measure["gyro_threshold_dps"], "°/s")
      self.create_text(96, 124, text=f"{measure['movements']} movements, {measure['moving_s']:.0f} s moving",
                       anchor="w", fill=BRAND["text"], font=SMALL_FONT)
    self.create_text(96, 144, text="beacon silent while the jack moves", anchor="w", fill=BRAND["text_muted"],
                     font=SMALL_FONT)


class LiveViewBase(tk.Frame):
  """Toolbar (title, status, buttons) and the session plumbing shared by the live views."""

  def __init__(self, parent, session_id: str, title: str, hint: str):
    super().__init__(parent, bg=BRAND["surface"])
    self.session_id = session_id
    self.summary_callback = None
    self.running = False
    self._resume_on_attach = False
    self.toolbar = tk.Frame(self, bg=BRAND["surface"], padx=6, pady=4)
    self.toolbar.pack(fill=tk.X)
    tk.Label(self.toolbar, text=title, bg=BRAND["surface"], fg=BRAND["accent"], font=BRAND["font_ui_b"]).pack(
      side=tk.LEFT, padx=(0, 8))
    self.buttons = {}
    self.lbl_status = tk.Label(self.toolbar, text=hint, bg=BRAND["surface"], fg=BRAND["text_muted"], font=SMALL_FONT,
                               anchor="e")
    self.lbl_status.pack(side=tk.RIGHT, padx=6)
    self.body = tk.Frame(self, bg=BRAND["surface"])
    self.body.pack(fill=tk.BOTH, expand=True)

  # ---- plumbing ----------------------------------------------------------------------------------------------------
  def add_button(self, key: str, text: str, command, colour: "str | None" = None) -> tk.Button:
    """A toolbar button, remembered under key."""
    button = tk.Button(self.toolbar, text=text, command=command, bg=colour or BRAND["accent_dark"],
                       fg=BRAND["accent_text"], relief=tk.FLAT, font=("Segoe UI", 8, "bold"), padx=8, pady=1,
                       cursor="hand2")
    button.pack(side=tk.LEFT, padx=2)
    self.buttons[key] = button
    return button

  def session(self):
    """The engine session of this tab, None when it is gone."""
    return engine.sessions.get(self.session_id)

  def gigo_device(self):
    """Gigo4gDevice of the session while connected."""
    current = self.session()
    return current.device if current is not None and current.connected else None

  def logger_device(self):
    """GasLoggerDevice (D&T / gas) of the session while connected."""
    current = self.session()
    return current.sc2 if current is not None and current.connected else None

  def post(self, function) -> None:
    """Run function on the Tk thread (from any thread); ignored once the view is destroyed."""
    def guarded():
      try:
        if self.winfo_exists():
          function()
      except tk.TclError:
        pass
    try:
      self.after(0, guarded)
    except (tk.TclError, RuntimeError):
      pass

  def status(self, text: str, colour: "str | None" = None) -> None:
    """Status text at the right of the toolbar (any thread)."""
    self.post(lambda: self.lbl_status.config(text=text, fg=colour or BRAND["text_dim"]))

  def summarise(self, text: str) -> None:
    """One-line summary for the tab's side panel (any thread)."""
    if self.summary_callback is not None:
      self.post(lambda: self.summary_callback(text))

  def run(self, coroutine) -> None:
    """Run a coroutine on the BLE thread for this session."""
    run_async(coroutine, session_id=self.session_id)

  def set_running(self, running: bool) -> None:
    """Remember the state and grey out Start / Stop accordingly (Tk thread)."""
    self.running = running
    if "start" in self.buttons:
      self.buttons["start"].config(state=tk.DISABLED if running else tk.NORMAL)
    if "stop" in self.buttons:
      self.buttons["stop"].config(state=tk.NORMAL if running else tk.DISABLED)

  # ---- life cycle (main window) ------------------------------------------------------------------------------------
  def attach(self) -> None:
    """The session (re)connected: start again if the view was running when the link dropped (auto-reconnect),
    else wait for Start.
    """
    resume = self._resume_on_attach
    self._resume_on_attach = False
    self.set_running(False)
    if resume:
      self.status("reconnected - restarting", BRAND["info"])
      self.start()
    else:
      self.status("connected - press Start", BRAND["text_dim"])

  def detach(self) -> None:
    """The session disconnected: everything stops (the device objects are gone); remember whether to resume."""
    self._resume_on_attach = self._resume_on_attach or self.running
    self.set_running(False)
    self.status("not connected", BRAND["text_muted"])

  def start(self) -> None:
    """Start the live data (subclasses)."""

  def close(self) -> None:
    """The tab closes: stop and leave the device's streams."""
    self.stop()

  def stop(self) -> None:
    """Stop the live data (subclasses)."""
    self.set_running(False)

  def save_chart_csv(self, chart: StripChart, columns: list, name: str) -> None:
    """Write the chart's samples as CSV (utc, epoch, then one column per series key)."""
    rows = chart.export_rows()
    if not rows:
      messagebox.showinfo("Save", "Nothing recorded yet.", parent=self)
      return
    path = filedialog.asksaveasfilename(parent=self, defaultextension=".csv", filetypes=[("CSV", "*.csv")],
                                        initialfile=f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    if not path:
      return
    with open(path, "w", encoding="utf-8", newline="") as handle:
      handle.write("utc,epoch," + ",".join(columns) + "\n")
      for epoch, values in rows:
        cells = ["" if values.get(key) is None else f"{values[key]:.6g}" for key in columns]
        handle.write(datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") +
                     f",{epoch:.3f}," + ",".join(cells) + "\n")
    log_system(f"Live data saved: {path}", BRAND["ok"], session_id=self.session_id)


class ImuLiveView(LiveViewBase):
  """GiGO Delphis and Rotation / Jack beacon: attitude, IMU values and the application display of the beacon's
  mode.
  """

  def __init__(self, parent, session_id: str, profile_key: str):
    self.profile_key = profile_key
    self.is_beacon = profile_key == "gigo_beacon"
    title = "MOVEMENT" if self.is_beacon else "MOTION & NANO-MODEM"
    hint = "press Start: reads the mode, then streams the IMU every 0.5 s" if self.is_beacon else \
      "press Start to stream the IMU every 0.5 s"
    super().__init__(parent, session_id, title, hint)
    self.mode = IMU_MODE_DISABLED if self.is_beacon else None
    self.rotation = RotationCounter()
    self.jack = JackDetector()
    self.modem_lines = []
    self.roles = ()
    self.link = None if self.is_beacon else link_for(session_id)
    if self.link is not None:
      self.link.subscribe(self._on_link_event)
    self.add_button("start", "Start", self.start, BRAND["ok"])
    self.add_button("stop", "Stop", self.stop, BRAND["err"])
    self.add_button("reset", "Reset counts", self.reset_counts)
    self.lbl_mode = tk.Label(self.toolbar, text="", bg=BRAND["surface"], fg=BRAND["text"], font=SMALL_FONT)
    self.lbl_mode.pack(side=tk.LEFT, padx=8)

    row = tk.Frame(self.body, bg=BRAND["surface"])
    row.pack(fill=tk.X, padx=6)
    self.attitude = AttitudeIndicator(row)
    self.attitude.pack(side=tk.LEFT, padx=(0, 8))
    grid = tk.Frame(row, bg=BRAND["surface"])
    grid.pack(side=tk.LEFT, anchor="n", pady=4)
    self.value_labels = {}
    for row_index, (key, title) in enumerate((("acc", "Accel. (g)"), ("gyro", "Rate (°/s)"),
                                              ("mag", "Magnet. (µT)"))):
      tk.Label(grid, text=title, bg=BRAND["surface"], fg=BRAND["text_dim"], font=SMALL_FONT, anchor="w",
               width=12).grid(row=row_index, column=0, sticky="w")
      label = tk.Label(grid, text="--", bg=BRAND["surface_alt"], fg=BRAND["text"], font=BRAND["font_mono"], width=26,
                       anchor="w")
      label.grid(row=row_index, column=1, sticky="w", pady=1)
      self.value_labels[key] = label
    self.lbl_rate = tk.Label(grid, text="", bg=BRAND["surface"], fg=BRAND["text_muted"], font=SMALL_FONT,
                             anchor="w", justify=tk.LEFT, wraplength=300)
    self.lbl_rate.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
    self.application = tk.Frame(row, bg=BRAND["surface"])
    self.application.pack(side=tk.LEFT, padx=8, anchor="n")
    self.chart_host = tk.Frame(self.body, bg=BRAND["surface"])
    self.chart_host.pack(fill=tk.BOTH, expand=True, padx=6, pady=(2, 4))
    self.chart = None
    self.app_widget = None
    self.counter_label = None
    self.modem_label = None
    self._build_application()
    self.set_running(False)

  # ---- application area and chart for the current mode ------------------------------------------------------------
  def _build_application(self) -> None:
    """Drum dial, jack gauge or nano-modem lines, and the matching chart series."""
    for child in self.application.winfo_children():
      child.destroy()
    if self.chart is not None:
      self.chart.destroy()
    self.app_widget = None
    self.counter_label = None
    self.modem_label = None
    if self.is_beacon and self.mode == IMU_MODE_NET_DRUM:
      self.app_widget = DrumDial(self.application)
      self.app_widget.pack(side=tk.LEFT)
      self.counter_label = tk.Label(self.application, text="", bg=BRAND["surface"], fg=BRAND["text"],
                                    font=BRAND["font_mono"], justify=tk.LEFT, anchor="nw")
      self.counter_label.pack(side=tk.LEFT, padx=8, anchor="n")
      self.chart = StripChart(self.chart_host, height=CHART_HEIGHT, window_s=IMU_CHART_WINDOW_S,
                              left_title="°/s", right_title="turns")
      self.chart.add_series("rate_dps", "Turn rate", BRAND["accent"], "left", "°/s", 1)
      self.chart.add_series("turns", "Turns", BRAND["warn"], "right", "", 2)
    elif self.is_beacon and self.mode == IMU_MODE_HYDRAULIC_JACK:
      self.app_widget = JackGauge(self.application)
      self.app_widget.pack(side=tk.LEFT)
      self.chart = StripChart(self.chart_host, height=CHART_HEIGHT, window_s=IMU_CHART_WINDOW_S,
                              left_title="g", right_title="°/s")
      self.chart.add_series("acc_motion_g", "Accel. motion", BRAND["accent"], "left", "g", 3)
      self.chart.add_series("gyro_dps", "Turn rate", BRAND["warn"], "right", "°/s", 1)
      acc_limit, gyro_limit = jack_thresholds(self.jack.sensitivity)
      self.chart.set_thresholds([("left", acc_limit, f"{acc_limit:g} g", BRAND["accent"]),
                                 ("right", gyro_limit, f"{gyro_limit:g} °/s", BRAND["warn"])])
    else:
      if not self.is_beacon:
        self.modem_label = tk.Label(self.application, text="Acoustic modem: nothing yet (messages and pictures: "
                                                           "GiGO window)",
                                    bg=BRAND["surface_alt"], fg=BRAND["text"], font=BRAND["font_mono"],
                                    justify=tk.LEFT, anchor="nw", width=44, height=MODEM_LINES_SHOWN + 1, padx=6,
                                    pady=4)
        self.modem_label.pack(side=tk.LEFT, anchor="n")
      else:
        tk.Label(self.application, text="IMU mode is 'disabled': the beacon does not\nstream. Choose net drum or "
                                        "jack in the\nGiGO window (Movement tab).",
                 bg=BRAND["surface"], fg=BRAND["warn"], font=SMALL_FONT, justify=tk.LEFT).pack(anchor="n")
      self.chart = StripChart(self.chart_host, height=CHART_HEIGHT, window_s=IMU_CHART_WINDOW_S,
                              left_title="° roll / pitch", right_title="° heading")
      self.chart.add_series("roll", "Roll", BRAND["accent"], "left", "°", 1)
      self.chart.add_series("pitch", "Pitch", BRAND["ok"], "left", "°", 1)
      self.chart.add_series("yaw", "Heading", BRAND["warn"], "right", "°", 0)
    self.chart.pack(fill=tk.BOTH, expand=True)
    if self.is_beacon:
      self.lbl_mode.config(text=f"mode: {BEACON_IMU_MODES.get(self.mode, self.mode)}")

  def reset_counts(self) -> None:
    """Counts back to zero (do it together with a beacon reset so both start equal)."""
    now_ms = time.monotonic() * MS_PER_S
    self.rotation.reset(now_ms)
    self.jack.reset()
    if self.chart is not None:
      self.chart.clear()

  # ---- start / stop -----------------------------------------------------------------------------------------------
  def start(self) -> None:
    """Read the beacon's mode and tuning, attach the listeners and join the IMU stream."""
    device = self.gigo_device()
    if device is None:
      self.status("not connected", BRAND["err"])
      return
    self.set_running(True)
    self.status("starting...", BRAND["info"])

    async def go():
      # The configuration decides what the device does (role): no stream is asked for when its IMU is off.
      try:
        config = await device.read_gigo_config()
      except Exception:
        config = None
      mode = None
      if self.is_beacon:
        mode = await device.beacon_read_mode()
        tuning = None
        try:
          tuning = await device.beacon_read_tuning()
        except Exception:
          tuning = None
        self.post(lambda: self._apply_beacon_settings(mode, tuning))
      roles = gigo_roles(self.profile_key, config, mode)
      self.post(lambda: self._show_roles(roles))
      if config is not None and not config.get("imu_enable"):
        self.status("IMU disabled in the configuration: no attitude data (enable it in the GiGO window)",
                    BRAND["warn"])
        self.post(lambda: self.set_running(False))
        return
      device.add_listener(IMU_STREAM_OPCODE, self._on_imu_frame)
      ok = await device.imu_stream_acquire(self)
      if ok:
        self.status("streaming (0xB2 every 0.5 s)", BRAND["ok"])
      else:
        self.status("no stream: IMU disabled in the configuration" +
                    (" or mode 'disabled'" if self.is_beacon else ""), BRAND["err"])
        self.post(lambda: self.set_running(False))
    self.run(go())

  def _show_roles(self, roles: tuple) -> None:
    """Role from the configuration in the toolbar and the side panel."""
    self.roles = roles
    text = roles_text(roles)
    self.lbl_mode.config(text=f"role: {text}")
    self.summarise(f"Role: {text}")
    if self.modem_label is not None and roles and "acoustic" not in roles:
      self.modem_label.config(text="Acoustic modem: disabled in the configuration")

  def close(self) -> None:
    """Stop, and stop following the acoustic link."""
    if self.link is not None:
      self.link.unsubscribe(self._on_link_event)
    super().close()

  def _apply_beacon_settings(self, mode: "int | None", tuning: "dict | None") -> None:
    """Rebuild the application area for the beacon's mode; take its tuning into the replayed algorithms."""
    if tuning:
      self.rotation.set_tuning(tuning)
      self.jack.sensitivity = int(tuning.get("jack_sensitivity", self.jack.sensitivity))
    if mode is not None and mode != self.mode:
      self.mode = mode
      self._build_application()
    elif tuning and self.mode == IMU_MODE_HYDRAULIC_JACK:
      self._build_application()
    self.reset_counts()
    if tuning:
      sensitivity = BEACON_JACK_SENSITIVITY.get(self.jack.sensitivity, self.jack.sensitivity)
      self.lbl_rate.config(text=f"tuning: angle {self.rotation.angle_threshold}°, dead zone "
                                f"{self.rotation.dead_zone}°, debounce {self.rotation.debounce_ms} ms, "
                                f"{self.rotation.rotation_count} counts, jack {sensitivity}")

  def stop(self) -> None:
    """Leave the stream (it stops unless the GiGO window still uses it) and drop the listeners."""
    super().stop()
    device = self.gigo_device()
    if device is None:
      return
    device.remove_listener(IMU_STREAM_OPCODE, self._on_imu_frame)

    async def go():
      try:
        await device.imu_stream_release(self)
      except Exception:
        pass
    self.run(go())
    self.status("stopped", BRAND["text_dim"])

  # ---- data ---------------------------------------------------------------------------------------------------------
  def _on_imu_frame(self, packet: bytes) -> None:
    """BLE thread: one 0xB2 sample."""
    sample = decode_imu_stream(packet)
    if sample is not None:
      received = time.time()
      monotonic_ms = time.monotonic() * MS_PER_S
      self.post(lambda: self._show_sample(sample, received, monotonic_ms))

  def _show_sample(self, sample: dict, epoch: float, now_ms: float) -> None:
    """Update attitude, values, application display and chart (Tk thread)."""
    self.attitude.show(sample["roll"], sample["pitch"], sample["yaw"])
    for key in ("acc", "gyro", "mag"):
      self.value_labels[key].config(text=f"{sample[key + '_x']:+8.2f} {sample[key + '_y']:+8.2f} "
                                         f"{sample[key + '_z']:+8.2f}")
    if self.is_beacon and self.mode == IMU_MODE_NET_DRUM:
      result = self.rotation.update(sample, now_ms)
      self.app_widget.show(result["drum_angle_deg"], result["direction_text"], result["suppressed"])
      state = "SUPPRESSED (net out)" if result["suppressed"] else "advertising"
      counter = result["ccw_count"] if result["suppressed"] else result["cw_count"]
      self.counter_label.config(text=f"beacon: {state}\ncount {counter} / {self.rotation.rotation_count}"
                                     f"\nup events   {result['up_events']}\ndown events {result['down_events']}"
                                     f"\nrate {result['rate_dps']:+.1f} °/s")
      self.chart.append("rate_dps", epoch, result["rate_dps"])
      self.chart.append("turns", epoch, result["turns"])
      self._announce(result["action"])
      self.summarise(f"Net drum: {state}, {result['turns']:+.2f} turns")
    elif self.is_beacon and self.mode == IMU_MODE_HYDRAULIC_JACK:
      result = self.jack.update(sample, now_ms)
      self.app_widget.show(result)
      self.chart.append("acc_motion_g", epoch, result["acc_motion_g"])
      self.chart.append("gyro_dps", epoch, result["gyro_dps"])
      self._announce(result["action"])
      self.summarise(f"Jack: {'MOVING' if result['moving'] else 'stationary'}, {result['movements']} movements")
    else:
      self.chart.append("roll", epoch, sample["roll"])
      self.chart.append("pitch", epoch, sample["pitch"])
      self.chart.append("yaw", epoch, sample["yaw"] % 360.0)
      self.summarise(f"Attitude: roll {sample['roll']:+.0f}° pitch {sample['pitch']:+.0f}° heading "
                     f"{sample['yaw'] % 360.0:.0f}°")

  def _announce(self, action: int) -> None:
    """Log a replayed beacon decision."""
    if action != ACTION_NONE:
      log_system(f"Beacon (replayed): {ACTION_NAMES[action]}", BRAND["warn"], session_id=self.session_id)

  def _on_link_event(self, kind: str, data) -> None:
    """BLE thread: messages, pictures and modem state of the Delphis acoustic link."""
    stamp = time.strftime('%H:%M:%S', time.gmtime())
    if kind == "message":
      arrow = "<<" if data["direction"] == "in" else ">>"
      status = f" ({data['status']})" if data.get("status") else ""
      line = f"{stamp} {arrow} {data['text'][:40]}{status}"
    elif kind == "image":
      line = f"{stamp} << picture {data['bytes']} B, CRC-32 {data['crc32']:08X} (picture tab)"
      self.summarise(f"Acoustic: {len(self.link.images)} picture(s) received")
    elif kind == "line" and data["kind"] in ("status", "ack", "timeout", "battery", "refused"):
      line = f"{stamp} {describe_event(data)}"
    else:
      return

    def update():
      self.modem_lines = (self.modem_lines + [line])[-MODEM_LINES_SHOWN:]
      if self.modem_label is not None:
        self.modem_label.config(text="Nano-modem:\n" + "\n".join(self.modem_lines))
    self.post(update)


class DepthTempLiveView(LiveViewBase):
  """GiGO Depth & Temp: the Keller values read every few seconds and charted (depth axis pointing down)."""

  def __init__(self, parent, session_id: str):
    super().__init__(parent, session_id, "DEPTH & TEMPERATURE", "press Start to read the sensor (0x12)")
    self._generation = 0
    self.poll_s = LOGGER_POLL_DEFAULT_S
    self.add_button("start", "Start", self.start, BRAND["ok"])
    self.add_button("stop", "Stop", self.stop, BRAND["err"])
    self.add_button("status", "Logger status", self.read_status)
    self.add_button("clear", "Clear", lambda: self.chart.clear())
    self.add_button("save", "Save CSV", lambda: self.save_chart_csv(
      self.chart, ["depth_m", "temperature_c", "pressure_bar", "temperature_t_c"], "depth_temp_live"))
    tk.Label(self.toolbar, text="every", bg=BRAND["surface"], fg=BRAND["text_dim"], font=SMALL_FONT).pack(
      side=tk.LEFT, padx=(8, 2))
    self.var_poll = tk.StringVar(value=f"{LOGGER_POLL_DEFAULT_S} s")
    poll = ttk.Combobox(self.toolbar, textvariable=self.var_poll, values=[f"{seconds} s" for seconds in
                                                                          LOGGER_POLL_CHOICES_S],
                        state="readonly", width=5)
    poll.pack(side=tk.LEFT)
    poll.bind("<<ComboboxSelected>>", lambda _event: self._take_poll())
    self.var_window = tk.StringVar(value=CHART_WINDOW_DEFAULT)
    window = ttk.Combobox(self.toolbar, textvariable=self.var_window,
                          values=[name for name, _seconds in CHART_WINDOW_CHOICES], state="readonly", width=7)
    window.pack(side=tk.LEFT, padx=4)
    window.bind("<<ComboboxSelected>>", lambda _event: self._take_window())

    tiles = TileRow(self.body)
    tiles.pack(fill=tk.X, padx=6, pady=(0, 4))
    for key, title in (("depth", "Depth"), ("pressure", "Pressure P1"), ("temperature", "Temperature (logged)"),
                       ("temperature_t", "Temperature T"), ("logger", "Logger")):
      tiles.add(key, title, width=11, compact=False)
    self.tiles = tiles.tiles
    self.chart = StripChart(self.body, height=CHART_HEIGHT + 30, window_s=dict(CHART_WINDOW_CHOICES)[
      CHART_WINDOW_DEFAULT], invert_left=True, left_title="depth m (down)", right_title="°C")
    self.chart.add_series("depth_m", "Depth", BRAND["accent"], "left", "m", 2)
    self.chart.add_series("temperature_c", "Temperature", BRAND["warn"], "right", "°C", 2)
    # Not drawn, kept for Save CSV.
    self.chart.add_series("pressure_bar", "P1", BRAND["text_muted"], "right", "bar", 4)
    self.chart.add_series("temperature_t_c", "T", BRAND["text_muted"], "right", "°C", 2)
    self.chart.set_visible("pressure_bar", False)
    self.chart.set_visible("temperature_t_c", False)
    self.chart.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))
    self.set_running(False)

  def _take_poll(self) -> None:
    """New poll period from the combo box."""
    try:
      self.poll_s = int(self.var_poll.get().split()[0])
    except (ValueError, IndexError):
      self.poll_s = LOGGER_POLL_DEFAULT_S

  def _take_window(self) -> None:
    """New chart time window from the combo box."""
    self.chart.window_s = dict(CHART_WINDOW_CHOICES).get(self.var_window.get())
    self.chart.redraw()

  def start(self) -> None:
    """Read the logger state once, then the sensor every poll period until stopped."""
    device = self.logger_device()
    if device is None:
      self.status("not connected", BRAND["err"])
      return
    self._generation += 1
    generation = self._generation
    self.set_running(True)
    self.status("reading...", BRAND["info"])
    self.read_status()

    async def poll():
      failures = 0
      while generation == self._generation:
        current = self.logger_device()
        if current is None:
          break
        try:
          snapshot = await current.read_all_sensors()
        except Exception as error:
          snapshot = {"error": str(error)}
        if snapshot and snapshot.get("success"):
          failures = 0
          received = time.time()
          self.post(lambda data=snapshot, epoch=received: self._show(data, epoch))
        else:
          failures += 1
          self.status(f"sensor read failed ({failures}): {snapshot.get('status') if snapshot else 'no answer'}",
                      BRAND["warn"])
        await asyncio.sleep(self.poll_s)
      self.post(lambda: self.set_running(False) if generation == self._generation else None)
    self.run(poll())

  def stop(self) -> None:
    """Stop polling after the read in progress."""
    self._generation += 1
    super().stop()
    self.status("stopped", BRAND["text_dim"])

  def detach(self) -> None:
    """Link lost: end the polling loop now (it would only time out)."""
    super().detach()
    self._generation += 1

  def read_status(self) -> None:
    """Recording state and record count (0x26) into the Logger tile."""
    device = self.logger_device()
    if device is None:
      return

    async def go():
      stats = await device.get_stats()

      def update():
        if not stats or "total_records" not in stats:
          self.tiles["logger"].show("--", f"stats: {stats.get('status') if stats else 'no answer'}")
          return
        active = stats.get("recording_active")
        self.tiles["logger"].show("RECORDING" if active else "idle",
                                  f"{stats['total_records']} records, phase {stats.get('current_phase')}",
                                  BRAND["ok"] if active else BRAND["text_dim"])
      self.post(update)
    self.run(go())

  def _show(self, snapshot: dict, epoch: float) -> None:
    """Tiles and chart from a decoded 0x12 snapshot (Tk thread)."""
    depth = snapshot.get("depth_m")
    pressure = snapshot.get("p1_bar")
    temperature = snapshot.get("tob1_c")
    temperature_t = snapshot.get("t_c")
    self.tiles["depth"].show("--" if depth is None else f"{depth:.2f} m", "as the logger computes it")
    self.tiles["pressure"].show("--" if pressure is None else f"{pressure:.4f}",
                                "bar gauge" + ("" if snapshot.get("p2_bar") is None else
                                               f", P2 {snapshot['p2_bar']:.3f}"))
    self.tiles["temperature"].show("--" if temperature is None else f"{temperature:.2f} °C", "TOB1 (records)")
    self.tiles["temperature_t"].show("--" if temperature_t is None else f"{temperature_t:.2f} °C", "T sensor")
    for key, value in (("depth_m", depth), ("temperature_c", temperature), ("pressure_bar", pressure),
                       ("temperature_t_c", temperature_t)):
      self.chart.append(key, epoch, value)
    self.status(f"last read {utc_text(epoch)}", BRAND["ok"])
    self.summarise("Depth " + ("--" if depth is None else f"{depth:.2f} m") + ", " +
                   ("--" if temperature is None else f"{temperature:.1f} °C"))


class GasLiveView(LiveViewBase):
  """GiGO Gas: live air values from the shared stream, the IAQ band and a chart of a chosen pair."""

  CHART_GROUPS = (
    ("IAQ / CO2 eq.", ("iaq", "co2_equivalent_ppm"), "IAQ", "ppm"),
    ("Temperature / humidity", ("temperature_c", "humidity_pct"), "°C", "%RH"),
    ("Pressure", ("pressure_hpa",), "hPa", ""),
    ("Gas resistance", ("gas_kohm",), "kΩ", ""),
  )
  SERIES = (("iaq", "IAQ", "#37dc99", "left", "", 0), ("co2_equivalent_ppm", "CO2 eq.", BRAND["warn"], "right",
                                                                         "ppm", 0),
            ("temperature_c", "Temperature", BRAND["warn"], "left", "°C", 2),
            ("humidity_pct", "Humidity", BRAND["accent"], "right", "%", 1),
            ("pressure_hpa", "Pressure", BRAND["info"], "left", "hPa", 2),
            ("gas_kohm", "Gas resistance", "#c084fc", "left", "kΩ", 1))

  def __init__(self, parent, session_id: str):
    super().__init__(parent, session_id, "AIR QUALITY", "press Start for the live stream (one sample about every 3 s)")
    self.add_button("start", "Start", self.start, BRAND["ok"])
    self.add_button("stop", "Stop", self.stop, BRAND["err"])
    self.add_button("read", "Read now", self.read_now)
    self.add_button("clear", "Clear", lambda: self.chart.clear())
    self.add_button("save", "Save CSV", lambda: self.save_chart_csv(
      self.chart, [key for key, *_rest in self.SERIES], "gas_live"))
    self.var_group = tk.StringVar(value=self.CHART_GROUPS[0][0])
    group = ttk.Combobox(self.toolbar, textvariable=self.var_group, values=[name for name, *_rest in self.CHART_GROUPS],
                         state="readonly", width=20)
    group.pack(side=tk.LEFT, padx=6)
    group.bind("<<ComboboxSelected>>", lambda _event: self._take_group())

    tiles = TileRow(self.body, TILE_MIN_WIDTH_COMPACT)
    tiles.pack(fill=tk.X, padx=6, pady=(0, 4))
    for key, title in (("iaq", "IAQ"), ("co2", "CO2 equivalent"), ("temperature", "Temperature"),
                       ("humidity", "Humidity"), ("pressure", "Pressure"), ("gas", "Gas resistance"),
                       ("static_iaq", "Static IAQ"), ("battery", "Battery")):
      tiles.add(key, title)
    self.tiles = tiles.tiles
    self.chart = StripChart(self.body, height=CHART_HEIGHT, window_s=dict(CHART_WINDOW_CHOICES)[CHART_WINDOW_DEFAULT])
    for key, label, colour, axis, unit, digits in self.SERIES:
      self.chart.add_series(key, label, colour, axis, unit, digits)
    self.chart.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 4))
    self._take_group()
    self.set_running(False)

  def _take_group(self) -> None:
    """Show only the chosen pair of series."""
    name = self.var_group.get()
    group = next((item for item in self.CHART_GROUPS if item[0] == name), self.CHART_GROUPS[0])
    for key, *_rest in self.SERIES:
      self.chart.set_visible(key, key in group[1])
    self.chart.left_title, self.chart.right_title = group[2], group[3]
    self.chart.redraw()

  def start(self) -> None:
    """Attach to the stream and join it (the gas window may already have it on)."""
    device = self.logger_device()
    if device is None:
      self.status("not connected", BRAND["err"])
      return
    self.set_running(True)
    device.add_stream_callback(self._on_stream)

    async def go():
      live = await device.read_live()
      if live and "epoch" in live:
        self.post(lambda: self._show(live))
      ok = await device.stream_acquire(self)
      if ok:
        self.status("streaming (0x30 frames)", BRAND["ok"])
      else:
        self.status("stream refused (0x31)", BRAND["err"])
        self.post(lambda: self.set_running(False))
    self.run(go())

  def stop(self) -> None:
    """Leave the stream (it stops unless the gas window still uses it)."""
    super().stop()
    device = self.logger_device()
    if device is None:
      return
    device.remove_stream_callback(self._on_stream)

    async def go():
      try:
        await device.stream_release(self)
      except Exception:
        pass
    self.run(go())
    self.status("stopped", BRAND["text_dim"])

  def read_now(self) -> None:
    """One sample without the stream."""
    device = self.logger_device()
    if device is None:
      self.status("not connected", BRAND["err"])
      return

    async def go():
      live = await device.read_live()
      if live and "epoch" in live:
        self.post(lambda: self._show(live))
      else:
        self.status(f"live read: {live or 'no answer'}", BRAND["warn"])
    self.run(go())

  def _on_stream(self, live: dict) -> None:
    """BLE thread: a streamed sample."""
    self.post(lambda: self._show(live))

  def _show(self, live: dict) -> None:
    """Tiles and chart from a decoded live sample (Tk thread)."""
    epoch = live.get("epoch") or 0
    if epoch < EPOCH_MIN_VALID:
      epoch = time.time()
    iaq = live.get("iaq")
    band, colour = iaq_band(iaq)
    accuracy = IAQ_ACCURACY_NAMES.get(live.get("iaq_accuracy"), "?")
    self.tiles["iaq"].show("--" if iaq is None else f"{iaq:.0f}", band, colour)
    co2 = live.get("co2_equivalent_ppm")
    self.tiles["co2"].show("--" if co2 is None else f"{co2} ppm", "estimate (BSEC)")
    temperature = live.get("temperature_c")
    self.tiles["temperature"].show("--" if temperature is None else f"{temperature:.2f} °C")
    humidity = live.get("humidity_pct")
    self.tiles["humidity"].show("--" if humidity is None else f"{humidity:.1f} %RH")
    pressure = live.get("pressure_pa")
    pressure_hpa = None if pressure is None else pressure / PASCAL_PER_HECTOPASCAL
    self.tiles["pressure"].show("--" if pressure_hpa is None else f"{pressure_hpa:.1f}", "hPa")
    resistance = live.get("gas_resistance_ohm")
    gas_kohm = None if resistance is None else resistance / OHM_PER_KILOOHM
    flags = ", ".join(name for name, on in (("fresh", live.get("gas_fresh")), ("settled", live.get("gas_settled")))
                      if on)
    self.tiles["gas"].show("--" if gas_kohm is None else f"{gas_kohm:.1f} kΩ", flags)
    static_iaq = live.get("static_iaq")
    self.tiles["static_iaq"].show("--" if static_iaq is None else str(static_iaq), accuracy)
    battery = live.get("battery_pct")
    self.tiles["battery"].show("--" if battery is None else f"{battery} %",
                               "clock synced" if live.get("time_synced") else "clock NOT set")
    for key, value in (("iaq", iaq), ("co2_equivalent_ppm", co2), ("temperature_c", temperature),
                       ("humidity_pct", humidity), ("pressure_hpa", pressure_hpa), ("gas_kohm", gas_kohm)):
      self.chart.append(key, epoch, value)
    self.status(f"sample {utc_text(epoch)}", BRAND["ok"])
    self.summarise(f"IAQ {'--' if iaq is None else f'{iaq:.0f}'} ({band}), "
                   f"{'--' if temperature is None else f'{temperature:.1f}'} °C")


class Sc2GpsFeed:
  """SC2 hub: GPS fixes (0x12, requested in the SC2 window or by live tracking) onto the tab's map."""

  def __init__(self, session_id: str, on_fix):
    """on_fix(latitude, longitude) is called on the BLE thread (the caller hands it to Tk)."""
    self.session_id = session_id
    self.on_fix = on_fix
    self.device = None

  def attach(self) -> None:
    """Listen to the connected session's 0x12 frames."""
    current = engine.sessions.get(self.session_id)
    device = current.device if current is not None and current.connected else None
    if device is None or device is self.device:
      return
    self.device = device
    device.add_listener(SC2_GPS_OPCODE, self._on_frame)

  def detach(self) -> None:
    """Session gone: forget its device."""
    self.device = None

  def close(self) -> None:
    """Stop listening."""
    if self.device is not None:
      self.device.remove_listener(SC2_GPS_OPCODE, self._on_frame)
    self.device = None

  def _on_frame(self, packet: bytes) -> None:
    """A decrypted 0x12 frame."""
    fix = sc2_decode_gps(packet)
    if fix and fix.get("valid"):
      self.on_fix(fix["latitude"], fix["longitude"])


class RecordPlot(tk.Frame):
  """Chart of the records downloaded from a logger, or read back from a CSV file the tool saved: one group of series at
  a time, the time span and record count in the toolbar.
  """

  def __init__(self, parent, series: tuple, groups: tuple, csv_converter, invert_left: bool = False,
               title: str = "RECORDS"):
    """series: (key, label, colour, axis, unit, digits); groups: (name, keys, left title, right title);
    csv_converter(list of CSV row dicts) -> plot rows.
    """
    super().__init__(parent, bg=BRAND["surface"])
    self.groups = groups
    self.csv_converter = csv_converter
    toolbar = tk.Frame(self, bg=BRAND["surface"], padx=6, pady=4)
    toolbar.pack(fill=tk.X)
    tk.Label(toolbar, text=title, bg=BRAND["surface"], fg=BRAND["accent"], font=BRAND["font_ui_b"]).pack(
      side=tk.LEFT, padx=(0, 8))
    self.var_group = tk.StringVar(value=groups[0][0])
    if len(groups) > 1:
      combo = ttk.Combobox(toolbar, textvariable=self.var_group, values=[group[0] for group in groups],
                           state="readonly", width=22)
      combo.pack(side=tk.LEFT, padx=4)
      combo.bind("<<ComboboxSelected>>", lambda _event: self._take_group())
    tk.Button(toolbar, text="Open CSV...", command=self.open_csv, bg=BRAND["accent_dark"], fg=BRAND["accent_text"],
              relief=tk.FLAT, font=("Segoe UI", 8, "bold"), padx=8, pady=1).pack(side=tk.LEFT, padx=4)
    self.lbl_summary = tk.Label(toolbar, text="", bg=BRAND["surface"], fg=BRAND["text_dim"], font=SMALL_FONT)
    self.lbl_summary.pack(side=tk.LEFT, padx=8)
    self.chart = StripChart(self, height=CHART_HEIGHT + 60, window_s=None, invert_left=invert_left,
                            empty_text="Download the records or open a saved CSV file")
    for key, label, colour, axis, unit, digits in series:
      self.chart.add_series(key, label, colour, axis, unit, digits)
    self.chart.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
    self._take_group()

  def _take_group(self) -> None:
    """Show only the chosen group's series."""
    group = next((item for item in self.groups if item[0] == self.var_group.get()), self.groups[0])
    for key in self.chart.series:
      self.chart.set_visible(key, key in group[1])
    self.chart.left_title, self.chart.right_title = group[2], group[3]
    self.chart.redraw()

  def set_rows(self, rows: list, source: str) -> None:
    """Plot rows (dicts with 'epoch' and series keys); rows without a valid clock time are left out and counted."""
    timed = [row for row in rows if (row.get("epoch") or 0) >= EPOCH_MIN_VALID]
    for key in self.chart.series:
      self.chart.set_points(key, [(row["epoch"], row.get(key)) for row in timed])
    skipped = len(rows) - len(timed)
    if timed:
      span = f"{datetime.fromtimestamp(timed[0]['epoch'], tz=timezone.utc):%d/%m %H:%M} - " \
             f"{datetime.fromtimestamp(timed[-1]['epoch'], tz=timezone.utc):%d/%m %H:%M} UTC"
    else:
      span = "no record with a valid time"
    self.lbl_summary.config(text=f"{len(timed)} records, {span} ({source})" +
                                 (f"; {skipped} without clock left out" if skipped else ""))

  def open_csv(self) -> None:
    """Plot a CSV file saved by this tool."""
    path = filedialog.askopenfilename(parent=self, filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
    if not path:
      return
    try:
      with open(path, encoding="utf-8", newline="") as handle:
        rows = self.csv_converter(list(csv.DictReader(handle)))
    except (OSError, ValueError, KeyError) as error:
      messagebox.showerror("Open CSV", f"Cannot read {path}:\n{error}", parent=self)
      return
    self.set_rows(sorted(rows, key=lambda row: row.get("epoch") or 0), path.replace("\\", "/").split("/")[-1])


def _number(text) -> "float | None":
  """float of a CSV cell, None when empty or not a number."""
  try:
    return float(text) if text not in (None, "") else None
  except ValueError:
    return None


DT_PLOT_SERIES = (("depth_m", "Depth", BRAND["accent"], "left", "m", 2),
                  ("temperature_c", "Temperature", BRAND["warn"], "right", "\u00b0C", 1),
                  ("pressure_bar", "Pressure", BRAND["info"], "left", "bar", 4),
                  ("battery_pct", "Battery", BRAND["ok"], "right", "%", 0))
DT_PLOT_GROUPS = (("Depth / temperature", ("depth_m", "temperature_c"), "depth m (down)", "\u00b0C"),
                  ("Pressure / battery", ("pressure_bar", "battery_pct"), "bar", "%"))


def dt_plot_row(record: dict) -> dict:
  """Plot row of a D&T record from retrieve() / decode_record()."""
  return {"epoch": record.get("epoch"), "depth_m": record.get("depth_m_est"), "temperature_c": record.get("temp_c"),
          "pressure_bar": record.get("pressure_bar"), "battery_pct": record.get("battery_pct"),
          "state": record.get("state")}


def dt_rows_from_csv(rows: list) -> list:
  """Plot rows of a D&T CSV (sc2_protocol CSV_HEADER); depth is recomputed from the pressure the way the logger
  does, so files saved by older builds (depth = bar x 10.197) plot the same.
  """
  result = []
  for row in rows:
    pressure = _number(row.get("pressure_bar"))
    result.append({"epoch": _number(row.get("epoch")),
                   "depth_m": depth_from_pressure(pressure) if pressure is not None else None,
                   "temperature_c": _number(row.get("temp_c")), "pressure_bar": pressure,
                   "battery_pct": _number(row.get("battery_pct")), "state": _number(row.get("state"))})
  return result


GAS_PLOT_SERIES = (("iaq", "IAQ", "#37dc99", "left", "", 0),
                   ("temperature_c", "Temperature", BRAND["warn"], "left", "\u00b0C", 2),
                   ("humidity_pct", "Humidity", BRAND["accent"], "right", "%", 0),
                   ("pressure_hpa", "Pressure", BRAND["info"], "left", "hPa", 1),
                   ("gas_kohm", "Gas resistance", "#c084fc", "left", "k\u03a9", 1),
                   ("battery_pct", "Battery", BRAND["ok"], "right", "%", 0))
GAS_PLOT_GROUPS = (("Temperature / humidity", ("temperature_c", "humidity_pct"), "\u00b0C", "%RH"),
                   ("IAQ / battery", ("iaq", "battery_pct"), "IAQ", "%"),
                   ("Pressure", ("pressure_hpa",), "hPa", ""),
                   ("Gas resistance", ("gas_kohm",), "k\u03a9", ""))


def gas_plot_row(record) -> dict:
  """Plot row of a decoded gas record (gas_protocol.GasRecord)."""
  return {"epoch": record.epoch, "temperature_c": record.temperature_c, "humidity_pct": record.humidity_pct,
          "pressure_hpa": None if record.pressure_pa is None else record.pressure_pa / PASCAL_PER_HECTOPASCAL,
          "gas_kohm": None if record.gas_resistance_ohm is None else record.gas_resistance_ohm / OHM_PER_KILOOHM,
          "iaq": record.iaq, "battery_pct": record.battery_pct}


def gas_rows_from_csv(rows: list) -> list:
  """Plot rows of a gas CSV (gas_device.gas_record_csv_header); error rows are left out."""
  result = []
  for row in rows:
    epoch = _number(row.get("epoch"))
    if epoch is None:
      continue
    pressure = _number(row.get("pressure_pa"))
    resistance = _number(row.get("gas_resistance_ohm"))
    result.append({"epoch": epoch, "temperature_c": _number(row.get("temperature_c")),
                   "humidity_pct": _number(row.get("humidity_pct")),
                   "pressure_hpa": None if pressure is None else pressure / PASCAL_PER_HECTOPASCAL,
                   "gas_kohm": None if resistance is None else resistance / OHM_PER_KILOOHM,
                   "iaq": _number(row.get("iaq")), "battery_pct": _number(row.get("battery_pct"))})
  return result


def create_live_view(parent, profile_key: "str | None", session_id: str) -> "LiveViewBase | None":
  """The live view of a device type; None for the GPS trackers, which show the map instead."""
  if profile_key in ("gigo_delphis", "gigo_beacon"):
    return ImuLiveView(parent, session_id, profile_key)
  if profile_key == "gigo_dt":
    return DepthTempLiveView(parent, session_id)
  if profile_key == "gigo_gas":
    return GasLiveView(parent, session_id)
  return None
