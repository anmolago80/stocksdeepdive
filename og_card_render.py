"""
og_card_render.py

Part 39 (13 Sep 2026, mock: mocks/social_card_mock.html) - pure PNG
rendering for social preview ("Open Graph") cards. Every function here
takes plain data in and returns PNG bytes out: NO network calls, NO
filesystem reads/writes, NO Streamlit import. server.py's own /og/*
routes own the on-disk cache (read/write, "render once per ticker per
day") and the snapshot_store lookup - this module only ever draws
pixels, so it's unit-testable standalone (see test_og_card_render.py in
this Part's own harness) just by calling a function and inspecting the
returned bytes/pixels with Pillow.

FONT: Pillow's own bundled scalable default font (ImageFont.load_default
(size=...), available since Pillow 10.1) - deliberately NOT a system
font path (e.g. /usr/share/fonts/...) or a bundled .ttf file, because
neither is guaranteed to exist on Railway's own container image, and a
missing font file would be exactly the kind of thing that turns "every
share becomes a small advertisement" into "every share 500s" (this
module's whole reason for existing is to never do that - see
render_default_card()'s own role as the fail-safe both server.py AND
every function below fall back to). Pillow's bundled font ships inside
the pillow wheel itself, so it exists wherever Pillow imports at all.

LAYOUT: every pixel position below is the mock's own CSS pixel offset
(social_card_mock.html's `.og` rules) doubled - the mock's own preview
box renders at max-width:600px while the real card this module produces
is 1200x630 (exactly 2x), so doubling its authored offsets reproduces
its layout exactly rather than approximating it by eye."""
import io
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

CARD_W, CARD_H = 1200, 630

FOOTER_TEXT = "Every input shown · described calculations, not advice · stocksdeepdive.com"
_BRAND_STOCKS = "Stocks"
_BRAND_DEEPDIVE = "DeepDive"

# Colors - every one lifted directly from social_card_mock.html's own
# <style> block (hex comments preserved so a future palette change can
# be cross-checked against the approved mock in one glance).
_BG_TOP = (11, 18, 32)        # #0b1220
_BG_MID = (14, 25, 48)        # #0e1930
_BG_END = (18, 48, 63)        # #12303f
_TEXT = (230, 237, 245)       # #e6edf5
_TEAL = (45, 212, 191)        # #2dd4bf
_TEAL_DARK = (20, 184, 166)   # #14b8a6
_TEAL_DEEP = (15, 118, 110)   # #0f766e
_GREEN = (52, 211, 153)       # #34d399
_MUTED = (138, 160, 184)      # #8aa0b8
_FAINT = (91, 114, 144)       # #5b7290
_BADGE_BG = (16, 49, 45)      # #10312d
_TRACK_BG = (26, 39, 64)      # #1a2740


def _font(size, bold=True):
    """Pillow's bundled scalable default font - see this module's own
    docstring for why this, not a filesystem font path. `bold` has no
    effect on the bundled font (it has no separate bold face); kept as a
    parameter anyway so every call site below still documents its own
    intent even though weight is approximated by size alone today."""
    return ImageFont.load_default(size=size)


def _diagonal_gradient(w, h, stops):
    """A `stops`-stop diagonal (top-left -> bottom-right) gradient, the
    closest plain-Pillow equivalent of the mock's own CSS `linear-
    gradient(135deg, c0 0%, c1 55%, c2 100%)`. `stops` is [(t, (r,g,b)),
    ...] with t in [0, 1], sorted ascending - t=0 must be present and
    t=1 must be present (the two CSS gradient endpoints); any number of
    stops in between is fine (the mock's own gradient has exactly one:
    55%).

    Built as a tiny (w+h)-pixel 1-D gradient strip resized up rather
    than computed per output pixel - correct because every pixel on the
    same diagonal (constant x+y) gets the same color in a 135deg linear
    gradient, so the whole 2-D field really is just that 1-D strip
    stretched across the diagonal. Cheap enough to redo per card render
    (once per ticker per day, per server.py's own cache policy) without
    reaching for numpy."""
    n = w + h
    strip = Image.new("RGB", (n, 1))
    px = strip.load()
    for i in range(n):
        t = i / (n - 1)
        # Find the two stops t sits between and interpolate.
        lo, hi = stops[0], stops[-1]
        for j in range(len(stops) - 1):
            if stops[j][0] <= t <= stops[j + 1][0]:
                lo, hi = stops[j], stops[j + 1]
                break
        span = (hi[0] - lo[0]) or 1.0
        f = (t - lo[0]) / span
        r = round(lo[1][0] + (hi[1][0] - lo[1][0]) * f)
        g = round(lo[1][1] + (hi[1][1] - lo[1][1]) * f)
        b = round(lo[1][2] + (hi[1][2] - lo[1][2]) * f)
        px[i, 0] = (r, g, b)
    # Rotate the 1-D strip 45 degrees and crop/resize isn't pixel-exact
    # to CSS's own 135deg math, and doesn't need to be (this is a card
    # background, not a data-bearing chart) - stretching the strip
    # diagonally via an affine paste reproduces the same "dark corner to
    # lighter corner" read at a fraction of the code.
    grad = strip.resize((n, n))
    grad = grad.rotate(-45, resample=Image.BICUBIC, expand=False)
    left = (grad.width - w) // 2
    top = (grad.height - h) // 2
    return grad.crop((left, top, left + w, top + h))


