"""@file gps_map_widget.py
@brief Embedded real-time Satellite MAP widget with precise Older -> Newer waypoint chain in Tkinter.

Uses pure Python standard library (Tk 9 native PhotoImage PNG/PPM + Windows GDI+ JPEG decoding)
with satellite imagery tiles so no third-party dependencies are required.
"""
from .constants import BRAND, FONT_MONO_FAMILY, FONT_UI_FAMILY
from collections import OrderedDict
import ctypes
import io
import math
import os
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import webbrowser


_TILE_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_OSM_USER_AGENT = "ValtrackDebugMonitor/1.0 (+https://succorfish.com)"
_MAX_LAT = 85.05112878
_TILE_SIZE = 256
_MAX_CACHE_TILES = 96
_MAX_WAYPOINTS = 200
_EARTH_RADIUS_M = 6371000.0

_GDIPLUS_LOCK = threading.Lock()
_GDIPLUS_TOKEN: ctypes.c_size_t | None = None
_GDIPLUS_READY = False


def _ensure_gdiplus() -> bool:
    """Initialize Windows GDI+ once for native in-memory JPEG -> PPM satellite tile decoding."""
    global _GDIPLUS_TOKEN, _GDIPLUS_READY
    if sys.platform != "win32":
        return False
    if _GDIPLUS_READY:
        return True
    with _GDIPLUS_LOCK:
        if _GDIPLUS_READY:
            return True
        try:
            class _GdiplusStartupInput(ctypes.Structure):
                _fields_ = [
                    ("GdiplusVersion", ctypes.c_uint32),
                    ("DebugEventCallback", ctypes.c_void_p),
                    ("SuppressBackgroundThread", ctypes.c_int),
                    ("SuppressExternalCodecs", ctypes.c_int),
                ]
            gdiplus = ctypes.windll.gdiplus
            shlwapi = ctypes.windll.shlwapi
            shlwapi.SHCreateMemStream.argtypes = [ctypes.c_char_p, ctypes.c_uint]
            shlwapi.SHCreateMemStream.restype = ctypes.c_void_p
            token = ctypes.c_size_t()
            inp = _GdiplusStartupInput(1, None, 0, 0)
            if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None) == 0:
                _GDIPLUS_TOKEN = token
                _GDIPLUS_READY = True
        except Exception:
            _GDIPLUS_READY = False
        return _GDIPLUS_READY


def _jpeg_to_ppm(jpg_bytes: bytes) -> bytes | None:
    """Decode JPEG bytes to PPM or PNG bytes for native Tk PhotoImage.

    Windows uses GDI+ in-memory (no third-party dependencies). On macOS/Linux,
    uses Pillow when available and falls back on macOS to the built-in ``sips``
    utility so Google Satellite JPEG tiles render natively without Pillow.
    """
    if sys.platform == "win32":
        return _jpeg_to_ppm_win32(jpg_bytes)
    try:
        from PIL import Image
        with Image.open(io.BytesIO(jpg_bytes)) as img:
            out = io.BytesIO()
            img.convert("RGB").save(out, format="PPM")
            return out.getvalue()
    except Exception:
        pass
    if sys.platform == "darwin":
        return _jpeg_to_png_macos(jpg_bytes)
    return None


