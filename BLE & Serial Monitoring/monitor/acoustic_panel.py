"""@file acoustic_panel.py
@brief Delphis tabs of the GiGO window: acoustic messages (NM3 modem) and picture transfer (send / receive / codec)
@version 1.0
@date 2026-11-22

Both tabs work on the session's AcousticLink (acoustic_link.py), which keeps listening while the window is closed, so
messages and pictures that arrived earlier are shown when a tab opens. Commands run on the BLE thread; link events
arrive on it too and are handed to Tk with after(). Picture coding is optional (latent_codec.py): without numpy /
onnxruntime / Pillow or the model files the tab still sends and receives latents and says what is missing.
"""
from .acoustic_link import BROADCAST_DATA_MAX
from .acoustic_link import MODE_ACK
from .acoustic_link import MODE_BROADCAST
from .acoustic_link import MODE_NAMES
from .acoustic_link import UNICAST_DATA_MAX
from .acoustic_link import AcousticError
from .acoustic_link import describe_event
from .acoustic_link import image_commands
from .acoustic_link import link_for
from .acoustic_link import text_parts
from .ble_engine import run_async
from .constants import BRAND, FONT_UI_FAMILY
from .latent_codec import CODEC
from .latent_codec import DECODER_FILE
from .latent_codec import ENCODER_FILE
from .latent_codec import IMAGE_SIZE
from .latent_codec import check_latent
from .latent_codec import unavailable_reason
from .latent_codec import model_path
from .latent_codec import remember_model
from .latent_codec import rgb_to_ppm
from datetime import datetime
from datetime import timezone
from tkinter import filedialog
from tkinter import messagebox
from tkinter import scrolledtext
from tkinter import ttk
import os
import subprocess
import sys
import threading
import time
import tkinter as tk


DEFAULT_TARGET = "101"
SECONDS_PER_FRAME_ESTIMATE = 1.5
PAYLOAD_FILE_MAX = 8000
SMALL_FONT = (FONT_UI_FAMILY, 8)
MODE_CHOICES = (MODE_ACK, "U", MODE_BROADCAST)


def utc_time(epoch: float) -> str:
  """HH:MM:SS UTC of an epoch."""
  return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%H:%M:%S")


def parse_address(text: str) -> int:
  """Modem address typed by the operator (AcousticError when not 0..255)."""
  try:
    value = int(text.strip())
  except ValueError as error:
    raise AcousticError("modem address must be a number 0..255") from error
  if not 0 <= value <= 255:
    raise AcousticError("modem address must be 0..255")
  return value


class _LinkTab(tk.Frame):
  """Shared plumbing: the session's link, event subscription, BLE-thread runner, buttons."""

  def __init__(self, parent, session_id: str):
    super().__init__(parent, bg=BRAND["surface"], padx=10, pady=8)
    self.session_id = session_id
    self.link = link_for(session_id)
    self.link.subscribe(self._on_link_event)
    self.bind("<Destroy>", self._on_destroy)

  def _on_destroy(self, event) -> None:
    if event.widget is self:
      self.link.unsubscribe(self._on_link_event)

  def _on_link_event(self, kind: str, data) -> None:
    """BLE thread: hand the event to Tk."""
    try:
      self.after(0, lambda: self.handle_event(kind, data) if self.winfo_exists() else None)
    except (tk.TclError, RuntimeError):
      pass

  def handle_event(self, kind: str, data) -> None:
    """Tk thread (subclasses)."""

  def run(self, coroutine) -> None:
    """Run a coroutine on the BLE thread."""
    run_async(coroutine, session_id=self.session_id)

  def post(self, function) -> None:
    """Run function on the Tk thread from any thread."""
    try:
      self.after(0, lambda: function() if self.winfo_exists() else None)
    except (tk.TclError, RuntimeError):
      pass

  def button(self, parent, text: str, command, colour: "str | None" = None) -> tk.Button:
    """Brand button packed left."""
    widget = tk.Button(parent, text=text, command=command, bg=colour or BRAND["accent_dark"], fg="white",
                       relief=tk.FLAT, padx=10, pady=2, font=("Segoe UI", 8, "bold"), cursor="hand2")
    widget.pack(side=tk.LEFT, padx=(0, 5))
    return widget

  def entry(self, parent, width: int, text: str = "") -> tk.Entry:
    """Brand entry packed left."""
    widget = tk.Entry(parent, width=width, bg=BRAND["surface_alt"], fg=BRAND["text"], insertbackground=BRAND["text"],
                      font=BRAND["font_mono"], relief=tk.FLAT, highlightthickness=1,
                      highlightbackground=BRAND["border"], highlightcolor=BRAND["accent"])
    widget.insert(0, text)
    widget.pack(side=tk.LEFT, padx=(0, 6))
    return widget

  def label(self, parent, text: str, colour: "str | None" = None, font=SMALL_FONT) -> tk.Label:
    """Label packed left."""
    widget = tk.Label(parent, text=text, bg=BRAND["surface"], fg=colour or BRAND["text_dim"], font=font)
    widget.pack(side=tk.LEFT, padx=(0, 6))
    return widget

  def row(self) -> tk.Frame:
    """A full-width row."""
    frame = tk.Frame(self, bg=BRAND["surface"])
    frame.pack(fill=tk.X, pady=2)
    return frame