def _base_card():
    """The shared background + brand wordmark every card variant starts
    from (ticker cards, the blog card, and the site-default card all
    open a share preview with the same visual identity)."""
    img = _diagonal_gradient(CARD_W, CARD_H, [
        (0.0, _BG_TOP), (0.55, _BG_MID), (1.0, _BG_END),
    ])
    d = ImageDraw.Draw(img)
    d.text((56, 44), _BRAND_STOCKS, font=_font(40), fill=_TEXT)
    _w = d.textlength(_BRAND_STOCKS, font=_font(40))
    d.text((56 + _w, 44), _BRAND_DEEPDIVE, font=_font(40), fill=_TEAL)
    return img, d


def _fmt_date(generated_at):
    """"12 Sep 2026" from an ISO timestamp - the mock's own stamp
    format. Falls back to the first 10 raw characters (still a readable
    ISO date, "2026-09-12") on anything that doesn't parse rather than
    ever raising - a card is never worth a 500 over a date string."""
    if not generated_at:
        return ""
    try:
        dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y")
    except (ValueError, TypeError):
        return str(generated_at)[:10]


def _fmt_money(v):
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "–"


def render_ticker_card(ticker, public, generated_at, moat=None):
    """The mock's own AFTER card: brand, data-date stamp, ticker +
    company + type, valuation badge, the four numbers (price, fair
    value, value score, quality), the MOS bar, and the footer line -
    every field read from `public` (snapshot_store.public_view()'s own
    output - the SAME dict the research/deep-dive pages already read,
    so this card can never show a number the page itself doesn't). No
    field is treated as required: any missing value renders as "–"
    (an em dash) rather than raising - a partially-stale/incomplete
    snapshot still produces a card, just with a gap where a number would
    be, never a 500 (server.py's own /og/ route additionally wraps this
    whole call in a try/except -> render_default_card() belt-and-braces,
    per the instruction's own fail-safe requirement)."""
    img, d = _base_card()

    stamp = f"data: {_fmt_date(generated_at)}" if generated_at else ""
    if stamp:
        _sw = d.textlength(stamp, font=_font(22))
        d.text((CARD_W - 56 - _sw, 56), stamp, font=_font(22), fill=_FAINT)

    d.text((56, 148), ticker, font=_font(88), fill=_TEXT)

    company = public.get("company_name") or ""
    ctype = public.get("company_type") or ""
    name_line = " · ".join(x for x in (company, ctype) if x)
    if name_line:
        d.text((56, 256), name_line, font=_font(30), fill=_MUTED)

    label = (public.get("valuation_label") or "").strip()
    if label:
        badge_text = label.upper()
        _bf = _font(36)
        _bw = d.textlength(badge_text, font=_bf)
        bx1 = CARD_W - 56
        bx0 = bx1 - _bw - 44
        by0, by1 = 168, 168 + 56
        d.rounded_rectangle((bx0, by0, bx1, by1), radius=14, outline=_TEAL_DARK, width=4, fill=_BADGE_BG)
        d.text((bx0 + 22, by0 + 10), badge_text, font=_bf, fill=_TEAL)

    def _kv(x, k_text, v_text, v_color):
        d.text((x, 340), k_text, font=_font(24), fill=_MUTED)
        d.text((x, 366), v_text, font=_font(52), fill=v_color)

    price = public.get("price")
    fair_value = public.get("intrinsic_value")
    value_score = public.get("value_score")
    quality = public.get("quality")
    _kv(56, "Price", _fmt_money(price), _TEXT)
    _kv(322, "Fair value", _fmt_money(fair_value), _TEAL)
    _kv(588, "Value score",
        f"{value_score:.1f}" if isinstance(value_score, (int, float)) else "–", _GREEN)
    _kv(854, "Quality",
        f"{quality:.0f}" if isinstance(quality, (int, float)) else "–", _TEXT)

    mos_pct = public.get("mos_pct")
    if isinstance(mos_pct, (int, float)):
        d.text((56, 492), "Margin of safety", font=_font(24), fill=_MUTED)
        track_x0, track_x1 = 56, CARD_W - 56
        track_y0, track_y1 = 522, 522 + 32
        d.rounded_rectangle((track_x0, track_y0, track_x1, track_y1), radius=16, fill=_TRACK_BG)
        fill_frac = max(0.0, min(mos_pct, 100.0)) / 100.0
        if fill_frac > 0:
            fill_w = (track_x1 - track_x0) * fill_frac
            _fill_img = Image.new("RGB", (max(int(fill_w), 1), track_y1 - track_y0))
            _fd = ImageDraw.Draw(_fill_img)
            for i in range(_fill_img.width):
                t = i / max(_fill_img.width - 1, 1)
                r = round(_TEAL_DEEP[0] + (_GREEN[0] - _TEAL_DEEP[0]) * t)
                g = round(_TEAL_DEEP[1] + (_GREEN[1] - _TEAL_DEEP[1]) * t)
                b = round(_TEAL_DEEP[2] + (_GREEN[2] - _TEAL_DEEP[2]) * t)
                _fd.line([(i, 0), (i, _fill_img.height)], fill=(r, g, b))
            _mask = Image.new("L", _fill_img.size, 0)
            ImageDraw.Draw(_mask).rounded_rectangle(
                (0, 0, _fill_img.width - 1, _fill_img.height - 1), radius=16, fill=255)
            img.paste(_fill_img, (track_x0, track_y0), _mask)
        label_x = track_x0 + (track_x1 - track_x0) * min(fill_frac + 0.02, 0.92)
        d.text((label_x, track_y0 + 6), f"{mos_pct:+.0f}%", font=_font(24), fill=_GREEN)

    d.text((56, CARD_H - 46), FOOTER_TEXT, font=_font(22), fill=_FAINT)
    return _to_png_bytes(img)


