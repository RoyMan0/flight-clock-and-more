"""
WorldClockPlugin — current time in any number of cities.

Layout (64×32, 4x6 BDF font, four 8 px rows, no header):
  Tokyo    +1  7:45A
  London      11:45P
The time is LIGHT_ORANGE while the sun is up in that city and LIGHT_BLUE
when it's down — the same colours as the clock/weather scene and the world
daylight map. Day/night comes from the real sun position (utilities/solar.py).
A dim "+1"/"-1" marks cities on a different date than the display's local date.

With more than four cities the list scrolls bottom-to-top like Breaking News,
looping for the plugin's display time.

Config (plugins.world_clock):
  cities        ordered list of {"id": "<cities.py id>", "label": "<optional name>"}
  scroll_speed  px/frame (10 fps) when scrolling
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from PIL import Image

from plugins.base_plugin import BasePlugin
from plugins.transit.abbrev import shorten
from plugins.transit.render import _FONT, _bdf_draw, _bdf_width, push_pil_to_canvas
from plugins.world_clock.cities import display_name, get_city
from utilities.solar import is_daylight

log = logging.getLogger(__name__)

MATRIX_W = 64
MATRIX_H = 32
CHAR_W = 4
ROW_H = 8
VISIBLE_ROWS = MATRIX_H // ROW_H       # 4
LOOP_GAP_H = 4                          # dashed divider where the scroll loops
LEFT_PAD = 1                            # 1 px margin on the left edge

COL_DAY     = (254, 167, 39)    # LIGHT_ORANGE — clock DAY_COLOUR
COL_NIGHT   = (40, 183, 246)    # LIGHT_BLUE   — clock NIGHT_COLOUR
COL_CITY    = (190, 190, 190)
COL_MARKER  = (110, 110, 110)
COL_DIVIDER = (50, 50, 50)

DEFAULT_SCROLL_SPEED = 1.0

# Compact glyphs for the day marker ("+1" is 6 px instead of 8 in 4x6), so
# longer city names still fit beside it. Rows top→bottom, 5 px tall.
_MINI = {
    "+": [".#.", "###", ".#."],
    "-": ["###"],
    "1": [".#", "##", ".#", ".#", ".#"],
    "2": ["##.", "..#", ".#.", "#..", "###"],
}


def _mini_width(text: str) -> int:
    return sum(len(_MINI[c][0]) + 1 for c in text if c in _MINI) - 1


def _mini_draw(img: Image.Image, x: int, y: int, text: str, color: tuple):
    px = img.load()
    for c in text:
        glyph = _MINI.get(c)
        if not glyph:
            continue
        top = y + (5 - len(glyph)) // 2          # centre short glyphs (+, -) vertically
        for r, line in enumerate(glyph):
            for k, bit in enumerate(line):
                if bit == "#" and 0 <= x + k < img.width and 0 <= top + r < img.height:
                    px[x + k, top + r] = color
        x += len(glyph[0]) + 1


# ------------------------------------------------------------------
# Rows and rendering (shared with the web preview)
# ------------------------------------------------------------------

def _clock_format() -> str:
    try:
        from core.config_manager import get_config
        return get_config().get("location", "clock_format", default="12hr") or "12hr"
    except Exception:
        return "12hr"


def _format_time(dt: datetime, fmt: str) -> str:
    if fmt == "24hr":
        return dt.strftime("%H:%M")
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d}{'A' if dt.hour < 12 else 'P'}"


def configured_cities(config: dict) -> list:
    """[(city dict, label)] for valid configured cities, in the user's order."""
    out = []
    for entry in config.get("cities") or []:
        cid = entry.get("id") if isinstance(entry, dict) else entry
        city = get_city(cid)
        if city:
            label = (entry.get("label") or "").strip() if isinstance(entry, dict) else ""
            out.append((city, label))
    return out


def build_rows(config: dict, now_utc: datetime = None, fmt: str = None) -> list:
    """One display row per configured city: name, time, day marker, day/night."""
    now_utc = now_utc or datetime.now(timezone.utc)
    fmt = fmt or _clock_format()
    local_date = now_utc.astimezone().date()
    rows = []
    for city, label in configured_cities(config):
        try:
            local = now_utc.astimezone(ZoneInfo(city["tz"]))
        except Exception as e:
            log.warning(f"[world_clock] Bad timezone {city['tz']}: {e}")
            continue
        diff = (local.date() - local_date).days
        rows.append({
            "name": label or display_name(city),
            "time": _format_time(local, fmt),
            "marker": f"{diff:+d}" if diff else "",
            "day": is_daylight(city["lat"], city["lon"], now_utc),
        })
    return rows


