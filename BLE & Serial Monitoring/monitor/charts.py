"""@file charts.py
@brief Time-series chart on a plain Tk canvas (no matplotlib): live views and record plots of every device type
@version 1.0
@date 2026-11-22

StripChart draws one or two value axes (left / right, the left one optionally inverted for depth) against UTC time,
with a legend that shows each series' latest value, dashed threshold lines and a hover read-out. Live views append
samples and show a moving time window; record plots set all points at once. The axis maths (nice_ticks, axis_range,
time_ticks, decimate) is pure and unit tested in tests/test_live_views.py. Plain Tk keeps the PyInstaller exe small.
"""
from .constants import BRAND
from datetime import datetime
from datetime import timezone
import bisect
import math
import tkinter as tk


MARGIN_LEFT = 58
MARGIN_RIGHT_AXIS = 58
MARGIN_RIGHT_NONE = 14
MARGIN_TOP = 24
PIXELS_PER_VALUE_TICK = 28
MARGIN_BOTTOM = 24
VALUE_TICKS = 5
TIME_TICKS = 6
RANGE_PAD_FRACTION = 0.08
REDRAW_DELAY_MS = 120
DEFAULT_MAX_POINTS = 20000
HOVER_TAG = "hover"
SECONDS_PER_DAY = 86400
TIME_TICK_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200,
                   SECONDS_PER_DAY, 2 * SECONDS_PER_DAY, 7 * SECONDS_PER_DAY, 30 * SECONDS_PER_DAY)
CHART_FONT = ("Segoe UI", 8)
CHART_FONT_BOLD = ("Segoe UI", 8, "bold")
GRID_COLOUR = "#1f2b35"


def nice_step(span: float, max_ticks: int) -> float:
  """Tick spacing of 1, 2, 2.5 or 5 times a power of ten that gives at most max_ticks intervals over span."""
  raw = span / max(1, max_ticks)
  if raw <= 0.0 or not math.isfinite(raw):
    return 1.0
  magnitude = 10.0 ** math.floor(math.log10(raw))
  for factor in (1.0, 2.0, 2.5, 5.0, 10.0):
    step = factor * magnitude
    if step >= raw * (1.0 - 1e-9):
      return step
  return 10.0 * magnitude


def nice_ticks(low: float, high: float, max_ticks: int = VALUE_TICKS) -> list:
  """Round tick values covering low..high (inclusive)."""
  if not (math.isfinite(low) and math.isfinite(high)):
    return []
  if high <= low:
    high = low + 1.0
  step = nice_step(high - low, max_ticks)
  ticks = []
  value = math.ceil(low / step - 1e-9) * step
  while value <= high + step * 1e-9:
    ticks.append(round(value, 9))
    value += step
  return ticks


def axis_range(values, min_span: float = 1.0) -> tuple:
  """(low, high) that holds every finite value with a little margin; at least min_span wide."""
  finite = [value for value in values if value is not None and math.isfinite(value)]
  if not finite:
    return (0.0, min_span)
  low, high = min(finite), max(finite)
  if high - low < min_span:
    middle = (low + high) / 2.0
    low, high = middle - min_span / 2.0, middle + min_span / 2.0
  pad = (high - low) * RANGE_PAD_FRACTION
  return (low - pad, high + pad)


def time_ticks(start: float, end: float, max_ticks: int = TIME_TICKS) -> tuple:
  """(step in s, tick times) on whole multiples of a step from TIME_TICK_STEPS between start and end (UTC)."""
  span = max(end - start, 1.0)
  step = next((candidate for candidate in TIME_TICK_STEPS if span / candidate <= max_ticks), TIME_TICK_STEPS[-1])
  first = math.ceil(start / step) * step
  ticks = []
  value = first
  while value <= end:
    ticks.append(value)
    value += step
  return step, ticks


def format_time_tick(epoch: float, step: float, span: float) -> str:
  """Tick label: seconds when the step is under a minute, the date when the span exceeds a day."""
  moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
  if span > SECONDS_PER_DAY:
    return moment.strftime("%d/%m %H:%M")
  return moment.strftime("%H:%M:%S" if step < 60 else "%H:%M")