class AcousticMessagesTab(_LinkTab):
  """Modem status, ping / range, remote battery, text messages (unicast, with acknowledge, broadcast), console."""

  def __init__(self, parent, session_id: str):
    super().__init__(parent, session_id)
    bar = self.row()
    self.button(bar, "Modem status ($?)", self._status)
    self.button(bar, "Wake ($W)", lambda: self._simple(self.link.wake(), "Wake"))
    self.button(bar, "Noise ($N)", lambda: self._simple(self.link.noise(), "Noise"))
    self.var_lqi = tk.IntVar(value=0)
    tk.Checkbutton(bar, text="Link quality on messages ($XQ)", variable=self.var_lqi, command=self._lqi,
                   bg=BRAND["surface"], fg=BRAND["text"], selectcolor=BRAND["surface_alt"],
                   activebackground=BRAND["surface"], font=SMALL_FONT).pack(side=tk.LEFT, padx=6)
    self.lbl_modem = self.label(bar, "modem: not read", BRAND["text"], BRAND["font_mono"])

    bar = self.row()
    self.label(bar, "Own address")
    self.ent_own = self.entry(bar, 5)
    self.button(bar, "Set ($A)", self._set_address)
    self.label(bar, "   Other modem")
    self.ent_target = self.entry(bar, 5, DEFAULT_TARGET)
    self.button(bar, "Ping / range ($P)", self._ping, BRAND["accent"])
    self.button(bar, "Its battery ($V)", self._battery)
    self.lbl_result = self.label(bar, "", BRAND["ok"], BRAND["font_ui_b"])

    self.txt_log = scrolledtext.ScrolledText(self, height=12, bg=BRAND["bg"], fg=BRAND["text"],
                                             font=BRAND["font_mono"], relief=tk.FLAT, wrap=tk.WORD)
    self.txt_log.pack(fill=tk.BOTH, expand=True, pady=4)
    self.txt_log.tag_config("in", foreground="#7dd3fc")
    self.txt_log.tag_config("out", foreground="#86efac")
    self.txt_log.tag_config("sys", foreground=BRAND["text_muted"])
    self.txt_log.tag_config("err", foreground=BRAND["err"])

    bar = self.row()
    self.label(bar, "Message")
    self.ent_message = self.entry(bar, 46)
    self.ent_message.bind("<Return>", lambda _event: self._send_message())
    self.ent_message.bind("<KeyRelease>", lambda _event: self._count())
    self.var_mode = tk.StringVar(value=MODE_NAMES[MODE_ACK])
    mode = ttk.Combobox(bar, textvariable=self.var_mode, values=[MODE_NAMES[key] for key in MODE_CHOICES],
                        state="readonly", width=28)
    mode.pack(side=tk.LEFT, padx=(0, 6))
    mode.bind("<<ComboboxSelected>>", lambda _event: self._count())
    self.button(bar, "Send", self._send_message, BRAND["ok"])
    self.lbl_count = self.label(bar, "")

    bar = self.row()
    self.label(bar, "Console")
    self.ent_raw = self.entry(bar, 30)
    self.ent_raw.bind("<Return>", lambda _event: self._send_raw())
    self.button(bar, "Send line", self._send_raw)
    self.button(bar, "Help ($H)", lambda: self._send_raw("$H"))
    self.button(bar, "Save log", self._save_log)
    self.var_raw = tk.IntVar(value=0)
    tk.Checkbutton(bar, text="show every modem line", variable=self.var_raw, bg=BRAND["surface"], fg=BRAND["text"],
                   selectcolor=BRAND["surface_alt"], activebackground=BRAND["surface"], font=SMALL_FONT).pack(
      side=tk.LEFT, padx=6)
    tk.Label(self, text=f"Delphis passes commands of up to 48 bytes: a message part carries {UNICAST_DATA_MAX} "
                        f"characters ({BROADCAST_DATA_MAX} broadcast); longer text is sent in parts. Printable ASCII "
                        "without # and $ (the receiving Delphis cuts lines there). $M waits for the other modem's "
                        "acknowledgement and gives the range; received unicast messages carry no sender address.",
             bg=BRAND["surface"], fg=BRAND["text_muted"], font=SMALL_FONT, wraplength=900, justify=tk.LEFT).pack(
      fill=tk.X)
    for entry in list(self.link.log):
      self._show_message(entry)
    if self.link.modem:
      self._show_state(self.link.modem)

  # ---- events --------------------------------------------------------------------------------------------------------
  def handle_event(self, kind: str, data) -> None:
    if kind == "message":
      self._show_message(data)
    elif kind == "state":
      self._show_state(data)
    elif kind == "line" and self.var_raw.get():
      self._append(f"   {describe_event(data)}", "sys")
    elif kind == "progress":
      self._append(f"   {data}", "sys")

  def _append(self, text: str, tag: str) -> None:
    self.txt_log.insert(tk.END, text + "\n", tag)
    self.txt_log.see(tk.END)

  def _show_message(self, entry: dict) -> None:
    stamp = utc_time(entry["time"])
    if entry["direction"] == "in":
      source = f"from {entry['peer']:03d} (broadcast)" if entry["peer"] is not None else "received"
      quality = f"  [quality {entry['quality']}]" if entry.get("quality") is not None else ""
      self._append(f"{stamp} << {source}: {entry['text']}{quality}", "in")
    else:
      target = "broadcast" if entry["peer"] is None else f"to {entry['peer']:03d}"
      failed = any(word in entry["status"] for word in ("no ", "refused", "FAILED", "stopped"))
      self._append(f"{stamp} >> {target}: {entry['text']}   -> {entry['status']}", "err" if failed else "out")

  def _show_state(self, state: dict) -> None:
    parts = []
    if "address" in state:
      parts.append(f"address {state['address']:03d}")
      if not self.ent_own.get().strip():
        self.ent_own.insert(0, f"{state['address']:03d}")
    if "volts" in state:
      parts.append(f"{state['volts']:.2f} V")
    if "release" in state:
      parts.append(f"firmware {state['release']}")
    if "link_quality" in state:
      self.var_lqi.set(1 if state["link_quality"] else 0)
    self.lbl_modem.config(text="modem: " + ", ".join(parts) if parts else "modem: not read")

  def _count(self) -> None:
    size = BROADCAST_DATA_MAX if self._mode() == MODE_BROADCAST else UNICAST_DATA_MAX
    length = len(self.ent_message.get())
    parts = -(-length // size) if length else 0
    self.lbl_count.config(text=f"{length} chars, {parts} part(s)" if length else "")

  def _mode(self) -> str:
    return next((key for key in MODE_CHOICES if MODE_NAMES[key] == self.var_mode.get()), MODE_ACK)

  # ---- actions -------------------------------------------------------------------------------------------------------
  def _report(self, event, what: str) -> None:
    """Show a command's outcome (any thread)."""
    text = describe_event(event) if event else f"{what}: no answer (modem switched off in the configuration?)"
    colour = BRAND["err"] if not event or event["kind"] in ("error", "refused", "timeout") else BRAND["ok"]
    self.post(lambda: (self.lbl_result.config(text=text, fg=colour), self._append(f"   {what}: {text}", "sys")))

  def _simple(self, coroutine, what: str) -> None:
    async def go():
      self._report(await coroutine, what)
    self.run(go())

  def _status(self) -> None:
    self._simple(self.link.query_status(), "Status")

  def _lqi(self) -> None:
    self._simple(self.link.set_link_quality(bool(self.var_lqi.get())), "Link quality")

  def _address(self, entry: tk.Entry) -> "int | None":
    try:
      return parse_address(entry.get())
    except AcousticError as error:
      messagebox.showerror("Modem address", str(error), parent=self)
      return None

  def _set_address(self) -> None:
    address = self._address(self.ent_own)
    if address is not None and messagebox.askyesno(
        "Modem address", f"Store address {address:03d} in the modem? Other units send to this address.", parent=self):
      self._simple(self.link.set_address(address), "Set address")

  def _ping(self) -> None:
    address = self._address(self.ent_target)
    if address is not None:
      self.lbl_result.config(text=f"pinging {address:03d}...", fg=BRAND["info"])
      self._simple(self.link.ping(address), f"Ping {address:03d}")

  def _battery(self) -> None:
    address = self._address(self.ent_target)
    if address is not None:
      self.lbl_result.config(text=f"asking {address:03d}...", fg=BRAND["info"])
      self._simple(self.link.remote_battery(address), f"Battery of {address:03d}")

  def _send_message(self) -> None:
    text = self.ent_message.get()
    mode = self._mode()
    address = 0
    if mode != MODE_BROADCAST:
      address = self._address(self.ent_target)
      if address is None:
        return
    try:
      text_parts(text, mode)
    except AcousticError as error:
      messagebox.showerror("Message", str(error), parent=self)
      return
    self.ent_message.delete(0, tk.END)
    self._count()

    async def go():
      try:
        await self.link.send_message(text, mode, address)
      except AcousticError as error:
        self.post(lambda text=f"   message not sent: {error}": self._append(text, "err"))
    self.run(go())

  def _send_raw(self, line: "str | None" = None) -> None:
    text = line if line is not None else self.ent_raw.get().strip()
    if not text:
      return
    try:
      data = text.encode("latin-1")
    except UnicodeEncodeError:
      messagebox.showerror("Console", "Only 8-bit characters can go to the modem.", parent=self)
      return
    if len(data) > 48:
      messagebox.showerror("Console", "Delphis passes at most 48 bytes per command.", parent=self)
      return
    self.var_raw.set(1)
    self._append(f"   >> {text}", "sys")

    async def go():
      try:
        await self.link.send_raw(data)
      except (AcousticError, ValueError) as error:
        self.post(lambda text=f"   not sent: {error}": self._append(text, "err"))
    self.run(go())

  def _save_log(self) -> None:
    path = filedialog.asksaveasfilename(parent=self, defaultextension=".txt", filetypes=[("Text", "*.txt")],
                                        initialfile=f"delphis_messages_{time.strftime('%Y%m%d_%H%M%S')}.txt")
    if path:
      with open(path, "w", encoding="utf-8") as handle:
        handle.write(self.txt_log.get("1.0", tk.END))


class ImageTransferTab(_LinkTab):
  """Pictures received over the acoustic link (saved, decoded when the codec is available) and sending a picture or
  any small file with the camera's image protocol.
  """

  def __init__(self, parent, session_id: str):
    super().__init__(parent, session_id)
    self._photo = None
    self._payload = None
    self._cancel = False
    self._sending = False
    self._decoded = {}
    self._items = {}

    columns = tk.Frame(self, bg=BRAND["surface"])
    columns.pack(fill=tk.BOTH, expand=True)
    receive = tk.Frame(columns, bg=BRAND["surface"])
    receive.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
    send = tk.Frame(columns, bg=BRAND["surface"], width=330)
    send.pack(side=tk.LEFT, fill=tk.Y)

    tk.Label(receive, text="RECEIVED PICTURES", bg=BRAND["surface"], fg=BRAND["accent"],
             font=BRAND["font_ui_b"]).pack(anchor="w")
    self.lbl_rx = tk.Label(receive, text="waiting for a picture (OFFER / DATA / DONE)", bg=BRAND["surface"],
                           fg=BRAND["text_dim"], font=SMALL_FONT, anchor="w")
    self.lbl_rx.pack(fill=tk.X)
    body = tk.Frame(receive, bg=BRAND["surface"])
    body.pack(fill=tk.BOTH, expand=True)
    self.tree = ttk.Treeview(body, columns=("time", "bytes", "crc", "frames", "file"), show="headings", height=8)
    for column, title, width in (("time", "UTC", 70), ("bytes", "Bytes", 50), ("crc", "CRC-32", 80),
                                 ("frames", "Frags / rep.", 80), ("file", "Saved as", 200)):
      self.tree.heading(column, text=title)
      self.tree.column(column, width=width, anchor="w")
    self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    self.tree.bind("<<TreeviewSelect>>", lambda _event: self._show_selected())
    self.canvas = tk.Canvas(body, width=IMAGE_SIZE, height=IMAGE_SIZE, bg=BRAND["bg"], highlightthickness=1,
                            highlightbackground=BRAND["border"])
    self.canvas.pack(side=tk.LEFT, padx=(8, 0))
    bar = tk.Frame(receive, bg=BRAND["surface"])
    bar.pack(fill=tk.X, pady=4)
    self.button(bar, "Decode selected", self._decode_selected)
    self.button(bar, "Save picture (PNG)", self._save_png)
    self.button(bar, "Open folder", self._open_folder)

    tk.Label(send, text="SEND A PICTURE", bg=BRAND["surface"], fg=BRAND["accent"], font=BRAND["font_ui_b"]).pack(
      anchor="w")
    bar = tk.Frame(send, bg=BRAND["surface"])
    bar.pack(fill=tk.X, pady=2)
    self.label(bar, "To modem")
    self.ent_target = self.entry(bar, 5, DEFAULT_TARGET)
    bar = tk.Frame(send, bg=BRAND["surface"])
    bar.pack(fill=tk.X, pady=2)
    self.button(bar, "Picture... (encode)", self._choose_picture)
    self.button(bar, "Latent / file...", self._choose_file)
    self.lbl_payload = tk.Label(send, text="nothing chosen", bg=BRAND["surface"], fg=BRAND["text_dim"],
                                font=SMALL_FONT, wraplength=320, justify=tk.LEFT, anchor="w")
    self.lbl_payload.pack(fill=tk.X, pady=2)
    bar = tk.Frame(send, bg=BRAND["surface"])
    bar.pack(fill=tk.X, pady=2)
    self.btn_send = self.button(bar, "Send", self._send, BRAND["ok"])
    self.button(bar, "Cancel", self._cancel_send, BRAND["err"])
    self.progress = ttk.Progressbar(send, orient="horizontal", length=300, mode="determinate")
    self.progress.pack(fill=tk.X, pady=4)
    self.lbl_tx = tk.Label(send, text="", bg=BRAND["surface"], fg=BRAND["text"], font=SMALL_FONT, wraplength=320,
                           justify=tk.LEFT, anchor="w")
    self.lbl_tx.pack(fill=tk.X)
    tk.Label(send, text="Each frame goes as $M and is repeated (up to 7 times) until the other modem acknowledges it. "
                        "The camera's receiver expects pictures at modem 101.", bg=BRAND["surface"],
             fg=BRAND["text_muted"], font=SMALL_FONT, wraplength=320, justify=tk.LEFT).pack(fill=tk.X, pady=(6, 0))

    models = tk.Frame(self, bg=BRAND["surface"])
    models.pack(fill=tk.X, pady=(6, 0))
    self.lbl_models = tk.Label(models, text="", bg=BRAND["surface"], fg=BRAND["text_muted"], font=SMALL_FONT,
                               justify=tk.LEFT, anchor="w")
    self.lbl_models.pack(side=tk.LEFT, fill=tk.X, expand=True)
    self.button(models, "Decoder model...", lambda: self._choose_model(DECODER_FILE))
    self.button(models, "Encoder model...", lambda: self._choose_model(ENCODER_FILE))
    self._show_models()
    for record in self.link.images:
      self._add_record(record)

  # ---- models ----------------------------------------------------------------------------------------------------------
  def _show_models(self) -> None:
    reason = unavailable_reason(need_pillow=True)
    decoder = model_path(DECODER_FILE)
    encoder = model_path(ENCODER_FILE)
    lines = [f"Decoder: {decoder or 'not chosen'}", f"Encoder: {encoder or 'not chosen'}"]
    if reason:
      lines.append(f"{reason} (sending / receiving latents works without)")
    self.lbl_models.config(text="\n".join(lines))

  def _choose_model(self, file_name: str) -> None:
    path = filedialog.askopenfilename(parent=self, title=f"Choose {file_name}",
                                      filetypes=[("ONNX model", "*.onnx"), ("All files", "*.*")])
    if path:
      remember_model(file_name, path)
      CODEC.forget()
      self._show_models()

  # ---- receive ---------------------------------------------------------------------------------------------------------
  def handle_event(self, kind: str, data) -> None:
    if kind == "progress":
      self.lbl_rx.config(text=data)
    elif kind == "image":
      self._decode(self._add_record(data))

  def _add_record(self, record: dict) -> str:
    """List a received picture; returns its row id."""
    item = self.tree.insert("", tk.END, values=(
      utc_time(record["time"]), record["bytes"], f"{record['crc32']:08X}",
      f"{record['fragments']} / {record['duplicates']}",
      os.path.basename(record["path"]) if record.get("path") else "not saved"))
    self._decoded[item] = None
    self._items[item] = record
    self.tree.selection_set(item)
    self.tree.see(item)
    return item

  def _record(self, item) -> "dict | None":
    return self._items.get(item)

  def _show_selected(self) -> None:
    selection = self.tree.selection()
    if not selection:
      return
    rgb = self._decoded.get(selection[0])
    self.canvas.delete("all")
    if rgb is None:
      record = self._record(selection[0])
      problem = check_latent(record["payload"]) if record else "unknown"
      text = "not decoded yet" if not problem else f"not a picture latent:\n{problem}"
      self.canvas.create_text(IMAGE_SIZE / 2, IMAGE_SIZE / 2, text=text, fill=BRAND["text_dim"], font=SMALL_FONT,
                              width=IMAGE_SIZE - 20)
      return
    self._photo = tk.PhotoImage(data=rgb_to_ppm(IMAGE_SIZE, IMAGE_SIZE, rgb), format="PPM")
    self.canvas.create_image(0, 0, image=self._photo, anchor="nw")

  def _decode(self, item: str) -> None:
    """Decode a listed picture in a worker thread (the first call loads the model)."""
    record = self._items[item]
    if check_latent(record["payload"]):
      self._show_selected()
      return
    self.lbl_rx.config(text="decoding the picture...")

    def work():
      try:
        rgb = CODEC.decode(record["payload"])
        message = f"picture decoded (CRC-32 {record['crc32']:08X})"
      except Exception as error:
        rgb, message = None, f"not decoded: {error}"

      def done():
        self._decoded[item] = rgb
        self.lbl_rx.config(text=message)
        if rgb is not None and record.get("path"):
          self._write_png(rgb, os.path.splitext(record["path"])[0] + ".png")
        self._show_selected()
      self.post(done)
    threading.Thread(target=work, daemon=True).start()

  def _decode_selected(self) -> None:
    selection = self.tree.selection()
    if selection and selection[0] in self._items:
      self._decode(selection[0])

  def _write_png(self, rgb: bytes, path: str) -> None:
    try:
      tk.PhotoImage(data=rgb_to_ppm(IMAGE_SIZE, IMAGE_SIZE, rgb), format="PPM").write(path, format="png")
    except (tk.TclError, OSError):
      pass

  def _save_png(self) -> None:
    selection = self.tree.selection()
    rgb = self._decoded.get(selection[0]) if selection else None
    if rgb is None:
      messagebox.showinfo("Save picture", "Decode a picture first.", parent=self)
      return
    path = filedialog.asksaveasfilename(parent=self, defaultextension=".png", filetypes=[("PNG", "*.png")])
    if path:
      self._write_png(rgb, path)

  def _open_folder(self) -> None:
    os.makedirs(self.link.image_folder, exist_ok=True)
    try:
      if sys.platform == "darwin":
        subprocess.Popen(["open", self.link.image_folder])
      elif sys.platform.startswith("win"):
        os.startfile(self.link.image_folder)
      else:
        subprocess.Popen(["xdg-open", self.link.image_folder])
    except (AttributeError, OSError):
      messagebox.showinfo("Pictures", self.link.image_folder, parent=self)

  # ---- send ------------------------------------------------------------------------------------------------------------
  def _set_payload(self, payload: bytes, source: str) -> None:
    try:
      frames = len(image_commands(parse_address(self.ent_target.get() or DEFAULT_TARGET), payload))
    except AcousticError as error:
      messagebox.showerror("Picture", str(error), parent=self)
      return
    self._payload = payload
    kind = "D128 picture latent" if check_latent(payload) is None else "data"
    self.lbl_payload.config(text=f"{source}: {len(payload)} B {kind}, {frames} frames, about "
                                 f"{frames * SECONDS_PER_FRAME_ESTIMATE:.0f} s without repeats")

  def _choose_file(self) -> None:
    path = filedialog.askopenfilename(parent=self, filetypes=[("Latent", "*.latent *.bin"), ("All files", "*.*")])
    if not path:
      return
    with open(path, "rb") as handle:
      payload = handle.read(PAYLOAD_FILE_MAX + 1)
    if len(payload) > PAYLOAD_FILE_MAX:
      messagebox.showerror("Picture", f"At most {PAYLOAD_FILE_MAX} bytes (255 frames).", parent=self)
      return
    self._set_payload(payload, os.path.basename(path))

  def _choose_picture(self) -> None:
    path = filedialog.askopenfilename(parent=self, filetypes=[("Pictures", "*.png *.jpg *.jpeg *.bmp"),
                                                              ("All files", "*.*")])
    if not path:
      return
    self.lbl_payload.config(text="encoding (the first time loads the encoder)...")

    def work():
      try:
        payload = CODEC.encode_file(path)
        self.post(lambda: self._set_payload(payload, os.path.basename(path)))
      except Exception as error:
        self.post(lambda text=f"not encoded: {error}": self.lbl_payload.config(text=text))
    threading.Thread(target=work, daemon=True).start()

  def _send(self) -> None:
    if self._payload is None:
      messagebox.showinfo("Picture", "Choose a picture or a latent file first.", parent=self)
      return
    if self._sending:
      return
    try:
      address = parse_address(self.ent_target.get())
    except AcousticError as error:
      messagebox.showerror("Picture", str(error), parent=self)
      return
    payload = self._payload
    self._sending = True
    self._cancel = False
    self.btn_send.config(state=tk.DISABLED)
    self.progress.configure(value=0)
    self.lbl_tx.config(text="sending...", fg=BRAND["info"])

    def progress(done, total, text):
      self.post(lambda: (self.progress.configure(value=100.0 * done / total), self.lbl_tx.config(text=text)))

    async def go():
      try:
        summary = await self.link.send_payload(address, payload, progress, lambda: self._cancel)
        text = (f"{summary['result']}: {summary['bytes']} B in {summary['frames']} frames, {summary['retries']} "
                f"repeats, {summary['seconds']:.0f} s")
        colour = BRAND["ok"] if summary["result"] == "DELIVERED" else BRAND["err"]
      except Exception as error:
        text, colour = f"not sent: {error}", BRAND["err"]

      def done():
        self._sending = False
        self.btn_send.config(state=tk.NORMAL)
        self.lbl_tx.config(text=text, fg=colour)
      self.post(done)
    self.run(go())

  def _cancel_send(self) -> None:
    self._cancel = True