def _draw_row(img: Image.Image, y: int, row: dict):
    """Draw one city row with its 8 px cell starting at y."""
    ty = y + 1
    time_x = MATRIX_W - _bdf_width(row["time"], _FONT)
    _bdf_draw(img, time_x, ty, row["time"], _FONT, COL_DAY if row["day"] else COL_NIGHT)
    if row["marker"]:
        mx = time_x - 4 - _mini_width(row["marker"])   # clear gap so "+1" never reads as part of the time
        _mini_draw(img, mx, ty, row["marker"], COL_MARKER)
        max_chars = (mx - 1) // CHAR_W         # name ink ends ≥1 px before the marker
    else:
        max_chars = (time_x - 2) // CHAR_W     # ≥2 px before the time
    _bdf_draw(img, LEFT_PAD, ty, shorten(row["name"], max(1, max_chars)), _FONT, COL_CITY)


def render_board(rows: list) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    for k, row in enumerate(rows[:VISIBLE_ROWS]):
        _draw_row(img, k * ROW_H, row)
    return img


def build_strip(rows: list) -> Image.Image:
    """All rows stacked, then a faint dashed divider marking where the list loops."""
    h = len(rows) * ROW_H + LOOP_GAP_H
    img = Image.new("RGB", (MATRIX_W, h), (0, 0, 0))
    for k, row in enumerate(rows):
        _draw_row(img, k * ROW_H, row)
    px = img.load()
    dy = len(rows) * ROW_H + LOOP_GAP_H // 2 - 1
    for x in range(8, MATRIX_W - 8, 3):
        px[x, dy] = COL_DIVIDER
        px[x + 1, dy] = COL_DIVIDER
    return img


def render_strip_frame(strip: Image.Image, offset: int) -> Image.Image:
    """The 64×32 window of the strip at `offset`, wrapping around."""
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    sh = strip.height
    offset %= sh
    y = 0
    while y < MATRIX_H:
        part = strip.crop((0, offset, MATRIX_W, min(sh, offset + MATRIX_H - y)))
        img.paste(part, (0, y))
        y += part.height
        offset = 0
    return img


def needs_scroll(rows: list) -> bool:
    return len(rows) > VISIBLE_ROWS


# ------------------------------------------------------------------
# Plugin
# ------------------------------------------------------------------

class WorldClockPlugin(BasePlugin):
    PLUGIN_ID = "world_clock"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._y_offset = 0.0
        self._strip: Optional[Image.Image] = None
        self._key: Optional[tuple] = None

    @property
    def enabled(self) -> bool:
        # Off unless configured: installs from before this module have no config
        # block, and the update loop polls every enabled plugin even when it isn't
        # in plugin_order.
        return bool(self.config.get("enabled", False))

    def reset(self):
        self._y_offset = 0.0
        self._key = None

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        self._strip = None
        self.reset()

    def update(self):
        pass    # no network data — times are computed at draw time

    def has_content(self) -> bool:
        return bool(configured_cities(self.config))

    def _scroll_speed(self) -> float:
        try:
            return max(0.1, float(self.config.get("scroll_speed", DEFAULT_SCROLL_SPEED)))
        except (TypeError, ValueError):
            return DEFAULT_SCROLL_SPEED

    @property
    def display_duration(self) -> float:
        # When scrolling, stay at least long enough for one full pass
        base = float(self.config.get("display_duration", 30))
        n = len(configured_cities(self.config))
        if n > VISIBLE_ROWS:
            strip_h = n * ROW_H + LOOP_GAP_H
            return max(base, strip_h / (self._scroll_speed() * 10) + 1)
        return base

    def draw(self) -> bool:
        rows = build_rows(self.config)
        if not rows:
            return False
        key = tuple((r["name"], r["time"], r["marker"], r["day"]) for r in rows)

        if not needs_scroll(rows):
            if key != self._key:
                push_pil_to_canvas(render_board(rows), self.display_manager)
                self._key = key
            return True

        # Rebuild the strip only when a time or day/night value changes
        if key != self._key or self._strip is None:
            self._strip = build_strip(rows)
            self._key = key
        push_pil_to_canvas(render_strip_frame(self._strip, int(self._y_offset)), self.display_manager)
        self._y_offset = (self._y_offset + self._scroll_speed()) % self._strip.height
        return True
