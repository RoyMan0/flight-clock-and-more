"""
Station-sign rendering for the transit plugin.

Layout (64×32, 4x6 BDF font, 4 px per character):
  y0      header (red):  Ln | Dest | Min
  y9/17/25  three arrival rows

The Ln column width adapts to the configured lines: a colour block for rail
lines that are identified by colour (CTA), or up to 4 characters of text
in the line colour (MTA "A", RTD "W", buses "133"). Dest gets whatever is left.
"""

import logging
import math
import os
import time

from PIL import Image

from plugins.transit.abbrev import shorten

log = logging.getLogger(__name__)

MATRIX_W = 64
MATRIX_H = 32
CHAR_W = 4
ROW_Y = (9, 17, 25)
MIN_COL_W = 3 * CHAR_W          # "Due", "Dly", "12", "99+"
MAX_LABEL_CHARS = 4
LEFT_PAD = 1                    # 1 px margin on the left edge
HEADER_Y = 1                    # 1 px margin above the header

COL_HEADER = (200, 20, 0)
COL_TEXT   = (255, 150, 0)
COL_DUE    = (255, 210, 0)
COL_MISSED = (90, 90, 90)       # arrivals sooner than the walk time

_FONTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "fonts"))


# ------------------------------------------------------------------
# BDF pixel-font renderer (same helpers as the sports/news plugins)
# ------------------------------------------------------------------

def _parse_bdf(path: str) -> dict:
    """Parse BDF font file → {char_code: {dw, bbw, bbh, rows}}."""
    glyphs: dict = {}
    cur: dict = {}
    rows: list = []
    reading = False
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ENCODING"):
                    cur = {"code": int(line.split()[1])}
                elif line.startswith("DWIDTH"):
                    cur["dw"] = int(line.split()[1])
                elif line.startswith("BBX"):
                    p = line.split()
                    cur.update(bbw=int(p[1]), bbh=int(p[2]), xoff=int(p[3]), yoff=int(p[4]))
                elif line == "BITMAP":
                    reading, rows = True, []
                elif line == "ENDCHAR" and reading:
                    cur["rows"] = rows
                    glyphs[cur["code"]] = cur
                    reading = False
                elif reading:
                    rows.append(int(line, 16))
    except Exception as e:
        log.warning(f"[transit] BDF parse error ({path}): {e}")
    return glyphs


def _bdf_width(text: str, font: dict) -> int:
    return sum(font[ord(c)]["dw"] for c in text if ord(c) in font)


