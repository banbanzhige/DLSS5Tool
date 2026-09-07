#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build DLSS5Tool Windows icons from the split-comparison Shiba master.

The master is a complete mark: rounded square, pixel original on the left,
neural-rendered HD on the right, cyan divider already in the artwork.
Re-run after replacing assets/src/app-master.png.
"""
from __future__ import annotations

import argparse
import os
import struct
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter
from PIL.PngImagePlugin import PngInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
SRC_MASTER = os.path.join(ASSETS, "src", "app-master.png")
ICO_SIZES = (16, 20, 24, 32, 36, 40, 48, 64, 96, 128, 256)
MASTER_SIZE = 1024

# Small Windows icon slots need optical sizing rather than a literal downscale.
# The plate silhouette stays fixed while the artwork inside it is enlarged.
OPTICAL_ZOOM = {
    16: 1.16,
    20: 1.14,
    24: 1.12,
    32: 1.09,
    36: 1.08,
    40: 1.07,
    48: 1.05,
    64: 1.03,
}


def clean_alpha(image):
    """Snap near-transparent pixels off and near-opaque pixels on."""
    red, green, blue, alpha = image.split()
    alpha = alpha.point(lambda value: 0 if value < 8 else (255 if value >= 250 else value))
    zero = Image.new("L", image.size, 0)
    keep = alpha.point(lambda value: 0 if value == 0 else 255)
    red = Image.composite(red, zero, keep)
    green = Image.composite(green, zero, keep)
    blue = Image.composite(blue, zero, keep)
    return Image.merge("RGBA", (red, green, blue, alpha))


def load_master(path):
    image = Image.open(path).convert("RGBA")
    for key in ("icc_profile", "exif", "xmp"):
        image.info.pop(key, None)
    image = clean_alpha(image)
    width, height = image.size
    if width != height:
        side = max(width, height)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(image, ((side - width) // 2, (side - height) // 2), image)
        image = canvas
    return image


def downscale(image, size):
    if image.size == (size, size):
        return image.copy()
    current = image
    while min(current.size) // 2 >= size:
        half = (current.size[0] // 2, current.size[1] // 2)
        current = current.resize(half, Image.Resampling.BOX)
    if current.size != (size, size):
        current = current.resize((size, size), Image.Resampling.LANCZOS)
    return current


def plate_source(master):
    """Crop to the rounded card so it fills the icon, keeping transparent corners."""
    box = master.getbbox() or (0, 0, master.size[0], master.size[1])
    cropped = master.crop(box)
    width, height = cropped.size
    side = max(width, height)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(
        cropped,
        ((side - width) // 2, (side - height) // 2),
        cropped,
    )
    return canvas


def optical_zoom(image, factor):
    """Enlarge interior artwork while preserving the original rounded plate."""
    if factor <= 1.0:
        return image.copy()
    width, height = image.size
    zoomed = image.resize(
        (int(round(width * factor)), int(round(height * factor))),
        Image.Resampling.LANCZOS,
    )
    left = (zoomed.size[0] - width) // 2
    top = (zoomed.size[1] - height) // 2
    zoomed = zoomed.crop((left, top, left + width, top + height))
    # Reuse the unscaled alpha silhouette so the optical zoom cannot turn the
    # icon into a white square or clip its rounded transparent corners.
    zoomed.putalpha(image.getchannel("A"))
    return clean_alpha(zoomed)


def sharpen_rgb(image, radius, percent, threshold):
    red, green, blue, alpha = image.split()
    rgb = Image.merge("RGB", (red, green, blue)).filter(
        ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold)
    )
    return Image.merge("RGBA", (*rgb.split(), alpha))


def tune_small_rgb(image, size):
    """Recover color separation that is normally lost in tiny taskbar slots."""
    alpha = image.getchannel("A")
    rgb = image.convert("RGB")
    contrast = 1.09 if size <= 24 else (1.06 if size <= 40 else 1.035)
    saturation = 1.10 if size <= 24 else (1.07 if size <= 40 else 1.04)
    rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
    rgb = ImageEnhance.Color(rgb).enhance(saturation)
    return Image.merge("RGBA", (*rgb.split(), alpha))


def silhouette_border(icon):
    """1px rim on the rounded plate so the card reads on light taskbars."""
    alpha = icon.getchannel("A")
    ring = ImageChops.subtract(alpha.filter(ImageFilter.MaxFilter(3)), alpha)
    if ring.getextrema()[1] == 0:
        return icon
    color = (150, 157, 166, 255)
    border = Image.new("RGBA", icon.size, color)
    border.putalpha(ring)
    return Image.alpha_composite(icon, border)


def render_size(master, size):
    source = plate_source(master) if size <= 64 else master
    if size <= 64:
        source = optical_zoom(source, OPTICAL_ZOOM.get(size, 1.0))
        hi = downscale(source, size * 2)
        hi = tune_small_rgb(hi, size)
        percent = 135 if size <= 24 else (115 if size <= 40 else 95)
        hi = sharpen_rgb(hi, 0.7, percent, 1)
        icon = hi.resize((size, size), Image.Resampling.BOX)
        if size <= 48:
            icon = silhouette_border(icon)
        return icon
    return downscale(source, size)


def save_png(image, dest):
    image.save(dest, format="PNG", optimize=True, pnginfo=PngInfo())


def _ico_image_bytes(image):
    """PNG for 256px; BMP/DIB for smaller sizes so Win32 LoadImage stays sharp."""
    image = image.convert("RGBA")
    width, height = image.size
    if width >= 256 and height >= 256:
        buf = BytesIO()
        save_png(image, buf)
        return buf.getvalue()
    buf = BytesIO()
    image.save(buf, format="DIB")
    header_and_xor = bytearray(buf.getvalue())
    struct.pack_into("<I", header_and_xor, 8, height * 2)
    and_row = ((width + 31) // 32) * 4
    return bytes(header_and_xor) + bytes(and_row * height)


def save_ico(path, images):
    images = [im.convert("RGBA") for im in sorted(images, key=lambda item: item.size[0])]
    blobs = [_ico_image_bytes(image) for image in images]
    count = len(images)
    offset = 6 + 16 * count
    entries = []
    for image, blob in zip(images, blobs):
        width, height = image.size
        entries.append(struct.pack(
            "<BBBBHHII",
            0 if width >= 256 else width,
            0 if height >= 256 else height,
            0, 0, 1, 32, len(blob), offset,
        ))
        offset += len(blob)
    with open(path, "wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, count))
        handle.write(b"".join(entries))
        for blob in blobs:
            handle.write(blob)


def zoomed_sheet(icons, scale=8, background=(0x12, 0x18, 0x1E, 255)):
    gap = 20
    cell = max(icon.size[0] for icon in icons) * scale
    width = gap + len(icons) * (cell + gap)
    height = cell + gap * 2 + 22
    sheet = Image.new("RGBA", (width, height), background)
    draw = ImageDraw.Draw(sheet)
    x = gap
    for icon in icons:
        zoomed = icon.resize(
            (icon.size[0] * scale, icon.size[1] * scale),
            Image.Resampling.NEAREST,
        )
        y = gap + (cell - zoomed.size[1]) // 2
        sheet.paste(zoomed, (x, y), zoomed)
        draw.text(
            (x, y + zoomed.size[1] + 6),
            f"{icon.size[0]}px",
            fill=(230, 235, 240, 255),
        )
        x += cell + gap
    return sheet


def install_master(source_path, dest_path=SRC_MASTER):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    image = load_master(source_path)
    save_png(image, dest_path)
    return dest_path, image


def build(master_path, out_dir, preview_path=None):
    os.makedirs(out_dir, exist_ok=True)
    source = load_master(master_path)
    master = downscale(source, MASTER_SIZE)
    master_out = os.path.join(out_dir, "app-1024.png")
    save_png(master, master_out)
    save_png(downscale(master, 512), os.path.join(out_dir, "app-512.png"))
    save_png(downscale(master, 256), os.path.join(out_dir, "app.png"))

    ico_images = [render_size(master, size) for size in ICO_SIZES]
    ico_path = os.path.join(out_dir, "app.ico")
    save_ico(ico_path, ico_images)

    if preview_path:
        os.makedirs(os.path.dirname(preview_path) or ".", exist_ok=True)
        small = [render_size(master, size) for size in (16, 20, 24, 32, 36, 48, 64)]
        save_png(zoomed_sheet(small), preview_path)
    return ico_path, master_out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", default=SRC_MASTER)
    parser.add_argument("--out", default=ASSETS)
    parser.add_argument(
        "--preview",
        default=os.path.join(ROOT, "output", "icon-zoomed.png"),
    )
    parser.add_argument(
        "--install-master",
        action="store_true",
        help="Copy --master into assets/src/app-master.png before building.",
    )
    args = parser.parse_args(argv)
    if not os.path.isfile(args.master):
        raise SystemExit(f"missing master artwork: {args.master}")
    master_path = args.master
    if args.install_master:
        master_path, _image = install_master(args.master, SRC_MASTER)
        print(f"installed {master_path}")
    ico_path, written_master = build(master_path, args.out, args.preview)
    print(f"wrote {written_master}")
    print(f"wrote {ico_path}")
    if args.preview:
        print(f"wrote {args.preview}")


if __name__ == "__main__":
    main()
