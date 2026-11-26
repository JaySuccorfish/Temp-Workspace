"""@file make_brand_assets.py
@brief Draws the Succorfish logo and icon and writes them as base64 text for brand_assets.py
@version 1.0
@date 2026-11-07

Run it only to regenerate the images:  python make_brand_assets.py  (needs Pillow).
It writes sf_logo.png / sf_icon.png and sf_logo.b64 / sf_icon.b64 next to itself; paste the .b64 strings into
brand_assets.py as BRAND_LOGO_B64 / BRAND_ICON_B64. The monitor itself needs no Pillow: Tk decodes the base64 PNG.
"""
import base64
import os
from PIL import Image, ImageDraw, ImageFont

DEEP = (11, 33, 46)
TEAL = (0, 176, 185)
TEAL_D = (0, 128, 140)
WHITE = (255, 255, 255)
MUTED = (148, 178, 190)

S = 4
OUT_W, OUT_H = 640, 120


def load_font(px, bold=True):
    """Return a Windows TrueType font at px pixels (scaled by the supersampling factor S), or Pillow's
    default font when none of the candidates is installed.
    """
    candidates = [
        r"C:\Windows\Fonts\seguisb.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, px * S)
    return ImageFont.load_default()


def build():
    """Draw the logo (badge, S-wave, wordmark, subtitle) at S times the size, then scale it down for smooth
    edges. Returns (logo 640x120, icon 256x256 cropped from the badge).
    """
    img = Image.new("RGBA", (OUT_W * S, OUT_H * S), (0, 0, 0, 0))

    # ---- badge with vertical gradient ------------------------------------------------
    bx, by, bs, radius = 8, 8, 104, 24
    grad = Image.new("RGBA", (bs * S, bs * S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for i in range(bs * S):
        t = i / (bs * S - 1)
        c = tuple(int(DEEP[k] + (TEAL_D[k] - DEEP[k]) * t) for k in range(3))
        gd.line([(0, i), (bs * S, i)], fill=c + (255,))

    mask = Image.new("L", (bs * S, bs * S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, bs * S - 1, bs * S - 1], radius=radius * S, fill=255
    )
    img.paste(grad, (bx * S, by * S), mask)

    # ---- stylised S / wave -------------------------------------------------------------
    d = ImageDraw.Draw(img)
    cx, cy = (bx + bs / 2) * S, (by + bs / 2) * S
    w = 9 * S
    d.arc([cx - 26 * S, cy - 30 * S, cx + 26 * S, cy + 2 * S], 200, 340, fill=WHITE, width=w)
    d.arc([cx - 26 * S, cy - 2 * S, cx + 26 * S, cy + 30 * S], 20, 160, fill=WHITE, width=w)

    # ---- wordmark ----------------------------------------------------------------------
    f1 = load_font(34, True)
    f2 = load_font(15, False)
    tx = (bx + bs + 22) * S

    d.text((tx, 14 * S), "SUCCORFISH", font=f1, fill=WHITE, anchor="lm")
    d.rectangle([tx, 46 * S, tx + 286 * S, 48 * S], fill=TEAL)
    d.text((tx, 62 * S), "VALTRACK  BLE  DEBUG  MONITOR", font=f2, fill=MUTED, anchor="lm")

    logo = img.resize((OUT_W, OUT_H), Image.LANCZOS)
    icon = img.crop((bx * S, by * S, (bx + bs) * S, (by + bs) * S)).resize((256, 256), Image.LANCZOS)
    return logo, icon


def main():
    """Build both images, save them as PNG and as base64 text next to this script, print the sizes."""
    here = os.path.dirname(os.path.abspath(__file__))
    logo, icon = build()

    logo_png = os.path.join(here, "sf_logo.png")
    icon_png = os.path.join(here, "sf_icon.png")
    logo.save(logo_png)
    icon.save(icon_png)

    for png, out in ((logo_png, "sf_logo.b64"), (icon_png, "sf_icon.b64")):
        b64 = base64.b64encode(open(png, "rb").read()).decode()
        with open(os.path.join(here, out), "w") as fh:
            fh.write(b64)
        print(f"{out}: {os.path.getsize(png)} B png -> {len(b64)} b64 chars")

    print(f"logo {logo.size}  icon {icon.size}")


if __name__ == "__main__":
    main()