def decimate(points: list, max_points: int) -> list:
  """Reduce (t, v) points to about max_points, keeping the minimum and maximum of each bucket so spikes stay
  visible.
  """
  if len(points) <= max_points or max_points < 4:
    return points
  bucket = math.ceil(len(points) / (max_points / 2))
  reduced = []
  for start in range(0, len(points), bucket):
    chunk = points[start:start + bucket]
    low = min(chunk, key=lambda point: point[1])
    high = max(chunk, key=lambda point: point[1])
    reduced.extend(sorted({low, high}, key=lambda point: point[0]))
  return reduced


class Series:
  """One line of the chart."""

  def __init__(self, key: str, label: str, colour: str, axis: str, unit: str, digits: int):
    self.key = key
    self.label = label
    self.colour = colour
    self.axis = axis
    self.unit = unit
    self.digits = digits
    self.times = []
    self.values = []
    self.visible = True

  def latest(self):
    """Last value (None when empty)."""
    return self.values[-1] if self.values else None

  def text(self, value) -> str:
    """Value with its unit, '--' for None."""
    if value is None:
      return "--"
    return f"{value:.{self.digits}f}{(' ' + self.unit) if self.unit else ''}"


class StripChart(tk.Frame):
  """Chart widget: add_series() once, then append() samples or set_points() a whole series; redraws itself."""

  def __init__(self, parent, height: int = 220, window_s: "float | None" = None, invert_left: bool = False,
               left_title: str = "", right_title: str = "", empty_text: str = "No data yet",
               max_points: int = DEFAULT_MAX_POINTS):
    """window_s: show only the last window_s seconds (live views); None shows everything (record plots)."""
    super().__init__(parent, bg=BRAND["surface"])
    self.window_s = window_s
    self.invert_left = invert_left
    self.left_title = left_title
    self.right_title = right_title
    self.empty_text = empty_text
    self.max_points = max_points
    self.series = {}
    self.thresholds = []
    self._redraw_pending = False
    self._layout = None
    self.canvas = tk.Canvas(self, height=height, bg=BRAND["bg"], highlightthickness=0, bd=0)
    self.canvas.pack(fill=tk.BOTH, expand=True)
    self.canvas.bind("<Configure>", lambda _event: self.redraw())
    self.canvas.bind("<Motion>", self._on_motion)
    self.canvas.bind("<Leave>", lambda _event: self.canvas.delete(HOVER_TAG))

  # ---- data --------------------------------------------------------------------------------------------------------
  def add_series(self, key: str, label: str, colour: str, axis: str = "left", unit: str = "", digits: int = 2):
    """Declare a series drawn against the 'left' or 'right' axis."""
    self.series[key] = Series(key, label, colour, axis, unit, digits)
    self._schedule()

  def set_visible(self, key: str, visible: bool) -> None:
    """Show or hide a series (its legend entry stays)."""
    if key in self.series:
      self.series[key].visible = visible
      self._schedule()

  def append(self, key: str, epoch: float, value) -> None:
    """Add one sample (time in epoch seconds; None leaves a gap)."""
    series = self.series.get(key)
    if series is None:
      return
    if series.times and epoch < series.times[-1]:
      index = bisect.bisect_right(series.times, epoch)
      series.times.insert(index, epoch)
      series.values.insert(index, value)
    else:
      series.times.append(epoch)
      series.values.append(value)
    if len(series.times) > self.max_points:
      del series.times[0]
      del series.values[0]
    self._schedule()

  def set_points(self, key: str, points: list) -> None:
    """Replace a series by (epoch, value) points (sorted here)."""
    series = self.series.get(key)
    if series is None:
      return
    ordered = sorted(points, key=lambda point: point[0])[-self.max_points:]
    series.times = [point[0] for point in ordered]
    series.values = [point[1] for point in ordered]
    self._schedule()

  def clear(self) -> None:
    """Drop every sample (series and thresholds stay)."""
    for series in self.series.values():
      series.times.clear()
      series.values.clear()
    self._schedule()

  def set_thresholds(self, thresholds: list) -> None:
    """Dashed horizontal lines: [(axis, value, label, colour)]."""
    self.thresholds = list(thresholds)
    self._schedule()

  def export_rows(self) -> list:
    """[(epoch, {key: value})] of every sample time, for saving to CSV."""
    rows = {}
    for series in self.series.values():
      for epoch, value in zip(series.times, series.values):
        rows.setdefault(epoch, {})[series.key] = value
    return sorted(rows.items())

  # ---- drawing -----------------------------------------------------------------------------------------------------
  def _schedule(self) -> None:
    """Redraw soon, once, however many samples arrive meanwhile."""
    if self._redraw_pending:
      return
    self._redraw_pending = True
    try:
      self.after(REDRAW_DELAY_MS, self.redraw)
    except tk.TclError:
      self._redraw_pending = False

  def _visible_points(self, series: Series, start: float) -> list:
    """(t, v) of series from start on, None values kept as gaps."""
    first = bisect.bisect_left(series.times, start)
    return list(zip(series.times[first:], series.values[first:]))

  def redraw(self) -> None:
    """Draw axes, grid, thresholds, lines and legend for the current size."""
    self._redraw_pending = False
    try:
      if not self.winfo_exists():
        return
    except tk.TclError:
      return
    canvas = self.canvas
    canvas.delete("all")
    width = max(canvas.winfo_width(), 50)
    height = max(canvas.winfo_height(), 50)
    has_right = any(series.axis == "right" for series in self.series.values())
    left = MARGIN_LEFT
    right = width - (MARGIN_RIGHT_AXIS if has_right else MARGIN_RIGHT_NONE)
    top = MARGIN_TOP
    bottom = height - MARGIN_BOTTOM
    self._layout = None
    all_times = [series.times[-1] for series in self.series.values() if series.times]
    if not all_times or right - left < 20 or bottom - top < 20:
      self._draw_legend(width)
      canvas.create_text(width / 2, height / 2, text=self.empty_text, fill=BRAND["text_muted"], font=CHART_FONT)
      return

    end = max(all_times)
    if self.window_s:
      start = end - self.window_s
    else:
      start = min(series.times[0] for series in self.series.values() if series.times)
    if end - start < 1.0:
      start = end - 1.0
    visible = {key: self._visible_points(series, start) for key, series in self.series.items() if series.visible}
    ranges = {}
    for axis in ("left", "right"):
      values = [value for key, points in visible.items() if self.series[key].axis == axis for _t, value in points]
      values += [value for threshold_axis, value, _label, _colour in self.thresholds if threshold_axis == axis]
      ranges[axis] = axis_range(values)

    def x_of(epoch):
      return left + (epoch - start) / (end - start) * (right - left)

    def y_of(axis, value):
      low, high = ranges[axis]
      fraction = (value - low) / (high - low)
      if axis == "left" and self.invert_left:
        fraction = 1.0 - fraction
      return bottom - fraction * (bottom - top)

    canvas.create_rectangle(left, top, right, bottom, outline=BRAND["border"])
    value_ticks = max(3, min(VALUE_TICKS, int((bottom - top) // PIXELS_PER_VALUE_TICK)))
    for value in nice_ticks(*ranges["left"], value_ticks):
      y = y_of("left", value)
      if top <= y <= bottom:
        canvas.create_line(left, y, right, y, fill=GRID_COLOUR)
        canvas.create_text(left - 4, y, text=f"{value:g}", anchor="e", fill=BRAND["text_dim"], font=CHART_FONT)
    if has_right:
      for value in nice_ticks(*ranges["right"], value_ticks):
        y = y_of("right", value)
        if top <= y <= bottom:
          canvas.create_text(right + 4, y, text=f"{value:g}", anchor="w", fill=BRAND["text_dim"], font=CHART_FONT)
    step, ticks = time_ticks(start, end)
    for epoch in ticks:
      x = x_of(epoch)
      canvas.create_line(x, top, x, bottom, fill=GRID_COLOUR)
      canvas.create_text(x, bottom + 3, text=format_time_tick(epoch, step, end - start), anchor="n",
                         fill=BRAND["text_dim"], font=CHART_FONT)
    canvas.create_text(left - 6, height - 2, text="UTC", anchor="se", fill=BRAND["text_muted"], font=CHART_FONT)

    for axis, value, label, colour in self.thresholds:
      y = y_of(axis, value)
      if top <= y <= bottom:
        canvas.create_line(left, y, right, y, fill=colour, dash=(4, 3))
        if axis == "left":
          canvas.create_text(left + 4, y - 2, text=label, anchor="sw", fill=colour, font=CHART_FONT)
        else:
          canvas.create_text(right - 4, y - 2, text=label, anchor="se", fill=colour, font=CHART_FONT)

    plot_width = int(right - left)
    for key, points in visible.items():
      series = self.series[key]
      segment = []
      for epoch, value in decimate(points, plot_width * 2):
        if value is None or not math.isfinite(value):
          self._draw_segment(segment, series.colour)
          segment = []
          continue
        segment.extend((x_of(epoch), y_of(series.axis, value)))
      self._draw_segment(segment, series.colour)
    self._layout = (left, right, top, bottom, start, end)
    self._draw_legend(width)

  def _draw_segment(self, coordinates: list, colour: str) -> None:
    """A polyline, or a dot for a single point."""
    if len(coordinates) >= 4:
      self.canvas.create_line(*coordinates, fill=colour, width=2)
    elif len(coordinates) == 2:
      x, y = coordinates
      self.canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=colour, outline=colour)

  def _draw_legend(self, width: int) -> None:
    """Axis titles at both ends of the top edge, the series names with their latest values between them."""
    x = 4
    has_right = any(series.axis == "right" for series in self.series.values())
    if self.left_title:
      item = self.canvas.create_text(x, 4, text=self.left_title, anchor="nw", fill=BRAND["text_dim"], font=CHART_FONT)
      bounds = self.canvas.bbox(item)
      x = (bounds[2] if bounds else x) + 12
    if has_right and self.right_title:
      self.canvas.create_text(width - 4, 4, text=self.right_title, anchor="ne", fill=BRAND["text_dim"],
                              font=CHART_FONT)
    for series in self.series.values():
      if not series.visible:
        continue
      item = self.canvas.create_text(x, 4, text=f"■ {series.label}: {series.text(series.latest())}",
                                     anchor="nw", fill=series.colour, font=CHART_FONT_BOLD)
      bounds = self.canvas.bbox(item)
      x = (bounds[2] if bounds else x) + 14
      if x > width - 40:
        break

  def _on_motion(self, event) -> None:
    """Vertical cursor with the values of every series at the nearest sample time."""
    self.canvas.delete(HOVER_TAG)
    if self._layout is None:
      return
    left, right, top, bottom, start, end = self._layout
    if not left <= event.x <= right or not top <= event.y <= bottom:
      return
    epoch = start + (event.x - left) / (right - left) * (end - start)
    lines = [datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")]
    for series in self.series.values():
      if not series.visible or not series.times:
        continue
      index = bisect.bisect_left(series.times, epoch)
      candidates = [position for position in (index - 1, index) if 0 <= position < len(series.times)]
      nearest = min(candidates, key=lambda position: abs(series.times[position] - epoch))
      lines.append(f"{series.label}: {series.text(series.values[nearest])}")
    self.canvas.create_line(event.x, top, event.x, bottom, fill=BRAND["text_muted"], dash=(2, 2), tags=HOVER_TAG)
    anchor = "nw" if event.x < (left + right) / 2 else "ne"
    offset = 8 if anchor == "nw" else -8
    text = self.canvas.create_text(event.x + offset, top + 6, text="\n".join(lines), anchor=anchor,
                                   fill=BRAND["text"], font=CHART_FONT, tags=HOVER_TAG)
    bounds = self.canvas.bbox(text)
    if bounds:
      box = self.canvas.create_rectangle(bounds[0] - 4, bounds[1] - 3, bounds[2] + 4, bounds[3] + 3,
                                         fill=BRAND["surface_alt"], outline=BRAND["border"], tags=HOVER_TAG)
      self.canvas.tag_lower(box, text)