def render_blog_card(title):
    """The blog-card variant (`/og/blog/{slug}.png`): brand + post title,
    no ticker-specific numbers - a blog post has none to show. `title`
    is wrapped across up to 3 lines at a generous width so a long
    headline never runs off the card."""
    img, d = _base_card()
    d.text((56, 140), "From the blog", font=_font(26), fill=_MUTED)

    words = (title or "StocksDeepDive").split()
    lines, cur = [], ""
    _tf = _font(56)
    for w in words:
        trial = f"{cur} {w}".strip()
        if d.textlength(trial, font=_tf) > CARD_W - 112 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    lines = lines[:3]
    y = 190
    for line in lines:
        d.text((56, y), line, font=_tf, fill=_TEXT)
        y += 68

    d.text((56, CARD_H - 46), FOOTER_TEXT, font=_font(22), fill=_FAINT)
    return _to_png_bytes(img)


def render_default_card():
    """The site-default card: used for the site's own default share
    preview, an unknown/never-scanned ticker, AND as the universal
    fail-safe any exception in render_ticker_card()/render_blog_card()
    falls back to (server.py's own /og/ routes wrap every call in
    try/except -> this function - see this Part's report for the exact
    call sites). Deliberately has NO external data dependency at all
    (no snapshot lookup, no title argument) so it cannot itself ever
    fail on missing/malformed data - the one card variant that is
    structurally impossible to 500."""
    img, d = _base_card()
    tagline = "Value investing with every number shown"
    d.text((56, 220), tagline, font=_font(44), fill=_TEXT)
    d.text((56, 290), "Fair value, quality, and psychology - for every stock, for free.",
           font=_font(26), fill=_MUTED)
    d.text((56, CARD_H - 46), FOOTER_TEXT, font=_font(22), fill=_FAINT)
    return _to_png_bytes(img)


def _to_png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
