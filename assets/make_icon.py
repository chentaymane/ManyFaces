"""Generate a professional multi-resolution app icon (assets/icon.ico).

A fingerprint whorl on a modern blue→purple gradient — on-theme for a
fingerprint-spoofing tool and legible down to 16x16.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw

SIZE = 512


def _rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def _gradient(size: int, top: tuple[int, int, int], bot: tuple[int, int, int]) -> Image.Image:
    base = Image.new("RGB", (size, size), top)
    d = ImageDraw.Draw(base)
    for y in range(size):
        t = y / (size - 1)
        # diagonal-ish blend
        r = int(top[0] + (bot[0] - top[0]) * t)
        g = int(top[1] + (bot[1] - top[1]) * t)
        b = int(top[2] + (bot[2] - top[2]) * t)
        d.line([(0, y), (size, y)], fill=(r, g, b))
    return base


def build() -> Image.Image:
    # Supersample for smooth curves, then downscale.
    S = SIZE * 2
    grad = _gradient(S, (79, 124, 255), (124, 60, 255)).convert("RGBA")
    grad.putalpha(_rounded_mask(S, radius=int(S * 0.22)))

    draw = ImageDraw.Draw(grad)
    cx, cy = S / 2, S / 2 + S * 0.03
    white = (255, 255, 255, 235)
    lw = int(S * 0.022)

    # Fingerprint whorl: a set of nested, slightly-open arcs of growing radius,
    # each offset in sweep so it reads as ridges rather than plain circles.
    rings = 7
    for i in range(rings):
        rad = S * (0.09 + i * 0.045)
        start = 20 + i * 12
        end = 340 - i * 8
        bbox = [cx - rad, cy - rad, cx + rad, cy + rad]
        draw.arc(bbox, start=start, end=end, fill=white, width=lw)

    # A short central core line to anchor the whorl.
    draw.arc(
        [cx - S * 0.045, cy - S * 0.055, cx + S * 0.045, cy + S * 0.035],
        start=200, end=70, fill=white, width=lw,
    )

    return grad.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    img = build()
    img.save(os.path.join(here, "icon.png"))
    img.save(
        os.path.join(here, "icon.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("wrote icon.png and icon.ico")


if __name__ == "__main__":
    main()
