"""Generate assets/icon.ico, the AccessCaster app icon, from code so it is reproducible.

A dark rounded square with a light "AC" monogram and a small red "on air" dot. Written as a
multi-resolution .ico (16 up to 256 px) so Windows picks a crisp size for the tray, the window
title bar, the taskbar and the installer. Run: python gen_icon.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

SIZE = 256
# icon.ico lives with the app, in Project/assets/ (this script is in the sibling ProjectBuilds/).
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "Project", "assets", "icon.ico")
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _font(px: int):
    for name in ("arialbd.ttf", "arial.ttf", "seguisb.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except Exception:
            continue
    return ImageFont.load_default()


def main() -> None:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = SIZE // 16
    d.rounded_rectangle([pad, pad, SIZE - pad - 1, SIZE - pad - 1],
                        radius=SIZE // 6, fill=(22, 28, 38, 255))
    # red "on air" dot, top-right
    r = SIZE // 9
    cx, cy = SIZE - pad - r - SIZE // 12, pad + r + SIZE // 12
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(220, 60, 60, 255))
    # "AC" monogram, centred
    font = _font(int(SIZE * 0.5))
    bbox = d.textbbox((0, 0), "AC", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((SIZE - tw) / 2 - bbox[0], (SIZE - th) / 2 - bbox[1] + SIZE * 0.02),
           "AC", font=font, fill=(238, 242, 248, 255))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT, format="ICO", sizes=ICO_SIZES)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
