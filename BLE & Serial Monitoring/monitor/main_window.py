"""@file main_window.py
@brief Main window of the BLE debug monitor: scan/connect bar, one log tab per device, serial terminal, GPS line
@version 2.1
@date 2026-11-22

Everything the BLE and serial threads report arrives as messages on gui_queue; _drain_queue() (polled by Tk)
turns them into log lines, scan results and button states. Buttons start coroutines on the BLE thread with
run_async(). The configuration window of the selected device's type (Valtrack, SC2 hub, GiGO Delphis / beacon,
GiGO Depth & Temp logger) is opened from here. A Valtrack tab's live panel follows the firmware's own log: the
configuration it prints at each report cycle, the 'Report timer: next report in N s' line (exact countdown) and the
measured gap between report cycles, so an interval that does not hold is visible at once.

Each device tab shows what its application needs (live_views.py): the GPS map for the trackers (Valtrack, SC2, whose
GPS fixes 0x12 are put on it too), and above the log a live view for the GiGO devices - attitude and nano-modem for
Delphis, the drum or jack movement for the Rotation / Jack beacon, a depth & temperature chart for the D&T logger and
the air values for the gas logger. The side panel keeps only the lines that mean something for that device type.
"""
from .ble_engine import engine
from .ble_engine import run_async
from .config_codecs import format_seconds
from .config_codecs import parse_config_print
from .config_codecs import parse_next_report
from .config_panel import ConfigPanel
from . import updater
from . import user_settings
from .constants import ADV_NAME_PRESETS
from .constants import BRAND
from .constants import FONT_MONO_FAMILY
from .constants import get_theme_mode
from .constants import next_theme_mode
from .constants import set_theme_mode
from .device_profiles import DEVICE_TYPES
from .device_profiles import device_type_profile
from .device_profiles import normalise_profile
from .device_profiles import profile as get_profile
from .device_profiles import profile_label
from .gas_panel import GasPanel
from .gigo_family_panel import GigoFamilyPanel
from .acoustic_link import drop_link
from .acoustic_link import link_for
from .device_profiles import gigo_roles
from .device_profiles import roles_text
from .gps_map_widget import RealtimeGpsMap
from .live_views import LIVE_VIEW_HEIGHT
from .live_views import Sc2GpsFeed
from .live_views import create_live_view
from .logbus import gui_queue
from .logbus import log_system
from .logbus import post_gui
from .sc2_hub_panel import Sc2HubConfigPanel
from .sc2_panel import Sc2Panel
from .serial_engine import BAUD_RATES
from .serial_engine import EOL_MAP
from .serial_engine import FLOW_MAP
from .serial_engine import PARITY_MAP
from .serial_engine import SERIAL_AVAILABLE
from .serial_engine import STOPBITS_MAP
from .serial_engine import SerialEngine
from .theme import BrandHeader
from .theme import apply_brand_theme
from .theme import apply_os_titlebar_theme
from .theme import apply_window_icon
from .theme import retheme_widget_tree
import asyncio
import os
import sys
import threading
import time
import re
from datetime import datetime, timezone
from tkinter import filedialog
from tkinter import messagebox
from tkinter import scrolledtext
from tkinter import ttk


# Side-panel entries per kind of device: the GPS trackers keep the report-cycle and GPS lines; the GiGO devices have
# neither and show their live-view summary instead (Delphis / beacon have no clock read either).
GPS_TRACKER_PROFILES = ("valtrack", "sc2_gen2", "sc2_gen3")
DASH_HIDDEN_FOR_TRACKERS = ("lbl_app",)
DASH_SHOWN_FOR_IMU = ("btn_close", "lbl_mac", "lbl_batt", "lbl_app")
DASH_SHOWN_FOR_LOGGERS = ("btn_close", "lbl_mac", "lbl_dev_clock", "lbl_batt", "lbl_app")
MAP_PLACE = {"relx": 1.0, "rely": 1.0, "anchor": "se", "x": -22, "y": -10}
# The GiGO devices have no debug log (their tab logs only the tool's own lines), so their live view takes the spare
# height and the log keeps a strip below it; the sash between them can be dragged.
LOG_STRIP_HEIGHT = 110
LOG_STRIP_MIN = 50
import tkinter as tk


# Battery and device clock are refreshed this often while a device is connected.
SESSION_REFRESH_S = 20.0


