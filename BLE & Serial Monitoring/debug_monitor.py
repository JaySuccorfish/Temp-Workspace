#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""@file debug_monitor.py
@brief Entry point of the Valtrack BLE debug & configuration monitor (with BLE firmware update, DOTA)
@version 1.0
@date 2026-11-07

Run:  python debug_monitor.py   (or the .exe built by build_exe.ps1)
Sets Windows DPI awareness before the first Tk window, makes the sibling modules importable, starts the GUI.
The implementation lives in the monitor package:
    monitor/constants.py      brand palette, opcodes, UUIDs, static tables
    monitor/protocol.py       auth challenge answer + AES framing
    monitor/ota_image.py      DOTA firmware image validation
    monitor/logbus.py         GUI message bus + per-session log helpers
    monitor/gigo_device.py    command channel for ONE connected device
    monitor/ble_engine.py     sessions, connect loop, asyncio worker thread
    monitor/serial_engine.py  Tera Term style serial port
    monitor/theme.py          brand theme, header, icon
    monitor/config_panel.py   config window (0xC1/C3/C4/C5/E2 + DOTA)
    monitor/sc2_panel.py      SC2 logger window
    monitor/main_window.py    main window
"""

import os
import sys

# Windows DPI awareness must be set before the first Tk window is created.
if sys.platform.startswith("win"):
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Make ``brand_assets`` / ``sc2_protocol`` / ``monitor`` importable however the script is launched.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from monitor.main_window import main  # noqa: E402

if __name__ == "__main__":
    main()
