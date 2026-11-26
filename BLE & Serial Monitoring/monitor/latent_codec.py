"""@file latent_codec.py
@brief Optional D128 picture codec for the Delphis image link: picture -> 256-byte latent -> picture
@version 1.1
@date 2026-11-22

The camera board (succorfish-Camera N6_DelphisImageLink) sends a picture as a D128 latent: 128 tokens, each an index
0..4095 into the codebook, stored as uint16 little-endian = 256 bytes. d128_encoder.onnx (input "image" float
[1,3,256,256] in 0..1, output "latent" int32 [1,128]) and d128_decoder.onnx (the reverse) do the work on the PC.
They are large (about 104 and 232 MB) and need numpy and onnxruntime (and Pillow to read picture files), so the
codec is OPTIONAL: without them the monitor still sends and receives latents, saves them, and says what is missing.
The models run in a separate Python process (latent_codec_worker.py): importing onnxruntime into the monitor crashed
it (access violation next to tkinter / bleak). Model paths are chosen once and remembered (user_settings.py). Pure
helpers (check_latent, rgb_to_ppm) need nothing and are unit tested.
"""
from . import user_settings
import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import tempfile


D128_LATENT_BYTES = 256
TOKEN_COUNT = 128
CODEBOOK_SIZE = 4096
IMAGE_SIZE = 256
CHANNELS = 3
WORKER_TIMEOUT_S = 180
ENCODER_FILE = "d128_encoder.onnx"
DECODER_FILE = "d128_decoder.onnx"
SETTING_KEYS = {ENCODER_FILE: "d128_encoder_path", DECODER_FILE: "d128_decoder_path"}
SETTING_PYTHON = "picture_codec_python"
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latent_codec_worker.py")
TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(TOOL_DIR)))
SEARCH_FOLDERS = (
  os.path.join(TOOL_DIR, "models"),
  os.path.join(PROJECTS_DIR, "succorfish-Camera", "N6_DelphisImageLink", "tests", "gigo_python_script", "models"),
  os.path.join(PROJECTS_DIR, "Dummy", "encoded_image_transmit_via_delphis", "Autoencode-D64-D128-v0.2.0", "models",
               "onnx"),
)


def check_latent(payload: bytes) -> "str | None":
  """Why payload is not a D128 latent (None when it is one)."""
  if len(payload) != D128_LATENT_BYTES:
    return f"{len(payload)} B: a D128 latent is exactly {D128_LATENT_BYTES} B"
  if max(struct.unpack(f"<{TOKEN_COUNT}H", payload)) >= CODEBOOK_SIZE:
    return "a token is >= 4096 (byte order or corruption)"
  return None


def rgb_to_ppm(width: int, height: int, rgb: bytes) -> bytes:
  """Binary PPM of RGB888 pixels (Tk's PhotoImage reads it without Pillow)."""
  if len(rgb) != width * height * CHANNELS:
    raise ValueError("pixel data does not match the size")
  return b"P6\n%d %d\n255\n" % (width, height) + bytes(rgb)


def worker_python() -> "str | None":
  """Python that runs the codec: a remembered one, this interpreter when running from source, else the py
  launcher (the packaged exe has no Python of its own).
  """
  remembered = user_settings.get(SETTING_PYTHON)
  if remembered and os.path.isfile(remembered):
    return remembered
  if not getattr(sys, "frozen", False):
    return sys.executable
  return shutil.which("py") or shutil.which("python3") or shutil.which("python")


def missing_packages(need_pillow: bool = False) -> list:
  """Packages the codec needs that this Python does not have (checked without loading them)."""
  if getattr(sys, "frozen", False):
    return []
  names = ("numpy", "onnxruntime") + (("PIL",) if need_pillow else ())
  return ["Pillow" if name == "PIL" else name for name in names if importlib.util.find_spec(name) is None]


def unavailable_reason(need_pillow: bool = False) -> "str | None":
  """Why pictures cannot be coded here (None when they can, model files aside)."""
  if not os.path.isfile(WORKER):
    return "picture coding runs from the source version of the monitor (python debug_monitor.py), not the exe"
  missing = missing_packages(need_pillow)
  if missing:
    return f"picture coding needs: pip install {' '.join(missing)}"
  return None


def model_path(file_name: str) -> "str | None":
  """Remembered path of a model, else the first copy in the known folders, else None."""
  remembered = user_settings.get(SETTING_KEYS[file_name])
  if remembered and os.path.isfile(remembered):
    return remembered
  for folder in SEARCH_FOLDERS:
    candidate = os.path.join(folder, file_name)
    if os.path.isfile(candidate):
      return candidate
  return None


def remember_model(file_name: str, path: str) -> bool:
  """Store the chosen model path for the next sessions."""
  return user_settings.put(SETTING_KEYS[file_name], path)


class LatentCodec:
  """Encodes / decodes through latent_codec_worker.py in a child process (a few seconds per picture, model load
  included).
  """

  def _run(self, action: str, file_name: str, input_bytes: "bytes | None", input_path: "str | None") -> bytes:
    """Run the worker; returns its output file's bytes, RuntimeError with the reason otherwise."""
    reason = unavailable_reason(need_pillow=action == "encode")
    if reason:
      raise RuntimeError(reason)
    model = model_path(file_name)
    if model is None:
      raise RuntimeError(f"{file_name} not found: choose it with the model button")
    python = worker_python()
    if python is None:
      raise RuntimeError("no Python found to run the picture codec")
    with tempfile.TemporaryDirectory() as folder:
      source = input_path or os.path.join(folder, "input.bin")
      if input_bytes is not None:
        with open(source, "wb") as handle:
          handle.write(input_bytes)
      target = os.path.join(folder, "output.bin")
      flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
      try:
        result = subprocess.run([python, "-I", WORKER, action, model, source, target], capture_output=True,
                                text=True, timeout=WORKER_TIMEOUT_S, creationflags=flags)
      except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"picture codec did not run: {error}") from error
      if result.returncode != 0 or not os.path.exists(target):
        reason = (result.stderr or "").strip().splitlines()
        raise RuntimeError(reason[-1] if reason else f"picture codec failed (exit {result.returncode})")
      with open(target, "rb") as handle:
        return handle.read()

  def decode(self, payload: bytes) -> bytes:
    """256-byte latent -> RGB888 bytes of the 256 x 256 picture."""
    problem = check_latent(payload)
    if problem:
      raise RuntimeError(problem)
    rgb = self._run("decode", DECODER_FILE, payload, None)
    if len(rgb) != IMAGE_SIZE * IMAGE_SIZE * CHANNELS:
      raise RuntimeError("the decoder returned a picture of the wrong size")
    return rgb

  def encode_file(self, path: str) -> bytes:
    """Picture file -> 256-byte latent."""
    latent = self._run("encode", ENCODER_FILE, None, path)
    problem = check_latent(latent)
    if problem:
      raise RuntimeError(f"encoder output: {problem}")
    return latent

  def forget(self) -> None:
    """Nothing is cached in this process (kept for the model buttons)."""


CODEC = LatentCodec()
