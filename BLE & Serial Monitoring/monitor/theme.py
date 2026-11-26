"""@file theme.py
@brief Dark brand theme (ttk styles), the logo header bar shown on every window, and the window icon
@version 1.0
@date 2026-11-07
"""
from .constants import BRAND, FONT_MONO_FAMILY, FONT_UI_FAMILY, THEME_PALETTES, get_theme_mode
from .logbus import log_system
from brand_assets import BRAND_ICON_B64
from brand_assets import BRAND_LOGO_B64
from datetime import datetime, timezone
from tkinter import ttk
import sys
import tkinter as tk


_MAC_BUTTON_SHIM_INSTALLED = False
_MAC_BTN_STYLE_CACHE: set[str] = set()


def _install_macos_button_shim() -> None:
    """On macOS Aqua, classic tk.Button ignores bg/fg and draws white pills.

    Replace tk.Button with a thin ttk.Button subclass backed by the 'clam' theme
    so every dark-theme button across all panels keeps its intended colours and
    contrast without rewriting call sites.
    """
    global _MAC_BUTTON_SHIM_INSTALLED
    if _MAC_BUTTON_SHIM_INSTALLED or sys.platform != "darwin":
        return
    _MAC_BUTTON_SHIM_INSTALLED = True

    _COLOR_KEYS = {
        "bg", "background", "fg", "foreground",
        "activebackground", "activeforeground", "disabledforeground",
        "font", "padx", "pady", "anchor", "justify",
    }
    _IGNORED_KEYS = {
        "relief", "bd", "borderwidth", "overrelief",
        "highlightthickness", "highlightbackground", "highlightcolor",
        "wraplength",
    }

    class _MacColorButton(ttk.Button):
        def __init__(self, master=None, **kw):
            self._visual = {
                "bg": BRAND["surface_alt"],
                "fg": BRAND["text"],
                "abg": BRAND["accent_dark"],
                "afg": BRAND["accent_text"],
                "dfg": BRAND["text_muted"],
                "font": BRAND["font_ui"],
                "padx": 8,
                "pady": 3,
                "anchor": "center",
            }
            ttk_kw = self._extract_visual(kw)
            super().__init__(master, **ttk_kw)
            self._apply_style()

        def _extract_visual(self, kw: dict) -> dict:
            out = {}
            for k, v in kw.items():
                if k in ("bg", "background") and v is not None:
                    self._visual["bg"] = v
                elif k in ("fg", "foreground") and v is not None:
                    self._visual["fg"] = v
                elif k == "activebackground" and v is not None:
                    self._visual["abg"] = v
                elif k == "activeforeground" and v is not None:
                    self._visual["afg"] = v
                elif k == "disabledforeground" and v is not None:
                    self._visual["dfg"] = v
                elif k == "font" and v is not None:
                    self._visual["font"] = v
                elif k == "padx" and v is not None:
                    self._visual["padx"] = v
                elif k == "pady" and v is not None:
                    self._visual["pady"] = v
                elif k == "anchor" and v is not None:
                    self._visual["anchor"] = v
                elif k in _IGNORED_KEYS or k == "justify":
                    continue
                else:
                    out[k] = v
            return out

        def _apply_style(self) -> None:
            v = self._visual
            key = (
                f"{v['bg']}|{v['fg']}|{v['abg']}|{v['afg']}|{v['dfg']}|"
                f"{v['font']}|{v['padx']}|{v['pady']}|{v['anchor']}"
            )
            style_name = f"MacBtn_{abs(hash(key)) & 0xFFFFFFFF:08x}.TButton"
            if style_name not in _MAC_BTN_STYLE_CACHE:
                style = ttk.Style(self)
                style.configure(
                    style_name,
                    background=v["bg"],
                    foreground=v["fg"],
                    font=v["font"],
                    padding=(v["padx"], v["pady"]),
                    borderwidth=0,
                    relief="flat",
                    anchor=v["anchor"],
                    focuscolor=BRAND["accent"],
                )
                style.map(
                    style_name,
                    background=[
                        ("disabled", BRAND["border"]),
                        ("pressed", v["abg"]),
                        ("active", v["abg"]),
                    ],
                    foreground=[
                        ("disabled", v["dfg"]),
                        ("pressed", v["afg"]),
                        ("active", v["afg"]),
                    ],
                )
                _MAC_BTN_STYLE_CACHE.add(style_name)
            super().configure(style=style_name)

        def configure(self, cnf=None, **kw):
            if isinstance(cnf, dict):
                kw = {**cnf, **kw}
                cnf = None
            if cnf is not None:
                return super().configure(cnf)
            if not kw:
                return super().configure()
            has_visual = any(k in _COLOR_KEYS or k in _IGNORED_KEYS for k in kw)
            ttk_kw = self._extract_visual(kw)
            res = super().configure(**ttk_kw) if ttk_kw else None
            if has_visual:
                self._apply_style()
            return res

        config = configure

        def cget(self, key):
            if key in ("bg", "background"):
                return self._visual["bg"]
            if key in ("fg", "foreground"):
                return self._visual["fg"]
            if key == "activebackground":
                return self._visual["abg"]
            if key == "activeforeground":
                return self._visual["afg"]
            if key == "font":
                return self._visual["font"]
            return super().cget(key)

        def __setitem__(self, key, value):
            self.configure(**{key: value})

        def __getitem__(self, key):
            return self.cget(key)

    tk.Button = _MacColorButton


