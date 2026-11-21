"""Native raster versions of desktop/shp-photocraft-tools{,-tray}.svg."""

from pathlib import Path


def icon_image(*, tray=False, size=256):
    """Render the canonical SVG shapes with Pillow (also used by the shortcut installer)."""
    from PIL import Image, ImageDraw

    scale = 2
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    unit = size * scale / 128
    background, border, foreground, half = (
        ("#181818", "#ffffff", "#e8e8e8", "#878787") if tray
        else ("#16181d", "#4c8dff", "#e6e8ee", "#4c8dff"))

    def at(*values):
        return [value * unit for value in values]

    draw.rounded_rectangle(at(6, 6, 122, 122), radius=14 * unit, fill=background,
                           outline=border, width=round(5.5 * unit))
    stroke = round(8 * unit)
    for line in ((27, 27, 57, 27), (27, 27, 27, 57), (101, 101, 71, 101), (101, 101, 101, 71)):
        draw.line(at(*line), fill=foreground, width=stroke)
        for x, y in (line[:2], line[2:]):
            draw.ellipse(at(x - 4, y - 4, x + 4, y + 4), fill=foreground)
    draw.ellipse(at(41, 41, 87, 87), fill=foreground)
    draw.pieslice(at(41, 41, 87, 87), 90, 270, fill=half)
    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def write_icon(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # WinForms/System.Drawing can misread PNG-compressed ICO frames; use native DIB frames.
    icon_image().save(path, format="ICO", bitmap_format="bmp",
                      sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
