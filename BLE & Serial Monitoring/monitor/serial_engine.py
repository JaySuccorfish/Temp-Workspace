"""@file serial_engine.py
@brief Serial-port side of the monitor (Tera Term style): open/close a port, background reader, send bytes
@version 1.0
@date 2026-11-07

BLE is for configuration; the serial terminal is for bring-up, boot logs and AT-level work on the modem UART.
Received text goes through the GUI message bus into the same log pane as BLE, so both share one timeline.
"""
from .logbus import post_gui
from .ota_image import _strip_ansi
import queue
import sys
import threading
import time


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Serial Engine -- the Tera Term side of the tool
#
# BLE is for configuration; a serial terminal is for bring-up, boot logs and AT-level work on the
# modem UART. Both feed the same log pane so a technician can compare them in one timeline.
#
# pyserial is optional: if it is missing, the Serial controls disable themselves and the rest of
# the application still works.
# --------------------------------------------------------------------------
try:
    import serial
    from serial.tools import list_ports as _serial_ports
    SERIAL_AVAILABLE = True
except ImportError:                                    # pragma: no cover
    serial = None
    _serial_ports = None
    SERIAL_AVAILABLE = False


# EOL presets, matching what Tera Term's transmit options do.
EOL_MAP = {
    "CRLF (\\r\\n)": b"\r\n",
    "CR (\\r)":      b"\r",
    "LF (\\n)":      b"\n",
    "None":          b"",
}

BAUD_RATES = ["9600", "19200", "38400", "57600", "115200", "230400",
              "460800", "921600", "1000000", "2000000"]
PARITY_MAP = {"None": "N", "Even": "E", "Odd": "O", "Mark": "M", "Space": "S"}
STOPBITS_MAP = {"1": 1, "1.5": 1.5, "2": 2}
BYTESIZE_MAP = {"5": 5, "6": 6, "7": 7, "8": 8}
FLOW_MAP = {"None": "none", "RTS/CTS": "rtscts", "XON/XOFF": "xonxoff", "DSR/DTR": "dsrdtr"}