# ---------------------------------------------------------------------------------------------
# Branding: theme, header bar, window icon
# ---------------------------------------------------------------------------------------------
_BRAND_IMAGES = {}   # PhotoImage refs must outlive their widgets or Tk drops the bitmap


def brand_images():
    """Lazily decode the embedded PNGs. Returns (logo, icon) PhotoImage or (None, None)."""
    if "logo" not in _BRAND_IMAGES:
        try:
            _BRAND_IMAGES["logo"] = tk.PhotoImage(data=BRAND_LOGO_B64)
            _BRAND_IMAGES["icon"] = tk.PhotoImage(data=BRAND_ICON_B64)
        except Exception as exc:                      # a corrupt embed must not kill startup
            log_system(f"Brand assets unavailable: {exc}", BRAND["warn"])
            _BRAND_IMAGES["logo"] = None
            _BRAND_IMAGES["icon"] = None
    return _BRAND_IMAGES["logo"], _BRAND_IMAGES["icon"]


def apply_brand_theme(root):
    """Install ttk styles AND Tk option defaults for a consistent dark theme.

    Two separate systems have to be covered:
      * ttk widgets  -> Style() below
      * classic Tk widgets (Entry, Text, Listbox, Menu, Scrollbar, Checkbutton) -> option_add
    A ttk style alone leaves the Combobox dropdown list, text selection and the scrolled-text
    scrollbar light -- that mismatch is what makes a dark theme look unfinished.
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")      # clam honours background/foreground; the native themes do not
    except tk.TclError:
        pass
    _install_macos_button_shim()

    b = BRAND

    # ---- classic Tk defaults (covers dropdowns, selection, menus, scrollbars) ------------
    defaults = {
        "*TCombobox*Listbox.background":        b["surface_alt"],
        "*TCombobox*Listbox.foreground":        b["text"],
        "*TCombobox*Listbox.selectBackground":  b["accent_dark"],
        "*TCombobox*Listbox.selectForeground":  b["accent_text"],
        "*TCombobox*Listbox.borderWidth":       0,
        "*Listbox.background":                  b["surface_alt"],
        "*Listbox.foreground":                  b["text"],
        "*Listbox.selectBackground":            b["accent_dark"],
        "*Listbox.selectForeground":            b["accent_text"],
        "*Menu.background":                     b["surface"],
        "*Menu.foreground":                     b["text"],
        "*Menu.activeBackground":               b["accent_dark"],
        "*Menu.activeForeground":               b["accent_text"],
        "*Menu.borderWidth":                    1,
        "*Text.background":                     b["bg"],
        "*Text.foreground":                     b["text"],
        "*Text.selectBackground":               b["accent_dark"],
        "*Text.selectForeground":               b["accent_text"],
        "*Text.insertBackground":               b["text"],
        "*Entry.selectBackground":              b["accent_dark"],
        "*Entry.selectForeground":              b["accent_text"],
        "*Entry.insertBackground":              b["text"],
        "*Scrollbar.background":                b["surface_alt"],
        "*Scrollbar.troughColor":               b["bg"],
        "*Scrollbar.activeBackground":          b["accent_dark"],
        "*Scrollbar.borderWidth":               0,
        "*Scrollbar.highlightThickness":        0,
        "*Checkbutton.selectColor":             b["accent_dark"],
        "*Checkbutton.activeBackground":        b["surface"],
        "*Canvas.background":                   b["bg"],
        "*Tooltip.background":                  b["surface_alt"],
        "*Tooltip.foreground":                  b["text"],
    }
    for key, val in defaults.items():
        try:
            root.option_add(key, val)
        except tk.TclError:
            pass

    # ---- ttk widgets --------------------------------------------------------------------
    style.configure("TNotebook", background=b["bg"], borderwidth=0, tabmargins=(6, 6, 6, 0))
    style.configure("TNotebook.Tab",
                    background=b["surface_alt"], foreground=b["text_dim"],
                    padding=(12, 6), borderwidth=0, font=b["font_ui_b"])
    style.map("TNotebook.Tab",
              background=[("selected", b["accent_dark"]), ("active", b["border"])],
              foreground=[("selected", b["accent_text"]), ("active", b["text"])])

    for name in ("TFrame", "TLabelframe"):
        style.configure(name, background=b["surface"], borderwidth=0)
    style.configure("TLabelframe.Label", background=b["surface"], foreground=b["accent"])
    style.configure("BrandHdr.TFrame", background=THEME_PALETTES["dark"]["bg"])
    style.configure("TLabel", background=b["surface"], foreground=b["text"], font=b["font_ui"])
    style.configure("TSeparator", background=b["border"])

    style.configure("TButton", background=b["surface_alt"], foreground=b["text"],
                    borderwidth=0, focuscolor=b["accent"], padding=(10, 5), font=b["font_ui"])
    style.map("TButton",
              background=[("active", b["accent_dark"]), ("pressed", b["accent_dark"])],
              foreground=[("active", b["accent_text"])])

    style.configure("TCheckbutton", background=b["surface"], foreground=b["text"],
                    focuscolor=b["accent"], font=b["font_ui"],
                    indicatorbackground=b["surface_alt"], indicatorforeground=b["accent_text"])
    style.map("TCheckbutton",
              background=[("active", b["surface"])],
              indicatorbackground=[("selected", b["accent_dark"]),
                                   ("active", b["border"])])
    style.configure("TRadiobutton", background=b["surface"], foreground=b["text"],
                    focuscolor=b["accent"], font=b["font_ui"],
                    indicatorbackground=b["surface_alt"])
    style.map("TRadiobutton",
              background=[("active", b["surface"])],
              indicatorbackground=[("selected", b["accent_dark"])])

    style.configure("TEntry", fieldbackground=b["surface_alt"], foreground=b["text"],
                    insertcolor=b["text"], bordercolor=b["border"], lightcolor=b["border"],
                    darkcolor=b["border"], padding=4)
    style.map("TEntry", fieldbackground=[("readonly", b["border"])])

    style.configure("TCombobox", fieldbackground=b["surface_alt"], background=b["surface_alt"],
                    foreground=b["text"], arrowcolor=b["text_dim"], padding=4,
                    bordercolor=b["border"], lightcolor=b["border"], darkcolor=b["border"])
    style.map("TCombobox",
              fieldbackground=[("readonly", b["surface_alt"])],
              foreground=[("readonly", b["text"])],
              selectbackground=[("readonly", b["surface_alt"])],
              selectforeground=[("readonly", b["text"])],
              background=[("active", b["border"])])

    style.configure("Treeview", background=b["surface_alt"], fieldbackground=b["surface_alt"],
                    foreground=b["text"], borderwidth=0, rowheight=22, font=b["font_ui"])
    style.configure("Treeview.Heading", background=b["surface"], foreground=b["accent"],
                    font=b["font_ui_b"], borderwidth=0)
    style.map("Treeview", background=[("selected", b["accent_dark"])],
              foreground=[("selected", b["accent_text"])])

    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=b["surface_alt"],
                        troughcolor=b["bg"], bordercolor=b["bg"], arrowcolor=b["text_dim"],
                        borderwidth=0, lightcolor=b["surface_alt"], darkcolor=b["surface_alt"])
        style.map(f"{orient}.TScrollbar",
                  background=[("active", b["accent_dark"])])

    style.configure("Horizontal.TProgressbar", background=b["accent"],
                    troughcolor=b["surface_alt"], bordercolor=b["border"],
                    lightcolor=b["accent"], darkcolor=b["accent_dark"])
    style.configure("Brand.Horizontal.TProgressbar", background=b["accent"],
                    troughcolor=b["surface_alt"], bordercolor=b["border"],
                    lightcolor=b["accent"], darkcolor=b["accent_dark"])


def retheme_widget_tree(root, old_palette: dict, new_palette: dict) -> None:
    """Walk root (and any open Toplevels) and remap classic Tk widget colours from old_palette -> new_palette.

    BrandHeader stays dark so the white Succorfish logo lockup remains crisp in both Light and Dark themes.
    """
    cmap: dict[str, str] = {}
    for key, old_val in old_palette.items():
        if key.startswith("log_"):
            continue
        new_val = new_palette.get(key)
        if isinstance(old_val, str) and isinstance(new_val, str) and old_val.lower() != new_val.lower():
            cmap[old_val.lower()] = new_val
    if not cmap:
        return

    color_opts = (
        "bg", "background", "fg", "foreground",
        "activebackground", "activeforeground",
        "selectcolor", "selectbackground", "selectforeground",
        "insertbackground", "highlightbackground", "highlightcolor",
        "readonlybackground", "troughcolor",
    )

    def _walk(widget):
        if isinstance(widget, BrandHeader):
            return
        updates = {}
        for opt in color_opts:
            try:
                cur = str(widget.cget(opt)).lower()
            except Exception:
                continue
            if cur in cmap:
                updates[opt] = cmap[cur]
        if updates:
            try:
                widget.configure(**updates)
            except Exception:
                pass
        for child in widget.winfo_children():
            _walk(child)

    _walk(root)


class BrandHeader(ttk.Frame):
    """The brand bar shown at the top of windows.

    ``compact=True`` renders a slim VS Code-style title strip (no large logo) for the main window.
    """

    def __init__(self, parent, subtitle=None, show_clock=True, on_toggle_theme=None, compact=False):
        super().__init__(parent, style="BrandHdr.TFrame")
        logo, _ = (None, None) if compact else brand_images()
        hdr = THEME_PALETTES["dark"]

        self.columnconfigure(1, weight=1)

        if compact:
            title_txt = f"{BRAND['company']}  ·  {subtitle or BRAND['product']}  v{BRAND['version']}"
            tk.Label(self, text=title_txt, bg=hdr["bg"], fg=hdr["accent"],
                     font=(FONT_UI_FAMILY, 9, "bold")).grid(
                row=0, column=0, sticky="w", padx=(10, 8), pady=3)
            self._ver = None
        elif logo is not None:
            self._logo_img = logo.subsample(2)
            tk.Label(self, image=self._logo_img, bg=hdr["bg"], bd=0).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(8, 10), pady=3)
            if subtitle and subtitle != BRAND["product"]:
                tk.Label(self, text=subtitle, bg=hdr["bg"], fg=hdr["text"],
                         font=(FONT_UI_FAMILY, 9, "bold")).grid(row=0, column=1, sticky="sw",
                                                            padx=(0, 8), pady=(4, 0))
            self._ver = tk.Label(self, text=f"v{BRAND['version']}  |  {BRAND['copyright']}",
                                 bg=hdr["bg"], fg=hdr["text_muted"],
                                 font=(FONT_UI_FAMILY, 7))
            self._ver.grid(row=1, column=1, sticky="nw", padx=(0, 10))
        else:
            tk.Label(self, text=BRAND["name"], bg=hdr["bg"], fg=hdr["accent"],
                     font=(FONT_UI_FAMILY, 13, "bold")).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(8, 10), pady=3)
            if subtitle:
                tk.Label(self, text=subtitle, bg=hdr["bg"], fg=hdr["text"],
                         font=(FONT_UI_FAMILY, 9, "bold")).grid(row=0, column=1, sticky="sw",
                                                            padx=(0, 8), pady=(4, 0))
                self._ver = tk.Label(self, text=f"v{BRAND['version']}  |  {BRAND['copyright']}",
                                     bg=hdr["bg"], fg=hdr["text_muted"],
                                     font=(FONT_UI_FAMILY, 7))
                self._ver.grid(row=1, column=1, sticky="nw", padx=(0, 8))
            else:
                self._ver = None

        rowspan = 1 if compact else 2

        # Live PC UTC clock
        self.show_clock = show_clock
        if self.show_clock:
            self.utc_frame = tk.Frame(self, bg=hdr["surface_alt"], padx=6, pady=1 if compact else 3,
                                      highlightthickness=1, highlightbackground=hdr["border"])
            self.utc_lbl = tk.Label(self.utc_frame, text="", bg=hdr["surface_alt"],
                                    fg=hdr["accent"], font=(FONT_MONO_FAMILY, 8 if compact else 9, "bold"))
            self.utc_lbl.pack(side=tk.LEFT)
            self.utc_frame.grid(row=0, column=2, rowspan=rowspan, sticky="e", padx=(8, 8), pady=2 if compact else 0)
            self._update_clock()
        else:
            self.utc_lbl = None

        # Optional Light / Dark theme toggle button
        if on_toggle_theme is not None:
            self.btn_theme = tk.Button(
                self,
                text="☀ Light" if get_theme_mode() == "dark" else "🌙 Dark",
                command=on_toggle_theme,
                bg=hdr["surface_alt"],
                fg=hdr["text"],
                activebackground=hdr["accent_dark"],
                activeforeground="#ffffff",
                font=(FONT_UI_FAMILY, 8, "bold"),
                relief=tk.FLAT,
                padx=7,
                pady=1 if compact else 2,
                cursor="hand2",
                bd=0,
            )
            self.btn_theme.grid(row=0, column=3, rowspan=rowspan, sticky="e", padx=(0, 8))
        else:
            self.btn_theme = None

        self.status_lbl = tk.Label(self, text="[DISCONNECTED]", bg=hdr["bg"],
                                   fg=hdr["text_muted"], font=(FONT_MONO_FAMILY, 8, "bold"))
        self.status_lbl.grid(row=0, column=4, rowspan=rowspan, sticky="e", padx=(0, 10))

        # thin accent rule under the bar
        rule_row = 1 if compact else 2
        tk.Frame(self, bg=hdr["accent"], height=1 if compact else 2).grid(
            row=rule_row, column=0, columnspan=5, sticky="ew")

    def refresh_theme_button(self):
        if self.btn_theme is not None:
            self.btn_theme.config(text="☀ Light" if get_theme_mode() == "dark" else "🌙 Dark")

    def _update_clock(self):
        """Update the live PC UTC clock every 500 ms."""
        if not getattr(self, "show_clock", False) or not hasattr(self, "utc_lbl") or self.utc_lbl is None:
            return
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            self.utc_lbl.config(text=f"UTC: {now_str}")
            self.after(500, self._update_clock)
        except tk.TclError:
            pass

    def set_link(self, connected: bool, name: str = ""):
        """Show the link state on the right of the bar: [NAME] in green when connected, else [DISCONNECTED]."""
        colour = BRAND["ok"] if connected else BRAND["text_muted"]
        text = f"[{name.upper()}]" if connected and name else ("[CONNECTED]" if connected
                                                               else "[DISCONNECTED]")
        try:
            self.status_lbl.config(text=text, fg=colour)
        except tk.TclError:
            pass


def apply_window_icon(win):
    """Set the taskbar/title icon from the embedded 256x256 mark."""
    _, icon = brand_images()
    if icon is not None:
        try:
            win.iconphoto(True, icon)
        except tk.TclError:
            pass
