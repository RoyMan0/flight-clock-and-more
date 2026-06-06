"""
EventCountdownPlugin — real-time event countdown on the 64×32 matrix.

Layout (all positions dynamic based on chosen title font height):
  Title:    orange text, largest font that fits in 64px
  Separator: 1px purple line
  Countdown: white text ("N DAYS / HH:MM:SS" or "HH:MM:SS")
  At event:  "NOW!" flashing in cycling colors
"""

import logging
import os
from datetime import datetime
from typing import Optional

from PIL import Image, ImageDraw

from plugins.base_plugin import BasePlugin

log = logging.getLogger(__name__)

MATRIX_W = 64
MATRIX_H = 32

COLOR_TITLE     = (255, 140, 0)    # orange
COLOR_SEP       = (128, 0, 255)    # purple
COLOR_COUNTDOWN = (255, 255, 255)  # white
COLOR_BLACK     = (0, 0, 0)

_NOW_COLORS = [
    (220, 0, 0),
    (255, 140, 0),
    (255, 230, 0),
    (255, 255, 255),
    (128, 0, 255),
    (0, 230, 230),
]
_NOW_FRAMES_PER_COLOR = 5

_BASE_DIR  = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_FONTS_DIR = os.path.join(_BASE_DIR, "fonts")

# ------------------------------------------------------------------
# BDF font renderer (copied from stock_ticker pattern)
# ------------------------------------------------------------------

def _parse_bdf(path: str) -> dict:
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
                    cur.update(bbw=int(p[1]), bbh=int(p[2]),
                               xoff=int(p[3]), yoff=int(p[4]))
                elif line == "BITMAP":
                    reading, rows = True, []
                elif line == "ENDCHAR" and reading:
                    cur["rows"] = rows
                    glyphs[cur["code"]] = cur
                    reading = False
                elif reading:
                    rows.append(int(line, 16))
    except Exception as e:
        log.warning(f"[countdown] BDF parse error ({path}): {e}")
    return glyphs


def _bdf_width(text: str, font: dict) -> int:
    return sum(font[ord(c)]["dw"] for c in text if ord(c) in font)


def _bdf_draw(img: Image.Image, x: int, y: int, text: str,
              font: dict, color: tuple):
    if not font:
        return
    px = img.load()
    iw, ih = img.size
    cx = x
    for ch in text:
        g = font.get(ord(ch))
        if g is None:
            cx += 4
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


# Load all candidate fonts once
_BDF_9x15 = _parse_bdf(os.path.join(_FONTS_DIR, "9x15.bdf"))
_BDF_7x13 = _parse_bdf(os.path.join(_FONTS_DIR, "7x13.bdf"))
_BDF_6x9  = _parse_bdf(os.path.join(_FONTS_DIR, "6x9.bdf"))
_BDF_5x7  = _parse_bdf(os.path.join(_FONTS_DIR, "5x7.bdf"))
_BDF_4x6  = _parse_bdf(os.path.join(_FONTS_DIR, "4x6.bdf"))

# (font_dict, cell_height) — largest to smallest
_TITLE_CANDIDATES = [
    (_BDF_7x13, 13),
    (_BDF_6x9,   9),
    (_BDF_5x7,   7),
    (_BDF_4x6,   6),
]


def _push_pil_to_canvas(img: Image.Image, display_manager):
    canvas = display_manager.canvas
    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            r, g, b = img.getpixel((x, y))
            canvas.SetPixel(x, y, r, g, b)
    with display_manager._lock:
        display_manager._pil_image.paste(img, (0, 0))


# ------------------------------------------------------------------
# Layout helpers
# ------------------------------------------------------------------

