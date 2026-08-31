"""
generate_icons.py - one-off script that renders the PWA icon set.

Not imported by the running app; run manually (or re-run after a palette
change) to regenerate static/icons/*. Draws everything as geometric shapes
at high resolution (SUPER x the largest target) and downsamples with
LANCZOS, so every exported size is crisp rather than a naive small-canvas
render.

Mark: three ascending bars (a bar-chart glyph) plus a small upward tick
- reads as "stocks going up" at any size, and survives being cropped to a
circle (Android/iOS maskable masking) because it's kept inside the central
80% safe zone on the maskable variant.

Run:  python3 scripts/generate_icons.py
"""
import os

from PIL import Image, ImageDraw

NAVY = (11, 18, 32, 255)       # #0b1220 - site background
TEAL = (45, 212, 191, 255)     # #2dd4bf - site accent

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "static", "icons")
os.makedirs(OUT_DIR, exist_ok=True)

SUPER = 8  # supersampling factor for crisp downsampled edges


def _draw_mark(canvas_size, safe_zone_frac=1.0):
    """A fresh navy square of `canvas_size` px with the teal bar-chart mark
    centered, scaled to fit inside a `safe_zone_frac` fraction of the
    canvas (1.0 = use nearly the whole icon; 0.8 = keep clear of a
    maskable OS mask's outer 20%)."""
    img = Image.new("RGBA", (canvas_size, canvas_size), NAVY)
    draw = ImageDraw.Draw(img)

    # The mark is authored on a 100x100 logical grid, then scaled/centered
    # into the safe zone -- easy to reason about regardless of final px size.
    zone = canvas_size * safe_zone_frac
    offset = (canvas_size - zone) / 2

    def pt(x, y):
        return (offset + x / 100 * zone, offset + y / 100 * zone)

    # Three ascending bars (a bar-chart), rounded caps, plus a short
    # up-arrow stroke riding along their tops -- "stocks going up" at a
    # glance, geometric only, legible all the way down to 32px.
    bar_w = 14
    bars = [
        # (x_center, top_y, bottom_y)
        (28, 58, 78),
        (50, 40, 78),
        (72, 22, 78),
    ]
    radius = bar_w / 2 * (zone / 100)
    for cx, top, bottom in bars:
        x0, y0 = pt(cx - bar_w / 2, top)
        x1, y1 = pt(cx + bar_w / 2, bottom)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=TEAL)

    # Up-arrow accent above the tallest bar, echoing the ascent.
    line_w = max(2, round(4 * (zone / 100)))
    draw.line([pt(14, 34), pt(86, 8)], fill=TEAL, width=line_w, joint="curve")
    # Arrowhead
    ax, ay = pt(86, 8)
    ah = 9 * (zone / 100)
    draw.polygon(
        [(ax, ay), (ax - ah * 1.4, ay + ah * 0.35), (ax - ah * 0.35, ay + ah * 1.4)],
        fill=TEAL,
    )
    return img


def _render(size, safe_zone_frac, path):
    big = _draw_mark(size * SUPER, safe_zone_frac)
    small = big.resize((size, size), Image.LANCZOS)
    small.save(path)
    print(f"wrote {path} ({size}x{size})")


if __name__ == "__main__":
    # Standard (non-maskable) icons: mark fills most of the canvas -- these
    # are shown as-drawn (iOS/most Android launchers), not forced through a
    # shape mask, so there's no need to shrink the mark defensively.
    _render(180, 0.86, os.path.join(OUT_DIR, "apple-touch-icon.png"))
    _render(192, 0.86, os.path.join(OUT_DIR, "icon-192.png"))
    _render(512, 0.86, os.path.join(OUT_DIR, "icon-512.png"))
    # Maskable 512: background must fill the full square edge-to-edge
    # (it already does -- navy has no transparency), mark kept inside the
    # central 80% so an aggressive OS mask (circle, squircle, ...) never
    # clips it.
    _render(512, 0.72, os.path.join(OUT_DIR, "icon-512-maskable.png"))
    # 32px favicon -- same mark, simplifies fine at this size since it's
    # pure geometry with no text.
    _render(32, 0.86, os.path.join(OUT_DIR, "favicon-32.png"))

    # favicon.ico for browsers that request it directly regardless of the
    # <link rel="icon"> tag.
    fav = Image.open(os.path.join(OUT_DIR, "favicon-32.png"))
    fav.save(os.path.join(OUT_DIR, "favicon.ico"), format="ICO", sizes=[(32, 32)])
    print(f"wrote {os.path.join(OUT_DIR, 'favicon.ico')}")