class SerialEngine:
    """Non-blocking serial port with a background reader thread."""

    def __init__(self):
        """Start closed: no port, no reader thread, display options off."""
        self.ser = None
        self._thread = None
        self._tx_thread = None
        self._tx_queue: "queue.Queue[tuple[bytes, object] | None]" = queue.Queue()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.timestamp_rx = False
        self.hex_rx = False
        self.local_echo = False
        self.byte_delay_ms = 10   # inter-byte pacing for STM32 USB CDC-ACM / slow UART CLIs
        self.last_config = {}

    # ---- discovery ---------------------------------------------------------------------
    @staticmethod
    def available_ports():
        if not SERIAL_AVAILABLE:
            return []
        out = []
        for p in _serial_ports.comports():
            desc = (p.description or "").strip()
            out.append(f"{p.device}" if not desc or desc == "n/a" else f"{p.device}  -  {desc}")
        if sys.platform == "darwin":
            # Put real USB-UART adapters ahead of macOS built-in Bluetooth/debug virtual ports.
            def _mac_sort_key(entry: str):
                low = entry.lower()
                is_virtual = ("bluetooth-incoming-port" in low) or ("debug-console" in low)
                is_usb = any(tok in low for tok in ("usbserial", "usbmodem", "slab_usbtouart", "wchusbserial"))
                return (1 if is_virtual else 0, 0 if is_usb else 1, low)
            return sorted(out, key=_mac_sort_key)
        return sorted(out)

    # ---- lifecycle ---------------------------------------------------------------------
    @property
    def is_open(self):
        return self.ser is not None and getattr(self.ser, "is_open", False)

    def open(self, port, baud, bytesize="8", parity="N", stopbits=1, flow="none"):
        """Open port (the combo text may carry a description after the name) with the given framing and
        flow control, then start the reader thread. Raises when pyserial is missing or the port fails.
        """
        if not SERIAL_AVAILABLE:
            raise RuntimeError("pyserial is not installed")
        self.close()
        # port string may carry the " - description" suffix from the combo box
        dev = port.split("  -  ")[0].strip()

        kwargs = dict(port=dev, baudrate=int(baud), bytesize=BYTESIZE_MAP[str(bytesize)],
                      parity=PARITY_MAP.get(parity, "N"),
                      stopbits=STOPBITS_MAP.get(str(stopbits), 1),
                      timeout=0.05, write_timeout=2.0)
        flow = FLOW_MAP.get(flow, "none")
        if flow in ("rtscts", "dsrdtr"):
            kwargs[flow] = True
        elif flow == "xonxoff":
            kwargs["xonxoff"] = True

        self.ser = serial.Serial(**kwargs)
        if flow == "none":
            # STM32 / nRF USB CDC-ACM virtual COM ports often require DTR/RTS asserted for I/O.
            try:
                self.ser.dtr = True
                self.ser.rts = True
            except Exception:
                pass
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        self.last_config = dict(kwargs)
        self._stop.clear()
        # Drain any stale queued TX items from a previous session
        while not self._tx_queue.empty():
            try:
                self._tx_queue.get_nowait()
            except queue.Empty:
                break
        self._thread = threading.Thread(target=self._reader, name="serial-rx", daemon=True)
        self._thread.start()
        self._tx_thread = threading.Thread(target=self._writer, name="serial-tx", daemon=True)
        self._tx_thread.start()
        return True

    def close(self):
        """Stop the reader/writer threads (wait up to 1 s) and close the port; safe to call when already closed."""
        self._stop.set()
        try:
            self._tx_queue.put_nowait(None)
        except Exception:
            pass
        for attr in ("_thread", "_tx_thread"):
            t = getattr(self, attr, None)
            if t and t.is_alive():
                t.join(timeout=1.0)
            setattr(self, attr, None)
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    # ---- io ----------------------------------------------------------------------------
    def write(self, data: bytes, byte_delay_ms: "float | None" = None) -> int:
        """Write bytes synchronously. Multi-byte writes use inter-byte pacing so USB CDC-ACM
        firmwares that consume 1 byte per packet never drop characters.
        """
        if not self.is_open:
            raise RuntimeError("serial port is not open")
        if not data:
            return 0
        delay_ms = self.byte_delay_ms if byte_delay_ms is None else byte_delay_ms
        delay_s = max(0.0, float(delay_ms) / 1000.0)
        with self._lock:
            if not self.is_open:
                raise RuntimeError("serial port is not open")
            if len(data) <= 1 or delay_s <= 0.0:
                written = self.ser.write(data)
                self.ser.flush()
                return written
            total = 0
            for idx in range(len(data)):
                if self._stop.is_set() or not self.is_open:
                    break
                total += self.ser.write(data[idx:idx + 1])
                self.ser.flush()
                if idx + 1 < len(data):
                    time.sleep(delay_s)
            return total

    def write_async(self, data: bytes, on_error=None) -> None:
        """Queue bytes for ordered background transmission without blocking the GUI thread."""
        if not self.is_open:
            raise RuntimeError("serial port is not open")
        self._tx_queue.put((bytes(data), on_error))

    def _writer(self):
        """Background TX worker preserving write order and byte pacing."""
        while not self._stop.is_set():
            try:
                item = self._tx_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            data, on_error = item
            try:
                self.write(data)
            except Exception as exc:
                if on_error is not None:
                    try:
                        on_error(exc)
                    except Exception:
                        pass
                elif not self._stop.is_set():
                    post_gui("serial_rx", text=f"\n[serial write error] {exc}\n", raw=None)

    def _reader(self):
        """Byte-stream reader. Emits whole lines where possible, raw chunks otherwise."""
        buf = bytearray()
        while not self._stop.is_set():
            try:
                n = self.ser.in_waiting if self.ser else 0
                chunk = self.ser.read(n or 1) if self.ser else b""
            except Exception as exc:
                if not self._stop.is_set():
                    post_gui("serial_rx", text=f"[serial error] {exc}", raw=None)
                break
            if not chunk:
                # flush a partial line that has gone quiet
                if buf:
                    self._emit(bytes(buf))
                    buf.clear()
                continue

            buf.extend(chunk)
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                self._emit(line + b"\n")
                buf = bytearray(rest)
            if len(buf) > 4096:                 # no newline in sight -- do not grow forever
                self._emit(bytes(buf))
                buf.clear()

    def _emit(self, data: bytes):
        """Send received bytes to the GUI, as hex or as text without ANSI colour codes."""
        if self.hex_rx:
            text = data.hex(" ").upper() + "\n"
        else:
            text = data.decode("utf-8", "replace")
            text = _strip_ansi(text)
            text = text.replace("\r\n", "\n").replace("\r", "")
        post_gui("serial_rx", text=text, raw=data, timestamp=self.timestamp_rx)
