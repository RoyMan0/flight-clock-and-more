"""
AI Usage rendering (64×32).

  y1-5    5×5 logo · "CLAUDE USAGE"          4x6 font (row 0 left as a margin)
  y7      separator
  y9      Current ................ 50%        4x6 font
  y15-16  2 px bar
  y18     Weekly ................. 11%
  y24-25  2 px bar
  y27-31  11:00PM ......... WED 8AM          tom-thumb (3×5) font

With the reset line off, the rows spread out and the bars get 3 px.
Bars run green → yellow → orange → red as usage rises (BAR_LEVELS).
"""

import os
import time
from datetime import datetime

from PIL import Image

from plugins.transit.render import _FONT, _FONTS_DIR, _bdf_draw, _bdf_width, _parse_bdf

MATRIX_W, MATRIX_H = 64, 32
LEFT = 1
RIGHT = MATRIX_W - 1        # right edge for right-aligned text (exclusive)
BAR_W = MATRIX_W - 2 * LEFT
HEADER_Y = 1                # 1 px margin above the title

_TINY = _parse_bdf(os.path.join(_FONTS_DIR, "tom-thumb.bdf"))

COL_LABEL   = (170, 170, 170)
COL_VALUE   = (240, 240, 240)
COL_RESET   = (120, 120, 120)
COL_SEP     = (60, 60, 60)
COL_TRACK   = (34, 34, 34)
COL_ERROR   = (230, 60, 40)

# (minimum %, colour) — highest matching threshold wins
BAR_LEVELS = (
    (0,  (60, 200, 90)),     # green
    (50, (240, 210, 40)),    # yellow
    (75, (255, 140, 30)),    # orange
    (90, (240, 50, 40)),     # red
)

PROVIDERS = {
    # id: (header text, header colour, logo colour, 5×5 logo)
    "claude": ("CLAUDE USAGE", (217, 119, 87), (217, 119, 87),
               ["#.#.#", ".###.", "#####", ".###.", "#.#.#"]),
    "codex":  ("CODEX USAGE", (230, 230, 230), (230, 230, 230),
               [".###.", "##.##", "#...#", "##.##", ".###."]),
}


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------

def bar_colour(pct) -> tuple:
    colour = BAR_LEVELS[0][1]
    for threshold, c in BAR_LEVELS:
        if (pct or 0) >= threshold:
            colour = c
    return colour


def _clock(dt: datetime, clock_format: str, minutes: bool, short_ampm: bool = False) -> str:
    if clock_format == "24hr":
        return dt.strftime("%H:%M")
    h = dt.hour % 12 or 12
    ampm = ("A" if dt.hour < 12 else "P") + ("" if short_ampm else "M")
    return f"{h}:{dt.minute:02d}{ampm}" if minutes else f"{h}{ampm}"


def time_labels(current_reset, weekly_reset, clock_format: str) -> tuple:
    """('11:00PM', 'WED 8AM'), dropping detail until both fit on one row."""
    cur = datetime.fromtimestamp(current_reset) if current_reset else None
    wk = datetime.fromtimestamp(weekly_reset) if weekly_reset else None
    left_opts = [_clock(cur, clock_format, True), _clock(cur, clock_format, cur.minute != 0),
                 _clock(cur, clock_format, cur.minute != 0, True)] if cur else ["--"]
    right_opts = [f"{wk:%a} {_clock(wk, clock_format, wk.minute != 0)}".upper(),
                  f"{wk:%a} {_clock(wk, clock_format, False, True)}".upper()] if wk else ["--"]
    for left in left_opts:
        for right in right_opts:
            if _bdf_width(left, _TINY) + _bdf_width(right, _TINY) + 4 <= MATRIX_W - 2 * LEFT:
                return left, right
    return left_opts[-1], right_opts[-1]


def countdown(reset, now=None) -> str:
    if not reset:
        return "--"
    secs = max(0, int(reset - (now or time.time())))
    d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def effective_pct(pct, reset, now=None):
    """A window whose reset time has passed has started over at 0 %."""
    if reset and reset <= (now or time.time()):
        return 0.0
    return pct