def _bdf_draw(img: Image.Image, x: int, y: int, text: str, font: dict, color: tuple):
    """Stamp BDF glyphs directly into img pixels. y = top of character cell."""
    if not font:
        return
    px = img.load()
    iw, ih = img.size
    cx = x
    for ch in text:
        g = font.get(ord(ch))
        if g is None:
            cx += CHAR_W
            continue
        bbw = g["bbw"]
        total_bits = ((bbw + 7) // 8) * 8
        for ri, rv in enumerate(g["rows"]):
            py = y + ri
            if py < 0 or py >= ih:
                continue
            for bi in range(bbw):
                if (rv >> (total_bits - 1 - bi)) & 1:
                    ox = cx + bi
                    if 0 <= ox < iw:
                        px[ox, py] = color
        cx += g["dw"]


_FONT = _parse_bdf(os.path.join(_FONTS_DIR, "4x6.bdf"))


def push_pil_to_canvas(img: Image.Image, display_manager):
    canvas = display_manager.canvas
    if hasattr(canvas, "SetImage"):
        canvas.SetImage(img)            # one C call on the Pi; mirrored for the web preview
        return
    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            r, g, b = img.getpixel((x, y))
            canvas.SetPixel(x, y, r, g, b)


# ------------------------------------------------------------------
# Layout
# ------------------------------------------------------------------

def led_color(rgb: tuple) -> tuple:
    """Make an agency brand colour readable on an LED panel."""
    if not rgb:
        return (255, 255, 255)
    peak = max(rgb)
    if peak < 40:
        return (255, 255, 255)          # near-black (e.g. MTA's grey/black lines)
    if peak < 160:
        scale = 160 / peak
        return tuple(min(255, int(c * scale)) for c in rgb)
    return tuple(rgb)


def _dim(rgb: tuple, f: float = 0.5) -> tuple:
    return tuple(int(c * f) for c in rgb)


def label_column_width(arrivals) -> int:
    """Ln column width in px: wide enough for the header and every line label."""
    w = 2 * CHAR_W                      # "Ln"
    for a in arrivals:
        if not a.line_block:
            w = max(w, CHAR_W * len(a.line_label[:MAX_LABEL_CHARS]))
    return w


def dest_x_for(ln_w: int) -> int:
    return LEFT_PAD + ln_w + 2


def dest_max_chars(ln_w: int) -> int:
    return (MATRIX_W - MIN_COL_W - 1 - dest_x_for(ln_w)) // CHAR_W


def minutes_text(a, now: float) -> str:
    mins = math.floor((a.arrival_ts - now) / 60)
    if a.delayed:
        return "Dly"
    if a.approaching or mins <= 1:
        return "Due"
    return "99+" if mins > 99 else str(mins)


def build_rows(arrivals, now: float = None, alias_set: str = "",
               user_aliases: dict = None, ln_w: int = None):
    """Turn Arrival objects into display rows. Returns (ln_w, rows)."""
    now = now or time.time()
    if ln_w is None:
        ln_w = label_column_width(arrivals)
    max_chars = dest_max_chars(ln_w)
    rows = []
    for a in arrivals[:len(ROW_Y)]:
        rows.append({
            "label": a.line_label[:MAX_LABEL_CHARS],
            "color": led_color(a.line_color),
            "block": a.line_block,
            "dest_full": a.dest,
            "dest": shorten(a.dest, max_chars, a.alias_set or alias_set, user_aliases),
            "min": minutes_text(a, now),
            "realtime": a.realtime,
            "missable": a.missable,
        })
    return ln_w, rows


HEADER_MAX_CHARS = (MATRIX_W - MIN_COL_W - 2) // CHAR_W

# Scroll mode: fixed header row, schedule scrolls in rows 8–31
SCROLL_Y = 8
SCROLL_H = MATRIX_H - SCROLL_Y
LINE_H = 8                       # 6 px glyph cell + 2 px gap
COL_DIVIDER = (50, 50, 50)


def _draw_header(img: Image.Image, ln_w: int, title: str = None):
    if title:
        _bdf_draw(img, LEFT_PAD, HEADER_Y, title[:HEADER_MAX_CHARS], _FONT, COL_HEADER)
    else:
        _bdf_draw(img, LEFT_PAD, HEADER_Y, "Ln", _FONT, COL_HEADER)
        _bdf_draw(img, dest_x_for(ln_w), HEADER_Y, "Dest", _FONT, COL_HEADER)
    _bdf_draw(img, MATRIX_W - _bdf_width("Min", _FONT), HEADER_Y, "Min", _FONT, COL_HEADER)


def _draw_row(img: Image.Image, y: int, ln_w: int, row: dict):
    px = img.load()
    if row.get("missable"):
        text_col = COL_MISSED
    else:
        text_col = COL_TEXT if row["realtime"] else _dim(COL_TEXT, 0.55)
    if row["block"]:
        for by in range(y, y + 5):
            for bx in range(LEFT_PAD, LEFT_PAD + ln_w - 2):
                px[bx, by] = row["color"]
    else:
        _bdf_draw(img, LEFT_PAD, y, row["label"], _FONT, row["color"])
    _bdf_draw(img, dest_x_for(ln_w), y, row["dest"], _FONT, text_col)
    m = row["min"]
    mcol = COL_DUE if m == "Due" and not row.get("missable") else text_col
    _bdf_draw(img, MATRIX_W - _bdf_width(m, _FONT), y, m, _FONT, mcol)


def render_board(ln_w: int, rows: list, title: str = None) -> Image.Image:
    """`title` replaces the "Ln  Dest" header (used to name the stop when cycling stops)."""
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    _draw_header(img, ln_w, title)
    for row, y in zip(rows, ROW_Y):
        _draw_row(img, y, ln_w, row)
    return img


def build_scroll_strip(ln_w: int, blocks: list) -> Image.Image:
    """
    One tall image for scroll mode: each stop's rows, with a faint divider
    between stops. blocks: [(title, rows)] (title unused on the panel). Loops seamlessly.
    """
    heights = [LINE_H * len(rows) + 3 for _t, rows in blocks]
    img = Image.new("RGB", (MATRIX_W, max(1, sum(heights))), (0, 0, 0))
    px = img.load()
    y = 0
    for (_title, rows), h in zip(blocks, heights):
        for k, row in enumerate(rows):
            _draw_row(img, y + LINE_H * k, ln_w, row)
        div_y = y + h - 2
        for x in range(8, MATRIX_W - 8, 3):
            px[x, div_y] = COL_DIVIDER
            px[x + 1, div_y] = COL_DIVIDER
        y += h
    return img


def render_scroll_frame(ln_w: int, strip: Image.Image, offset: int) -> Image.Image:
    """Fixed header row + the strip scrolled to `offset`, wrapping around."""
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    _draw_header(img, ln_w)
    sh = strip.height
    offset %= sh
    y = SCROLL_Y
    while y < MATRIX_H:
        part = strip.crop((0, offset, MATRIX_W, min(sh, offset + MATRIX_H - y)))
        img.paste(part, (0, y))
        y += part.height
        offset = 0
    return img
