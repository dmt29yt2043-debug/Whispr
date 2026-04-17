#!/usr/bin/env python3
"""Install a custom icon into the Whisper Dictation .app."""
import os
import subprocess
import sys
from PIL import Image

SRC = "/Users/maxsnigirev/Downloads/Gemini_Generated_Image_jk3787jk3787jk37.png"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICONSET_DIR = os.path.join(SCRIPT_DIR, "icon.iconset")
ICNS_PATH = os.path.join(SCRIPT_DIR, "icon.icns")


def crop_to_icon(img: Image.Image) -> Image.Image:
    """Auto-crop to the main rounded-square icon — find darkest/most-saturated region."""
    import numpy as np
    img = img.convert("RGB")
    arr = np.array(img)
    h, w, _ = arr.shape

    # Score each pixel: lower = more icon-like (colorful/dark)
    # Icon has purple/blue gradient, so R is relatively low, B is high
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    # Brightness of whitish background is ~240+. Icon body pixels are <220.
    # Saturation helps: white has r≈g≈b, icon has strong B>R
    brightness = (r.astype(int) + g.astype(int) + b.astype(int)) / 3
    is_icon = brightness < 230  # strict: skip subtle shadows/decorations

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

    pad = 6
    min_x = max(0, min_x - pad)
    min_y = max(0, min_y - pad)
    max_x = min(w, max_x + pad)
    max_y = min(h, max_y + pad)

    cropped = img.convert("RGBA").crop((min_x, min_y, max_x, max_y))
    cw, ch = cropped.size
    side = max(cw, ch)
    square = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    square.paste(cropped, ((side - cw) // 2, (side - ch) // 2))
    return square


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