# ------------------------------------------------------------------
# Drawing
# ------------------------------------------------------------------

def _tiny(img, x, baseline, text, colour):
    """tom-thumb glyphs have per-glyph boxes, so place each by its baseline offset."""
    px = img.load()
    cx = x
    for ch in text:
        g = _TINY.get(ord(ch))
        if g is None:
            cx += 4
            continue
        top = baseline - g["bbh"] - g["yoff"] + 1
        bits = ((g["bbw"] + 7) // 8) * 8
        for ri, rv in enumerate(g["rows"]):
            for bi in range(g["bbw"]):
                if (rv >> (bits - 1 - bi)) & 1:
                    ox, oy = cx + g["xoff"] + bi, top + ri
                    if 0 <= ox < MATRIX_W and 0 <= oy < MATRIX_H:
                        px[ox, oy] = colour
        cx += g["dw"]


def _right(img, y, text, colour):
    _bdf_draw(img, RIGHT - _bdf_width(text, _FONT), y, text, _FONT, colour)


def _bar(img, y, pct, height):
    px = img.load()
    frac = max(0.0, min(1.0, (pct or 0) / 100.0))
    filled = int(round(frac * BAR_W))
    if pct and pct > 0 and filled == 0:
        filled = 1                          # any usage at all shows a sliver
    colour = bar_colour(pct)
    for dy in range(height):
        for i in range(BAR_W):
            px[LEFT + i, y + dy] = colour if i < filled else COL_TRACK


def _header(img, provider: str):
    text, text_col, logo_col, logo = PROVIDERS[provider]
    px = img.load()
    for r, line in enumerate(logo):
        for c, bit in enumerate(line):
            if bit == "#":
                px[LEFT + c, HEADER_Y + r] = logo_col
    _bdf_draw(img, LEFT + 7, HEADER_Y, text, _FONT, text_col)
    for x in range(MATRIX_W):
        px[x, 7] = COL_SEP


def _row(img, y, label, pct, bar_y, bar_h):
    _bdf_draw(img, LEFT, y, label, _FONT, COL_LABEL)
    _right(img, y, "--" if pct is None else f"{round(pct)}%", COL_VALUE)
    _bar(img, bar_y, pct, bar_h)


def render(provider: str, usage: dict, reset_format: str, clock_format: str,
           phase: int = 0, now=None) -> Image.Image:
    """phase picks times (0) or countdowns (1) when reset_format is 'both'."""
    now = now or time.time()
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    _header(img, provider)

    cur_reset, wk_reset = usage.get("current_reset"), usage.get("weekly_reset")
    cur = effective_pct(usage.get("current_pct"), cur_reset, now)
    wk = effective_pct(usage.get("weekly_pct"), wk_reset, now)

    if reset_format == "off":
        _row(img, 10, "Current", cur, 17, 3)
        _row(img, 22, "Weekly", wk, 29, 3)
        return img

    _row(img, 9, "Current", cur, 15, 2)
    _row(img, 18, "Weekly", wk, 24, 2)

    use_countdown = reset_format == "countdown" or (reset_format == "both" and phase == 1)
    if use_countdown:
        left, right = countdown(cur_reset, now), countdown(wk_reset, now)
    else:
        left, right = time_labels(cur_reset, wk_reset, clock_format)
    _tiny(img, LEFT, 31, left, COL_RESET)
    _tiny(img, RIGHT - _bdf_width(right, _TINY), 31, right, COL_RESET)
    return img


def render_message(provider: str, line1: str, line2: str = "") -> Image.Image:
    """Header plus an error/status message (e.g. LOGIN NEEDED)."""
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    _header(img, provider)
    _bdf_draw(img, (MATRIX_W - _bdf_width(line1, _FONT)) // 2, 13, line1, _FONT, COL_ERROR)
    if line2:
        _tiny(img, (MATRIX_W - _bdf_width(line2, _TINY)) // 2, 28, line2, COL_RESET)
    return img
