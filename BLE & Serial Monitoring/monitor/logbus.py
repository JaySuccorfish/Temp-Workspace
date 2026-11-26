"""@file logbus.py
@brief Thread-safe message bus from the BLE/serial worker threads to the Tk GUI, plus log-line helpers
@version 1.0
@date 2026-11-07

Tk may only be touched from its own thread, so workers put dicts on gui_queue and the main window drains it.
The context variables tag each log line with the BLE session it belongs to, so it lands in the right log tab.
"""
from .constants import DIAG_ALIVE_RE
from .constants import DIAG_CRUMB_NAMES
from .constants import DIAG_FAULT_NAMES
from .constants import DIAG_FAULT_RE
from .constants import DIAG_RESET_RE
from .constants import HCI_ERROR_CODES
from datetime import datetime, timezone
import contextvars
import queue


# --------------------------------------------------------------------------
# Inter-thread Queues & Log Handlers
# --------------------------------------------------------------------------
gui_queue = queue.Queue()
_log_session_id = contextvars.ContextVar("log_session_id", default=None)
_selected_ble_session_id = contextvars.ContextVar("selected_ble_session_id", default=None)


def post_gui(msg_type: str, **kwargs):
    """Queue a message of type msg_type (with its keyword fields) for the GUI thread."""
    kwargs["type"] = msg_type
    gui_queue.put(kwargs)


def log_system(msg: str, color: str = "#38bdf8", session_id: str | None = None):
    """Queue a timestamped [SYSTEM] log line (monitor's own message) for the session's log tab."""
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    if session_id is None:
        session_id = _log_session_id.get()
    post_gui("log_line", msg=f"[{timestamp}] [SYSTEM] {msg}", color=color,
             session_id=session_id)


def log_device_line(line: str, session_id: str | None = None):
    """Queue a timestamped log line. DIAG reset, fault and alive lines from the firmware
    are colour-coded and decoded (reset reason, fault name, last waypoint) at the end of the line.
    """
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {line}".rstrip()
    tag_color = "#4ade80"
    
    if "GPS NOT FIXED" in line:
        tag_color = "#f59e0b"
    elif "GPS FIXED" in line or "Reporting Successful" in line:
        tag_color = "#4ade80"
    elif "Reporting Error" in line:
        tag_color = "#ef4444"
    elif "DIAG rst" in line:
        tag_color = "#f59e0b"
        m = DIAG_RESET_RE.search(line)
        if m:
            disc = int(m.group("disc"), 16)
            reason = HCI_ERROR_CODES.get(disc, f"Unknown (0x{disc:02X})")
            formatted += f" --> [RESET REASON: {reason}, Heap Min: {m.group('heap')} bytes]"
            
    elif "DIAG fault=" in line:
        m = DIAG_FAULT_RE.search(line)
        if m:
            f_code = int(m.group("fault"))
            a_val = int(m.group("a"), 16)
            b_val = int(m.group("b"), 16)
            f_name = DIAG_FAULT_NAMES.get(f_code, "UNKNOWN")
            if f_code == 6:
                tag_color = "#f59e0b"
                formatted += f" --> [INTENTIONAL RESET: {f_name} (line {a_val}, cause=0x{b_val:02X}) @ {timestamp} UTC]"
            else:
                tag_color = "#ef4444"
                formatted += f" --> [LAST RESET: {f_name} (Code {f_code}, a=0x{a_val:X}, b=0x{b_val:X}) @ {timestamp} UTC]"
        else:
            tag_color = "#ef4444"
            
    elif "DIAG prev alive=" in line:
        tag_color = "#38bdf8"
        m = DIAG_ALIVE_RE.search(line)
        if m:
            crumb = int(m.group("crumb"), 16)
            crumb_name = DIAG_CRUMB_NAMES.get(crumb, f"0x{crumb:X}")
            formatted += f" --> [LAST WAYPOINT: {crumb_name} at {m.group('alive')}ms]"

    if session_id is None:
        session_id = _log_session_id.get()
    post_gui("log_line", msg=formatted, color=tag_color, raw_line=line,
             session_id=session_id)
