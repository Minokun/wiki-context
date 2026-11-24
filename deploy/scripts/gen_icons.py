#!/usr/bin/env python3
"""Generate clean vector-style raster icons (.png, .ico, .icns) for Tauri v2."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
ICON_DIR = ROOT / "deploy" / "desktop" / "src-tauri" / "icons"


def draw_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, int(size * 0.06))
    radius = int(size * 0.22)
    # Indigo-slate rounded square background
    draw.rounded_rectangle(
        [pad, pad, size - pad - 1, size - pad - 1],
        radius=radius,
        fill=(79, 70, 229, 255),
        outline=(129, 140, 248, 255),
        width=max(1, size // 64),
    )
    # Geometric 'W' emblem in crisp white/cyan
    cx = size / 2
    cy = size / 2
    w_span = size * 0.50
    h_span = size * 0.36
    x0 = cx - w_span / 2
    x4 = cx + w_span / 2
    x1 = x0 + w_span * 0.25
    x2 = cx
    x3 = x0 + w_span * 0.75
    top = cy - h_span / 2
    bot = cy + h_span / 2
    mid = cy - h_span * 0.05
    stroke = max(2, int(size * 0.095))
    pts = [(x0, top), (x1, bot), (x2, mid), (x3, bot), (x4, top)]
    draw.line(pts, fill=(255, 255, 255, 255), width=stroke, joint="curve")
    return img


def main() -> int:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    base = draw_icon(512)
    base.save(ICON_DIR / "icon.png")
    for sz, name in [(32, "32x32.png"), (128, "128x128.png"), (256, "128x128@2x.png")]:
        draw_icon(sz).save(ICON_DIR / name)

    # Windows .ico
    base.save(
        ICON_DIR / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    # macOS .icns via iconutil when on darwin
    if sys.platform == "darwin":
        iconset = ICON_DIR / "icon.iconset"
        iconset.mkdir(exist_ok=True)
        mapping = [
            (16, "icon_16x16.png"),
            (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"),
            (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
        ]
        for sz, fname in mapping:
            draw_icon(sz).save(iconset / fname)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICON_DIR / "icon.icns")], check=True)
        for p in iconset.iterdir():
            p.unlink()
        iconset.rmdir()

    print(f"[gen_icons] Generated icons in {ICON_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
