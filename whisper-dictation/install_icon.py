#!/usr/bin/env python3
"""Install a custom icon into the Whisper Dictation .app."""
import os
import subprocess
import sys
from PIL import Image, ImageDraw, ImageFilter

SRC = "/Users/maxsnigirev/Downloads/Gemini_Generated_Image_8d87l8d87l8d87l8.png"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICONSET_DIR = os.path.join(SCRIPT_DIR, "icon.iconset")
ICNS_PATH = os.path.join(SCRIPT_DIR, "icon.icns")


def crop_to_icon(img: Image.Image) -> Image.Image:
    """Auto-crop to the icon — detects checkerboard/white backgrounds.

    Uses color saturation + hue channel variance to find the colorful icon
    (which stands out from grey/white checkerboard patterns and plain white).
    """
    import numpy as np

    img_rgba = img.convert("RGBA")
    arr = np.array(img_rgba)
    h, w, _ = arr.shape

    # If real transparency exists, prefer it
    alpha = arr[:, :, 3]
    if alpha.min() < 240:
        is_icon = alpha > 180
    else:
        # Use saturation (HSV's S channel): grey/white have S≈0, colored has S>0
        r = arr[:, :, 0].astype(np.int16)
        g = arr[:, :, 1].astype(np.int16)
        b = arr[:, :, 2].astype(np.int16)
        mx = np.maximum(np.maximum(r, g), b)
        mn = np.minimum(np.minimum(r, g), b)
        # Saturation ~ (max-min)/max
        sat = np.zeros_like(mx, dtype=np.float32)
        nonzero = mx > 0
        sat[nonzero] = (mx[nonzero] - mn[nonzero]) / mx[nonzero]
        # The icon has strong saturation; checkerboard greys have sat ≈ 0;
        # the white rounded frame has sat ≈ 0 too — we'll miss it, but the
        # colorful content inside the frame is what matters.
        is_icon = sat > 0.18

    # Find rows and cols with enough "icon" pixels
    row_has = is_icon.sum(axis=1) > (w * 0.05)
    col_has = is_icon.sum(axis=0) > (h * 0.05)

    if not row_has.any() or not col_has.any():
        return img.convert("RGBA")

    rows = np.where(row_has)[0]
    cols = np.where(col_has)[0]
    min_y, max_y = int(rows[0]), int(rows[-1])
    min_x, max_x = int(cols[0]), int(cols[-1])

    # For the square icon, we want the largest SQUARE bbox centered on icon centroid.
    # First get basic rect, then expand/contract to square.
    box_w = max_x - min_x
    box_h = max_y - min_y

    # The icon itself is roughly square. If bbox is much wider than tall,
    # the extra width is likely decoration; trim to height.
    if box_w > box_h * 1.3:
        # Find densest square-sized window
        side = box_h
        # Project icon density along x
        col_density = is_icon[min_y:max_y + 1, :].sum(axis=0)
        # Sliding window sum of size `side`
        best_x = min_x
        best_sum = 0
        for sx in range(max(0, min_x - 20), min(w - side, max_x + 20 - side) + 1):
            s = int(col_density[sx:sx + side].sum())
            if s > best_sum:
                best_sum = s
                best_x = sx
        min_x = best_x
        max_x = best_x + side
    elif box_h > box_w * 1.3:
        side = box_w
        row_density = is_icon[:, min_x:max_x + 1].sum(axis=1)
        best_y = min_y
        best_sum = 0
        for sy in range(max(0, min_y - 20), min(h - side, max_y + 20 - side) + 1):
            s = int(row_density[sy:sy + side].sum())
            if s > best_sum:
                best_sum = s
                best_y = sy
        min_y = best_y
        max_y = best_y + side

    # Expand to include the white rounded frame around the colorful content
    expand_frac = 0.12
    cur_w = max_x - min_x
    cur_h = max_y - min_y
    pad_x = int(cur_w * expand_frac)
    pad_y = int(cur_h * expand_frac)
    min_x = max(0, min_x - pad_x)
    min_y = max(0, min_y - pad_y)
    max_x = min(w, max_x + pad_x)
    max_y = min(h, max_y + pad_y)

    cropped = img_rgba.crop((min_x, min_y, max_x, max_y))

    # Remove the fake checkerboard background:
    # flood-fill from each corner replacing grey/white patterns with transparency.
    cropped = _remove_fake_background(cropped)

    cw, ch = cropped.size
    side = max(cw, ch)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(cropped, ((side - cw) // 2, (side - ch) // 2), cropped)
    return square


def _remove_fake_background(img: Image.Image) -> Image.Image:
    """Detect and remove the fake checkerboard/white background around the icon.

    Uses flood-fill from the 4 corners. Any pixel reachable from a corner
    (through connected grey/white pixels within a color tolerance) is marked
    as background and gets alpha=0.
    """
    import numpy as np
    img = img.convert("RGBA")
    arr = np.array(img)
    h, w, _ = arr.shape

    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)

    # A pixel is "background-like" if it's mostly grey (low saturation)
    # AND lightish (brightness > 170) — i.e. part of the checkerboard.
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.zeros_like(mx, dtype=np.float32)
    nonzero = mx > 0
    sat[nonzero] = (mx[nonzero] - mn[nonzero]) / np.maximum(mx[nonzero], 1)
    brightness = (r + g + b) / 3.0
    bg_like = (sat < 0.08) & (brightness > 170)

    # Flood fill from each corner over bg_like pixels. BFS using a queue.
    visited = np.zeros((h, w), dtype=bool)
    from collections import deque
    q = deque()
    for (cx, cy) in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        if bg_like[cy, cx]:
            q.append((cx, cy))
            visited[cy, cx] = True

    while q:
        x, y = q.popleft()
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not visited[ny, nx] and bg_like[ny, nx]:
                visited[ny, nx] = True
                q.append((nx, ny))

    # Set alpha=0 for background-flood pixels
    arr[:, :, 3] = np.where(visited, 0, 255)

    # Smooth the alpha edge a bit so the rounded corner doesn't look jagged
    result = Image.fromarray(arr, mode="RGBA")
    return result


def main():
    if not os.path.exists(SRC):
        print(f"Source not found: {SRC}")
        sys.exit(1)

    print(f"Loading {SRC}...")
    src = Image.open(SRC)
    print(f"Original size: {src.size}")

    icon = crop_to_icon(src)
    print(f"Cropped to: {icon.size}")

    # Save a preview
    preview = os.path.join(SCRIPT_DIR, "icon_preview.png")
    icon.save(preview)
    print(f"Preview: {preview}")

    # Generate iconset sizes
    os.makedirs(ICONSET_DIR, exist_ok=True)
    for f in os.listdir(ICONSET_DIR):
        os.remove(os.path.join(ICONSET_DIR, f))

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
        resized = icon.resize((size, size), Image.LANCZOS)
        path = os.path.join(ICONSET_DIR, f"icon_{name}.png")
        resized.save(path, "PNG")

    # Build .icns
    if os.path.exists(ICNS_PATH):
        os.remove(ICNS_PATH)
    subprocess.run(["iconutil", "-c", "icns", ICONSET_DIR, "-o", ICNS_PATH], check=True)
    print(f"Created {ICNS_PATH}")

    # Install into .app
    app_dir = "/Applications/Whisper Dictation.app"
    dest = os.path.join(app_dir, "Contents", "Resources", "icon.icns")
    if os.path.exists(app_dir):
        subprocess.run(["cp", ICNS_PATH, dest], check=True)
        subprocess.run(["touch", app_dir], check=True)
        print(f"Installed into {app_dir}")

        # Refresh macOS icon cache
        subprocess.run([
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
            "LaunchServices.framework/Support/lsregister",
            "-f", app_dir,
        ], check=False)


if __name__ == "__main__":
    main()