def _jpeg_to_png_macos(jpg_bytes: bytes) -> bytes | None:
    """Decode JPEG bytes to PNG on macOS using the built-in /usr/bin/sips tool."""
    src_path = dst_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as src:
            src.write(jpg_bytes)
            src_path = src.name
        dst_path = src_path + ".png"
        res = subprocess.run(
            ["/usr/bin/sips", "-s", "format", "png", src_path, "--out", dst_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if res.returncode == 0 and os.path.isfile(dst_path):
            with open(dst_path, "rb") as fh:
                return fh.read()
    except Exception:
        return None
    finally:
        for p in (src_path, dst_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
    return None


def _jpeg_to_ppm_win32(jpg_bytes: bytes) -> bytes | None:
    """Decode JPEG bytes to uncompressed PPM P6 bytes via Windows GDI+."""
    if not _ensure_gdiplus():
        return None
    gdiplus = ctypes.windll.gdiplus
    shlwapi = ctypes.windll.shlwapi

    class _Rect(ctypes.Structure):
        _fields_ = [("X", ctypes.c_int), ("Y", ctypes.c_int), ("Width", ctypes.c_int), ("Height", ctypes.c_int)]

    class _BitmapData(ctypes.Structure):
        _fields_ = [
            ("Width", ctypes.c_uint),
            ("Height", ctypes.c_uint),
            ("Stride", ctypes.c_int),
            ("PixelFormat", ctypes.c_int),
            ("Scan0", ctypes.c_void_p),
            ("Reserved", ctypes.c_void_p),
        ]

    stream = shlwapi.SHCreateMemStream(jpg_bytes, len(jpg_bytes))
    if not stream:
        return None
    bmp = ctypes.c_void_p()
    try:
        if gdiplus.GdipCreateBitmapFromStream(ctypes.c_void_p(stream), ctypes.byref(bmp)) != 0 or not bmp:
            return None
        w = ctypes.c_uint()
        h = ctypes.c_uint()
        gdiplus.GdipGetImageWidth(bmp, ctypes.byref(w))
        gdiplus.GdipGetImageHeight(bmp, ctypes.byref(h))
        if w.value == 0 or h.value == 0:
            return None
        rect = _Rect(0, 0, w.value, h.value)
        bdata = _BitmapData()
        if gdiplus.GdipBitmapLockBits(bmp, ctypes.byref(rect), 1, 0x26200A, ctypes.byref(bdata)) != 0:
            return None
        try:
            raw = ctypes.string_at(bdata.Scan0, abs(bdata.Stride) * h.value)
        finally:
            gdiplus.GdipBitmapUnlockBits(bmp, ctypes.byref(bdata))
        buf = bytearray(raw)
        rgb = bytearray(w.value * h.value * 3)
        rgb[0::3] = buf[2::4]
        rgb[1::3] = buf[1::4]
        rgb[2::3] = buf[0::4]
        return f"P6\n{w.value} {h.value}\n255\n".encode("ascii") + bytes(rgb)
    except Exception:
        return None
    finally:
        if bmp:
            gdiplus.GdipDisposeImage(bmp)
        try:
            vtbl = ctypes.cast(ctypes.cast(stream, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
            release_fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
            release_fn(ctypes.c_void_p(stream))
        except Exception:
            pass


def _haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two WGS-84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * _EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _parse_utc_seconds(utc_str: str) -> int | None:
    """Extract seconds since midnight from a 'HH:MM:SS...' string."""
    if not utc_str:
        return None
    digits = utc_str.split()[0]
    parts = digits.split(":")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
        if 0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60:
            return h * 3600 + m * 60 + s
    return None


class RealtimeGpsMap(tk.Frame):
    """Interactive map overlay showing live GPS fixes, numbered Older -> Newer waypoint chain, and HUD."""

    def __init__(self, parent, size: int = 310):
        self._box_size = size
        self._collapsed_h = 28
        self._canvas_w = size
        self._canvas_h = max(120, size - 74)
        super().__init__(
            parent,
            width=size,
            height=size,
            bg=BRAND["surface"],
            highlightbackground=BRAND["accent"],
            highlightthickness=1,
        )
        self.pack_propagate(False)
        self.zoom = 3
        self.lat: float | None = None
        self.lon: float | None = None
        self.center_lat: float | None = 20.0
        self.center_lon: float | None = 78.0
        self._has_first_fix = False
        self.fixed = False
        self.sats: str | None = None
        self.timed_out = False
        self.auto_follow = True
        self.collapsed = False

        # Chronological waypoint chain (index 0 = oldest, index -1 = newest)
        self.waypoints: list[dict] = []
        self._next_wp_idx = 1
        self.selected_wp_idx: int | None = None

        self._tile_cache: OrderedDict[tuple[int, int, int], tk.PhotoImage] = OrderedDict()
        self._pending_tiles: set[tuple[int, int, int]] = set()
        self._failed_tiles: set[tuple[int, int, int]] = set()
        self._fetch_sem = threading.Semaphore(4)
        self._drag_origin: tuple[int, int] | None = None
        self._destroyed = False

        self._wp_string_var = tk.StringVar(value="")
        self._build_ui()
        self.bind("<Destroy>", self._on_destroy)
        self._refresh_waypoint_footer()
        self._redraw()

    @property
    def track_points(self) -> list[tuple[float, float]]:
        """Return (lat, lon) coordinates for all recorded waypoints in Older -> Newer order."""
        return [(w["lat"], w["lon"]) for w in self.waypoints]

    def _build_ui(self):
        head = tk.Frame(self, bg=BRAND["surface_alt"], padx=4, pady=3)
        head.pack(side=tk.TOP, fill=tk.X)

        self.lbl_title = tk.Label(
            head,
            text="🗺️ MAP",
            font=("Segoe UI", 8, "bold"),
            fg=BRAND["warn"],
            bg=BRAND["surface_alt"],
            anchor="w",
        )
        self.lbl_title.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

        btn_cfg = {
            "bg": BRAND["surface"],
            "fg": BRAND["text"],
            "activebackground": BRAND["accent_dark"],
            "activeforeground": BRAND["accent_text"],
            "relief": tk.FLAT,
            "font": ("Consolas", 8, "bold"),
            "padx": 3,
            "pady": 0,
            "cursor": "hand2",
        }
        self.btn_collapse = tk.Button(head, text="–", command=self.toggle_collapse, **btn_cfg)
        self.btn_collapse.pack(side=tk.RIGHT, padx=(1, 0))

        tk.Button(head, text="↗", command=self.open_in_browser, **btn_cfg).pack(side=tk.RIGHT, padx=1)
        tk.Button(head, text="⌖", command=self.recenter, **btn_cfg).pack(side=tk.RIGHT, padx=1)
        tk.Button(head, text="⊡", command=self.fit_all_waypoints, **btn_cfg).pack(side=tk.RIGHT, padx=1)
        tk.Button(head, text="+", command=self.zoom_in, **btn_cfg).pack(side=tk.RIGHT, padx=1)
        tk.Button(head, text="-", command=self.zoom_out, **btn_cfg).pack(side=tk.RIGHT, padx=1)
        tk.Button(head, text="▶", command=lambda: self.step_waypoint(1), **btn_cfg).pack(side=tk.RIGHT, padx=1)
        tk.Button(head, text="◀", command=lambda: self.step_waypoint(-1), **btn_cfg).pack(side=tk.RIGHT, padx=1)

        self.canvas = tk.Canvas(
            self,
            width=self._canvas_w,
            height=self._canvas_h,
            bg="#0b1120",
            highlightthickness=0,
            cursor="fleur",
        )
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_move)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", lambda _e: self.zoom_in())
        self.canvas.bind("<Button-5>", lambda _e: self.zoom_out())
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

        # Footer strip displaying Older -> Newer precise waypoint chain
        self.wp_frame = tk.Frame(self, bg=BRAND["surface_alt"], padx=5, pady=3)
        self.wp_frame.pack(side=tk.TOP, fill=tk.X)

        self.lbl_wp_summary = tk.Label(
            self.wp_frame,
            text="No waypoints recorded yet",
            font=("Consolas", 8, "bold"),
            fg=BRAND["text_dim"],
            bg=BRAND["surface_alt"],
            anchor="w",
        )
        self.lbl_wp_summary.pack(side=tk.TOP, fill=tk.X)

        self.txt_wp_string = tk.Entry(
            self.wp_frame,
            textvariable=self._wp_string_var,
            state="readonly",
            readonlybackground=BRAND["bg"],
            fg=BRAND["info"],
            font=("Consolas", 8),
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=BRAND["border"],
            highlightcolor=BRAND["accent"],
        )
        self.txt_wp_string.pack(side=tk.TOP, fill=tk.X, pady=(2, 0))

    def _on_destroy(self, event):
        if event.widget is self:
            self._destroyed = True
            self._tile_cache.clear()
            self._pending_tiles.clear()

    # ---- Projection helpers -----------------------------------------------------------
    @staticmethod
    def _clamp_lat(lat: float) -> float:
        return max(-_MAX_LAT, min(_MAX_LAT, lat))

    @classmethod
    def _latlon_to_tile(cls, lat: float, lon: float, zoom: int) -> tuple[float, float]:
        n = float(1 << zoom)
        lat_rad = math.radians(cls._clamp_lat(lat))
        xtile = ((lon + 180.0) % 360.0) / 360.0 * n
        ytile = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n
        return xtile, ytile

    @staticmethod
    def _tile_to_latlon(xtile: float, ytile: float, zoom: int) -> tuple[float, float]:
        n = float(1 << zoom)
        lon = (xtile / n) * 360.0 - 180.0
        lon = ((lon + 180.0) % 360.0) - 180.0
        lat_rad = math.atan(math.sinh(math.pi * (1.0 - 2.0 * ytile / n)))
        lat = math.degrees(lat_rad)
        return lat, lon

    def _latlon_to_canvas(self, lat: float, lon: float, w: int, h: int, zoom: int | None = None) -> tuple[float, float] | None:
        if self.center_lat is None or self.center_lon is None:
            return None
        z = self.zoom if zoom is None else zoom
        cx, cy = self._latlon_to_tile(self.center_lat, self.center_lon, z)
        px_t, py_t = self._latlon_to_tile(lat, lon, z)
        return (px_t - cx) * _TILE_SIZE + (w / 2.0), (py_t - cy) * _TILE_SIZE + (h / 2.0)

    # ---- Public status/fix API --------------------------------------------------------
    def update_fix(self, lat: float, lon: float, fixed: bool = True, sats: str | None = None, utc_time: str | None = None):
        """Record a valid GPS fix in the Older -> Newer waypoint chain and locate it on Google Maps."""
        if abs(lat) < 1e-5 and abs(lon) < 1e-5:
            return
        self.lat = round(lat, 6)
        self.lon = round(lon, 6)
        self.fixed = fixed
        self.timed_out = False
        clean_utc = (utc_time or "").strip()
        clean_sats = str(sats).strip() if (sats is not None and str(sats).strip()) else ""
        if clean_sats:
            self.sats = clean_sats

        append_new = False
        if not self.waypoints:
            append_new = True
        else:
            last = self.waypoints[-1]
            moved = abs(last["lat"] - self.lat) > 5e-6 or abs(last["lon"] - self.lon) > 5e-6
            sec_prev = _parse_utc_seconds(last.get("utc", ""))
            sec_cur = _parse_utc_seconds(clean_utc)
            utc_advanced = False
            if sec_prev is not None and sec_cur is not None:
                diff = abs(sec_cur - sec_prev)
                utc_advanced = min(diff, 86400 - diff) >= 5
            if last.get("milestone") or moved or utc_advanced:
                append_new = True
            else:
                last["lat"] = self.lat
                last["lon"] = self.lon
                if clean_utc:
                    last["utc"] = clean_utc
                if clean_sats:
                    last["sats"] = clean_sats

        if append_new:
            self.waypoints.append({
                "idx": self._next_wp_idx,
                "lat": self.lat,
                "lon": self.lon,
                "utc": clean_utc,
                "sats": self.sats or "",
                "milestone": False,
            })
            self._next_wp_idx += 1
            if len(self.waypoints) > _MAX_WAYPOINTS:
                self.waypoints.pop(0)
                if self.selected_wp_idx is not None:
                    self.selected_wp_idx = max(0, self.selected_wp_idx - 1)

        if not self._has_first_fix:
            self._has_first_fix = True
            self.zoom = 16
            self.center_lat = self.lat
            self.center_lon = self.lon
        elif self.auto_follow or self.center_lat is None or self.center_lon is None:
            self.center_lat = self.lat
            self.center_lon = self.lon

        self._update_header_title()
        self._refresh_waypoint_footer()
        self._redraw()

    def set_searching(self, timeout: bool = False):
        """Update header state when GPS is searching or timed out without erasing waypoint chain."""
        self.fixed = False
        self.timed_out = timeout
        if timeout:
            suffix = f" #{self.waypoints[-1]['idx']}" if self.waypoints else ""
            self.lbl_title.config(text=f"🗺️ MAP: Timeout{suffix}", fg=BRAND["warn"])
        elif self.lat is not None and self.lon is not None:
            self.lbl_title.config(text=f"🗺️ MAP: Search #{self.waypoints[-1]['idx']}", fg=BRAND["warn"])
        else:
            self.lbl_title.config(text="🗺️ MAP", fg=BRAND["warn"])
        self._redraw()

    def mark_fixed_banner(self):
        """Lock the newest waypoint on confirmed firmware '*** GPS FIXED ***' milestone."""
        self.fixed = True
        self.timed_out = False
        if self.waypoints:
            self.waypoints[-1]["milestone"] = True
        self._update_header_title()
        self._refresh_waypoint_footer()
        self._redraw()

    def _update_header_title(self):
        sat_suffix = f" ({self.sats}SV)" if self.sats else ""
        wp_count = len(self.waypoints)
        wp_tag = f"#{self.waypoints[-1]['idx']}" if wp_count else ""
        if self.fixed and self.lat is not None and self.lon is not None:
            self.lbl_title.config(
                text=f"🗺️ MAP: FIX {wp_tag}{sat_suffix}",
                fg="#4ade80",
            )
        elif self.fixed:
            self.lbl_title.config(text="🗺️ MAP: FIXED", fg="#4ade80")
        elif self.lat is not None and self.lon is not None:
            self.lbl_title.config(
                text=f"🗺️ MAP: WP {wp_tag}",
                fg=BRAND["warn"],
            )

    def _refresh_waypoint_footer(self):
        if not self.waypoints:
            self.lbl_wp_summary.config(text="Waypoints (Older ➔ Newer): awaiting GPS fix...", fg=BRAND["text_dim"])
            self._wp_string_var.set("")
            return

        total_dist_m = 0.0
        for i in range(len(self.waypoints) - 1):
            a, b = self.waypoints[i], self.waypoints[i + 1]
            total_dist_m += _haversine_meters(a["lat"], a["lon"], b["lat"], b["lon"])
        dist_str = f"{total_dist_m:.1f} m" if total_dist_m < 1000.0 else f"{total_dist_m / 1000.0:.2f} km"

        oldest = self.waypoints[0]
        newest = self.waypoints[-1]
        if len(self.waypoints) == 1:
            utc_part = f" [{newest['utc'].split()[0]}]" if newest["utc"] else ""
            summary = f"WP #{newest['idx']}{utc_part}: {newest['lat']:.6f}, {newest['lon']:.6f}  (1 WP)"
        else:
            summary = (
                f"OLD #{oldest['idx']}: {oldest['lat']:.5f},{oldest['lon']:.5f}  ➔  "
                f"NEW #{newest['idx']}: {newest['lat']:.5f},{newest['lon']:.5f}  "
                f"({len(self.waypoints)} WPs | {dist_str})"
            )
        self.lbl_wp_summary.config(text=summary, fg="#4ade80" if self.fixed else BRAND["text"])

        chain_parts = []
        for idx_pos, wp in enumerate(self.waypoints):
            short_utc = wp["utc"].split()[0] if wp["utc"] else "--:--:--"
            suffix = ""
            if len(self.waypoints) == 1:
                suffix = " (FIX)"
            elif idx_pos == 0:
                suffix = " (OLD)"
            elif idx_pos == len(self.waypoints) - 1:
                suffix = " (NEW)"
            chain_parts.append(f"#{wp['idx']} [{short_utc}] {wp['lat']:.6f},{wp['lon']:.6f}{suffix}")
        self._wp_string_var.set(" ➔ ".join(chain_parts))
        try:
            self.txt_wp_string.xview_moveto(1.0)
        except Exception:
            pass

    # ---- User controls ----------------------------------------------------------------
    def zoom_in(self):
        if self.zoom < 19:
            self.zoom += 1
            self._redraw()

    def zoom_out(self):
        if self.zoom > 2:
            self.zoom -= 1
            self._redraw()

    def recenter(self):
        self.auto_follow = True
        self.selected_wp_idx = None
        if self.lat is not None and self.lon is not None:
            self.center_lat = self.lat
            self.center_lon = self.lon
            if self.zoom < 14:
                self.zoom = 16
            self._redraw()

    def step_waypoint(self, delta: int):
        """Step backward (Older, delta=-1) or forward (Newer, delta=+1) along the waypoint string."""
        if not self.waypoints:
            return
        cur = (len(self.waypoints) - 1) if self.selected_wp_idx is None else self.selected_wp_idx
        nxt = max(0, min(len(self.waypoints) - 1, cur + delta))
        self.selected_wp_idx = nxt
        wp = self.waypoints[nxt]
        self.center_lat = wp["lat"]
        self.center_lon = wp["lon"]
        self.auto_follow = False
        self._redraw()

    def fit_all_waypoints(self):
        """Adjust center and zoom so the entire Older -> Newer waypoint chain fits in view."""
        if not self.waypoints:
            return
        lats = [w["lat"] for w in self.waypoints]
        lons = [w["lon"] for w in self.waypoints]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        self.center_lat = (min_lat + max_lat) / 2.0
        self.center_lon = (min_lon + max_lon) / 2.0
        self.auto_follow = False

        w = max(self._canvas_w, self.canvas.winfo_width()) - 60
        h = max(self._canvas_h, self.canvas.winfo_height()) - 60
        chosen_zoom = 16 if len(self.waypoints) == 1 else 3
        if len(self.waypoints) > 1 and (max_lat - min_lat > 1e-7 or max_lon - min_lon > 1e-7):
            for z in range(18, 2, -1):
                x1, y1 = self._latlon_to_tile(max_lat, min_lon, z)
                x2, y2 = self._latlon_to_tile(min_lat, max_lon, z)
                span_px_x = abs(x2 - x1) * _TILE_SIZE
                span_px_y = abs(y2 - y1) * _TILE_SIZE
                if span_px_x <= max(80, w) and span_px_y <= max(60, h):
                    chosen_zoom = z
                    break
        self.zoom = chosen_zoom
        self._redraw()

    def open_in_browser(self):
        if self.selected_wp_idx is not None and 0 <= self.selected_wp_idx < len(self.waypoints):
            wp = self.waypoints[self.selected_wp_idx]
            webbrowser.open(f"https://www.google.com/maps?q={wp['lat']:.6f},{wp['lon']:.6f}")
        elif len(self.waypoints) >= 2:
            pts = self.waypoints[-15:]
            if pts[0] is not self.waypoints[0]:
                pts = [self.waypoints[0]] + pts[1:]
            path = "/".join(f"{w['lat']:.6f},{w['lon']:.6f}" for w in pts)
            webbrowser.open(f"https://www.google.com/maps/dir/{path}")
        elif self.lat is not None and self.lon is not None:
            webbrowser.open(f"https://www.google.com/maps?q={self.lat:.6f},{self.lon:.6f}")

    def toggle_collapse(self):
        self.collapsed = not self.collapsed
        if self.collapsed:
            self.wp_frame.pack_forget()
            self.canvas.pack_forget()
            self.configure(width=self._box_size, height=self._collapsed_h)
            self.btn_collapse.config(text="□")
        else:
            self.configure(width=self._box_size, height=self._box_size)
            self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
            self.wp_frame.pack(side=tk.TOP, fill=tk.X)
            self.btn_collapse.config(text="–")
            self._redraw()

    def _on_pan_start(self, event):
        self._drag_origin = (event.x, event.y)

    def _on_pan_move(self, event):
        if self._drag_origin is None or self.center_lat is None or self.center_lon is None:
            return
        dx = event.x - self._drag_origin[0]
        dy = event.y - self._drag_origin[1]
        self._drag_origin = (event.x, event.y)
        cx, cy = self._latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        new_cx = cx - dx / _TILE_SIZE
        new_cy = max(0.0, min(float(1 << self.zoom) - 1e-6, cy - dy / _TILE_SIZE))
        self.center_lat, self.center_lon = self._tile_to_latlon(new_cx, new_cy, self.zoom)
        self.auto_follow = False
        self._redraw()

    def _on_mouse_wheel(self, event):
        if getattr(event, "delta", 0) > 0:
            self.zoom_in()
        elif getattr(event, "delta", 0) < 0:
            self.zoom_out()

    # ---- Tile fetching & drawing ------------------------------------------------------
    def _request_tile(self, key: tuple[int, int, int]):
        if key in self._tile_cache or key in self._pending_tiles or key in self._failed_tiles:
            return
        self._pending_tiles.add(key)

        def worker():
            z, tx, ty = key
            server = (tx + ty) % 4
            candidates = (
                (f"https://mt{server}.google.com/vt/lyrs=y&x={tx}&y={ty}&z={z}", _TILE_USER_AGENT),
                (f"https://mt{server}.google.com/vt/lyrs=s&x={tx}&y={ty}&z={z}", _TILE_USER_AGENT),
                (f"https://mt{server}.google.com/vt/lyrs=m&x={tx}&y={ty}&z={z}", _TILE_USER_AGENT),
                (f"https://tile.openstreetmap.org/{z}/{tx}/{ty}.png", _OSM_USER_AGENT),
            )
            data = None
            with self._fetch_sem:
                if self._destroyed:
                    return
                for url, ua in candidates:
                    try:
                        req = urllib.request.Request(url, headers={"User-Agent": ua})
                        with urllib.request.urlopen(req, timeout=4) as resp:
                            raw = resp.read()
                        if raw and raw[:4] == b"\x89PNG":
                            data = raw
                            break
                        if raw and raw[:2] == b"\xff\xd8":
                            ppm = _jpeg_to_ppm(raw)
                            if ppm:
                                data = ppm
                                break
                    except Exception:
                        data = None
            if not self._destroyed:
                try:
                    self.after(0, self._on_tile_bytes, key, data)
                except tk.TclError:
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_tile_bytes(self, key: tuple[int, int, int], data: bytes | None):
        self._pending_tiles.discard(key)
        if self._destroyed:
            return
        if not data:
            self._failed_tiles.add(key)
            return
        try:
            photo = tk.PhotoImage(data=data)
            self._tile_cache[key] = photo
            self._tile_cache.move_to_end(key)
            while len(self._tile_cache) > _MAX_CACHE_TILES:
                self._tile_cache.popitem(last=False)
            self._redraw()
        except Exception:
            self._failed_tiles.add(key)

    def _redraw(self):
        if self._destroyed or self.collapsed:
            return
        c = self.canvas
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1:
            w = self._canvas_w
        if h <= 1:
            h = self._canvas_h
        c.delete("all")

        # 1. Draw visible satellite MAP tiles (with dark grid placeholder until loaded)
        cx, cy = self._latlon_to_tile(self.center_lat, self.center_lon, self.zoom)
        half_w_tiles = (w / 2.0) / _TILE_SIZE
        half_h_tiles = (h / 2.0) / _TILE_SIZE
        n_tiles = 1 << self.zoom
        tx_min = math.floor(cx - half_w_tiles)
        tx_max = math.floor(cx + half_w_tiles)
        ty_min = max(0, math.floor(cy - half_h_tiles))
        ty_max = min(n_tiles - 1, math.floor(cy + half_h_tiles))

        for tx_raw in range(tx_min, tx_max + 1):
            tx = tx_raw % n_tiles
            px = (tx_raw - cx) * _TILE_SIZE + (w / 2.0)
            for ty in range(ty_min, ty_max + 1):
                py = (ty - cy) * _TILE_SIZE + (h / 2.0)
                key = (self.zoom, tx, ty)
                photo = self._tile_cache.get(key)
                if photo is not None:
                    self._tile_cache.move_to_end(key)
                    c.create_image(px, py, anchor="nw", image=photo)
                else:
                    c.create_rectangle(px, py, px + _TILE_SIZE, py + _TILE_SIZE, fill="#0f172a", outline="#1e293b")
                    self._request_tile(key)

        if not self.waypoints:
            banner = (
                "🛰 MAP — Timeout, awaiting next fix"
                if self.timed_out
                else "🛰 MAP — Waiting for waypoint fix"
            )
            c.create_rectangle(6, 6, w - 6, 26, fill="#091117", outline=BRAND["warn"])
            c.create_text(w / 2.0, 16, text=banner, fill=BRAND["warn"], font=("Segoe UI", 8, "bold"))
            c.create_rectangle(0, h - 20, w, h, fill="#091117", outline="")
            c.create_text(w / 2.0, h - 10, text=f"No Waypoint Yet  |  Z:{self.zoom}  |  MAP", fill="#e2e8f0", font=("Consolas", 8))
            return

        # 2. Draw directional Older -> Newer waypoint string (only after real waypoints arrive)
        wp_pts: list[tuple[float, float]] = []
        for wp in self.waypoints:
            pos = self._latlon_to_canvas(wp["lat"], wp["lon"], w, h)
            wp_pts.append(pos if pos is not None else (0.0, 0.0))

        if len(wp_pts) >= 2:
            flat_pts: list[float] = [coord for pt in wp_pts for coord in pt]
            c.create_line(*flat_pts, fill="#082f49", width=4, smooth=False)
            for i in range(len(wp_pts) - 1):
                x1, y1 = wp_pts[i]
                x2, y2 = wp_pts[i + 1]
                c.create_line(x1, y1, x2, y2, fill="#38bdf8", width=2, arrow=tk.LAST, arrowshape=(7, 9, 3))

        def _outlined_text(x: float, y: float, text: str, fg: str, font_spec):
            c.create_text(x + 1, y + 1, text=text, fill="#091117", anchor="w", font=font_spec)
            c.create_text(x, y, text=text, fill=fg, anchor="w", font=font_spec)

        # 3. Draw numbered waypoint nodes (Oldest, intermediate, Newest, and Selected)
        last_label_pos: tuple[float, float] | None = None
        for i, (wp, (mx, my)) in enumerate(zip(self.waypoints, wp_pts)):
            is_newest = (i == len(self.waypoints) - 1)
            is_oldest = (i == 0 and len(self.waypoints) > 1)
            is_selected = (self.selected_wp_idx == i)

            if is_selected:
                c.create_oval(mx - 10, my - 10, mx + 10, my + 10, outline="#fde047", width=2)

            if is_newest:
                ring_color = "#22c55e" if self.fixed else "#f59e0b"
                dot_color = "#16a34a" if self.fixed else "#f59e0b"
                c.create_oval(mx - 13, my - 13, mx + 13, my + 13, outline=ring_color, width=2)
                c.create_line(mx - 17, my, mx - 6, my, fill="#ffffff", width=1.5)
                c.create_line(mx + 6, my, mx + 17, my, fill="#ffffff", width=1.5)
                c.create_line(mx, my - 17, mx, my - 6, fill="#ffffff", width=1.5)
                c.create_line(mx, my + 6, mx, my + 17, fill="#ffffff", width=1.5)
                c.create_oval(mx - 5, my - 5, mx + 5, my + 5, fill=dot_color, outline="#ffffff", width=1.5)
                tag_txt = f"#{wp['idx']} FIX" if len(self.waypoints) == 1 else f"#{wp['idx']} NEW"
                _outlined_text(mx + 14, my - 10, tag_txt, "#4ade80", ("Consolas", 8, "bold"))
            elif is_oldest:
                c.create_oval(mx - 4.5, my - 4.5, mx + 4.5, my + 4.5, fill="#d97706", outline="#ffffff", width=1.2)
                _outlined_text(mx + 8, my - 8, f"#{wp['idx']} OLD", "#fbbf24", ("Consolas", 8, "bold"))
                last_label_pos = (mx, my)
            else:
                c.create_oval(mx - 3.5, my - 3.5, mx + 3.5, my + 3.5, fill="#0284c7", outline="#ffffff", width=1)
                far_enough = last_label_pos is None or (abs(mx - last_label_pos[0]) > 22 or abs(my - last_label_pos[1]) > 14)
                if far_enough:
                    _outlined_text(mx + 7, my - 6, f"#{wp['idx']}", "#7dd3fc", ("Consolas", 7, "bold"))
                    last_label_pos = (mx, my)

        # 4. Selected waypoint precision callout at top of canvas
        if self.selected_wp_idx is not None and 0 <= self.selected_wp_idx < len(self.waypoints):
            swp = self.waypoints[self.selected_wp_idx]
            role = "OLD" if self.selected_wp_idx == 0 and len(self.waypoints) > 1 else (
                "NEW" if self.selected_wp_idx == len(self.waypoints) - 1 else "WP"
            )
            callout = f"{role} #{swp['idx']} | {swp['lat']:.6f}, {swp['lon']:.6f} | {swp['utc'] or '--'}"
            c.create_rectangle(4, 4, w - 4, 22, fill="#091117", outline="#fde047")
            c.create_text(w / 2.0, 13, text=callout, fill="#fde047", font=("Consolas", 8, "bold"))

        # 5. Bottom HUD strip
        c.create_rectangle(0, h - 20, w, h, fill="#091117", outline="")
        hud = f"Lat: {self.lat:.6f}  Lon: {self.lon:.6f}  |  Z:{self.zoom}  |  MAP"
        c.create_text(w / 2.0, h - 10, text=hud, fill="#e2e8f0", font=("Consolas", 8))