def _center_x(text: str, font: dict) -> int:
    return max(0, (MATRIX_W - _bdf_width(text, font)) // 2)


def _choose_title_font(title: str):
    """Return (bdf, cell_h) — largest font where title fits in MATRIX_W."""
    for bdf, cell_h in _TITLE_CANDIDATES:
        if _bdf_width(title, bdf) <= MATRIX_W:
            return bdf, cell_h
    return _BDF_4x6, 6


def _wrap_title(title: str, bdf: dict) -> list:
    """Split title into 1 or 2 lines so each fits in MATRIX_W."""
    if _bdf_width(title, bdf) <= MATRIX_W:
        return [title]
    # Try splitting at the last space before the midpoint
    words = title.split()
    best_split = 1
    for i in range(1, len(words)):
        line1 = " ".join(words[:i])
        line2 = " ".join(words[i:])
        if _bdf_width(line1, bdf) <= MATRIX_W and _bdf_width(line2, bdf) <= MATRIX_W:
            best_split = i
    line1 = " ".join(words[:best_split])
    line2 = " ".join(words[best_split:])
    return [line1, line2] if line2 else [line1]


def _parse_event_dt(dt_str: str) -> Optional[datetime]:
    """Parse the ISO string from the datetime-local input as naive local time."""
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None


def _format_countdown(remaining_secs: float, event_dt: Optional[datetime] = None):
    """Return (line1, line2) — line2 is empty string for single-line.

    Uses calendar-day comparison when event_dt is provided so an event
    later today always shows TODAY, not a raw day count.
    """
    total = int(max(0, remaining_secs))
    h = (total % 86400) // 3600
    m = (total % 3600) // 60
    s = total % 60
    time_str = f"{h:02d}:{m:02d}:{s:02d}"

    if event_dt is not None:
        today = datetime.now().date()
        days_diff = (event_dt.date() - today).days
        if days_diff <= 0:
            return ("TODAY", time_str)
        elif days_diff == 1:
            return ("1 DAY", time_str)
        else:
            return (f"{days_diff} DAYS", time_str)

    # Fallback when event_dt not provided
    d = total // 86400
    if d == 1:
        return ("1 DAY", time_str)
    elif d > 1:
        return (f"{d} DAYS", time_str)
    return (time_str, "")


# ------------------------------------------------------------------
# Frame renderer
# ------------------------------------------------------------------

def _render_frame(title: str, remaining_secs: float, flash_frame: int,
                   event_dt: Optional[datetime] = None) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), COLOR_BLACK)
    draw = ImageDraw.Draw(img)

    # --- Title font selection ---
    bdf, cell_h = _choose_title_font(title)
    lines = _wrap_title(title, bdf)

    TOP_PAD = 1

    # Two-line title uses 4x6 with extra height
    if len(lines) == 2:
        title_h = 6 + 1 + 6  # line1 + 1px gap + line2
    else:
        title_h = cell_h

    sep_y   = TOP_PAD + title_h + 1
    count_y = sep_y + 1
    count_h = MATRIX_H - count_y

    # --- Draw title ---
    if len(lines) == 2:
        _bdf_draw(img, _center_x(lines[0], bdf), TOP_PAD,     lines[0], bdf, COLOR_TITLE)
        _bdf_draw(img, _center_x(lines[1], bdf), TOP_PAD + 7, lines[1], bdf, COLOR_TITLE)
    else:
        _bdf_draw(img, _center_x(lines[0], bdf), TOP_PAD, lines[0], bdf, COLOR_TITLE)

    # --- Purple separator ---
    draw.line([(0, sep_y), (MATRIX_W - 1, sep_y)], fill=COLOR_SEP)

    # --- Countdown or NOW! ---
    if remaining_secs <= 0:
        # "NOW!" flashing animation
        color_idx = (flash_frame // _NOW_FRAMES_PER_COLOR) % len(_NOW_COLORS)
        color = _NOW_COLORS[color_idx]
        now_font = _BDF_9x15
        now_h = 15
        if _bdf_width("NOW!", now_font) > MATRIX_W:
            now_font = _BDF_7x13
            now_h = 13
        nx = _center_x("NOW!", now_font)
        ny = count_y + max(0, (count_h - now_h) // 2)
        _bdf_draw(img, nx, ny, "NOW!", now_font, color)
    else:
        line1, line2 = _format_countdown(remaining_secs, event_dt)
        if line2:
            # Two-line countdown: "N DAYS" + "HH:MM:SS" using 5x7
            font = _BDF_5x7
            fh   = 7
            total_count_h = fh + 1 + fh
            base_y = count_y + max(0, (count_h - total_count_h) // 2)
            _bdf_draw(img, _center_x(line1, font), base_y,     line1, font, COLOR_COUNTDOWN)
            _bdf_draw(img, _center_x(line2, font), base_y + 8, line2, font, COLOR_COUNTDOWN)
        else:
            font = _BDF_5x7
            fh   = 7
            cy   = count_y + max(0, (count_h - fh) // 2)
            _bdf_draw(img, _center_x(line1, font), cy, line1, font, COLOR_COUNTDOWN)

    return img


# ------------------------------------------------------------------
# Plugin class
# ------------------------------------------------------------------

class EventCountdownPlugin(BasePlugin):
    PLUGIN_ID = "event_countdown"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._render_key = None
        self._cached_frame: Optional[Image.Image] = None
        self._flash_frame: int = 0

    def reset(self):
        self._render_key = None
        self._cached_frame = None
        self._flash_frame = 0

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        self._render_key = None
        self._cached_frame = None

    def has_content(self) -> bool:
        title  = self.config.get("event_title", "").strip()
        dt_str = self.config.get("event_datetime", "").strip()
        return bool(title and dt_str)

    def update(self):
        pass  # all state computed from datetime.now() at draw time

    def draw(self) -> bool:
        title  = self.config.get("event_title", "").strip()
        dt_str = self.config.get("event_datetime", "").strip()
        if not title or not dt_str:
            return False

        event_dt = _parse_event_dt(dt_str)
        if event_dt is None:
            return False

        now = datetime.now() if event_dt.tzinfo is None else datetime.now().astimezone()
        remaining = (event_dt - now).total_seconds()

        if remaining <= 0:
            phase = (self._flash_frame // _NOW_FRAMES_PER_COLOR) % len(_NOW_COLORS)
            key = (0, phase)
            self._flash_frame += 1
        else:
            key = (int(remaining), -1)

        if key != self._render_key:
            self._cached_frame = _render_frame(title, remaining, self._flash_frame, event_dt)
            self._render_key = key

        if self._cached_frame is not None:
            _push_pil_to_canvas(self._cached_frame, self.display_manager)
        return True

    def is_cycle_complete(self) -> bool:
        return False
