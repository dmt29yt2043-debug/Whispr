#!/usr/bin/env python3
"""Generate a macOS app icon — laptop with a microphone on its screen."""
import os
import subprocess
from PIL import Image, ImageDraw, ImageFilter


def make_icon(size: int) -> Image.Image:
    """Create a square icon at the given size."""
    # Work at 4x for anti-aliasing, then downsample
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded square background with gradient
    radius = int(s * 0.225)

    # Gradient: deep purple top → vibrant blue bottom
    grad = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gpix = grad.load()
    for y in range(s):
        t = y / s
        r = int(95 + (50 - 95) * t)
        g = int(70 + (110 - 70) * t)
        b = int(220 + (245 - 220) * t)
        for x in range(s):
            gpix[x, y] = (r, g, b, 255)

    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s, s), radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    draw = ImageDraw.Draw(img)

    # ── Laptop ──
    # Screen (top part)
    screen_w = int(s * 0.62)
    screen_h = int(s * 0.42)
    screen_x = (s - screen_w) // 2
    screen_y = int(s * 0.22)
    screen_radius = int(s * 0.025)

    # Laptop body outline (white)
    outline_w = max(4, s // 128)

    # Screen (rounded rectangle, white)
    draw.rounded_rectangle(
        (screen_x, screen_y, screen_x + screen_w, screen_y + screen_h),
        radius=screen_radius,
        fill=(255, 255, 255, 255),
    )

    # Screen inner (dark area where mic sits)
    inset = int(s * 0.02)
    draw.rounded_rectangle(
        (screen_x + inset, screen_y + inset,
         screen_x + screen_w - inset, screen_y + screen_h - inset),
        radius=max(2, screen_radius - inset // 2),
        fill=(40, 30, 80, 255),
    )

    # Laptop base (trapezoid-like)
    base_top_w = int(s * 0.72)
    base_bot_w = int(s * 0.78)
    base_y_top = screen_y + screen_h + int(s * 0.015)
    base_y_bot = base_y_top + int(s * 0.04)

    base_top_x = (s - base_top_w) // 2
    base_bot_x = (s - base_bot_w) // 2

    draw.polygon(
        [
            (base_top_x, base_y_top),
            (base_top_x + base_top_w, base_y_top),
            (base_bot_x + base_bot_w, base_y_bot),
            (base_bot_x, base_y_bot),
        ],
        fill=(255, 255, 255, 255),
    )

    # Base notch (the rounded dip at the top center)
    notch_w = int(s * 0.12)
    notch_h = int(s * 0.018)
    notch_x = (s - notch_w) // 2
    draw.rounded_rectangle(
        (notch_x, base_y_top - 1, notch_x + notch_w, base_y_top + notch_h),
        radius=notch_h // 2,
        fill=(40, 30, 80, 255),
    )

    # ── Microphone on screen ──
    mic_w = int(screen_w * 0.22)
    mic_h = int(screen_h * 0.58)
    mic_x = (s - mic_w) // 2
    mic_y = screen_y + int(screen_h * 0.18)

    # Mic body (white rounded capsule)
    draw.rounded_rectangle(
        (mic_x, mic_y, mic_x + mic_w, mic_y + mic_h),
        radius=mic_w // 2,
        fill=(255, 255, 255, 255),
    )

    # Mic grille lines (purple)
    grille_color = (95, 70, 220, 200)
    line_w = max(2, s // 256)
    for i in range(3):
        ly = mic_y + int(mic_h * 0.22) + i * int(mic_h * 0.16)
        draw.line(
            [(mic_x + int(mic_w * 0.22), ly),
             (mic_x + int(mic_w * 0.78), ly)],
            fill=grille_color,
            width=line_w,
        )

    # Mic stand arc (U shape under mic)
    arc_w = int(mic_w * 1.9)
    arc_h = int(screen_h * 0.14)
    arc_x = (s - arc_w) // 2
    arc_y_top = mic_y + mic_h - int(arc_h * 0.3)
    arc_line_w = max(3, s // 192)

    draw.arc(
        (arc_x, arc_y_top, arc_x + arc_w, arc_y_top + arc_h * 2),
        start=0,
        end=180,
        fill=(255, 255, 255, 255),
        width=arc_line_w,
    )

    # Stand vertical
    stand_y_top = arc_y_top + arc_h
    stand_y_bot = screen_y + screen_h - inset - int(s * 0.02)
    draw.line(
        [(s // 2, stand_y_top), (s // 2, stand_y_bot)],
        fill=(255, 255, 255, 255),
        width=arc_line_w,
    )

    # Downsample to target size for anti-aliasing
    return img.resize((size, size), Image.LANCZOS)


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(script_dir, "icon.iconset")
    os.makedirs(out_dir, exist_ok=True)

    sizes = [
        (16, "16x16"),
        (32, "16x16@2x"),
        (32, "32x32"),
        (64, "32x32@2x"),
        (128, "128x128"),
        (256, "128x128@2x"),
        (256, "256x256"),
        (512, "256x256@2x"),
        (512, "512x512"),
        (1024, "512x512@2x"),
    ]

    for size, name in sizes:
        img = make_icon(size)
        path = os.path.join(out_dir, f"icon_{name}.png")
        img.save(path, "PNG")

    icns_path = os.path.join(script_dir, "icon.icns")
    subprocess.run(["iconutil", "-c", "icns", out_dir, "-o", icns_path], check=True)
    print(f"Created {icns_path}")


if __name__ == "__main__":
    main()