class BleGuiApp:
    """The main window (see the module header)."""
    def __init__(self, root: tk.Tk):
        """Size and centre the window, apply the brand theme and icon, build the header and the UI,
        start polling the message queue and run a first scan.
        """
        self.root = root
        set_theme_mode(user_settings.get("theme", "dark"))
        self.root.title(f"{BRAND['company']} {BRAND['product']}  v{BRAND['version']}")
        # Auto-adjust window dimensions to screen resolution
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        target_w = max(960, min(1440, int(screen_w * 0.90)))
        target_h = max(640, min(920, int(screen_h * 0.88)))
        pos_x = max(0, (screen_w - target_w) // 2)
        pos_y = max(0, (screen_h - target_h) // 2)
        self.root.geometry(f"{target_w}x{target_h}+{pos_x}+{pos_y}")
        self.root.minsize(min(860, screen_w - 40), min(540, screen_h - 60))
        self.root.configure(bg=BRAND["bg"])
        apply_window_icon(self.root)
        apply_brand_theme(self.root)
        apply_os_titlebar_theme(self.root)

        self.is_connected = False
        self.is_streaming = False
        self.devices_cache = []
        self.total_lines = 0
        self.total_gps_packets = 0
        self.device_log_widgets: dict[str, scrolledtext.ScrolledText] = {}
        self.device_log_counts: dict[str, int] = {}
        self.device_log_frames: dict[str, tk.Frame] = {}
        self.device_telemetry: dict[str, dict] = {}
        self._config_windows: dict[str, tk.Toplevel] = {}
        self._log_font_size = 10
        self._search_matches: list[tuple[str, str]] = []
        self._search_idx = -1
        self._drag_tab_id: "str | None" = None
        self._serial_ansi_fg: "int | None" = None
        self._serial_ansi_bold = False
        self._search_matches: list[tuple[str, str]] = []
        self._search_idx = -1
        self._serial_ansi_fg: "int | None" = None
        self._serial_ansi_bold = False
        self.log_tab_sessions = {}
        self.session_status = {}
        self.var_auto_reconnect = tk.BooleanVar(value=True)
        self.var_auto_debug_log = tk.BooleanVar(value=True)
        engine.auto_reconnect = self.var_auto_reconnect.get()

        self._build_ui()
        self.root.after(40, self.process_queue)
        self.root.after(300, self.do_scan)
        self.root.after(1000, self._tick_device_clocks)
        self.root.after(500, self._tick_utc_clock)
        self.root.after(4000, self._check_update_background)

    def _tick_utc_clock(self):
        """Update the compact UTC clock in the top bar every 500 ms."""
        try:
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            self.lbl_utc_clock.config(text=now_str)
            self.root.after(500, self._tick_utc_clock)
        except tk.TclError:
            pass

    def _set_header_link(self, connected: bool, name: str = ""):
        colour = BRAND["ok"] if connected else BRAND["text_muted"]
        text = f"[{name.upper()}]" if connected and name else ("[CONNECTED]" if connected else "[DISCONNECTED]")
        try:
            self.lbl_link_badge.config(text=text, fg=colour)
        except tk.TclError:
            pass

    def _build_ui(self):
        """Build the window: single merged VS Code-style top bar, compact BLE + Serial rows,
        and the draggable/resizable notebook (Serial Terminal + one tab per device).
        """
        # ---- single merged top bar (no duplicate title bar below OS title bar) ------------
        top_bar = tk.Frame(self.root, bg=BRAND["surface"], padx=8, pady=3)
        top_bar.pack(fill=tk.X)
        tk.Frame(self.root, bg=BRAND["border"], height=1).pack(fill=tk.X)

        self.lbl_status = tk.Label(top_bar, text="Ready", font=("Segoe UI", 8, "bold"),
                                   fg=BRAND["text"], bg=BRAND["border"], padx=7, pady=1)
        self.lbl_status.pack(side=tk.LEFT)

        self.btn_toggle_stream = tk.Button(
            top_bar, text="Pause", command=self.toggle_stream, state=tk.DISABLED,
            bg=BRAND["warn"], fg="#ffffff", font=("Segoe UI", 8, "bold"),
            relief=tk.FLAT, padx=7, pady=1)
        self.btn_toggle_stream.pack(side=tk.LEFT, padx=(6, 3))

        self.var_autoscroll = tk.BooleanVar(value=True)
        tk.Checkbutton(
            top_bar, text="Auto-Scroll", variable=self.var_autoscroll,
            bg=BRAND["surface"], fg=BRAND["text"], selectcolor=BRAND["accent_dark"],
            activebackground=BRAND["surface"], font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(4, 6))

        self.lbl_lines = tk.Label(top_bar, text="Lines: 0", font=(FONT_MONO_FAMILY, 8),
                                  fg=BRAND["text_dim"], bg=BRAND["surface"])
        self.lbl_lines.pack(side=tk.LEFT, padx=(2, 8))

        self.lbl_details = tk.Label(top_bar, text="",
                                    font=(FONT_MONO_FAMILY, 8), fg=BRAND["text_dim"], bg=BRAND["surface"])
        self.lbl_details.pack(side=tk.LEFT, padx=(4, 8))

        # Right side: Link badge, Light/Dark toggle, UTC clock, Save, Clear, Find
        self.lbl_link_badge = tk.Label(
            top_bar, text="[DISCONNECTED]", bg=BRAND["surface"],
            fg=BRAND["text_muted"], font=(FONT_MONO_FAMILY, 8, "bold"))
        self.lbl_link_badge.pack(side=tk.RIGHT, padx=(6, 2))

        self.btn_theme = tk.Button(
            top_bar,
            text=self._theme_button_label(),
            command=self.toggle_theme,
            bg=BRAND["surface_alt"], fg=BRAND["text"],
            activebackground=BRAND["accent_dark"], activeforeground="#ffffff",
            font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=7, pady=1, cursor="hand2", bd=0)
        self.btn_theme.pack(side=tk.RIGHT, padx=(4, 2))

        self.lbl_utc_clock = tk.Label(
            top_bar, text=datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            bg=BRAND["surface_alt"], fg=BRAND["accent"],
            font=(FONT_MONO_FAMILY, 8, "bold"), padx=6, pady=1)
        self.lbl_utc_clock.pack(side=tk.RIGHT, padx=(6, 2))

        self._btn(top_bar, "Save", self._save_logs, BRAND["accent_dark"]).pack(side=tk.RIGHT, padx=(4, 0))
        self._btn(top_bar, "Clear", self._clear_logs, BRAND["border"]).pack(side=tk.RIGHT, padx=(4, 0))
        self._btn(top_bar, "🔍 Find", self._toggle_find_bar, BRAND["surface_alt"]).pack(side=tk.RIGHT, padx=(4, 0))
        self.btn_update = self._btn(top_bar, "↻ Update", self._open_update_dialog, BRAND["surface_alt"])
        self.btn_update.pack(side=tk.RIGHT, padx=(4, 0))
        self._latest_release_info = None

        # Lightweight header adapter so existing self.header.set_link(...) calls keep working seamlessly
        class _HeaderAdapter:
            def __init__(self, app_ref):
                self._app = app_ref
            def set_link(self, connected: bool, name: str = ""):
                self._app._set_header_link(connected, name)
            def refresh_theme_button(self):
                self._app.btn_theme.config(text=self._app._theme_button_label())
        self.header = _HeaderAdapter(self)

        # Global Ctrl+F / Cmd+F opens the floating VS Code-style Find bar
        for seq in ("<Control-f>", "<Control-F>", "<Command-f>", "<Command-F>"):
            self.root.bind_all(seq, self._show_find_bar)

        # ---- compact single-row BLE control bar -------------------------------------------
        ctrl_panel = tk.Frame(self.root, bg=BRAND["surface"], padx=6, pady=3)
        ctrl_panel.pack(fill=tk.X, padx=8, pady=(1, 2))

        self._build_serial_panel()

        tk.Label(ctrl_panel, text="Filter:", font=("Segoe UI", 8, "bold"), fg=BRAND["text_dim"], bg=BRAND["surface"]).pack(side=tk.LEFT, padx=(2, 2))
        self.preset_var = tk.StringVar(value=ADV_NAME_PRESETS[0][0])
        self.preset_combo = ttk.Combobox(ctrl_panel, textvariable=self.preset_var, values=[p[0] for p in ADV_NAME_PRESETS], state="readonly", width=16, font=("Segoe UI", 8))
        self.preset_combo.pack(side=tk.LEFT, padx=2)

        tk.Label(ctrl_panel, text="Type:", font=("Segoe UI", 8, "bold"), fg=BRAND["text_dim"], bg=BRAND["surface"]).pack(side=tk.LEFT, padx=(5, 2))
        self.device_type_var = tk.StringVar(value=DEVICE_TYPES[0][1])
        self.device_type_combo = ttk.Combobox(ctrl_panel, textvariable=self.device_type_var,
                                              values=[label for _key, label, _profile in DEVICE_TYPES],
                                              state="readonly", width=14, font=("Segoe UI", 8))
        self.device_type_combo.pack(side=tk.LEFT, padx=2)

        self.btn_scan = tk.Button(ctrl_panel, text="Scan", command=self.do_scan, bg=BRAND["accent_dark"], fg=BRAND["accent_text"], font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=8, pady=2)
        self.btn_scan.pack(side=tk.LEFT, padx=(4, 4))

        self.device_combo = ttk.Combobox(ctrl_panel, values=[], state="disabled", width=34, font=(FONT_MONO_FAMILY, 8))
        self.device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_combo_change)

        self.btn_connect = tk.Button(ctrl_panel, text="Connect", command=self.do_connect, state=tk.DISABLED, bg=BRAND["ok"], fg="#ffffff", font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=8, pady=2)
        self.btn_connect.pack(side=tk.LEFT, padx=2)

        self.btn_disconnect = tk.Button(ctrl_panel, text="Disconnect", command=self.do_disconnect, state=tk.DISABLED, bg=BRAND["err"], fg="#ffffff", font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=8, pady=2)
        self.btn_disconnect.pack(side=tk.LEFT, padx=2)

        self.btn_config = tk.Button(ctrl_panel, text="Config", command=self.open_config_panel, state=tk.DISABLED, bg=BRAND["accent_dark"], fg=BRAND["accent_text"], font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=8, pady=2)
        self.btn_config.pack(side=tk.LEFT, padx=2)

        self.btn_sensors = tk.Button(ctrl_panel, text="Logger", command=self.open_sensor_panel,
                                     state=tk.DISABLED, bg=BRAND["accent"], fg=BRAND["accent_text"],
                                     font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=7, pady=2)
        self.btn_sensors.pack(side=tk.LEFT, padx=2)

        self.debug_log_on = False
        self.btn_debuglog = tk.Button(
            ctrl_panel, text="Dbg: OFF", command=self.toggle_debug_log,
            state=tk.DISABLED, bg=BRAND["surface_alt"], fg=BRAND["text"],
            font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=7, pady=2)
        self.btn_debuglog.pack(side=tk.LEFT, padx=2)

        self.chk_auto_reconnect = tk.Checkbutton(
            ctrl_panel, text="Auto-Recon", variable=self.var_auto_reconnect,
            command=self._on_toggle_auto_reconnect, bg=BRAND["surface"],
            fg=BRAND["text"], selectcolor=BRAND["accent_dark"],
            activebackground=BRAND["surface"], font=("Segoe UI", 8))
        self.chk_auto_reconnect.pack(side=tk.LEFT, padx=(3, 0))

        # GPS Dashboard (hidden legacy labels kept for compatibility)
        gps_card = tk.Frame(self.root, bg=BRAND["bg"], padx=10, pady=6)
        self.lbl_gps_status = tk.Label(gps_card, text="GPS: Waiting for stream", font=(FONT_MONO_FAMILY, 9, "bold"), fg=BRAND["warn"], bg=BRAND["surface_alt"], padx=8, pady=2)
        self.lbl_gps_time = tk.Label(gps_card, text="UTC Time: --:--:--", font=(FONT_MONO_FAMILY, 9), fg=BRAND["text"], bg=BRAND["bg"])
        self.lbl_gps_coords = tk.Label(gps_card, text="Coords: --", font=(FONT_MONO_FAMILY, 9), fg=BRAND["text"], bg=BRAND["bg"])
        self.lbl_gps_count = tk.Label(gps_card, text="GPS Sentences: 0", font=(FONT_MONO_FAMILY, 9, "bold"), fg=BRAND["ok"], bg=BRAND["bg"])

        # Log Console Window (maximised vertical area)
        self._log_frame = tk.Frame(self.root, bg=BRAND["bg"])
        self._log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        self.log_notebook = ttk.Notebook(self._log_frame)
        self.log_notebook.pack(fill=tk.BOTH, expand=True)
        self.log_notebook.bind("<<NotebookTabChanged>>", self._on_log_tab_changed)
        self.log_notebook.bind("<Button-1>", self._on_tab_left_click)
        self.log_notebook.bind("<B1-Motion>", self._on_tab_drag)
        self.log_notebook.bind("<ButtonRelease-1>", self._on_tab_release)

        # Floating VS Code-style Find overlay (top-right corner of log pane; hidden until Ctrl+F / Find button)
        self._find_visible = False
        self._find_case_var = tk.BooleanVar(value=False)
        self._find_regex_var = tk.BooleanVar(value=False)
        self._find_bar = tk.Frame(
            self._log_frame, bg=BRAND["surface"], padx=6, pady=4,
            highlightthickness=1, highlightbackground=BRAND["accent"],
        )
        self.search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            self._find_bar, textvariable=self.search_var, width=22,
            bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground=BRAND["text"],
            font=(FONT_MONO_FAMILY, 9), relief=tk.FLAT,
            highlightthickness=1, highlightbackground=BRAND["border"], highlightcolor=BRAND["accent"],
        )
        self._search_entry.pack(side=tk.LEFT, padx=(0, 4), ipady=2)
        self._search_entry.bind("<Return>", lambda e: self._search_next())
        self._search_entry.bind("<Shift-Return>", lambda e: self._search_prev())
        self._search_entry.bind("<Escape>", lambda e: self._hide_find_bar())
        self.search_var.trace_add("write", lambda *_: self._search_apply())

        self._btn_find_case = tk.Button(
            self._find_bar, text="Aa", command=self._toggle_find_case,
            bg=BRAND["surface_alt"], fg=BRAND["text_dim"], font=(FONT_MONO_FAMILY, 8, "bold"),
            relief=tk.FLAT, padx=4, pady=1, cursor="hand2", bd=0,
        )
        self._btn_find_case.pack(side=tk.LEFT, padx=1)

        self._btn_find_regex = tk.Button(
            self._find_bar, text=".*", command=self._toggle_find_regex,
            bg=BRAND["surface_alt"], fg=BRAND["text_dim"], font=(FONT_MONO_FAMILY, 8, "bold"),
            relief=tk.FLAT, padx=4, pady=1, cursor="hand2", bd=0,
        )
        self._btn_find_regex.pack(side=tk.LEFT, padx=(1, 4))

        self.lbl_search_count = tk.Label(
            self._find_bar, text="", font=(FONT_MONO_FAMILY, 8),
            fg=BRAND["text_dim"], bg=BRAND["surface"], width=10, anchor="e",
        )
        self.lbl_search_count.pack(side=tk.LEFT, padx=(2, 4))

        self._btn(self._find_bar, "↑", self._search_prev, BRAND["surface_alt"]).pack(side=tk.LEFT, padx=1)
        self._btn(self._find_bar, "↓", self._search_next, BRAND["surface_alt"]).pack(side=tk.LEFT, padx=1)
        self._btn(self._find_bar, "×", self._hide_find_bar, BRAND["surface_alt"]).pack(side=tk.LEFT, padx=(2, 0))

        # Tab management: inline '×' close button, right-click menu, and middle-click to close
        self.tab_menu = tk.Menu(self.root, tearoff=0, bg=BRAND["surface"], fg=BRAND["text"],
                                activebackground=BRAND["accent"], activeforeground=BRAND["accent_text"])
        self.tab_menu.add_command(label="Save Tab Log...", command=self._save_logs)
        self.tab_menu.add_command(label="Clear Tab Log", command=self.clear_logs)
        self.tab_menu.add_separator()
        self.tab_menu.add_command(label="Close Tab", command=self._close_current_tab)
        self.tab_menu.add_command(label="Close Disconnected Tabs", command=self._close_disconnected_tabs)

        if sys.platform == "darwin":
            self.log_notebook.bind("<Button-2>", self._on_tab_right_click)
            self.log_notebook.bind("<Control-Button-1>", self._on_tab_right_click)
            self.log_notebook.bind("<Button-3>", self._on_tab_middle_click)
        else:
            self.log_notebook.bind("<Button-3>", self._on_tab_right_click)
            self.log_notebook.bind("<Button-2>", self._on_tab_middle_click)

        # Tab 1: Dedicated Serial Terminal (no System Log tab)
        self.serial_tab = tk.Frame(self.log_notebook, bg=BRAND["bg"])
        self.log_notebook.add(self.serial_tab, text="Serial Terminal")
        self.log_tab_sessions[str(self.serial_tab)] = "serial"
        self.txt_serial = scrolledtext.ScrolledText(
            self.serial_tab, bg=BRAND["log_bg"], fg=BRAND["log_fg"],
            font=(FONT_MONO_FAMILY, self._log_font_size), padx=8, pady=6, wrap=tk.NONE)
        self.txt_serial.pack(fill=tk.BOTH, expand=True)
        self._configure_log_widget(self.txt_serial)
        self.txt_serial.bind("<Key>", self._on_serial_terminal_key)
        self.system_tab = None
        self.txt_terminal = self.txt_serial

    def _apply_log_widget_colors(self, widget):
        """Apply current Light/Dark theme colours and font size to a log/terminal Text widget."""
        sz = self._log_font_size
        font_reg = (FONT_MONO_FAMILY, sz)
        font_bold = (FONT_MONO_FAMILY, sz, "bold")
        widget.configure(bg=BRAND["log_bg"], fg=BRAND["log_fg"], font=font_reg,
                         insertbackground=BRAND["log_fg"],
                         selectbackground=BRAND["accent_dark"],
                         selectforeground=BRAND["accent_text"])
        widget.tag_config("default", foreground=BRAND["log_fg"])
        widget.tag_config("device", foreground=BRAND["log_device"])
        widget.tag_config("system", foreground=BRAND["log_sys"])
        widget.tag_config("gps", foreground=BRAND["log_gps"], font=font_bold)
        widget.tag_config("warning", foreground=BRAND["log_warn"])
        widget.tag_config("error", foreground=BRAND["log_err"])
        widget.tag_config("diag_warn", foreground=BRAND["log_warn"])
        widget.tag_config("diag_err", foreground=BRAND["log_err"])
        widget.tag_config("diag_info", foreground=BRAND["log_sys"])
        widget.tag_config("hi_success", foreground="#052e16", background="#4ade80", font=font_bold)
        widget.tag_config("hi_warn", foreground="#451a03", background="#fbbf24", font=font_bold)
        widget.tag_config("hi_error", foreground="#450a0a", background="#f87171", font=font_bold)
        widget.tag_config("search_hit", background=BRAND["warn"], foreground="#000000")
        widget.tag_config("search_cur", background=BRAND["accent"], foreground=BRAND["accent_text"])
        # Native ANSI SGR terminal colours (30..37, 90..97) adapted for Dark & Light themes
        ansi_map = {
            30: "#64748b", 31: "#f87171", 32: "#4ade80", 33: "#facc15",
            34: "#60a5fa", 35: "#e879f9", 36: "#22d3ee", 37: "#f8fafc",
            90: "#94a3b8", 91: "#fca5a5", 92: "#86efac", 93: "#fde047",
            94: "#93c5fd", 95: "#f0abfc", 96: "#67e8f9", 97: "#ffffff",
        } if get_theme_mode() in ("dark", "black") else {
            30: "#334155", 31: "#dc2626", 32: "#15803d", 33: "#b45309",
            34: "#1d4ed8", 35: "#a21caf", 36: "#0e7490", 37: "#1e293b",
            90: "#64748b", 91: "#ef4444", 92: "#16a34a", 93: "#ca8a04",
            94: "#2563eb", 95: "#c026d3", 96: "#0891b2", 97: "#0f172a",
        }
        for code, col in ansi_map.items():
            widget.tag_config(f"ansi_{code}", foreground=col)
            widget.tag_config(f"ansi_{code}_b", foreground=col, font=font_bold)
        widget.tag_config("ansi_bold", font=font_bold)

    def _configure_log_widget(self, widget):
        """Give a log text widget crisp standard text, colour tags, and VS Code-style wheel zoom."""
        self._apply_log_widget_colors(widget)
        # VS Code-style text zoom: Ctrl+Wheel (Win/Linux), Command+Wheel (macOS), Ctrl/Cmd + / - / 0
        widget.bind("<Control-MouseWheel>", self._on_log_wheel_zoom)
        widget.bind("<Command-MouseWheel>", self._on_log_wheel_zoom)
        widget.bind("<Control-Button-4>", lambda _e: self._zoom_log_font(1))
        widget.bind("<Control-Button-5>", lambda _e: self._zoom_log_font(-1))
        for mod in ("Control", "Command"):
            widget.bind(f"<{mod}-plus>", lambda _e: self._zoom_log_font(1))
            widget.bind(f"<{mod}-equal>", lambda _e: self._zoom_log_font(1))
            widget.bind(f"<{mod}-KP_Add>", lambda _e: self._zoom_log_font(1))
            widget.bind(f"<{mod}-minus>", lambda _e: self._zoom_log_font(-1))
            widget.bind(f"<{mod}-KP_Subtract>", lambda _e: self._zoom_log_font(-1))
            widget.bind(f"<{mod}-0>", lambda _e: self._set_log_font_size(10))
            widget.bind(f"<{mod}-KP_0>", lambda _e: self._set_log_font_size(10))

    @staticmethod
    def _theme_button_label() -> str:
        nxt = next_theme_mode()
        return {"dark": "🌙 Dark", "black": "⬛ Black", "light": "☀ Light"}[nxt]

    def toggle_theme(self):
        """Cycle live between Dark, Pure Black, and Light themes and remember the choice."""
        new_mode = next_theme_mode()
        old_palette = set_theme_mode(new_mode)
        user_settings.put("theme", new_mode)
        apply_brand_theme(self.root)
        retheme_widget_tree(self.root, old_palette, BRAND)
        apply_os_titlebar_theme(self.root)
        for w in (getattr(self, "txt_serial", None), getattr(self, "txt_terminal", None),
                  *self.device_log_widgets.values()):
            if w is not None:
                try:
                    self._apply_log_widget_colors(w)
                except tk.TclError:
                    pass
        self.header.refresh_theme_button()

    def _on_log_wheel_zoom(self, event):
        delta = getattr(event, "delta", 0)
        if delta > 0:
            return self._zoom_log_font(1)
        if delta < 0:
            return self._zoom_log_font(-1)
        return "break"

    def _zoom_log_font(self, step: int):
        return self._set_log_font_size(self._log_font_size + step)

    def _set_log_font_size(self, size: int):
        new_size = max(7, min(28, int(size)))
        if new_size == self._log_font_size:
            return "break"
        self._log_font_size = new_size
        font_reg = (FONT_MONO_FAMILY, new_size)
        font_bold = (FONT_MONO_FAMILY, new_size, "bold")
        widgets = [getattr(self, "txt_serial", None), getattr(self, "txt_terminal", None)]
        widgets.extend(self.device_log_widgets.values())
        for w in widgets:
            if w is None:
                continue
            try:
                w.configure(font=font_reg)
                for tag in ("gps", "hi_success", "hi_warn", "hi_error"):
                    w.tag_config(tag, font=font_bold)
            except tk.TclError:
                pass
        return "break"

    @staticmethod
    def _classify_log_tag(msg: str, default_tag: str = "default") -> str:
        """Return the Tk text tag name for a log line across BLE and Serial streams."""
        if "GPS NOT FIXED" in msg:
            return "hi_warn"
        if "GPS FIXED" in msg or "Reporting Successful" in msg:
            return "hi_success"
        if "Reporting Error" in msg:
            return "hi_error"
        if "GPS DATA:" in msg or "$GN" in msg or "$GP" in msg:
            return "gps"
        if "[SYSTEM]" in msg:
            return "system"
        if "INTENTIONAL RESET:" in msg:
            return "diag_warn"
        if "LAST RESET :" in msg or "DIAG fault=" in msg:
            return "diag_err"
        if "RESET REASON:" in msg or "DIAG rst" in msg:
            return "diag_warn"
        if "LAST WAYPOINT:" in msg or "DIAG prev alive=" in msg:
            return "diag_info"
        return default_tag

    def _ensure_device_log_tab(self, session_id, name, address, profile):
        """Return the log widget of a device session, creating its tab on first use (and
        renaming it when the name or address changed).
        """
        if session_id in self.device_log_widgets:
            frame = self.device_log_frames.get(session_id)
            if frame:
                status = self.session_status.get(session_id, "")
                suffix = f" - {status.upper()}" if status else ""
                self.log_notebook.tab(frame, text=f"{name} [{address}]{suffix}   ×")
            return self.device_log_widgets[session_id]

        frame = tk.Frame(self.log_notebook, bg=BRAND["bg"])
        status = self.session_status.get(session_id, "connecting")
        suffix = f" - {status.upper()}" if status else ""
        self.log_notebook.add(frame, text=f"{name} [{address}]{suffix}   ×")
        self.log_tab_sessions[str(frame)] = session_id

        # -----------------------------------------------------------------------------------
        # Resizable VS Code-style Split Pane: Log/Map (left) | Scrollable Telemetry (right)
        # -----------------------------------------------------------------------------------
        h_split = tk.PanedWindow(frame, orient=tk.HORIZONTAL, sashwidth=6, sashrelief=tk.FLAT,
                                 bg=BRAND["border"], bd=0)
        h_split.pack(fill=tk.BOTH, expand=True)

        log_pane = tk.PanedWindow(h_split, orient=tk.VERTICAL, sashwidth=6, sashrelief=tk.FLAT,
                                  bg=BRAND["border"], bd=0)
        h_split.add(log_pane, stretch="always", minsize=320)

        dash_outer = tk.Frame(h_split, bg=BRAND["surface"], width=265)
        h_split.add(dash_outer, stretch="never", minsize=190, width=265)

        dash_canvas = tk.Canvas(dash_outer, bg=BRAND["surface"], highlightthickness=0, bd=0)
        dash_sb = ttk.Scrollbar(dash_outer, orient=tk.VERTICAL, command=dash_canvas.yview)
        dash_canvas.configure(yscrollcommand=dash_sb.set)
        dash_sb.pack(side=tk.RIGHT, fill=tk.Y)
        dash_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        dash = tk.Frame(dash_canvas, bg=BRAND["surface"], padx=6, pady=4, relief=tk.FLAT)
        dash_win_id = dash_canvas.create_window((0, 0), window=dash, anchor="nw")

        def _sync_dash_scroll(_e=None, c=dash_canvas, d=dash):
            c.configure(scrollregion=c.bbox("all"))

        def _sync_dash_width(event, c=dash_canvas, wid=dash_win_id):
            c.itemconfigure(wid, width=event.width)

        def _on_dash_wheel(event, c=dash_canvas):
            delta = getattr(event, "delta", 0)
            if delta:
                step = -1 if delta > 0 else 1
                if abs(delta) >= 120:
                    step = int(-delta / 120)
                c.yview_scroll(step, "units")
                return "break"
            return None

        dash.bind("<Configure>", _sync_dash_scroll)
        dash_canvas.bind("<Configure>", _sync_dash_width)
        dash_canvas.bind("<MouseWheel>", _on_dash_wheel)
        dash_canvas.bind("<Button-4>", lambda _e, c=dash_canvas: c.yview_scroll(-1, "units"))
        dash_canvas.bind("<Button-5>", lambda _e, c=dash_canvas: c.yview_scroll(1, "units"))

        # Row 1: Primary Telemetry, Reporting Counters, Device Actual Clock, Close Tab button
        row1 = tk.Frame(dash, bg=BRAND["surface"])
        row1.pack(fill=tk.X)

        # Right corner (operator's perspective): Dedicated Close Tab button
        btn_close = tk.Button(
            row1, text="✕ Close Tab", font=("Segoe UI", 8, "bold"),
            bg="#7f1d1d", fg="#fee2e2", activebackground="#b91c1c", activeforeground="#ffffff",
            relief=tk.FLAT, padx=8, pady=1, cursor="hand2",
            command=lambda s=session_id: self._close_device_tab(s)
        )
        btn_close.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        # Respective device MAC address, IMEI (for cellular trackers) & auto-detected profile
        lbl_mac = tk.Label(
            row1, text=self._format_identity_text(address, profile, self._selected_device_type(), None),
            font=("Consolas", 8, "bold"), fg=BRAND["text_dim"], bg=BRAND["surface_alt"],
            padx=6, pady=2, anchor="w", justify=tk.LEFT
        )
        lbl_mac.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Per-device Auto Debug Log toggle inside vertical panel
        var_auto_debug = tk.BooleanVar(value=True)
        chk_auto_debug = tk.Checkbutton(
            row1, text="Auto Debug Log", variable=var_auto_debug,
            command=lambda s=session_id: self._on_toggle_device_auto_debug(s),
            bg=BRAND["surface_alt"], fg=BRAND["text"], selectcolor=BRAND["accent_dark"],
            activebackground=BRAND["surface_alt"], activeforeground=BRAND["text"],
            font=("Segoe UI", 8, "bold"), anchor="w", padx=4, pady=2
        )
        chk_auto_debug.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Device Actual Clock (ticks live in UTC)
        lbl_dev_clock = tk.Label(
            row1, text="🕒 Dev Clock: --:--:-- UTC",
            font=("Consolas", 9, "bold"), fg="#38bdf8", bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT
        )
        lbl_dev_clock.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Live Firmware Behavior / Active Phase
        lbl_behavior = tk.Label(
            row1, text="⚙️ Phase: Standby / Observing",
            font=("Consolas", 8, "bold"), fg="#e2e8f0", bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT, wraplength=220
        )
        lbl_behavior.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # GI (GSM Reporting Interval) & Live Countdown
        lbl_gi = tk.Label(
            row1, text="📶 GI: --",
            font=("Consolas", 8, "bold"), fg=BRAND["accent"], bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT, wraplength=220
        )
        lbl_gi.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # GPSI (GPS Interval) & Live Countdown
        lbl_gpsi = tk.Label(
            row1, text="🛰 GPSI: --",
            font=("Consolas", 8, "bold"), fg=BRAND["accent"], bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT, wraplength=245
        )
        lbl_gpsi.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # GPS Fix Status & Timeout
        lbl_gps_status = tk.Label(
            row1, text="GPS: Idle (120s max)",
            font=("Consolas", 8, "bold"), fg=BRAND["text_dim"], bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT, wraplength=220
        )
        lbl_gps_status.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Battery
        lbl_batt = tk.Label(
            row1, text="🔋 Batt: --",
            font=("Consolas", 8), fg="#4ade80", bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT
        )
        lbl_batt.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Application summary of the GiGO devices (filled by the live view; hidden for the GPS trackers)
        lbl_app = tk.Label(
            row1, text="📊 Live view: press Start", font=("Consolas", 8, "bold"), fg=BRAND["text"],
            bg=BRAND["surface_alt"], padx=6, pady=3, anchor="w", justify=tk.LEFT, wraplength=220
        )
        lbl_app.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Row 2: Live activities: Low Power, Motion, LED status, Coordinates, Uptime
        row2 = tk.Frame(dash, bg=BRAND["surface"])
        row2.pack(fill=tk.X, pady=(4, 0))

        # Low Power Status
        lbl_lowpower = tk.Label(
            row2, text="⚡ Low Power: --",
            font=("Segoe UI", 8, "bold"), fg="#94a3b8", bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT
        )
        lbl_lowpower.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Motion Detection Status
        lbl_motion = tk.Label(
            row2, text="🏃 Motion: --",
            font=("Segoe UI", 8, "bold"), fg="#94a3b8", bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT
        )
        lbl_motion.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # LED Fix / Active Status
        lbl_led = tk.Label(
            row2, text="💡 LEDs: --",
            font=("Segoe UI", 8, "bold"), fg="#94a3b8", bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT
        )
        lbl_led.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # GPS UTC time (from NMEA) and sentence counter
        lbl_utc = tk.Label(
            row2, text="🌐 GPS UTC: --:--:--",
            font=("Consolas", 8), fg=BRAND["text"], bg=BRAND["surface_alt"],
            padx=6, pady=3, anchor="w", justify=tk.LEFT
        )
        lbl_utc.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)
        lbl_sent = tk.Label(
            row2, text="📡 GPS Sentences: 0",
            font=("Consolas", 8, "bold"), fg=BRAND["ok"], bg=BRAND["surface"],
            anchor="w", justify=tk.LEFT
        )
        lbl_sent.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Coordinates (two lines so nothing is cut off)
        lbl_coords = tk.Label(
            row2, text="📍 Lat: --\n     Lon: --",
            font=("Consolas", 8), fg=BRAND["text_dim"], bg=BRAND["surface"],
            anchor="w", justify=tk.LEFT, wraplength=210
        )
        lbl_coords.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Device Uptime
        lbl_uptime = tk.Label(
            row2, text="⏱️ Uptime: --",
            font=("Consolas", 8), fg=BRAND["text_dim"], bg=BRAND["surface"],
            anchor="w", justify=tk.LEFT, wraplength=210
        )
        lbl_uptime.pack(side=tk.TOP, fill=tk.X, anchor=tk.W, pady=2)

        # Bind mouse-wheel on all sidebar child widgets so hovering anywhere scrolls the sidebar cleanly
        def _bind_wheel_recursive(w):
            w.bind("<MouseWheel>", _on_dash_wheel, add="+")
            w.bind("<Button-4>", lambda _e, c=dash_canvas: c.yview_scroll(-1, "units"), add="+")
            w.bind("<Button-5>", lambda _e, c=dash_canvas: c.yview_scroll(1, "units"), add="+")
            for ch in w.winfo_children():
                _bind_wheel_recursive(ch)
        _bind_wheel_recursive(dash)

        # Live view of the device's application above the log (GiGO devices, added by _apply_tab_profile), the log
        # text, and the floating GPS map in its bottom-right corner (GPS trackers)
        log_container = tk.Frame(log_pane, bg=BRAND["bg"])
        log_pane.add(log_container, stretch="always")

        text = scrolledtext.ScrolledText(
            log_container, bg=BRAND["log_bg"], fg=BRAND["log_fg"],
            font=(FONT_MONO_FAMILY, self._log_font_size), padx=8, pady=6, wrap=tk.NONE)
        text.pack(fill=tk.BOTH, expand=True)
        self._configure_log_widget(text)

        gps_map = RealtimeGpsMap(log_container)
        gps_map.place(**MAP_PLACE)

        # Side-panel widgets in display order with their pack options (_apply_tab_profile shows a subset)
        dash_widgets = []
        for name, widget in (("btn_close", btn_close), ("lbl_mac", lbl_mac), ("chk_auto_debug", chk_auto_debug),
                             ("lbl_dev_clock", lbl_dev_clock), ("lbl_behavior", lbl_behavior), ("lbl_gi", lbl_gi),
                             ("lbl_gpsi", lbl_gpsi), ("lbl_gps_status", lbl_gps_status), ("lbl_batt", lbl_batt),
                             ("lbl_app", lbl_app), ("lbl_lowpower", lbl_lowpower), ("lbl_motion", lbl_motion),
                             ("lbl_led", lbl_led), ("lbl_utc", lbl_utc), ("lbl_sent", lbl_sent),
                             ("lbl_coords", lbl_coords), ("lbl_uptime", lbl_uptime)):
            dash_widgets.append((name, widget, widget.pack_info()))

        self.device_log_widgets[session_id] = text
        self.device_log_frames[session_id] = frame
        self.device_log_counts[session_id] = 0

        # Register telemetry state store
        now_ts = time.time()
        self.device_telemetry[session_id] = {
            "dash": dash,
            "dash_widgets": dash_widgets,
            "gps_map": gps_map,
            "log_pane": log_pane,
            "log_container": log_container,
            "live_view": None,
            "live_profile": "",
            "gps_feed": None,
            "lbl_app": lbl_app,
            "lbl_mac": lbl_mac,
            "var_auto_debug": var_auto_debug,
            "chk_auto_debug": chk_auto_debug,
            "auto_debug_enabled": True,
            "address": address,
            "imei": None,
            "profile": profile,
            "lbl_dev_clock": lbl_dev_clock,
            "lbl_behavior": lbl_behavior,
            "lbl_gi": lbl_gi,
            "lbl_gpsi": lbl_gpsi,
            "lbl_gps_status": lbl_gps_status,
            "lbl_batt": lbl_batt,
            "lbl_lowpower": lbl_lowpower,
            "lbl_motion": lbl_motion,
            "lbl_led": lbl_led,
            "lbl_coords": lbl_coords,
            "lbl_utc": lbl_utc,
            "lbl_sent": lbl_sent,
            "gps_sentences": 0,
            "lbl_uptime": lbl_uptime,
            "btn_close": btn_close,
            "dev_epoch": None,
            "dev_epoch_monotonic": 0.0,
            # Unknown until read from the device or its log (a guessed 900 s showed wrong countdowns).
            "gi_interval_sec": None,
            "gpsi_interval_sec": None,
            "cycle_start_ts": None,
            "last_gap_s": None,
            "report_cycles": 0,
            "report_timer_exact": False,
            "gi_last_report": None,
            "gpsi_last_fix": None,
            "fw_phase": "OBSERVING",
            "phase_start_ts": now_ts,
            "gps_fix_start_ts": None,
            "gps_timeout_sec": 120,
            "gps_last_result": None,
            "gsm_state_num": None,
            "gsm_state_name": None,
            "gsm_csq": None,
            "gsm_last_result": None,
            "gsm_done_ts": None,
            "timer_synced": False,
            "low_power": None,
            "motion_enabled": None,
            "motion_thresh": None,
            "led_all_flag": None,
            "device_type": self._selected_device_type(),
            "gps_fix_state": "IDLE",
            "battery_pct": None,
            "battery_mv": None,
            "coords": "--",
            "uptime_sec": None,
            "uptime_monotonic": 0.0,
        }

        self.log_notebook.select(frame)
        engine.select_session(session_id)
        return text

    def _close_device_tab(self, session_id: str):
        """Cleanly close a device session tab and free its UI and state resources."""
        if session_id not in self.device_log_frames:
            return

        status = self.session_status.get(session_id, "disconnected")
        sess = engine.sessions.get(session_id)

        # If connected or connecting, confirm with the operator before closing
        if status in ("connected", "connecting") or (sess and sess.connected):
            dev_name = sess.name if sess else session_id
            if not messagebox.askyesno(
                "Close Active Device Tab",
                f"Device '{dev_name}' [{session_id}] is currently {status.upper()}.\n\n"
                f"Do you want to disconnect and close this tab?"
            ):
                return
            self._close_live_parts(session_id)
            run_async(engine.disconnect_selected(), session_id=session_id)

        self._close_live_parts(session_id)
        drop_link(session_id)
        frame = self.device_log_frames.pop(session_id, None)
        if frame:
            try:
                self.log_notebook.forget(frame)
                frame.destroy()
            except Exception:
                pass

        self.device_log_widgets.pop(session_id, None)
        self.device_log_counts.pop(session_id, None)
        self.device_telemetry.pop(session_id, None)
        self.log_tab_sessions.pop(str(frame), None)
        self.session_status.pop(session_id, None)

        try:
            cur = self.log_notebook.select()
            if not cur and self.log_notebook.tabs():
                self.log_notebook.select(self.log_notebook.tabs()[-1])
        except Exception:
            pass
        self._sync_selected_session_controls()

    def _close_live_parts(self, session_id: str):
        """Stop the tab's live view and GPS feed (they leave the device's streams) before the tab goes."""
        telem = self.device_telemetry.get(session_id) or {}
        for key in ("live_view", "gps_feed"):
            part = telem.get(key)
            if part is not None:
                try:
                    part.close()
                except Exception:
                    pass
                telem[key] = None

    def _on_tab_left_click(self, event):
        """VS Code-style inline '×' close button + start drag-to-reorder on any tab."""
        self._drag_tab_id = None
        try:
            clicked_tab = self.log_notebook.tk.call(self.log_notebook._w, "identify", "tab", event.x, event.y)
            if clicked_tab is None or str(clicked_tab) == "":
                return None
            tab_idx = int(clicked_tab)
            tab_id = self.log_notebook.tabs()[tab_idx]
            session_id = self.log_tab_sessions.get(str(tab_id))
            if session_id and session_id not in ("serial", "system"):
                # If 22 px to the right is no longer the same tab, the click landed on the trailing '×'.
                probe = self.log_notebook.tk.call(self.log_notebook._w, "identify", "tab", event.x + 22, event.y)
                if str(probe) != str(tab_idx):
                    self._close_device_tab(session_id)
                    return "break"
            self._drag_tab_id = tab_id
        except Exception:
            pass
        return None

    def _on_tab_drag(self, event):
        """Drag any tab left or right to reorder tabs (VS Code style)."""
        if not self._drag_tab_id:
            return
        try:
            target = self.log_notebook.tk.call(self.log_notebook._w, "identify", "tab", event.x, event.y)
            if target is None or str(target) == "":
                return
            target_idx = int(target)
            tabs = list(self.log_notebook.tabs())
            if self._drag_tab_id not in tabs:
                self._drag_tab_id = None
                return
            cur_idx = tabs.index(self._drag_tab_id)
            if target_idx != cur_idx:
                self.log_notebook.insert(target_idx, self._drag_tab_id)
        except Exception:
            pass

    def _on_tab_release(self, _event):
        self._drag_tab_id = None

    def _on_tab_right_click(self, event):
        """Show context menu on right click of a notebook tab."""
        try:
            clicked_tab = self.log_notebook.tk.call(self.log_notebook._w, "identify", "tab", event.x, event.y)
            if clicked_tab is not None and str(clicked_tab) != "":
                tab_idx = int(clicked_tab)
                tab_id = self.log_notebook.tabs()[tab_idx]
                self.log_notebook.select(tab_id)
                session_id = self.log_tab_sessions.get(str(tab_id))
                self.clicked_tab_session = session_id
                self.tab_menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            pass

    def _on_tab_middle_click(self, event):
        """Middle click closes the device tab under cursor."""
        try:
            clicked_tab = self.log_notebook.tk.call(self.log_notebook._w, "identify", "tab", event.x, event.y)
            if clicked_tab is not None and str(clicked_tab) != "":
                tab_idx = int(clicked_tab)
                tab_id = self.log_notebook.tabs()[tab_idx]
                session_id = self.log_tab_sessions.get(str(tab_id))
                if session_id and session_id not in ("serial", "system"):
                    self._close_device_tab(session_id)
        except Exception:
            pass

    def _close_current_tab(self):
        """Close the currently selected or right-clicked tab."""
        session_id = getattr(self, "clicked_tab_session", None)
        if not session_id:
            cur = self.log_notebook.select()
            session_id = self.log_tab_sessions.get(str(cur))
        if session_id and session_id not in ("serial", "system"):
            self._close_device_tab(session_id)

    def _close_disconnected_tabs(self):
        """Close all tabs whose device is currently disconnected."""
        for s_id in list(self.device_log_frames.keys()):
            st = self.session_status.get(s_id, "disconnected")
            sess = engine.sessions.get(s_id)
            if st == "disconnected" and (not sess or not sess.connected):
                self._close_device_tab(s_id)

    def _restore_motion_badge(self, session_id: str):
        """Restore motion badge to its configured state after a detection flash."""
        if session_id in self.device_telemetry:
            telem = self.device_telemetry[session_id]
            me = telem.get("motion_enabled")
            thresh = telem.get("motion_thresh")
            thresh_text = f" ({thresh}%)" if thresh is not None else ""
            if me == 1:
                telem["lbl_motion"].config(text=f"🏃 Motion: ARMED{thresh_text}", bg="#065f46", fg="#a7f3d0")
            elif me == 0:
                telem["lbl_motion"].config(text="🏃 Motion: OFF", bg=BRAND["surface_alt"], fg="#94a3b8")
            else:
                telem["lbl_motion"].config(text="🏃 Motion: --", bg=BRAND["surface_alt"], fg="#94a3b8")

    def _inspect_device_log_line(self, msg: str, raw_line: str, session_id: "str | None"):
        """Extract live telemetry (CCLK, uptime, motion events, transmit timestamps) from log text."""
        sid = session_id or engine.selected_session_id
        if not sid or sid not in self.device_telemetry:
            return

        telem = self.device_telemetry[sid]
        combined = f"{msg} {raw_line}"

        # 1. Modem CCLK timestamp (e.g. CCLK raw: '26/11/17,11:27:04+00' or +CCLK: "26/11/17,11:27:04+00")
        m_cclk = re.search(r'CCLK(?:\s*raw)?:\s*[\'"]?(\d{2})/(\d{2})/(\d{2}),(\d{2}):(\d{2}):(\d{2})', combined)
        if m_cclk:
            try:
                yr, mo, dy, hr, mn, sc = map(int, m_cclk.groups())
                dt = datetime(2000 + yr, mo, dy, hr, mn, sc, tzinfo=timezone.utc)
                telem["dev_epoch"] = dt.timestamp()
                telem["dev_epoch_monotonic"] = time.monotonic()
                telem["lbl_dev_clock"].config(text=f"🕒 Dev Clock: {dt.strftime('%H:%M:%S UTC')}")
            except Exception:
                pass

        # 2. Device uptime in ms: up=(\d+)ms
        m_up = re.search(r'up=(\d+)ms', combined)
        if m_up:
            try:
                ms = int(m_up.group(1))
                telem["uptime_sec"] = ms // 1000
                telem["uptime_monotonic"] = time.monotonic()
            except Exception:
                pass

        # 3. Firmware cycle start & GPS search start (app_sm.c NORMAL_PROCES / GpsGetFix)
        now_wall = time.time()
        gpsi_cfg = telem.get("gpsi_interval_sec") or 0
        gi_cfg = telem.get("gi_interval_sec") or 0

        # 2b. Configuration printed by the firmware at every report cycle (also after an SMS or server change).
        printed = parse_config_print(combined)
        if printed is not None and printed[0] in ("gsm_interval", "gps_interval", "low_power_flag", "motion_enable",
                                                   "motion_thresh", "led_all_flag"):
            self._apply_device_config(sid, {printed[0]: printed[1]})

        # 2c. Start of a report cycle: measure the real gap to the previous one (the interval the server sees).
        if "Waypoint Start Reporting" in combined:
            previous = telem.get("cycle_start_ts")
            if previous is not None:
                telem["last_gap_s"] = int(round(time.time() - previous))
            telem["cycle_start_ts"] = time.time()
            telem["report_cycles"] = telem.get("report_cycles", 0) + 1
            telem["report_timer_exact"] = False

        # 2d. 'Report timer: next report in N s': the firmware's own schedule (fixed rate since 2026-11-21).
        next_report = parse_next_report(combined)
        if next_report is not None:
            if next_report["interval_s"]:
                telem["gi_interval_sec"] = next_report["interval_s"]
            interval = telem.get("gi_interval_sec") or 0
            telem["gi_last_report"] = time.time() + next_report["next_s"] - interval
            telem["report_timer_exact"] = True
        default_gps_timeout = 33 if (0 < gpsi_cfg <= 60) else 120

        if any(w in combined for w in ("Waypoint Start Reporting", "GPS: Boot init command sent to queue", "GPS soft-start")):
            telem["fw_phase"] = "GPS_FIXING"
            telem["phase_start_ts"] = now_wall
            telem["gps_fix_start_ts"] = now_wall
            telem["gps_timeout_sec"] = default_gps_timeout
            telem["gsm_state_num"] = None
            telem["gsm_state_name"] = None
            telem["gsm_csq"] = None
            telem["gsm_done_ts"] = None
            if gpsi_cfg > 0 and gpsi_cfg != gi_cfg:
                # Independent GPS timer (AppSmGpsCallback) restarts at the start of the fix request
                telem["gpsi_last_fix"] = now_wall
        elif "GPS DATA:" in combined or "$GN" in combined or "$GP" in combined:
            if telem.get("fw_phase") not in ("GPS_FIXING", "GSM_ACTIVE"):
                telem["fw_phase"] = "GPS_FIXING"
                telem["phase_start_ts"] = now_wall
                if not telem.get("gps_fix_start_ts"):
                    telem["gps_fix_start_ts"] = now_wall
                telem["gps_timeout_sec"] = default_gps_timeout

        # 3b. GPS cycle result -> update exact fix/timeout badge and advance phase to GSM_ACTIVE
        m_gps_timeout = re.search(r'GPS NOT FIXED\s*\(Timeout after\s*(\d+)\s*s\)', combined)
        m_gps_fixed = re.search(r'GPS FIXED\s*\(Time taken:\s*(\d+)\s*s\)', combined)
        if m_gps_timeout or "GPS NOT FIXED" in combined:
            t_sec = int(m_gps_timeout.group(1)) if m_gps_timeout else telem.get("gps_timeout_sec", default_gps_timeout)
            telem["gps_timeout_sec"] = t_sec
            telem["gps_last_result"] = f"Timeout ({t_sec}s)"
            telem["gps_fix_state"] = "TIMEOUT"
            telem["gps_fix_start_ts"] = None
            telem["lbl_gps_status"].config(
                text=f"GPS: No Fix (Timeout {t_sec}s)", bg="#7f1d1d", fg="#fecaca"
            )
            if telem.get("low_power") == 1 and telem.get("led_all_flag") != 0:
                telem["lbl_led"].config(text="💡 LEDs: OFF (Low Power)", bg=BRAND["surface_alt"], fg=BRAND["warn"])
            if gpsi_cfg > 0 and gpsi_cfg != gi_cfg and not telem.get("gpsi_last_fix"):
                telem["gpsi_last_fix"] = now_wall
            telem["fw_phase"] = "GSM_ACTIVE"
            telem["phase_start_ts"] = now_wall
            if telem.get("gps_map"):
                telem["gps_map"].set_searching(timeout=True)
        elif m_gps_fixed or "GPS FIXED" in combined:
            taken = int(m_gps_fixed.group(1)) if m_gps_fixed else 0
            t_sec = telem.get("gps_timeout_sec", default_gps_timeout)
            telem["gps_last_result"] = f"3D Fix ({taken}s / {t_sec}s)"
            telem["gps_fix_state"] = "A"
            telem["gps_fix_start_ts"] = None
            telem["lbl_gps_status"].config(
                text=f"GPS: 3D Fix ({taken}s / {t_sec}s)", bg=BRAND["ok"], fg="#ffffff"
            )
            telem["fw_phase"] = "GSM_ACTIVE"
            telem["phase_start_ts"] = now_wall
            if telem.get("gps_map"):
                telem["gps_map"].mark_fixed_banner()

        # 3b2. GSM modem state, CSQ signal & cycle completion (app_sm.c restarts GI AFTER GSM completes)
        _MODEM_STATES = {
            0: "OFF", 1: "PWR_ON", 2: "SYNC", 3: "CONFIG", 4: "SEARCHING",
            5: "REGISTERED", 6: "DATA_READY", 7: "SOCKET_OPEN",
            8: "SOCKET_LOST", 9: "RECOVERY", 10: "FAULT",
        }
        m_state = re.search(r'GSM modem:\s*state=(\d+)', combined)
        if m_state:
            st_num = int(m_state.group(1))
            telem["gsm_state_num"] = st_num
            telem["gsm_state_name"] = _MODEM_STATES.get(st_num, f"S{st_num}")
            if st_num > 0:
                if telem.get("fw_phase") != "GSM_ACTIVE":
                    telem["fw_phase"] = "GSM_ACTIVE"
                    telem["phase_start_ts"] = now_wall
                telem["gsm_done_ts"] = None
                if telem.get("gps_fix_state") == "IDLE":
                    telem["lbl_gps_status"].config(
                        text=f"GPS: Off / Idle ({telem.get('gps_timeout_sec', default_gps_timeout)}s max)",
                        bg=BRAND["surface_alt"], fg=BRAND["text_dim"],
                    )
            else:
                # state=0: modem powered off after GsmAction() -> appSmEnsureReportingTimer(TRUE) arms GI now
                if telem.get("fw_phase") == "GSM_ACTIVE" or telem.get("gsm_done_ts") is not None:
                    telem["fw_phase"] = "IDLE_TIMER"
                    telem["phase_start_ts"] = now_wall
                    if not telem.get("report_timer_exact"):
                        telem["gi_last_report"] = now_wall
                    if gpsi_cfg == gi_cfg or gpsi_cfg == 0:
                        telem["gpsi_last_fix"] = now_wall
                    telem["timer_synced"] = True
                    telem["gsm_done_ts"] = None

        m_csq = re.search(r'\+CSQ:\s*(\d+),(\d+)', combined)
        if m_csq:
            telem["gsm_csq"] = m_csq.group(1)

        if any(w in combined for w in ("GSM TX:", "Reporting data:", "Direction:MO", "Received payload with size")):
            if telem.get("fw_phase") != "GSM_ACTIVE" and "AT+CPOF" not in combined:
                telem["fw_phase"] = "GSM_ACTIVE"
                telem["phase_start_ts"] = now_wall
                if not telem.get("gsm_state_name"):
                    telem["gsm_state_name"] = "ACTIVE"

        report_done = False
        if "Reporting Successful" in combined:
            telem["gsm_last_result"] = "Report OK"
            report_done = True
        elif "Reporting Error" in combined:
            telem["gsm_last_result"] = "Report Error"
            report_done = True
        elif "Reporting data: 0 bytes" in combined:
            telem["gsm_last_result"] = "Idle (0B)"
            report_done = True

        if report_done:
            if not telem.get("report_timer_exact"):
                telem["gi_last_report"] = now_wall
            if gpsi_cfg == gi_cfg or gpsi_cfg == 0:
                telem["gpsi_last_fix"] = now_wall
            telem["timer_synced"] = True
            telem["gsm_done_ts"] = now_wall
            if telem.get("low_power") != 1:
                telem["fw_phase"] = "IDLE_TIMER"
                telem["phase_start_ts"] = now_wall
                telem["gsm_done_ts"] = None

        # 3c. Configuration applied log -> refresh Low Power / Motion / LED badges
        m_cfg = re.search(r'Configuration applied:\s*motion\s+(on|off),\s*low power\s+(on|off),\s*led\s+(on|off)', combined, re.IGNORECASE)
        if m_cfg:
            mot_on = m_cfg.group(1).lower() == "on"
            lp_on = m_cfg.group(2).lower() == "on"
            led_on = m_cfg.group(3).lower() == "on"
            telem["low_power"] = 1 if lp_on else 0
            telem["motion_enabled"] = 1 if mot_on else 0
            telem["led_all_flag"] = 1 if led_on else 0
            telem["lbl_lowpower"].config(
                text="⚡ Low Power: ON" if lp_on else "⚡ Low Power: OFF",
                bg="#0e7490" if lp_on else BRAND["surface_alt"],
                fg="#cffafe" if lp_on else "#94a3b8",
            )
            thresh = telem.get("motion_thresh")
            thresh_text = f" ({thresh}%)" if thresh is not None else ""
            telem["lbl_motion"].config(
                text=f"🏃 Motion: ARMED{thresh_text}" if mot_on else "🏃 Motion: OFF",
                bg="#065f46" if mot_on else BRAND["surface_alt"],
                fg="#a7f3d0" if mot_on else "#94a3b8",
            )
            if not led_on:
                telem["lbl_led"].config(text="💡 LEDs: DISABLED", bg=BRAND["surface_alt"], fg="#f87171")
            elif lp_on:
                telem["lbl_led"].config(text="💡 LEDs: OFF (Low Power)", bg=BRAND["surface_alt"], fg=BRAND["warn"])
            else:
                telem["lbl_led"].config(text="💡 LEDs: ACTIVE", bg="#064e3b", fg="#6ee7b7")
            self._render_device_cycle_status(telem, now_wall)

        # 4. Battery percentage in event/diagnostic: batteryPercentage=(\d+)
        m_batt = re.search(r'batteryPercentage=(\d+)', combined)
        if m_batt:
            pct = int(m_batt.group(1))
            telem["battery_pct"] = pct
            mv_str = f" ({telem['battery_mv']}mV)" if telem.get("battery_mv") else ""
            telem["lbl_batt"].config(text=f"🔋 Batt: {pct}%{mv_str}")

        # 5. Motion detection alert in log
        if any(w in combined.lower() for w in ("motion detected", "motion interrupt", "acce_mma8652")):
            telem["lbl_motion"].config(text="🏃 Motion: DETECTED!", bg=BRAND["warn"], fg="#000000")
            self.root.after(4000, lambda s=sid: self._restore_motion_badge(s))

        # 6. Low power state active in log
        if "low power" in combined.lower() or "lowpower" in combined.lower():
            if "enter" in combined.lower() or "sleep" in combined.lower():
                telem["lbl_lowpower"].config(text="⚡ Low Power: SLEEPING", bg="#0e7490", fg="#cffafe")
                if telem.get("led_all_flag") != 0:
                    telem["lbl_led"].config(text="💡 LEDs: OFF (Low Power)", bg=BRAND["surface_alt"], fg=BRAND["warn"])

    @staticmethod
    def _fmt_interval(sec: int) -> str:
        """Format seconds concisely as e.g. '15m', '1m30s' or '1d'."""
        return format_seconds(sec)

    def _render_device_cycle_status(self, telem: dict, now_wall: float):
        """Render live firmware phase, accurate GPS fix timeout (33s/120s), and synced GI/GPSI countdowns."""
        if telem.get("cycle_telemetry") is False:
            return
        gi = telem.get("gi_interval_sec") or 0
        gpsi = telem.get("gpsi_interval_sec") or 0
        gi_str = self._fmt_interval(gi) if gi > 0 else ("OFF" if telem.get("gi_interval_sec") == 0 else "--")
        gpsi_str = self._fmt_interval(gpsi) if gpsi > 0 else ("Per-GI" if telem.get("gpsi_interval_sec") == 0 else "--")
        t_max = 33 if (0 < gpsi <= 60) else 120
        telem["gps_timeout_sec"] = t_max
        motion_on = (telem.get("motion_enabled") == 1)
        lp_on = (telem.get("low_power") == 1)
        coupled = (gpsi == 0 or gpsi == gi)

        fw_phase = telem.get("fw_phase", "OBSERVING")
        # In low-power mode if GSM finished and state=0 wasn't logged within 4s, transition cleanly
        if fw_phase == "GSM_ACTIVE" and telem.get("gsm_done_ts") is not None:
            if (now_wall - telem["gsm_done_ts"]) >= 4.0:
                fw_phase = "IDLE_TIMER"
                telem["fw_phase"] = "IDLE_TIMER"
                telem["phase_start_ts"] = telem["gsm_done_ts"] + (3.0 if lp_on else 1.0)
                if not telem.get("report_timer_exact"):
                    telem["gi_last_report"] = telem["phase_start_ts"]
                if coupled:
                    telem["gpsi_last_fix"] = telem["phase_start_ts"]
                telem["gsm_done_ts"] = None

        # 1. Live Device Behavior badge & active GPS search counter
        if fw_phase == "GPS_FIXING":
            start_ts = telem.get("gps_fix_start_ts") or telem.get("phase_start_ts") or now_wall
            el = max(0, int(now_wall - start_ts))
            rem_fix = max(0, t_max - el)
            telem["lbl_behavior"].config(
                text=f"🛰 Phase: GPS Fix ({el}s/{t_max}s | {rem_fix}s left)",
                bg="#78350f", fg="#fde68a",
            )
            if telem.get("gps_fix_state") != "A":
                telem["lbl_gps_status"].config(
                    text=f"GPS: Searching ({min(el, t_max)}s/{t_max}s max)",
                    bg=BRAND["warn"], fg="#000000",
                )
        elif fw_phase == "GSM_ACTIVE":
            el = max(0, int(now_wall - (telem.get("phase_start_ts") or now_wall)))
            st_name = telem.get("gsm_state_name") or "ACTIVE"
            csq = telem.get("gsm_csq")
            csq_part = ""
            if csq is not None:
                csq_part = " | NoSig" if csq == "99" else f" | CSQ {csq}"
            suffix = " (Shutting down)" if telem.get("gsm_done_ts") is not None else csq_part
            telem["lbl_behavior"].config(
                text=f"📡 Phase: GSM {st_name} ({el}s{suffix})",
                bg="#1e3a8a", fg="#bfdbfe",
            )
        elif fw_phase == "IDLE_TIMER":
            mode_txt = "LP Sleep" if lp_on else "Idle"
            res_txt = telem.get("gsm_last_result") or "Done"
            telem["lbl_behavior"].config(
                text=f"💤 Phase: {mode_txt} ({res_txt})",
                bg=BRAND["surface_alt"], fg="#6ee7b7",
            )
        elif motion_on:
            telem["lbl_behavior"].config(
                text="🏃 Phase: Motion Armed (Timer Off)",
                bg="#065f46", fg="#a7f3d0",
            )
        else:
            telem["lbl_behavior"].config(
                text="⚙️ Phase: Standby / Monitoring",
                bg=BRAND["surface_alt"], fg="#e2e8f0",
            )

        # Estimate current device UTC clock for "@ HH:MM:SS" target annotations
        cur_epoch = None
        if telem.get("dev_epoch") is not None:
            cur_epoch = telem["dev_epoch"] + (time.monotonic() - telem["dev_epoch_monotonic"])

        def _eta_str(rem_sec: int) -> str:
            mm, ss = divmod(max(0, rem_sec), 60)
            if cur_epoch is not None:
                tgt = datetime.fromtimestamp(cur_epoch + rem_sec, tz=timezone.utc).strftime("%H:%M:%S")
                return f"{mm:02d}:{ss:02d} @ {tgt}"
            return f"{mm:02d}:{ss:02d}"

        def _render_interval_line(label, icon: str, prefix: str, interval_str: str,
                                  interval_sec: int, last_ts: float | None, is_gps: bool = False):
            if motion_on and not telem.get("report_timer_exact"):
                # With motion detection the timers only run during a trip; the firmware's report-timer line
                # (see _inspect_device_log_line) shows that a trip is running.
                label.config(text=f"{icon} {prefix}: {interval_str} (waiting for motion)", fg=BRAND["text_dim"])
                return
            if not is_gps and interval_sec <= 0:
                label.config(text=f"{icon} {prefix}: {interval_str}", fg=BRAND["text_dim"])
                return
            if is_gps and fw_phase == "GPS_FIXING":
                el = max(0, int(now_wall - (telem.get("gps_fix_start_ts") or telem.get("phase_start_ts") or now_wall)))
                label.config(text=f"{icon} {prefix}: {interval_str} (fixing {el}s/{t_max}s)", fg=BRAND["warn"])
                return
            if fw_phase in ("GPS_FIXING", "GSM_ACTIVE") and (not is_gps or coupled):
                cyc_el = max(0, int(now_wall - (telem.get("phase_start_ts") or now_wall)))
                cm, cs = divmod(cyc_el, 60)
                label.config(text=f"{icon} {prefix}: {interval_str} (active +{cm:02d}:{cs:02d})", fg=BRAND["info"])
                return
            if last_ts is not None and interval_sec > 0:
                rem = int(interval_sec - (now_wall - last_ts))
                if rem > 0:
                    label.config(text=f"{icon} {prefix}: {interval_str} (next {_eta_str(rem)})", fg=BRAND["accent"])
                else:
                    om, os_ = divmod(-rem, 60)
                    label.config(text=f"{icon} {prefix}: {interval_str} (due +{om:02d}:{os_:02d})", fg=BRAND["ok"])
            else:
                label.config(text=f"{icon} {prefix}: {interval_str} (syncing on cycle)", fg=BRAND["text_dim"])

        # 2. Accurate GI & GPSI lines in identical single-line format
        _render_interval_line(telem["lbl_gi"], "📶", "GI", gi_str, gi, telem.get("gi_last_report"), is_gps=False)
        if telem.get("last_gap_s") is not None and gi > 0 and telem.get("report_timer_exact"):
            gap = telem["last_gap_s"]
            drift = gap - gi
            colour = BRAND["ok"] if abs(drift) <= max(5, gi // 100) else BRAND["warn"]
            telem["lbl_gi"].config(text=f"{telem['lbl_gi'].cget('text')}\n   last gap {format_seconds(gap)} "
                                        f"({drift:+d} s vs GI)", fg=colour)
        gps_ref_sec = gi if coupled else gpsi
        gps_ref_ts = telem.get("gi_last_report") if coupled else telem.get("gpsi_last_fix")
        _render_interval_line(telem["lbl_gpsi"], "🛰", "GPSI", gpsi_str, gps_ref_sec, gps_ref_ts, is_gps=True)

    def _tick_device_clocks(self):
        """Advance device clocks, uptime counters, and live cycle/GI/GPSI state every second."""
        try:
            now_mono = time.monotonic()
            now_wall = time.time()

            for session_id, telem in list(self.device_telemetry.items()):
                # 1. Device Actual Clock
                if telem.get("dev_epoch") is not None:
                    elapsed = now_mono - telem["dev_epoch_monotonic"]
                    cur_epoch = telem["dev_epoch"] + elapsed
                    dt = datetime.fromtimestamp(cur_epoch, tz=timezone.utc)
                    telem["lbl_dev_clock"].config(text=f"🕒 Dev Clock: {dt.strftime('%H:%M:%S UTC')}")

                # 2. Live Firmware Behavior + GI + GPSI + GPS Timeout
                self._render_device_cycle_status(telem, now_wall)

                # 3. Device Uptime
                up_sec = telem.get("uptime_sec")
                if up_sec is not None:
                    elapsed_up = int(up_sec + (now_mono - telem["uptime_monotonic"]))
                    hours, rem_s = divmod(elapsed_up, 3600)
                    mins, secs = divmod(rem_s, 60)
                    telem["lbl_uptime"].config(text=f"⏱️ Uptime: {hours}h {mins:02d}m {secs:02d}s")

        except Exception:
            pass
        finally:
            self.root.after(1000, self._tick_device_clocks)

    def _detect_device_profile(self, target_mac: str | None = None) -> str:
        """Auto-resolve device profile from active session, scanned advertisement cache, or engine default."""
        sid = (target_mac or "").lower().strip()
        if not sid:
            combo_sel = self.device_combo.get()
            if combo_sel and "|" in combo_sel:
                sid = combo_sel.split("|")[1].strip().lower()
        if sid:
            sess = engine.sessions.get(sid)
            if sess and sess.profile:
                return sess.profile
            dev_info = next((d for d in self.devices_cache if d["mac"].lower() == sid), None)
            if dev_info and dev_info.get("suggested_profile"):
                return dev_info["suggested_profile"]
        if engine.selected_session and engine.selected_session.profile:
            return engine.selected_session.profile
        return engine.profile or "valtrack"

    def _profile_from_combo(self, label: str | None = None) -> str:
        """Return auto-detected profile key for the selected device."""
        return self._detect_device_profile()

    def _select_profile_combo(self, profile_key: str):
        """Sync engine.profile to the auto-detected profile key."""
        if profile_key:
            engine.profile = profile_key

    def _on_device_combo_change(self, _event=None):
        """Device chosen in the combo box: show its log tab and make it the selected session."""
        selection = self.device_combo.get()
        if not selection or "|" not in selection:
            return
        target = selection.split("|")[1].strip().lower()
        if target in self.device_log_frames:
            frame = self.device_log_frames[target]
            try:
                self.log_notebook.select(frame)
            except Exception:
                pass
        else:
            dev_info = next((d for d in self.devices_cache if d["mac"].lower() == target), None)
            if dev_info and dev_info.get("suggested_profile"):
                self._select_profile_combo(dev_info["suggested_profile"])
        engine.select_session(target)
        self._sync_selected_session_controls()

    def _on_log_tab_changed(self, _event=None):
        """Log tab switched: select that device's session and the matching combo entry."""
        selected = self.log_notebook.select()
        session_id = self.log_tab_sessions.get(selected)
        if session_id not in ("serial", "system", None):
            engine.select_session(session_id)
            for idx, opt in enumerate(self.device_combo.cget("values") or []):
                if "|" in opt and opt.split("|")[1].strip().lower() == session_id:
                    self.device_combo.current(idx)
                    break
        line_count = self.device_log_counts.get(session_id, 0) if session_id else self.total_lines
        self.lbl_lines.config(text=f"Lines: {line_count}")
        self._sync_selected_session_controls()

    def _sync_selected_session_controls(self):
        """Update header link state, button states/colours and status texts to match
        the selected session (connected, connecting or disconnected).
        """
        combo_sel = self.device_combo.get()
        combo_mac = combo_sel.split("|")[1].strip().lower() if (combo_sel and "|" in combo_sel) else None

        active_sid = combo_mac or engine.selected_session_id
        session = engine.sessions.get(active_sid) if active_sid else engine.selected_session

        connected = bool(session and session.connected)
        connecting = bool(session and session.connecting)
        profile = session.profile if session else ""

        self.is_connected = connected
        dev = getattr(engine, "active_device", None)
        has_dev = dev is not None
        self.is_streaming = bool(dev and dev.streaming_enabled)

        # Standardized link indicator on header for single / multiple devices
        connected_count = sum(1 for s in engine.sessions.values() if s.connected)
        if connected_count > 1:
            self.header.set_link(True, f"{connected_count} DEVICES LINKED")
        elif connected_count == 1:
            conn_sess = next((s for s in engine.sessions.values() if s.connected), None)
            self.header.set_link(True, conn_sess.name if conn_sess else "CONNECTED")
        else:
            self.header.set_link(False)

        # Standardized control button states & colors
        combo_dev = next((d for d in self.devices_cache
                          if combo_mac and d["mac"].lower() == combo_mac), None)
        combo_is_ota = bool(combo_dev and (
            combo_dev.get("is_ota") or "stm_ota" in (combo_dev.get("name") or "").lower()
        ))
        profile_key = normalise_profile(profile)
        prof = get_profile(profile_key) if profile_key else None
        can_stream = bool((connected or has_dev) and prof is not None and prof.has_debug_log)
        can_config = bool(connected or combo_is_ota or (session is not None and prof is not None))
        effective_profile = (
            profile
            or (combo_dev.get("suggested_profile") if combo_dev else None)
            or self._profile_from_combo()
        )
        cfg_label = f"{profile_label(effective_profile)} Config" if normalise_profile(effective_profile) \
            else "Device Config"
        self.btn_config.config(text=cfg_label, state=tk.NORMAL if can_config else tk.DISABLED)
        self.btn_sensors.config(state=tk.NORMAL if connected and profile_key in ("gigo_dt", "gigo_gas")
                                else tk.DISABLED)
        self.btn_debuglog.config(state=tk.NORMAL if can_stream else tk.DISABLED)

        # Connect / Disconnect button states & standardized brand colors
        if connected:
            self.btn_connect.config(state=tk.DISABLED, text="Connected", bg=BRAND["ok"], fg=BRAND["text"])
            self.btn_disconnect.config(state=tk.NORMAL, text="Disconnect", bg=BRAND["err"], fg=BRAND["text"])
        elif connecting:
            self.btn_connect.config(state=tk.DISABLED, text="Connecting...", bg=BRAND["info"], fg="#000000")
            self.btn_disconnect.config(state=tk.NORMAL, text="Disconnect", bg=BRAND["err"], fg=BRAND["text"])
        else:
            can_connect = bool(self.devices_cache)
            self.btn_connect.config(state=tk.NORMAL if can_connect else tk.DISABLED,
                                    text="DOTA Flash" if combo_is_ota else "Connect",
                                    bg=BRAND["ok"], fg=BRAND["text"])
            self.btn_disconnect.config(state=tk.DISABLED, text="Disconnect", bg=BRAND["border"], fg=BRAND["text_muted"])

        self.btn_scan.config(state=tk.DISABLED if any(s.connecting for s in engine.sessions.values()) else tk.NORMAL)
        self.btn_toggle_stream.config(state=tk.NORMAL if can_stream else tk.DISABLED)

        # Standardized status text & colors
        if session:
            if connected:
                self.lbl_status.config(text=f"Connected: {session.name}", bg=BRAND["ok"], fg="#ffffff")
                self.lbl_details.config(text=f"Linked: {session.name} - {profile_label(profile)}")
                self.debug_log_on = bool(dev and dev.streaming_enabled)
            elif connecting:
                self.lbl_status.config(text=f"Connecting: {session.name}", bg=BRAND["info"], fg="#000000")
                self.lbl_details.config(text=f"{session.name} - Connecting...")
                self.debug_log_on = False
            else:
                st = self.session_status.get(session.session_id, "Disconnected")
                self.lbl_status.config(text=f"{st}: {session.name}", bg=BRAND["err"], fg="#ffffff")
                self.lbl_details.config(text=f"{session.name} - {st}")
                self.debug_log_on = False
        else:
            if connected_count > 0:
                self.lbl_status.config(text=f"{connected_count} Connected", bg=BRAND["ok"], fg="#ffffff")
                self.lbl_details.config(text=f"{connected_count} device(s) connected. Select a tab.")
            else:
                self.lbl_status.config(text="Ready", bg=BRAND["border"], fg=BRAND["text"])
                self.lbl_details.config(text="No BLE device selected")
            self.debug_log_on = False
        self._render_debuglog_button()

    async def _init_device_session(self, session_id: str, session):
        """After connecting: read what the device type offers (configuration, battery, clock), switch the debug log on
        when this tab wants it, then keep battery and clock fresh every SESSION_REFRESH_S while connected.
        """
        await asyncio.sleep(0.3)
        prof = get_profile(session.profile)
        device = session.device
        logger = session.sc2
        if prof is None:
            return

        async def refresh_battery():
            try:
                if device is not None:
                    batt = await device.read_battery()
                elif logger is not None:
                    percent = await logger.get_battery()
                    batt = None if percent is None else {"percent": percent, "voltage_mv": None}
                else:
                    batt = None
                if batt:
                    post_gui("battery_state", session_id=session_id, battery=batt)
            except Exception:
                pass

        async def read_application_summary():
            # A first line for the side panel before the live view runs: the GiGO device's role (from its
            # configuration flags and, on the beacon, its mode), the logger's state.
            try:
                if device is not None and prof.key in ("gigo_delphis", "gigo_beacon"):
                    config = await device.read_gigo_config()
                    mode = await device.beacon_read_mode() if prof.key == "gigo_beacon" else None
                    text = f"Role: {roles_text(gigo_roles(prof.key, config, mode))}"
                elif logger is not None and prof.key == "gigo_gas":
                    stats = await logger.gas_stats()
                    text = (f"Logger: {'RECORDING' if stats.get('recording') else 'idle'}, {stats.get('total')} "
                            "records") if stats and "total" in stats else "Logger: state unknown"
                elif logger is not None:
                    stats = await logger.get_stats()
                    text = (f"Logger: {'RECORDING' if stats.get('recording_active') else 'idle'}, "
                            f"{stats.get('total_records')} records") if stats and "total_records" in stats \
                        else "Logger: state unknown"
                else:
                    return
                post_gui("app_summary", session_id=session_id, text=text)
            except Exception:
                pass

        async def refresh_clock():
            # Valtrack answers 0x11, the D&T / gas loggers 0x25; the SC2 hub has no clock read over BLE.
            try:
                if device is not None and prof.key == "valtrack":
                    dev_time = await device.read_device_time()
                elif logger is not None:
                    dev_time = await logger.get_rtc()
                else:
                    return
                if dev_time:
                    post_gui("device_time", session_id=session_id, epoch=dev_time)
            except Exception:
                pass

        try:
            if device is not None and prof.config_window in ("valtrack", "sc2_hub"):
                try:
                    cfg = await device.read_device_config()
                    post_gui("device_config", session_id=session_id, config=cfg)
                except Exception as ex:
                    log_system(f"Device config read note: {ex}", BRAND["info"], session_id=session_id)
                try:
                    imei = await device.read_imei()
                    if imei:
                        post_gui("device_imei", session_id=session_id, imei=imei)
                except Exception:
                    pass
            await refresh_battery()
            await refresh_clock()
            await read_application_summary()

            telem_tab = self.device_telemetry.get(session_id)
            auto_dbg = telem_tab.get("auto_debug_enabled", True) if telem_tab else True
            if prof.has_debug_log and device is not None and auto_dbg:
                accepted = await device.set_debug_log(True)
                if not accepted:
                    log_system("Auto debug log did not stay enabled.", BRAND["err"], session_id=session_id)
                post_gui("stream_state", streaming=device.streaming_enabled, session_id=session_id)
            elif not prof.has_debug_log:
                log_system(f"{prof.label}: no live debug log on this device type; status is read on request.",
                           BRAND["info"], session_id=session_id)

            while session.connected and not session.disconnect_evt.is_set() and \
                    not (device is not None and device.disconnected_evt.is_set()):
                await asyncio.sleep(SESSION_REFRESH_S)
                if not session.connected or session.ota_active or \
                        (device is not None and device.disconnected_evt.is_set()):
                    break
                await refresh_battery()
                await refresh_clock()
        except Exception as exc:
            log_system(f"Device session init failed: {exc}", BRAND["err"], session_id=session_id)

    def _apply_device_config(self, session_id: "str | None", cfg: dict):
        """Show configuration values (all of them or just a few) in a device tab's live panel. Sources: a 0xC1 read,
        a write from a config window, or the configuration the firmware prints at every report cycle.
        """
        if not session_id or session_id not in self.device_telemetry or not cfg:
            return
        telem = self.device_telemetry[session_id]
        if cfg.get("gsm_interval") is not None:
            telem["gi_interval_sec"] = int(cfg["gsm_interval"])
        if cfg.get("gps_interval") is not None:
            telem["gpsi_interval_sec"] = int(cfg["gps_interval"])
            gpsi = telem["gpsi_interval_sec"]
            telem["gps_timeout_sec"] = 33 if (0 < gpsi <= 60) else 120
        if cfg.get("motion_thresh") is not None:
            telem["motion_thresh"] = int(cfg["motion_thresh"])
        if cfg.get("low_power_flag") is not None:
            lp = int(cfg["low_power_flag"])
            telem["low_power"] = lp
            if lp == 1:
                telem["lbl_lowpower"].config(text="⚡ Low Power: ON", bg="#0e7490", fg="#cffafe")
            else:
                telem["lbl_lowpower"].config(text="⚡ Low Power: OFF", bg=BRAND["surface_alt"], fg="#94a3b8")
        if cfg.get("motion_enable") is not None:
            telem["motion_enabled"] = int(cfg["motion_enable"])
            self._restore_motion_badge(session_id)
        if cfg.get("led_all_flag") is not None:
            led = int(cfg["led_all_flag"])
            telem["led_all_flag"] = led
            if led == 0:
                telem["lbl_led"].config(text="💡 LEDs: DISABLED", bg=BRAND["surface_alt"], fg="#f87171")
            elif telem.get("low_power") == 1:
                telem["lbl_led"].config(text="💡 LEDs: OFF (Low Power)", bg=BRAND["surface_alt"], fg=BRAND["warn"])
            else:
                telem["lbl_led"].config(text="💡 LEDs: ACTIVE", bg="#064e3b", fg="#6ee7b7")
        self._render_device_cycle_status(telem, time.time())

    def _apply_tab_profile(self, telem: dict, profile_key: "str | None", session_id: "str | None" = None):
        """Fit the tab to the device type: the side panel keeps the lines that mean something for it, the GPS
        trackers keep the map, the GiGO devices get their live view (session_id given). Device types without report
        cycles also get 'n/a' instead of interval countdowns.
        """
        prof = get_profile(profile_key)
        key = prof.key if prof else None
        if key in ("gigo_delphis", "gigo_beacon"):
            shown = DASH_SHOWN_FOR_IMU
        elif key in ("gigo_dt", "gigo_gas"):
            shown = DASH_SHOWN_FOR_LOGGERS
        else:
            shown = None
        for _name, widget, _options in telem.get("dash_widgets", ()):
            widget.pack_forget()
        for name, widget, options in telem.get("dash_widgets", ()):
            if (name in shown) if shown is not None else (name not in DASH_HIDDEN_FOR_TRACKERS):
                widget.pack(**{option: value for option, value in options.items() if option != "in"})
        if session_id:
            self._set_live_view(session_id, telem, key)
        if prof is None or prof.key in GPS_TRACKER_PROFILES:
            telem["cycle_telemetry"] = True
            return
        telem["cycle_telemetry"] = False
        for key, text in (("lbl_behavior", "⚙️ Phase: n/a (no report cycle)"), ("lbl_gi", "📶 GI: n/a"),
                          ("lbl_gpsi", "🛰 GPSI: n/a"), ("lbl_gps_status", "GPS: n/a"),
                          ("lbl_lowpower", "⚡ Low Power: n/a"), ("lbl_motion", "🏃 Motion: n/a"),
                          ("lbl_led", "💡 LEDs: n/a")):
            telem[key].config(text=text, fg=BRAND["text_muted"], bg=BRAND["surface_alt"])

    def _set_live_view(self, session_id: str, telem: dict, profile_key: "str | None"):
        """Put the device type's live view above the log (GiGO devices) or the GPS map on the log (trackers); the
        SC2 hubs also get their GPS fixes on the map. Rebuilt only when the profile changes (detection may correct
        the guess from the advertisement).
        """
        if telem.get("live_profile") == profile_key:
            return
        telem["live_profile"] = profile_key
        self._close_live_parts(session_id)
        old_view = telem.get("live_view_widget")
        if old_view is not None:
            telem["log_pane"].forget(old_view)
            old_view.destroy()
            telem["live_view_widget"] = None
        session = engine.sessions.get(session_id)
        connected = bool(session and session.connected)
        view = create_live_view(telem["log_pane"], profile_key, session_id)
        if view is not None:
            telem["log_pane"].add(view, before=telem["log_container"], height=LIVE_VIEW_HEIGHT, stretch="always",
                                  minsize=LOG_STRIP_MIN)
            telem["log_pane"].paneconfigure(telem["log_container"], height=LOG_STRIP_HEIGHT, minsize=LOG_STRIP_MIN,
                                            stretch="never")
            view.summary_callback = lambda text, label=telem["lbl_app"]: label.config(text=f"📊 {text}")
            telem["gps_map"].place_forget()
            if connected:
                view.attach()
            else:
                view.detach()
        else:
            telem["log_pane"].paneconfigure(telem["log_container"], minsize=0, stretch="always")
            telem["gps_map"].place(**MAP_PLACE)
        telem["live_view"] = view
        telem["live_view_widget"] = view
        if profile_key in ("sc2_gen2", "sc2_gen3"):
            feed = Sc2GpsFeed(session_id, lambda latitude, longitude, sid=session_id: post_gui(
                "sc2_gps_fix", session_id=sid, latitude=latitude, longitude=longitude))
            telem["gps_feed"] = feed
            if connected:
                feed.attach()

    def _selected_device_type(self) -> str:
        """Key of the device type chosen in the selector ('auto' when detection decides)."""
        label = self.device_type_var.get() if hasattr(self, "device_type_var") else ""
        return next((key for key, text, _profile in DEVICE_TYPES if text == label), "auto")

    def process_queue(self):
        """Pump the GUI queue. Never lets one bad message kill the pump (that froze every log)."""
        try:
            self._drain_queue()
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            self.root.after(30, self.process_queue)

    def _drain_queue(self):
        """Handle up to 50 queued messages from the worker threads (called from the Tk poll).

        Message types: log_line (to the right log tab; NMEA lines also update the GPS line),
        serial_rx, status_update, scan_results, connection_state, session_state, stream_state.
        """
        batch = 0
        while not gui_queue.empty() and batch < 50:
            batch += 1
            item = gui_queue.get_nowait()
            msg_type = item.get("type")

            if msg_type == "log_line":
                msg = item["msg"]
                raw_line = item.get("raw_line", "")
                session_id = (item.get("session_id") or "").lower().strip() or None

                if "GPS DATA:" in msg or "$GN" in msg or "$GP" in msg:
                    self.total_gps_packets += 1
                    self.lbl_gps_count.config(text=f"GPS Sentences: {self.total_gps_packets}")
                    _t = self.device_telemetry.get(session_id or engine.selected_session_id)
                    if _t:
                        _t["gps_sentences"] += 1
                        _t["lbl_sent"].config(text=f"📡 GPS Sentences: {_t['gps_sentences']}")
                    self.parse_nmea(raw_line or msg, session_id=session_id)

                self._inspect_device_log_line(msg, raw_line, session_id)

                tag = self._classify_log_tag(msg)
                if session_id and session_id in self.device_log_widgets:
                    widget = self.device_log_widgets[session_id]
                    self._insert_log_widget(widget, msg + "\n", tag, session_id)
                else:
                    # System Log tab removed: surface global status notes concisely in the top bar
                    clean = msg.split("] ", 1)[-1].strip() if "] " in msg else msg.strip()
                    if clean:
                        self.lbl_details.config(text=clean[:80])

            elif msg_type == "serial_rx":
                self._on_serial_rx(item)

            elif msg_type == "serial_disconnected":
                self.serial.close()
                if self._serial_want_open and self._serial_last_open:
                    self.btn_serial.config(text="Cancel reconnect")
                    self.lbl_serial_state.config(text="--- reconnecting...", fg=BRAND["warn"])
                    self._schedule_serial_reconnect(1000)
                else:
                    self.btn_serial.config(text="Open port")
                    self.lbl_serial_state.config(text="--- closed", fg=BRAND["text_muted"])
                    self.root.after(800, self._serial_refresh)

            elif msg_type == "status_update":
                self.lbl_status.config(text=item["status"], bg=item["color"])
                self.header.set_link(any(session.connected for session in engine.sessions.values()))

            elif msg_type == "scan_results":
                self.btn_scan.config(state=tk.NORMAL)
                devices = item["devices"]
                self.devices_cache = devices
                if devices:
                    options = [
                        f"{d['name']:<22} | {d['mac']} | {d['rssi']} dBm"
                        + (f" | {d['kind']}" if d.get("kind") else "")
                        + (f" {d['fw_version']}" if d.get("fw_version") else "")
                        for d in devices
                    ]
                    self.device_combo.config(values=options, state="readonly")
                    self.device_combo.current(0)
                    self.btn_connect.config(state=tk.NORMAL)
                    if devices[0].get("suggested_profile") and not any(s.connected for s in engine.sessions.values()):
                        self._select_profile_combo(devices[0]["suggested_profile"])
                else:
                    self.device_combo.set("No devices found")
                    self.device_combo.config(state="disabled")
                    self.btn_connect.config(state=tk.DISABLED)
                self._sync_selected_session_controls()

            elif msg_type == "connection_state":
                session_id = (item.get("session_id") or "").lower().strip()
                connected = item.get("connected", False)
                session = engine.sessions.get(session_id)
                if session is not None:
                    session.connected = connected
                    session.connecting = False if connected else session.connecting
                    if connected and session.profile and session_id == engine.selected_session_id:
                        self._select_profile_combo(session.profile)
                engine.select_session(session_id)
                self._sync_selected_session_controls()
                if connected and session:
                    run_async(self._init_device_session(session_id, session), session_id=session_id)
                # Delphis: the acoustic link listens from the moment of connection (pictures, messages)
                if session is not None and session.profile == "gigo_delphis":
                    if connected and session.device is not None:
                        link_for(session_id).attach(session.device)
                    elif not connected:
                        link_for(session_id).detach()
                telem_tab = self.device_telemetry.get(session_id) or {}
                for key in ("live_view", "gps_feed"):
                    part = telem_tab.get(key)
                    if part is not None:
                        if connected:
                            part.attach()
                        else:
                            part.detach()

            elif msg_type == "sc2_gps_fix":
                telem = self.device_telemetry.get(item.get("session_id"))
                if telem:
                    latitude, longitude = item["latitude"], item["longitude"]
                    telem["lbl_coords"].config(text=f"📍 Lat: {latitude:.6f}\n     Lon: {longitude:.6f}")
                    telem["gps_map"].update_fix(latitude, longitude, fixed=True)

            elif msg_type == "app_summary":
                telem = self.device_telemetry.get(item.get("session_id"))
                if telem and telem.get("lbl_app"):
                    telem["lbl_app"].config(text=f"📊 {item.get('text', '')}")

            elif msg_type == "device_config":
                session_id = item.get("session_id")
                self._apply_device_config(session_id, item.get("config", {}))

            elif msg_type == "device_imei":
                session_id = (item.get("session_id") or "").lower().strip()
                imei = (item.get("imei") or "").strip()
                telem_tab = self.device_telemetry.get(session_id)
                if telem_tab and imei:
                    telem_tab["imei"] = imei
                    if telem_tab.get("lbl_mac"):
                        telem_tab["lbl_mac"].config(
                            text=self._format_identity_text(
                                telem_tab.get("address", session_id),
                                telem_tab.get("profile", "valtrack"),
                                telem_tab.get("device_type"),
                                imei,
                            )
                        )

            elif msg_type == "battery_state":
                session_id = item.get("session_id")
                batt = item.get("battery", {})
                if session_id in self.device_telemetry:
                    telem = self.device_telemetry[session_id]
                    pct = batt.get("percent")
                    mv = batt.get("voltage_mv")
                    telem["battery_pct"] = pct
                    telem["battery_mv"] = mv
                    if pct is not None and mv:
                        telem["lbl_batt"].config(text=f"🔋 Batt: {pct}% ({mv}mV)")
                    elif pct is not None:
                        telem["lbl_batt"].config(text=f"🔋 Batt: {pct}%")

            elif msg_type == "device_time":
                session_id = item.get("session_id")
                epoch = item.get("epoch")
                if session_id in self.device_telemetry and epoch and epoch > 1577836800:
                    telem = self.device_telemetry[session_id]
                    telem["dev_epoch"] = epoch
                    telem["dev_epoch_monotonic"] = time.monotonic()
                    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
                    telem["lbl_dev_clock"].config(text=f"🕒 Dev Clock: {dt.strftime('%H:%M:%S UTC')}")

            elif msg_type == "session_state":
                session_id = item["session_id"]
                name = item.get("name", session_id)
                address = item.get("address", session_id)
                profile = item.get("profile", "valtrack")
                status = item.get("status", "disconnected")
                self.session_status[session_id] = status
                self._ensure_device_log_tab(session_id, name, address, profile)
                telem_tab = self.device_telemetry.get(session_id)
                if telem_tab and telem_tab.get("lbl_mac"):
                    telem_tab["address"] = address
                    telem_tab["profile"] = profile
                    telem_tab["lbl_mac"].config(
                        text=self._format_identity_text(
                            address, profile, telem_tab.get("device_type"), telem_tab.get("imei")
                        )
                    )
                    self._apply_tab_profile(telem_tab, profile, session_id)
                session_tab = self.device_log_frames.get(session_id)
                if session_tab:
                    self.log_notebook.tab(session_tab, text=f"{name} [{address}] - {status.upper()}   ×")
                # Never steal the operator's tab/selection here: with several devices connected,
                # a background reconnect of device A must not yank the view away from device B.
                self._sync_selected_session_controls()

                if session_id == engine.selected_session_id:
                    if status == "connecting":
                        self.lbl_status.config(text=f"Connecting: {name}", bg="#38bdf8")
                    elif status == "connected":
                        self.lbl_status.config(text=f"Connected: {name}", bg=BRAND["ok"])
                    elif status == "reconnecting":
                        self.lbl_status.config(text=f"Reconnecting: {name}", bg=BRAND["warn"])
                    elif status == "disconnected":
                        self.lbl_status.config(text=f"Disconnected: {name}", bg=BRAND["border"])

            elif msg_type == "stream_state":
                session_id = item.get("session_id")
                if session_id == engine.selected_session_id:
                    self.debug_log_on = item["streaming"]
                    self.is_streaming = item["streaming"]
                    self._render_debuglog_button()


    # ---------------------------------------------------------------------------------------
    # Serial / Tera Term panel
    # ---------------------------------------------------------------------------------------
    def _build_serial_panel(self):
        self.serial = SerialEngine()

        wrap = tk.Frame(self.root, bg=BRAND["surface"], padx=6, pady=3)
        wrap.pack(fill=tk.X, padx=8, pady=(0, 3))

        self._serial_open_row = tk.Frame(wrap, bg=BRAND["surface"])
        self._serial_open_row.pack(fill=tk.X)

        row = self._serial_open_row

        tk.Label(row, text="SERIAL", font=("Segoe UI", 8, "bold"),
                 fg=BRAND["accent"], bg=BRAND["surface"]).pack(side=tk.LEFT, padx=(2, 4))
        self.lbl_serial_state = tk.Label(row, text="--- closed", font=(FONT_MONO_FAMILY, 8, "bold"),
                                         fg=BRAND["text_muted"], bg=BRAND["surface"])
        self.lbl_serial_state.pack(side=tk.LEFT, padx=(0, 8))

        def lbl(text, pad=(6, 2)):
            """Small bold label in the serial settings row."""
            tk.Label(row, text=text, font=("Segoe UI", 8, "bold"), fg=BRAND["text_dim"],
                     bg=BRAND["surface"]).pack(side=tk.LEFT, padx=pad)

        def combo(values, width, default=None):
            """Read-only combo box in the serial settings row, set to default."""
            c = ttk.Combobox(row, values=values, state="readonly", width=width,
                             font=("Segoe UI", 8))
            if default is not None:
                c.set(default)
            c.pack(side=tk.LEFT, padx=2)
            return c

        lbl("Port", (0, 2))
        self.cmb_port = combo([], 24)

        self._btn(row, "↻", self._serial_refresh, BRAND["surface_alt"]).pack(
            side=tk.LEFT, padx=(2, 6))

        lbl("Baud")
        self.cmb_baud = combo(BAUD_RATES, 8, "115200")

        lbl("Data")
        self.cmb_data = combo(["8", "7", "6", "5"], 3, "8")

        lbl("Parity")
        self.cmb_parity = combo(list(PARITY_MAP), 5, "None")

        lbl("Stop")
        self.cmb_stop = combo(list(STOPBITS_MAP), 3, "1")

        lbl("Flow")
        self.cmb_flow = combo(list(FLOW_MAP), 7, "None")

        self.btn_serial = self._btn(row, "Open port", self._serial_toggle)
        self.btn_serial.pack(side=tk.LEFT, padx=(6, 0))

        self._serial_adv_open = False
        self.btn_serial_adv = self._btn(row, "Advanced ▸", self._toggle_serial_advanced, BRAND["surface_alt"])
        self.btn_serial_adv.pack(side=tk.RIGHT, padx=(6, 0))

        # --- send / advanced row (hidden by default; shown when 'Advanced' is clicked) -----
        self._serial_send_row = tk.Frame(wrap, bg=BRAND["surface"])
        send_row = self._serial_send_row

        self.ent_send = tk.Entry(send_row, bg=BRAND["surface_alt"], fg=BRAND["text"],
                                 insertbackground=BRAND["text"], font=("Consolas", 9),
                                 relief=tk.FLAT, highlightthickness=1,
                                 highlightbackground=BRAND["border"],
                                 highlightcolor=BRAND["accent"])
        self.ent_send.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.ent_send.bind("<Return>", lambda e: self._serial_send())
        self.ent_send.bind("<Up>", lambda e: self._serial_history(-1))
        self.ent_send.bind("<Down>", lambda e: self._serial_history(1))

        self.cmb_eol = ttk.Combobox(send_row, values=list(EOL_MAP), state="readonly",
                                    width=13, font=("Segoe UI", 8))
        self.cmb_eol.set("CR (\\r)")
        self.cmb_eol.pack(side=tk.LEFT, padx=(0, 6))

        self.var_hex_send = tk.BooleanVar(value=False)
        tk.Checkbutton(send_row, text="Hex", variable=self.var_hex_send,
                       bg=BRAND["surface"], fg=BRAND["text_dim"], selectcolor=BRAND["surface_alt"],
                       activebackground=BRAND["surface"], font=("Segoe UI", 8)).pack(
            side=tk.LEFT, padx=(0, 4))

        self.var_hex_rx = tk.BooleanVar(value=False)
        tk.Checkbutton(send_row, text="Hex view", variable=self.var_hex_rx,
                       command=lambda: setattr(self.serial, "hex_rx", self.var_hex_rx.get()),
                       bg=BRAND["surface"], fg=BRAND["text_dim"], selectcolor=BRAND["surface_alt"],
                       activebackground=BRAND["surface"], font=("Segoe UI", 8)).pack(
            side=tk.LEFT, padx=(0, 4))

        self.var_echo = tk.BooleanVar(value=False)
        tk.Checkbutton(send_row, text="Local echo", variable=self.var_echo,
                       command=lambda: setattr(self.serial, "local_echo", self.var_echo.get()),
                       bg=BRAND["surface"], fg=BRAND["text_dim"], selectcolor=BRAND["surface_alt"],
                       activebackground=BRAND["surface"], font=("Segoe UI", 8)).pack(
            side=tk.LEFT, padx=(0, 4))

        self.var_ts = tk.BooleanVar(value=False)
        tk.Checkbutton(send_row, text="Timestamp", variable=self.var_ts,
                       command=lambda: setattr(self.serial, "timestamp_rx", self.var_ts.get()),
                       bg=BRAND["surface"], fg=BRAND["text_dim"], selectcolor=BRAND["surface_alt"],
                       activebackground=BRAND["surface"], font=("Segoe UI", 8)).pack(
            side=tk.LEFT, padx=(0, 8))

        self._btn(send_row, "Send", self._serial_send, BRAND["accent_dark"]).pack(side=tk.LEFT)

        self._serial_history_buf = []
        self._serial_history_pos = -1
        self._serial_want_open = False
        self._serial_last_open = None
        self._serial_reconn_job = None

        self._serial_refresh()

    # ---- serial handlers ---------------------------------------------------------------
    def _serial_refresh(self):
        ports = SerialEngine.available_ports() if SERIAL_AVAILABLE else []
        self.cmb_port["values"] = ports
        if ports:
            if self.cmb_port.get() not in ports:
                self.cmb_port.set(ports[0])
        else:
            self.cmb_port.set("")
        if not SERIAL_AVAILABLE:
            self.btn_serial.config(state=tk.DISABLED)
            self.lbl_serial_state.config(text="--- pyserial missing", fg=BRAND["warn"])

    def _toggle_serial_advanced(self):
        """Show or hide the Serial Advanced send/options row."""
        self._serial_adv_open = not self._serial_adv_open
        if self._serial_adv_open:
            self._serial_send_row.pack(fill=tk.X, pady=(3, 0))
            self.btn_serial_adv.config(text="Advanced ▾")
            self.ent_send.focus_set()
        else:
            self._serial_send_row.pack_forget()
            self.btn_serial_adv.config(text="Advanced ▸")

    def _cancel_serial_reconnect(self):
        if self._serial_reconn_job is not None:
            try:
                self.root.after_cancel(self._serial_reconn_job)
            except tk.TclError:
                pass
            self._serial_reconn_job = None

    def _schedule_serial_reconnect(self, delay_ms: int = 1000):
        self._cancel_serial_reconnect()
        self._serial_reconn_job = self.root.after(delay_ms, self._serial_try_reconnect)

    def _serial_try_reconnect(self):
        self._serial_reconn_job = None
        if not self._serial_want_open or not self._serial_last_open or self.serial.is_open:
            return
        self._serial_refresh()
        port, baud, data, parity, stop, flow = self._serial_last_open
        want_dev = port.split("  -  ")[0].strip().lower()
        ports = SerialEngine.available_ports() if SERIAL_AVAILABLE else []
        match = next((p for p in ports if p.split("  -  ")[0].strip().lower() == want_dev), None)
        if match is None:
            self._schedule_serial_reconnect(1000)
            return
        try:
            self.serial.open(match, baud, data, parity, stop, flow)
        except Exception:
            self._schedule_serial_reconnect(1000)
            return
        self.cmb_port.set(match)
        self._serial_last_open = (match, baud, data, parity, stop, flow)
        self.btn_serial.config(text="Close port")
        self.lbl_serial_state.config(text="--- open", fg=BRAND["ok"])
        msg = f"Serial reconnected: {match} @ {baud} {data}{parity}{stop}"
        log_system(msg, BRAND["ok"])
        self.insert_serial(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}\n", "system")

    def _serial_toggle(self):
        """Open the serial port with the chosen settings, or close / cancel reconnect when active."""
        if self.serial.is_open or self._serial_want_open:
            self._serial_want_open = False
            self._cancel_serial_reconnect()
            self.serial.close()
            self.btn_serial.config(text="Open port")
            self.lbl_serial_state.config(text="--- closed", fg=BRAND["text_muted"])
            log_system("Serial port closed.", BRAND["text_dim"])
            return

        port = self.cmb_port.get().strip()
        if not port:
            messagebox.showwarning("No port", "No serial port selected. Plug the device in "
                                              "and press ---.")
            return
        baud = self.cmb_baud.get()
        data = self.cmb_data.get()
        parity = self.cmb_parity.get()
        stop = self.cmb_stop.get()
        flow = self.cmb_flow.get()
        try:
            self.serial.open(port, baud, data, parity, stop, flow)
        except Exception as exc:
            messagebox.showerror("Cannot open port",
                                 f"{port}\n\n{exc}\n\n"
                                 f"If another program holds the port, close it first.")
            log_system(f"Serial open failed: {exc}", BRAND["err"])
            return

        self._serial_want_open = True
        self._serial_last_open = (port, baud, data, parity, stop, flow)
        self.btn_serial.config(text="Close port")
        self.lbl_serial_state.config(text="--- open", fg=BRAND["ok"])
        open_msg = f"Serial open: {port} @ {baud} {data}{parity}{stop}"
        log_system(open_msg, BRAND["ok"])
        self.insert_serial(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {open_msg}\n", "system")
        try:
            self.log_notebook.select(self.serial_tab)
        except tk.TclError:
            pass
        if self._serial_adv_open:
            self.ent_send.focus_set()
        else:
            self.txt_serial.focus_set()

    def _on_serial_terminal_key(self, event):
        """Allow direct Tera Term-style interactive typing inside the Serial Terminal pane."""
        # Allow pure navigation & selection shortcuts without modifying the log buffer.
        nav_keys = {
            "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End",
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
            "Meta_L", "Meta_R", "Super_L", "Super_R", "Caps_Lock",
        }
        if event.keysym in nav_keys:
            return None

        if sys.platform == "darwin":
            ctrl_or_cmd = bool(event.state & (0x0004 | 0x0008))
        else:
            # On Windows, 0x0008 is NumLock! Only 0x0004 is Control.
            ctrl_or_cmd = bool(event.state & 0x0004)
        key_low = (event.keysym or "").lower()
        if ctrl_or_cmd and key_low == "a":
            return None
        if ctrl_or_cmd and key_low == "f":
            return self._show_find_bar()
        if ctrl_or_cmd and key_low == "c":
            try:
                if self.txt_serial.tag_ranges(tk.SEL):
                    return None  # copy selected text normally
            except tk.TclError:
                pass
            if self.serial.is_open:
                self.serial.write_async(b"\x03")
            return "break"
        if ctrl_or_cmd and key_low == "v":
            if self.serial.is_open:
                try:
                    clip = self.root.clipboard_get()
                except tk.TclError:
                    clip = ""
                if clip:
                    payload = clip.replace("\r\n", "\r").replace("\n", "\r").encode("utf-8", "replace")
                    self.serial.write_async(payload)
            return "break"

        if not self.serial.is_open:
            return "break"

        if event.keysym in ("Return", "KP_Enter"):
            payload = EOL_MAP.get(self.cmb_eol.get(), b"\r")
        elif event.keysym == "BackSpace" or event.char in ("\x08", "\x7f"):
            payload = b"\x08"
        elif event.keysym == "Tab":
            payload = b"\t"
        elif event.char:
            payload = event.char.encode("utf-8", "replace")
        else:
            return "break"

        try:
            self.serial.write_async(
                payload,
                on_error=lambda exc: self.root.after(0, lambda e=exc: log_system(f"Serial write failed: {e}", BRAND["err"])),
            )
            if self.var_echo.get() and not self.var_hex_rx.get():
                if payload == b"\x08":
                    self.insert_serial("\x08")
                elif event.keysym in ("Return", "KP_Enter"):
                    self.insert_serial("\n")
                elif event.char:
                    self.insert_serial(event.char)
        except Exception as exc:
            log_system(f"Serial write failed: {exc}", BRAND["err"])
        return "break"

    def _serial_send(self):
        """Send the entry text on the serial port, with the chosen line ending (or as hex bytes)."""
        text = self.ent_send.get()
        if not self.serial.is_open:
            log_system("Serial send: port not open.", BRAND["warn"])
            return
        try:
            if self.var_hex_send.get():
                payload = bytes.fromhex(text.replace(",", " ").replace("0x", ""))
            else:
                payload = text.encode("utf-8", "replace") + EOL_MAP[self.cmb_eol.get()]
        except ValueError as exc:
            messagebox.showerror("Invalid hex", f"Could not parse as hex:\n{exc}")
            return

        try:
            self.serial.write_async(
                payload,
                on_error=lambda exc: self.root.after(0, lambda e=exc: log_system(f"Serial write failed: {e}", BRAND["err"])),
            )
        except Exception as exc:
            log_system(f"Serial write failed: {exc}", BRAND["err"])
            return

        try:
            self.log_notebook.select(self.serial_tab)
        except tk.TclError:
            pass
        if self.var_echo.get() and not self.var_hex_send.get():
            self.insert_serial(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] [TX] {text}\n",
                               "system")
        if text:
            self._serial_history_buf.append(text)
            self._serial_history_pos = len(self._serial_history_buf)
        self.ent_send.delete(0, tk.END)

    def _serial_history(self, delta):
        """Up/Down walk the send history, like a shell."""
        buf = self._serial_history_buf
        if not buf:
            return
        self._serial_history_pos = max(0, min(len(buf), self._serial_history_pos + delta))
        self.ent_send.delete(0, tk.END)
        if self._serial_history_pos < len(buf):
            self.ent_send.insert(0, buf[self._serial_history_pos])

    def _on_serial_rx(self, item):
        """Show received serial text in the serial tab, with a timestamp when enabled."""
        text = item.get("text", "")
        if item.get("timestamp"):
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
            text = f"[{stamp}] {text}"
        tag = self._classify_log_tag(text)
        self.insert_serial(text, tag)

    def _active_serial_tag(self, fallback_tag: str) -> str:
        if self._serial_ansi_fg is not None:
            return f"ansi_{self._serial_ansi_fg}_b" if self._serial_ansi_bold else f"ansi_{self._serial_ansi_fg}"
        if self._serial_ansi_bold:
            return "ansi_bold"
        return fallback_tag

    def _apply_ansi_sgr(self, params: str) -> None:
        codes = [int(p) for p in params.split(";") if p.isdigit()] if params else [0]
        for code in codes:
            if code == 0:
                self._serial_ansi_fg = None
                self._serial_ansi_bold = False
            elif code == 1:
                self._serial_ansi_bold = True
            elif code == 22:
                self._serial_ansi_bold = False
            elif code == 39:
                self._serial_ansi_fg = None
            elif (30 <= code <= 37) or (90 <= code <= 97):
                self._serial_ansi_fg = code

    def insert_serial(self, text: str, tag: str = "default"):
        """Append text to the serial log tab with native ANSI colour codes and terminal backspace."""
        widget = getattr(self, "txt_serial", self.txt_terminal)
        if not text:
            return
        if getattr(self.serial, "hex_rx", False):
            self._insert_log_widget(widget, text, tag, "serial")
            return
        if "\x1b" not in text and "\x08" not in text and "\x7f" not in text and self._serial_ansi_fg is None and not self._serial_ansi_bold:
            self._insert_log_widget(widget, text, tag, "serial")
            return

        # Parse ANSI escape sequences (\x1b[...m for SGR colour; strip non-SGR CSI) and BS/DEL inline.
        line_delta = 0
        pos = 0
        n = len(text)
        buf = []

        def _flush():
            nonlocal line_delta
            if not buf:
                return
            chunk = "".join(buf)
            widget.insert(tk.END, chunk, self._active_serial_tag(tag))
            line_delta += chunk.count("\n")
            buf.clear()

        while pos < n:
            ch = text[pos]
            if ch == "\x1b" and pos + 1 < n and text[pos + 1] == "[":
                end = pos + 2
                while end < n and (text[end].isdigit() or text[end] in ";?"):
                    end += 1
                if end < n:
                    _flush()
                    if text[end] == "m":
                        self._apply_ansi_sgr(text[pos + 2:end])
                    pos = end + 1
                    continue
            if ch in ("\x08", "\x7f"):
                _flush()
                if widget.compare("end-1c", ">", "end-1c linestart"):
                    widget.delete("end-2c", "end-1c")
                pos += 1
                continue
            buf.append(ch)
            pos += 1
        _flush()

        if line_delta:
            self.device_log_counts["serial"] = self.device_log_counts.get("serial", 0) + line_delta
            if self.log_notebook.select() == str(getattr(self, "serial_tab", "")):
                self.lbl_lines.config(text=f"Lines: {self.device_log_counts['serial']}")
        if self.var_autoscroll.get():
            widget.see(tk.END)

    def insert_terminal(self, text: str, tag: str = "default"):
        """Append text to the system log tab."""
        self._insert_log_widget(self.txt_terminal, text, tag, None)

    def _insert_log_widget(self, widget, text: str, tag: str, session_id: str | None):
        """Append text to a log widget, count its lines (shown when its tab is visible) and scroll
        to the end when auto-scroll is on.
        """
        widget.insert(tk.END, text, tag)
        line_count = text.count("\n")
        if session_id is None:
            self.total_lines += line_count
            count = self.total_lines
        else:
            self.device_log_counts[session_id] = (
                self.device_log_counts.get(session_id, 0) + line_count
            )
            count = self.device_log_counts[session_id]
        cur_sel = self.log_notebook.select()
        if session_id is None:
            active = cur_sel == str(getattr(self, "system_tab", ""))
        elif session_id == "serial":
            active = cur_sel == str(getattr(self, "serial_tab", ""))
        else:
            active = cur_sel == str(self.device_log_frames.get(session_id, ""))
        if active:
            self.lbl_lines.config(text=f"Lines: {count}")
        if self.var_autoscroll.get():
            widget.see(tk.END)

    def _current_log_widget(self):
        """The log widget of the visible tab and its session id (None for system/serial)."""
        selected = self.log_notebook.select()
        session_id = self.log_tab_sessions.get(selected)
        if session_id == "serial":
            return getattr(self, "txt_serial", self.txt_terminal), None
        if session_id in ("system", None):
            return self.txt_terminal, None
        return self.device_log_widgets.get(session_id, self.txt_terminal), session_id

    # ---------------------------------------------------------------------------------------
    # Toolbar helpers: small button factory, save/clear logs, in-terminal search
    # ---------------------------------------------------------------------------------------
    def _btn(self, parent, text, command, colour=None):
        bg = colour or BRAND["accent_dark"]
        is_neutral = bg.lower() in (BRAND["surface_alt"].lower(), BRAND["border"].lower(), BRAND["surface"].lower())
        fg = BRAND["text"] if is_neutral else BRAND["accent_text"]
        return tk.Button(parent, text=text, command=command,
                         bg=bg, fg=fg,
                         activebackground=BRAND["accent"], activeforeground=BRAND["accent_text"],
                         font=("Segoe UI", 8, "bold"), relief=tk.FLAT, padx=9, pady=2,
                         cursor="hand2", bd=0)

    @staticmethod
    def _safe_filename_part(text: str) -> str:
        """Sanitise a label/MAC/port for Windows & macOS filenames."""
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
        return cleaned.strip("_") or "log"

    def _build_tab_log_meta(self):
        """Return (widget, tab_key, default_filename, header_text) for the currently visible tab."""
        selected = self.log_notebook.select()
        tab_key = self.log_tab_sessions.get(selected)
        now_local = datetime.now()
        now_utc = datetime.now(timezone.utc)
        stamp = now_local.strftime("%Y%m%d_%H%M%S")

        if tab_key == "serial" or tab_key is None:
            widget = self.txt_serial
            port_raw = (self.cmb_port.get().split("  -  ")[0].strip()
                        if hasattr(self, "cmb_port") else "")
            port_part = self._safe_filename_part(os.path.basename(port_raw) if port_raw else "serial")
            baud = self.cmb_baud.get() if hasattr(self, "cmb_baud") else ""
            line_count = self.device_log_counts.get("serial", 0)
            default_name = f"serial_{port_part}_{stamp}.log"
            header = (
                f"# {BRAND['company']} {BRAND['product']} v{BRAND['version']}\n"
                f"# Tab: Serial Terminal\n"
                f"# Port: {port_raw or '--'} @ {baud or '--'}\n"
                f"# Saved: {now_local.isoformat(timespec='seconds')} (UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')})\n"
                f"# Lines: {line_count}\n"
                f"{'-' * 78}\n"
            )
            return widget, "serial", default_name, header

        widget = self.device_log_widgets.get(tab_key, self.txt_serial)
        session = engine.sessions.get(tab_key)
        telem = self.device_telemetry.get(tab_key, {})
        name = getattr(session, "name", None) or "device"
        address = getattr(session, "address", None) or telem.get("address") or tab_key
        profile = getattr(session, "profile", None) or telem.get("profile") or "device"
        imei = telem.get("imei")
        line_count = self.device_log_counts.get(tab_key, 0)
        gps_count = telem.get("gps_sentences", 0)

        prof_part = self._safe_filename_part(profile)
        addr_part = self._safe_filename_part(address.replace(":", ""))
        default_name = f"{prof_part}_{addr_part}_{stamp}.log"
        imei_line = f"# IMEI: {imei}\n" if imei else ""
        header = (
            f"# {BRAND['company']} {BRAND['product']} v{BRAND['version']}\n"
            f"# Tab: {name} ({profile_label(profile, telem.get('device_type'))})\n"
            f"# MAC: {address}\n"
            f"{imei_line}"
            f"# Saved: {now_local.isoformat(timespec='seconds')} (UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')})\n"
            f"# Lines: {line_count}   GPS sentences: {gps_count}\n"
            f"{'-' * 78}\n"
        )
        return widget, tab_key, default_name, header

    # ---------------------------------------------------------------------------------------
    # Software update (GitHub Releases + offline local .exe upgrade)
    # ---------------------------------------------------------------------------------------
    def _check_update_background(self):
        """Non-blocking startup check for a newer GitHub Release."""
        def _worker():
            try:
                info = updater.check_latest_release(timeout=6.0)
            except Exception:
                return
            if info and info.get("is_newer"):
                self._latest_release_info = info
                self.root.after(0, lambda i=info: self._mark_update_available(i))
        threading.Thread(target=_worker, name="update-check", daemon=True).start()

    def _mark_update_available(self, info: dict):
        """Highlight the top-bar Update button AND show a non-blocking VS Code-style update toast."""
        try:
            self.btn_update.config(
                text=f"⬆ {info['tag']}",
                bg=BRAND["ok"], fg="#ffffff",
            )
            self._show_update_toast(info)
        except tk.TclError:
            pass

    def _show_update_toast(self, info: dict):
        """Show a sleek bottom-right notification banner when a newer release is available."""
        if getattr(self, "_update_toast", None) is not None:
            try:
                self._update_toast.destroy()
            except tk.TclError:
                pass
        toast = tk.Frame(
            self.root, bg=BRAND["surface"], padx=10, pady=8,
            highlightthickness=1, highlightbackground=BRAND["ok"],
        )
        self._update_toast = toast
        tk.Label(
            toast,
            text=f"⬆ New update available: {info['tag']}  (current v{BRAND['version']})",
            font=("Segoe UI", 9, "bold"), fg=BRAND["text"], bg=BRAND["surface"],
        ).pack(side=tk.LEFT, padx=(0, 10))
        self._btn(
            toast, "Update Now",
            lambda: (self._dismiss_update_toast(), self._open_update_dialog()),
            BRAND["ok"],
        ).pack(side=tk.LEFT, padx=(0, 4))
        self._btn(toast, "×", self._dismiss_update_toast, BRAND["surface_alt"]).pack(side=tk.LEFT)
        toast.place(relx=1.0, rely=1.0, anchor="se", x=-16, y=-16)
        toast.lift()

    def _dismiss_update_toast(self):
        toast = getattr(self, "_update_toast", None)
        if toast is not None:
            try:
                toast.destroy()
            except tk.TclError:
                pass
            self._update_toast = None

    def _open_update_dialog(self):
        """Show Software Update dialog: check online GitHub Release or install from a local .exe."""
        dlg = tk.Toplevel(self.root)
        dlg.title(f"{BRAND['company']} — Software Update")
        dlg.geometry("460x260")
        dlg.resizable(False, False)
        dlg.configure(bg=BRAND["bg"])
        dlg.transient(self.root)
        apply_window_icon(dlg)
        apply_os_titlebar_theme(dlg)

        body = tk.Frame(dlg, bg=BRAND["surface"], padx=14, pady=12)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        tk.Label(body, text=f"Installed version: v{BRAND['version']}",
                 font=("Segoe UI", 10, "bold"), fg=BRAND["text"], bg=BRAND["surface"]).pack(anchor="w")

        lbl_status = tk.Label(body, text="Checking GitHub Releases for updates...",
                              font=("Segoe UI", 9), fg=BRAND["text_dim"], bg=BRAND["surface"],
                              wraplength=410, justify=tk.LEFT)
        lbl_status.pack(anchor="w", pady=(6, 8))

        pbar = ttk.Progressbar(body, mode="determinate", maximum=100)
        pbar.pack(fill=tk.X, pady=(0, 10))

        btn_row = tk.Frame(body, bg=BRAND["surface"])
        btn_row.pack(fill=tk.X, pady=(4, 0))

        btn_download = self._btn(btn_row, "Update from Git", lambda: None, BRAND["ok"])
        btn_download.config(state=tk.DISABLED)
        btn_download.pack(side=tk.LEFT, padx=(0, 6))

        def _install_local_exe():
            path = filedialog.askopenfilename(
                parent=dlg,
                title="Select new Succorfish Monitor .exe / Setup installer",
                filetypes=[("Windows Executables", "*.exe"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                msg = updater.apply_windows_exe_update(path)
                lbl_status.config(text=msg, fg=BRAND["ok"])
                dlg.update_idletasks()
                self.root.after(400, self._quit_for_update)
            except Exception as exc:
                messagebox.showerror("Update failed", str(exc), parent=dlg)

        if sys.platform.startswith("win"):
            self._btn(btn_row, "Update from .exe...", _install_local_exe, BRAND["accent_dark"]).pack(
                side=tk.LEFT, padx=(0, 6)
            )

        self._btn(btn_row, "Close", dlg.destroy, BRAND["surface_alt"]).pack(side=tk.RIGHT)

        def _start_online_update(info: dict):
            asset_url = info.get("asset_url")
            asset_name = info.get("asset_name")
            if not asset_url or not asset_name:
                updater.open_macos_update(url=info.get("html_url"))
                return
            btn_download.config(state=tk.DISABLED, text="Downloading...")
            lbl_status.config(text=f"Downloading {asset_name}...", fg=BRAND["info"])

            def _dl():
                try:
                    def _prog(done, total):
                        pct = int(done * 100 / total) if total else 0
                        self.root.after(0, lambda p=pct, d=done, t=total: (
                            pbar.configure(value=p),
                            lbl_status.config(
                                text=f"Downloading {asset_name}... {d / 1048576:.1f} MB"
                                     + (f" / {t / 1048576:.1f} MB ({p}%)" if t else "")
                            ),
                        ))
                    local_path = updater.download_asset(asset_url, asset_name, progress_cb=_prog)
                    if sys.platform.startswith("win"):
                        msg = updater.apply_windows_exe_update(local_path)
                        self.root.after(0, lambda m=msg: lbl_status.config(text=m, fg=BRAND["ok"]))
                        self.root.after(500, self._quit_for_update)
                    else:
                        updater.open_macos_update(zip_path=local_path, url=info.get("html_url"))
                        self.root.after(0, lambda: lbl_status.config(
                            text=f"Downloaded {asset_name} and revealed in Finder. Unzip & replace the app.",
                            fg=BRAND["ok"],
                        ))
                except Exception as exc:
                    self.root.after(0, lambda e=exc: (
                        lbl_status.config(text=f"Download failed: {e}", fg=BRAND["err"]),
                        btn_download.config(state=tk.NORMAL, text="Update from Git"),
                    ))

            threading.Thread(target=_dl, name="update-download", daemon=True).start()

        def _populate(info, err=None):
            if not dlg.winfo_exists():
                return
            if err is not None:
                lbl_status.config(
                    text=f"Could not reach GitHub Releases ({err}).\nYou can still update offline using 'Update from .exe...'.",
                    fg=BRAND["warn"],
                )
                return
            if not info:
                lbl_status.config(text="No releases published yet.", fg=BRAND["text_dim"])
                return
            self._latest_release_info = info
            if info["is_newer"]:
                self._mark_update_available(info)
                lbl_status.config(
                    text=f"New version available: {info['tag']} ({info.get('asset_name') or 'Release page'})",
                    fg=BRAND["ok"],
                )
                btn_download.config(
                    state=tk.NORMAL,
                    text=f"Update from Git ({info['tag']})",
                    command=lambda i=info: _start_online_update(i),
                )
            else:
                lbl_status.config(
                    text=f"You are on the latest version (v{BRAND['version']} · latest release {info['tag']}).",
                    fg=BRAND["ok"],
                )
                if info.get("asset_url"):
                    btn_download.config(
                        state=tk.NORMAL,
                        text="Update from Git",
                        command=lambda i=info: _start_online_update(i),
                    )

        def _fetch():
            try:
                info = updater.check_latest_release(timeout=8.0)
                self.root.after(0, lambda i=info: _populate(i, None))
            except Exception as exc:
                self.root.after(0, lambda e=exc: _populate(None, e))

        threading.Thread(target=_fetch, name="update-dialog-check", daemon=True).start()

    def _quit_for_update(self):
        """Disconnect active links cleanly and exit so the installer / updater can replace the exe."""
        try:
            self.serial.close()
        except Exception:
            pass
        try:
            fut = run_async(engine.disconnect_all())
            fut.result(timeout=2)
        except Exception:
            pass
        self.root.destroy()
        sys.exit(0)

    # ---------------------------------------------------------------------------------------
    # Software update (GitHub Releases + offline local .exe upgrade)
    # ---------------------------------------------------------------------------------------
    def _check_update_background(self):
        """Non-blocking startup check for a newer GitHub Release."""
        def _worker():
            try:
                info = updater.check_latest_release(timeout=6.0)
            except Exception:
                return
            if info and info.get("is_newer"):
                self._latest_release_info = info
                self.root.after(0, lambda i=info: self._mark_update_available(i))
        threading.Thread(target=_worker, name="update-check", daemon=True).start()

    def _mark_update_available(self, info: dict):
        try:
            self.btn_update.config(
                text=f"⬆ {info['tag']}",
                bg=BRAND["ok"], fg="#ffffff",
            )
        except tk.TclError:
            pass

    def _open_update_dialog(self):
        """Show Software Update dialog: check online GitHub Release or install from a local .exe."""
        dlg = tk.Toplevel(self.root)
        dlg.title(f"{BRAND['company']} — Software Update")
        dlg.geometry("460x260")
        dlg.resizable(False, False)
        dlg.configure(bg=BRAND["bg"])
        dlg.transient(self.root)
        apply_window_icon(dlg)
        apply_os_titlebar_theme(dlg)

        body = tk.Frame(dlg, bg=BRAND["surface"], padx=14, pady=12)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        tk.Label(body, text=f"Installed version: v{BRAND['version']}",
                 font=("Segoe UI", 10, "bold"), fg=BRAND["text"], bg=BRAND["surface"]).pack(anchor="w")

        lbl_status = tk.Label(body, text="Checking GitHub Releases for updates...",
                              font=("Segoe UI", 9), fg=BRAND["text_dim"], bg=BRAND["surface"],
                              wraplength=410, justify=tk.LEFT)
        lbl_status.pack(anchor="w", pady=(6, 8))

        pbar = ttk.Progressbar(body, mode="determinate", maximum=100)
        pbar.pack(fill=tk.X, pady=(0, 10))

        btn_row = tk.Frame(body, bg=BRAND["surface"])
        btn_row.pack(fill=tk.X, pady=(4, 0))

        btn_download = self._btn(btn_row, "Update from Git", lambda: None, BRAND["ok"])
        btn_download.config(state=tk.DISABLED)
        btn_download.pack(side=tk.LEFT, padx=(0, 6))

        def _install_local_exe():
            path = filedialog.askopenfilename(
                parent=dlg,
                title="Select new Succorfish Monitor .exe / Setup installer",
                filetypes=[("Windows Executables", "*.exe"), ("All files", "*.*")],
            )
            if not path:
                return
            try:
                msg = updater.apply_windows_exe_update(path)
                lbl_status.config(text=msg, fg=BRAND["ok"])
                dlg.update_idletasks()
                self.root.after(400, self._quit_for_update)
            except Exception as exc:
                messagebox.showerror("Update failed", str(exc), parent=dlg)

        if sys.platform.startswith("win"):
            self._btn(btn_row, "Update from .exe...", _install_local_exe, BRAND["accent_dark"]).pack(
                side=tk.LEFT, padx=(0, 6)
            )

        self._btn(btn_row, "Close", dlg.destroy, BRAND["surface_alt"]).pack(side=tk.RIGHT)

        def _start_online_update(info: dict):
            asset_url = info.get("asset_url")
            asset_name = info.get("asset_name")
            if not asset_url or not asset_name:
                updater.open_macos_update(url=info.get("html_url"))
                return
            btn_download.config(state=tk.DISABLED, text="Downloading...")
            lbl_status.config(text=f"Downloading {asset_name}...", fg=BRAND["info"])

            def _dl():
                try:
                    def _prog(done, total):
                        pct = int(done * 100 / total) if total else 0
                        self.root.after(0, lambda p=pct, d=done, t=total: (
                            pbar.configure(value=p),
                            lbl_status.config(
                                text=f"Downloading {asset_name}... {d / 1048576:.1f} MB"
                                     + (f" / {t / 1048576:.1f} MB ({p}%)" if t else "")
                            ),
                        ))
                    local_path = updater.download_asset(asset_url, asset_name, progress_cb=_prog)
                    if sys.platform.startswith("win"):
                        msg = updater.apply_windows_exe_update(local_path)
                        self.root.after(0, lambda m=msg: lbl_status.config(text=m, fg=BRAND["ok"]))
                        self.root.after(500, self._quit_for_update)
                    else:
                        updater.open_macos_update(zip_path=local_path, url=info.get("html_url"))
                        self.root.after(0, lambda: lbl_status.config(
                            text=f"Downloaded {asset_name} and revealed in Finder. Unzip & replace the app.",
                            fg=BRAND["ok"],
                        ))
                except Exception as exc:
                    self.root.after(0, lambda e=exc: (
                        lbl_status.config(text=f"Download failed: {e}", fg=BRAND["err"]),
                        btn_download.config(state=tk.NORMAL, text="Update from Git"),
                    ))

            threading.Thread(target=_dl, name="update-download", daemon=True).start()

        def _populate(info, err=None):
            if not dlg.winfo_exists():
                return
            if err is not None:
                lbl_status.config(
                    text=f"Could not reach GitHub Releases ({err}).\nYou can still update offline using 'Update from .exe...'.",
                    fg=BRAND["warn"],
                )
                return
            if not info:
                lbl_status.config(text="No releases published yet.", fg=BRAND["text_dim"])
                return
            self._latest_release_info = info
            if info["is_newer"]:
                self._mark_update_available(info)
                lbl_status.config(
                    text=f"New version available: {info['tag']} ({info.get('asset_name') or 'Release page'})",
                    fg=BRAND["ok"],
                )
                btn_download.config(
                    state=tk.NORMAL,
                    text=f"Update from Git ({info['tag']})",
                    command=lambda i=info: _start_online_update(i),
                )
            else:
                lbl_status.config(
                    text=f"You are on the latest version (v{BRAND['version']} · latest release {info['tag']}).",
                    fg=BRAND["ok"],
                )
                if info.get("asset_url"):
                    btn_download.config(
                        state=tk.NORMAL,
                        text="Update from Git",
                        command=lambda i=info: _start_online_update(i),
                    )

        def _fetch():
            try:
                info = updater.check_latest_release(timeout=8.0)
                self.root.after(0, lambda i=info: _populate(i, None))
            except Exception as exc:
                self.root.after(0, lambda e=exc: _populate(None, e))

        threading.Thread(target=_fetch, name="update-dialog-check", daemon=True).start()

    def _quit_for_update(self):
        """Disconnect active links cleanly and exit so the installer / updater can replace the exe."""
        try:
            self.serial.close()
        except Exception:
            pass
        try:
            fut = run_async(engine.disconnect_all())
            fut.result(timeout=2)
        except Exception:
            pass
        self.root.destroy()
        sys.exit(0)

    def _save_logs(self):
        """Save the currently selected tab's log with a tab-specific timestamped filename."""
        widget, tab_key, default_name, header = self._build_tab_log_meta()
        path = filedialog.asksaveasfilename(
            title=f"Save log ({default_name})",
            defaultextension=".log",
            initialfile=default_name,
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            content = widget.get("1.0", tk.END)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(header)
                fh.write(content)
            log_system(f"Log saved: {os.path.basename(path)}", BRAND["ok"],
                       session_id=None if tab_key == "serial" else tab_key)
        except Exception as exc:
            messagebox.showerror("Save failed", f"Could not write the log file:\n{exc}")

    def _clear_logs(self):
        """Clear the visible log tab (asks first when it holds lines)."""
        selected = self.log_notebook.select()
        tab_key = self.log_tab_sessions.get(selected)
        widget, session_id = self._current_log_widget()
        count_key = "serial" if tab_key == "serial" else session_id
        line_count = self.device_log_counts.get(count_key, 0) if count_key else self.total_lines
        if line_count and not messagebox.askyesno(
                "Clear log", f"Discard {line_count} lines from this log tab?\n\n"
                             f"Use 'Save' first if you need them."):
            return
        widget.delete("1.0", tk.END)
        if count_key:
            self.device_log_counts[count_key] = 0
        else:
            self.total_lines = 0
        self.lbl_lines.config(text="Lines: 0")
        log_system("Log view cleared.", BRAND["text_dim"],
                   session_id=None if count_key == "serial" else count_key)

    def _show_find_bar(self, _event=None):
        """Show and focus the floating VS Code-style Find overlay (Ctrl+F / Cmd+F / Find button)."""
        self._find_visible = True
        self._find_bar.place(relx=1.0, rely=0.0, anchor="ne", x=-18, y=30)
        self._find_bar.lift()
        self._search_entry.focus_set()
        self._search_entry.selection_range(0, tk.END)
        if self.search_var.get():
            self._search_apply()
        return "break"

    def _hide_find_bar(self, _event=None):
        """Hide the floating Find overlay and clear highlights (Esc or ×)."""
        self._find_visible = False
        self._find_bar.place_forget()
        self._search_clear()
        return "break"

    def _toggle_find_bar(self):
        if self._find_visible:
            self._hide_find_bar()
        else:
            self._show_find_bar()

    def _toggle_find_case(self):
        on = not self._find_case_var.get()
        self._find_case_var.set(on)
        self._btn_find_case.config(
            bg=BRAND["accent_dark"] if on else BRAND["surface_alt"],
            fg=BRAND["accent_text"] if on else BRAND["text_dim"],
        )
        self._search_apply()

    def _toggle_find_regex(self):
        on = not self._find_regex_var.get()
        self._find_regex_var.set(on)
        self._btn_find_regex.config(
            bg=BRAND["accent_dark"] if on else BRAND["surface_alt"],
            fg=BRAND["accent_text"] if on else BRAND["text_dim"],
        )
        self._search_apply()

    def _search_focus_match(self, widget, idx: int):
        """Highlight and scroll to match ``idx`` and update the VS Code-style counter."""
        total = len(self._search_matches)
        if total == 0:
            self._search_idx = -1
            if hasattr(self, "lbl_search_count"):
                self.lbl_search_count.config(text="No results", fg=BRAND["err"])
            return
        self._search_idx = idx % total
        start, end = self._search_matches[self._search_idx]
        widget.tag_remove("search_cur", "1.0", tk.END)
        widget.tag_add("search_cur", start, end)
        widget.tag_raise("search_cur")
        widget.tag_config("search_cur", background=BRAND["accent"], foreground=BRAND["accent_text"])
        widget.mark_set(tk.INSERT, start)
        widget.see(start)
        if hasattr(self, "lbl_search_count"):
            self.lbl_search_count.config(text=f"{self._search_idx + 1} of {total}", fg=BRAND["text_dim"])

    def _search_apply(self):
        """Highlight every match as the user types and jump to the first match (VS Code Find)."""
        widget, _ = self._current_log_widget()
        widget.tag_remove("search_hit", "1.0", tk.END)
        widget.tag_remove("search_cur", "1.0", tk.END)
        self._search_matches = []
        self._search_idx = -1
        needle = self.search_var.get()
        if not needle:
            if hasattr(self, "lbl_search_count"):
                self.lbl_search_count.config(text="", fg=BRAND["text_dim"])
            return
        nocase = not self._find_case_var.get()
        use_regex = self._find_regex_var.get()
        count_var = tk.IntVar()
        start = "1.0"
        while True:
            try:
                pos = widget.search(
                    needle, start, stopindex=tk.END,
                    nocase=nocase, regexp=use_regex, count=count_var,
                )
            except tk.TclError:
                # Incomplete regex while typing
                if hasattr(self, "lbl_search_count"):
                    self.lbl_search_count.config(text="Bad regex", fg=BRAND["err"])
                return
            if not pos:
                break
            match_len = count_var.get() if use_regex else len(needle)
            if match_len <= 0:
                start = widget.index(f"{pos}+1c")
                if widget.compare(start, ">=", tk.END):
                    break
                continue
            end = f"{pos}+{match_len}c"
            widget.tag_add("search_hit", pos, end)
            self._search_matches.append((pos, end))
            start = end
            if len(self._search_matches) >= 5000:
                break
        widget.tag_config("search_hit", background=BRAND["warn"], foreground="#000000")
        self._search_focus_match(widget, 0)

    def _search_next(self):
        """Jump to the next match (▼ or Enter)."""
        if not self.search_var.get():
            return
        widget, _ = self._current_log_widget()
        if not self._search_matches:
            self._search_apply()
            return
        self._search_focus_match(widget, self._search_idx + 1)

    def _search_prev(self):
        """Jump to the previous match (▲ or Shift+Enter)."""
        if not self.search_var.get():
            return
        widget, _ = self._current_log_widget()
        if not self._search_matches:
            self._search_apply()
            return
        self._search_focus_match(widget, self._search_idx - 1)

    def _search_clear(self):
        """Clear the Find box and remove all search highlights (× or Esc)."""
        self.search_var.set("")
        self._search_matches = []
        self._search_idx = -1
        if hasattr(self, "lbl_search_count"):
            self.lbl_search_count.config(text="", fg=BRAND["text_dim"])
        widget, _ = self._current_log_widget()
        widget.tag_remove("search_hit", "1.0", tk.END)
        widget.tag_remove("search_cur", "1.0", tk.END)

    def parse_nmea(self, line: str, session_id: "str | None" = None):
        """Update the GPS line (UTC time, fix state, lat/lon), telemetry, and map from RMC/GGA sentences."""
        def _nmea_to_dd(val_str: str, hemi: str, deg_digits: int) -> float:
            d = int(val_str[:deg_digits])
            m = float(val_str[deg_digits:])
            dd = d + m / 60.0
            return -dd if hemi in ("S", "W") else dd

        try:
            sid = session_id or engine.selected_session_id
            telem = self.device_telemetry.get(sid) if sid else None

            if "$GNRMC" in line or "$GPRMC" in line:
                idx = line.find("$GN") if "$GN" in line else line.find("$GP")
                parts = line[idx:].strip().split(",")
                if len(parts) >= 10:
                    raw_time, status = parts[1], parts[2]
                    lat, lon = (f"{parts[3]} {parts[4]}" if parts[3] else "--"), (f"{parts[5]} {parts[6]}" if parts[5] else "--")
                    formatted_time = f"{raw_time[0:2]}:{raw_time[2:4]}:{raw_time[4:6]} UTC" if len(raw_time) >= 6 else raw_time
                    self.lbl_gps_time.config(text=f"UTC Time: {formatted_time}")
                    if status == "A":
                        self.lbl_gps_status.config(text="GPS: 3D Fix (A)", bg=BRAND["ok"], fg=BRAND["text"])
                        self.lbl_gps_coords.config(text=f"Lat: {lat} | Lon: {lon}")
                    else:
                        self.lbl_gps_status.config(text="GPS: Searching (V)", bg=BRAND["warn"], fg=BRAND["text"])

                    if telem is not None:
                        raw_date = parts[9] if len(parts) > 9 else ""
                        if status == "A" and len(raw_time) >= 6 and len(raw_date) >= 6:
                            try:
                                dy = int(raw_date[0:2])
                                mo = int(raw_date[2:4])
                                yr = 2000 + int(raw_date[4:6])
                                if yr >= 2020:
                                    hr = int(raw_time[0:2])
                                    mn = int(raw_time[2:4])
                                    sc = int(raw_time[4:6])
                                    dt = datetime(yr, mo, dy, hr, mn, sc, tzinfo=timezone.utc)
                                    telem["dev_epoch"] = dt.timestamp()
                                    telem["dev_epoch_monotonic"] = time.monotonic()
                                    telem["lbl_dev_clock"].config(text=f"🕒 Dev Clock: {dt.strftime('%H:%M:%S UTC')}")
                            except Exception:
                                pass

                        telem["gps_fix_state"] = status
                        telem["lbl_utc"].config(text=f"🌐 GPS UTC: {formatted_time}")
                        led_off = telem.get("led_all_flag") == 0 or telem.get("low_power") == 1
                        if status == "A":
                            t_max = telem.get("gps_timeout_sec", 120)
                            telem["lbl_gps_status"].config(text=f"GPS: 3D Fix ({t_max}s max)", bg=BRAND["ok"], fg="#ffffff")
                            if not led_off:
                                telem["lbl_led"].config(text="💡 LEDs: ON | GPS fixed", bg="#064e3b", fg="#6ee7b7")
                        else:
                            t_max = telem.get("gps_timeout_sec", 120)
                            if telem.get("gps_fix_start_ts"):
                                el = max(0, int(time.time() - telem["gps_fix_start_ts"]))
                                telem["lbl_gps_status"].config(
                                    text=f"GPS: Searching ({min(el, t_max)}s/{t_max}s max)",
                                    bg=BRAND["warn"], fg="#000000",
                                )
                            else:
                                telem["lbl_gps_status"].config(
                                    text=f"GPS: Searching (0s/{t_max}s max)",
                                    bg=BRAND["warn"], fg="#000000",
                                )
                            if not led_off:
                                telem["lbl_led"].config(text="💡 LEDs: ON | GPS searching", bg=BRAND["surface_alt"], fg=BRAND["warn"])
                            if telem.get("gps_map"):
                                telem["gps_map"].set_searching()

                        if status == "A" and parts[3] and parts[5]:
                            try:
                                lat_dd = _nmea_to_dd(parts[3], parts[4], 2)
                                lon_dd = _nmea_to_dd(parts[5], parts[6], 3)
                                telem["lbl_coords"].config(
                                    text=f"📍 Lat: {lat_dd:.6f}\n     Lon: {lon_dd:.6f}")
                                if telem.get("gps_map"):
                                    telem["gps_map"].update_fix(lat_dd, lon_dd, fixed=True, utc_time=formatted_time)
                            except Exception:
                                telem["lbl_coords"].config(text=f"📍 Lat: {lat}\n     Lon: {lon}")

            elif "$GNGGA" in line or "$GPGGA" in line:
                idx = line.find("$GN") if "$GN" in line else line.find("$GP")
                parts = line[idx:].strip().split(",")
                if len(parts) >= 8:
                    raw_time = parts[1]
                    lat_raw, lat_hemi = parts[2], parts[3]
                    lon_raw, lon_hemi = parts[4], parts[5]
                    fix_qual, num_sv = parts[6], parts[7]
                    formatted_time = f"{raw_time[0:2]}:{raw_time[2:4]}:{raw_time[4:6]} UTC" if len(raw_time) >= 6 else raw_time
                    if formatted_time:
                        self.lbl_gps_time.config(text=f"UTC Time: {formatted_time}")

                    if fix_qual not in ("", "0") and lat_raw and lon_raw:
                        try:
                            lat_dd = _nmea_to_dd(lat_raw, lat_hemi, 2)
                            lon_dd = _nmea_to_dd(lon_raw, lon_hemi, 3)
                            sat_txt = f" ({num_sv} SV)" if num_sv else ""
                            self.lbl_gps_status.config(text=f"GPS: 3D Fix (A){sat_txt}", bg=BRAND["ok"], fg=BRAND["text"])
                            self.lbl_gps_coords.config(text=f"Lat: {lat_dd:.6f} | Lon: {lon_dd:.6f}")
                            if telem is not None:
                                telem["gps_fix_state"] = "A"
                                telem["lbl_gps_status"].config(text=f"GPS: 3D Fix (A){sat_txt}", bg=BRAND["ok"], fg="#ffffff")
                                if formatted_time:
                                    telem["lbl_utc"].config(text=f"🌐 GPS UTC: {formatted_time}")
                                telem["lbl_coords"].config(
                                    text=f"📍 Lat: {lat_dd:.6f}\n     Lon: {lon_dd:.6f}")
                                if telem.get("gps_map"):
                                    telem["gps_map"].update_fix(lat_dd, lon_dd, fixed=True, sats=num_sv, utc_time=formatted_time)
                        except Exception:
                            pass
        except Exception:
            pass

    def do_scan(self):
        """Scan for devices with the chosen name filter (results arrive as a scan_results message)."""
        self.btn_scan.config(state=tk.DISABLED)
        self.device_combo.set("")
        selected_preset = self.preset_var.get()
        filter_text = next((val for label, val in ADV_NAME_PRESETS if label == selected_preset), "")
        run_async(engine.scan(filter_text=filter_text, timeout=6.0), session_id="")

    def do_connect(self):
        """Connect to the device chosen in the combo box with the chosen profile."""
        selection = self.device_combo.get()
        if not selection:
            log_system("Select a scanned BLE device before connecting.", BRAND["warn"])
            return
        fields = selection.split("|")
        if len(fields) < 2:
            log_system("Selected BLE device entry is invalid. Scan again and select a device.",
                       BRAND["err"])
            return
        target = fields[1].strip()
        device_info = next((device for device in self.devices_cache
                            if device["mac"].lower() == target.lower()), None)
        if device_info is None:
            log_system("Selected BLE device is no longer in the scan results. Scan again.",
                       BRAND["warn"])
            return

        is_ota_device = bool(
            device_info.get("is_ota") or "stm_ota" in (device_info.get("name") or "").lower()
        )
        if is_ota_device:
            log_system(
                f"Device {device_info['name']} [{target}] is in STM_OTA bootloader mode; "
                f"opening DOTA Update panel...",
                BRAND["info"],
            )
            ConfigPanel(self.root, engine, initial_tab="dota", target_address=target)
            return

        device_type = self._selected_device_type()
        profile = device_type_profile(device_type) or device_info.get("suggested_profile") or "valtrack"
        session_id = target.lower()
        self._ensure_device_log_tab(
            session_id, device_info["name"], target, profile)
        frame = self.device_log_frames.get(session_id)
        if frame:
            self.log_notebook.select(frame)
            self.log_notebook.tab(
                frame, text=f"{device_info['name']} [{target}] - CONNECTING")
        self.session_status[session_id] = "connecting" 
        log_system(f"Connection requested for {device_info['name']} [{target}].",
                   BRAND["info"], session_id=session_id)
        self._sync_selected_session_controls()
        run_async(engine.connect(target_identifier=target, profile=profile, device_type=device_type),
                  session_id=session_id)

    def do_disconnect(self):
        """Disconnect the selected device."""
        self.btn_disconnect.config(state=tk.DISABLED)
        run_async(engine.disconnect_selected(),
                  session_id=engine.selected_session_id)

    @staticmethod
    def _format_identity_text(address: str, profile: str, device_type=None, imei: "str | None" = None) -> str:
        """Format the per-tab identity block: MAC, IMEI (for cellular trackers) and Device type."""
        prof_label = profile_label(profile, device_type)
        prof = get_profile(profile)
        if prof and prof.config_window in ("valtrack", "sc2_hub"):
            return f"MAC: {address}\nIMEI: {imei or '--'}\nDevice: {prof_label}"
        return f"MAC: {address}\nDevice: {prof_label}"

    def open_config_panel(self):
        """Open (or focus) the configuration window strictly for the currently selected device tab."""
        # Prefer the active device tab so Valtrack and SC2 sessions never conflict when both are connected.
        selected_tab = self.log_notebook.select()
        tab_sid = self.log_tab_sessions.get(selected_tab)
        if tab_sid in ("serial", "system", None):
            tab_sid = None

        combo_sel = self.device_combo.get()
        combo_mac = combo_sel.split("|")[1].strip() if (combo_sel and "|" in combo_sel) else None

        active_sid = tab_sid or engine.selected_session_id or (combo_mac.lower() if combo_mac else None)
        session = engine.sessions.get(active_sid) if active_sid else engine.selected_session
        if session is not None:
            engine.select_session(session.session_id)

        prof = get_profile(getattr(session, "profile", None) or self._profile_from_combo())

        if session is None or not session.connected:
            target_addr = getattr(session, "address", None) or combo_mac
            ConfigPanel(
                self.root, engine, initial_tab="dota",
                target_address=target_addr,
                session_id=session.session_id if session else None,
            )
            return

        sid = session.session_id
        window = prof.config_window if prof else "valtrack"
        if window in ("dt_logger", "gas_logger"):
            self.open_sensor_panel()
            return

        # Reuse existing config window for THIS exact session & profile type if already open.
        existing = self._config_windows.get(sid)
        if existing is not None:
            try:
                if existing.winfo_exists() and getattr(existing, "_config_window_kind", None) == window:
                    existing.deiconify()
                    existing.lift()
                    existing.focus_force()
                    return
                if existing.winfo_exists():
                    existing.destroy()
            except tk.TclError:
                pass
            self._config_windows.pop(sid, None)

        if window == "sc2_hub":
            win = Sc2HubConfigPanel(self.root, engine, target_address=session.address, session_id=sid)
        elif window == "gigo":
            win = GigoFamilyPanel(self.root, engine, target_address=session.address, session_id=sid)
        else:
            win = ConfigPanel(self.root, engine, target_address=session.address, session_id=sid)
        win._config_window_kind = window
        self._config_windows[sid] = win
        win.bind("<Destroy>", lambda e, s=sid, w=win: self._config_windows.pop(s, None) if e.widget is w else None)

    def open_sensor_panel(self):
        """Open the logger window of the connected GiGO Depth & Temp or GiGO Gas logger."""
        if not self.is_connected:
            messagebox.showwarning("Not connected", "Connect to a device first.")
            return
        if getattr(engine, "active_sc2", None) is None:
            messagebox.showinfo(
                "Logger required",
                "The logger window is for the GiGO Depth & Temp and GiGO Gas loggers.\n\n"
                "Connect to one (Device type 'GiGO Depth & Temp', 'GiGO Gas' or Auto-detect).")
            return
        session = engine.selected_session
        if getattr(session, "profile", None) == "gigo_gas":
            GasPanel(self.root, engine)
        else:
            Sc2Panel(self.root, engine)

    def _on_toggle_device_auto_debug(self, session_id: str):
        """Toggle Auto Debug Log for a specific device tab and apply it to its connected session."""
        telem = self.device_telemetry.get(session_id)
        if not telem:
            return
        enabled = bool(telem["var_auto_debug"].get())
        telem["auto_debug_enabled"] = enabled
        session = engine.sessions.get(session_id)
        if session and session.connected and session.device:
            dev = session.device
            if session_id == engine.selected_session_id:
                self.debug_log_on = enabled
                self._render_debuglog_button()

            async def _apply():
                ok = await dev.set_debug_log(enabled)
                if not ok:
                    log_system(
                        f"Device did not accept debug log {'ON' if enabled else 'OFF'}.",
                        BRAND["warn"], session_id=session_id,
                    )
                post_gui("stream_state", streaming=dev.streaming_enabled, session_id=session_id)

            run_async(_apply(), session_id=session_id)

    def _on_toggle_auto_reconnect(self):
        """Turn auto-reconnect on or off for all sessions."""
        engine.auto_reconnect = self.var_auto_reconnect.get()
        state_str = "ENABLED" if engine.auto_reconnect else "DISABLED"
        log_system(f"Auto-reconnect is now {state_str}.", BRAND["info"])

    # ---- manual debug-log toggle --------------------------------------------------------
    def _render_debuglog_button(self):
        """Reflect the current log state on the toolbar button."""
        if self.debug_log_on:
            self.btn_debuglog.config(text="Debug Log: ON", bg=BRAND["ok"])
        else:
            self.btn_debuglog.config(text="Debug Log: OFF", bg=BRAND["surface_alt"])

    def toggle_debug_log(self):
        """Manual override of the device's 0xFD debug stream.

        The firmware enables the stream automatically once authentication completes, and remembers
        that the client took manual control for the rest of the link. So this button is the single
        place the operator needs: it silences the stream without disconnecting, and brings it back.
        """
        if not self.is_connected:
            return
        dev = getattr(engine, "active_device", None)
        if dev is None:
            messagebox.showinfo(
                "Not available",
                "The live debug stream (0xFD) exists on Valtrack and SC2 only.\n\n"
                "GiGO Delphis, Rotation / Jack and Depth & Temp have no debug stream.")
            return

        target = not self.debug_log_on
        session_id = engine.selected_session_id
        self.debug_log_on = target
        self._render_debuglog_button()
        log_system(f"Debug log --- {'ON' if target else 'OFF'} (manual)", BRAND["info"],
                   session_id=session_id)

        async def go():
            """Send the debug-log command; put the button back when the device refuses it."""
            try:
                ok = await dev.set_debug_log(target)
                if not ok:
                    self.root.after(0, lambda: self._revert_debuglog(target, session_id))
                else:
                    post_gui("stream_state", streaming=dev.streaming_enabled,
                             session_id=session_id)
            except Exception as exc:
                log_system(f"Debug log request failed: {exc}", BRAND["err"],
                           session_id=session_id)
                self.root.after(0, lambda: self._revert_debuglog(target, session_id))
        run_async(go(), session_id=session_id)

    def _revert_debuglog(self, attempted, session_id=None):
        """The device refused the debug-log change: restore the button and log a warning."""
        if session_id == engine.selected_session_id:
            self.debug_log_on = not attempted
            self._render_debuglog_button()
        log_system(f"Device did not accept debug log {'ON' if attempted else 'OFF'} --- "
                       f"device remains {'OFF' if attempted else 'ON'}", BRAND["warn"],
                       session_id=session_id)

    def toggle_stream(self):
        """Switch the selected device's debug stream on or off."""
        if not self.is_connected:
            return
        run_async(engine.toggle_stream(not self.is_streaming))

    def clear_logs(self):
        """Clear the visible log tab (button handler)."""
        self._clear_logs()


def main():
    """Create the Tk root and the main window, disconnect every device on close, run the Tk main loop."""
    root = tk.Tk()
    BleGuiApp(root)

    def on_closing():
        """Window closed: disconnect all devices (up to 3 s), then destroy the window."""
        try:
            fut = run_async(engine.disconnect_all())
            fut.result(timeout=3)
        except Exception:
            pass
        root.destroy()
        sys.exit(0)

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()
