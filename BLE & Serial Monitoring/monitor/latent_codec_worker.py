"""@file latent_codec_worker.py
@brief Runs the D128 picture encoder / decoder in its own Python process for latent_codec.py
@version 1.0
@date 2026-11-22

onnxruntime is loaded here, never in the monitor: importing it into the monitor process (next to tkinter, bleak and
the WinRT libraries) crashed with an access violation. A separate process keeps the monitor alive whatever the codec
does, and frees the model memory after each picture.
    python -I latent_codec_worker.py decode <decoder.onnx> <latent file> <rgb out file>
    python -I latent_codec_worker.py encode <encoder.onnx> <picture file> <latent out file>
Exit code 0 on success, 2 on bad arguments, 3 on any error (message on stderr).
"""
import sys

TOKEN_COUNT = 128
CODEBOOK_SIZE = 4096
IMAGE_SIZE = 256
PIXEL_MAX = 255.0
FLOAT_IMAGE_LIMIT = 1.5
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ERROR = 3


def session(model: str):
  """CPU inference session of a model file."""
  import onnxruntime
  return onnxruntime.InferenceSession(model, providers=["CPUExecutionProvider"])


def decode(model: str, latent_path: str, out_path: str) -> None:
  """256-byte latent -> RGB888 bytes of the 256 x 256 picture."""
  import numpy
  with open(latent_path, "rb") as handle:
    payload = handle.read()
  run = session(model)
  tokens = numpy.frombuffer(payload, dtype="<u2").astype(numpy.int32).reshape(1, TOKEN_COUNT)
  image = run.run(None, {run.get_inputs()[0].name: tokens})[0][0]
  scale = 1.0 if float(image.max()) > FLOAT_IMAGE_LIMIT else PIXEL_MAX
  pixels = numpy.clip(image * scale + 0.5, 0.0, PIXEL_MAX).astype(numpy.uint8)
  with open(out_path, "wb") as handle:
    handle.write(pixels.transpose(1, 2, 0).tobytes())


def encode(model: str, picture_path: str, out_path: str) -> None:
  """Picture -> 256-byte latent: centre square, 256 x 256, RGB / 255 (as the camera project's reference script)."""
  import numpy
  from PIL import Image
  with Image.open(picture_path) as picture:
    rgb = picture.convert("RGB")
    side = min(rgb.size)
    left = (rgb.size[0] - side) // 2
    top = (rgb.size[1] - side) // 2
    square = rgb.crop((left, top, left + side, top + side)).resize((IMAGE_SIZE, IMAGE_SIZE), Image.BILINEAR)
  data = numpy.asarray(square, dtype=numpy.float32) / PIXEL_MAX
  run = session(model)
  tokens = numpy.asarray(run.run(None, {run.get_inputs()[0].name: data.transpose(2, 0, 1)[numpy.newaxis, ...]})[0])
  tokens = tokens.reshape(-1)
  if tokens.size != TOKEN_COUNT or int(tokens.min()) < 0 or int(tokens.max()) >= CODEBOOK_SIZE:
    raise RuntimeError("the encoder did not return 128 tokens in 0..4095")
  with open(out_path, "wb") as handle:
    handle.write(tokens.astype("<u2").tobytes())


def main(arguments: list) -> int:
  """Command line entry (see the module header)."""
  if len(arguments) != 4 or arguments[0] not in ("decode", "encode"):
    sys.stderr.write("usage: latent_codec_worker.py decode|encode <model> <input> <output>\n")
    return EXIT_USAGE
  try:
    (decode if arguments[0] == "decode" else encode)(*arguments[1:])
  except Exception as error:
    sys.stderr.write(f"{type(error).__name__}: {error}\n")
    return EXIT_ERROR
  return EXIT_OK


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
