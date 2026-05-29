"""
WorldDaylightPlugin — 64×32 real-time world daylight map.

Shows day/night shading using solar terminator calculation, sun and moon
positions, and an optional time/temperature/date overlay in the top-right
corner matching the clock/weather plugin's color scheme.

No extra dependencies: solar and lunar positions are computed in pure Python
using simplified Astronomical Almanac / Jean Meeus formulas.
"""

import logging
import math
import os
import threading
from datetime import datetime, timezone
from typing import Optional, Tuple

from PIL import Image

from plugins.base_plugin import BasePlugin
from utilities.temperature import get_temperature_cached, get_forecast_cached
from config import NIGHT_START, NIGHT_END, CLOCK_FORMAT, TEMPERATURE_UNITS

log = logging.getLogger(__name__)

MATRIX_W = 64
MATRIX_H = 32

_FONTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "fonts"))
_ASSET_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets", "world_daylight"))
_LAND_MASK_PATH = os.path.join(_ASSET_DIR, "land_mask.png")

# ------------------------------------------------------------------
# Map colors (RGB tuples)
# ------------------------------------------------------------------
COL_DAY_LAND    = (200, 200, 195)   # light grey-white
COL_DAY_WATER   = (0,    72, 148)   # mid ocean blue
COL_NIGHT_LAND  = (22,   42,  78)   # dark blue-grey
COL_NIGHT_WATER = (4,    12,  32)   # near-black blue
COL_TWILIGHT    = (60,  105, 170)   # lighter blue band at terminator

# Sun and moon markers
COL_SUN  = (254, 167, 39)   # LIGHT_ORANGE — matches clock DAY_COLOUR
COL_MOON = (200, 200, 185)  # off-white/grey

# Text overlay colors (matching clock/weather plugin)
COL_TIME_DAY   = (254, 167,  39)   # LIGHT_ORANGE
COL_TIME_NIGHT = (40,  183, 246)   # LIGHT_BLUE
COL_DATE       = (188, 189, 188)   # GREY
COL_WHITE      = (255, 255, 255)
COL_DARK_BLUE  = (12,   70, 230)

# Twilight band half-width in degrees (civil twilight ≈ 6°)
_TWILIGHT_DEG = 6.0
_DAY_EDGE   = 90.0 - _TWILIGHT_DEG   # 84°
_NIGHT_EDGE = 90.0 + _TWILIGHT_DEG   # 96°

# ------------------------------------------------------------------
# Land mask (module-level; loaded once)
# ------------------------------------------------------------------
_land_mask: Optional[list] = None  # MATRIX_H × MATRIX_W list of bool


def _load_land_mask() -> list:
    global _land_mask
    if _land_mask is not None:
        return _land_mask
    try:
        img = Image.open(_LAND_MASK_PATH).convert("L")
        img = img.resize((MATRIX_W, MATRIX_H), Image.NEAREST)
        mask = []
        for y in range(MATRIX_H):
            row = []
            for x in range(MATRIX_W):
                row.append(img.getpixel((x, y)) > 127)
            mask.append(row)
        _land_mask = mask
        log.debug("[world_daylight] Land mask loaded")
    except Exception as e:
        log.warning(f"[world_daylight] Could not load land mask: {e} — using blank")
        _land_mask = [[False] * MATRIX_W for _ in range(MATRIX_H)]
    return _land_mask


# ------------------------------------------------------------------
# BDF pixel-font renderer (same as sports plugin)
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
        log.warning(f"[world_daylight] BDF parse error ({path}): {e}")
    return glyphs


def _bdf_text_width(text: str, font: dict, kern: dict = None) -> int:
    total = 0
    for c in text:
        if ord(c) in font:
            g = font[ord(c)]
            total += kern[c] if (kern and c in kern) else g["dw"]
    return total


def _bdf_draw(img: Image.Image, x: int, y: int, text: str, font: dict, color: tuple, kern: dict = None):
    """Stamp BDF glyphs directly into img pixels. y = top of character cell."""
    if not font:
        return
    px = img.load()
    iw, ih = img.size
    cx = x
    for ch in text:
        g = font.get(ord(ch))
        if g is None:
            cx += kern[ch] if (kern and ch in kern) else 4
            continue
        bbw = g["bbw"]
        total_bits = ((bbw + 7) // 8) * 8
        adv = kern[ch] if (kern and ch in kern) else g["dw"]
        # When advance is narrowed, shift rendering left to keep 1px margin each side
        x_shift = (g["dw"] - adv) // 2
        for ri, rv in enumerate(g["rows"]):
            py = y + ri
            if py < 0 or py >= ih:
                continue
            for bi in range(bbw):
                if (rv >> (total_bits - 1 - bi)) & 1:
                    ox = cx + bi - x_shift
                    if 0 <= ox < iw:
                        px[ox, py] = color
        cx += adv


# 4x6 BDF font — smallest readable font in the fonts directory
_BDF = _parse_bdf(os.path.join(_FONTS_DIR, "4x6.bdf"))
_BDF_H = 6   # character cell height

# Colon advance override: 1px margin each side instead of 2px
_TIME_KERN = {':': 2}


# ------------------------------------------------------------------
# Astronomical helpers
# ------------------------------------------------------------------

def _sun_geo_position(dt_utc: datetime) -> Tuple[float, float]:
    """Return (lat, lon) of sub-solar point (where sun is directly overhead)."""
    jd = dt_utc.timestamp() / 86400.0 + 2440587.5
    n  = jd - 2451545.0   # days from J2000.0

    # Mean longitude and anomaly
    L = (280.460 + 0.9856474 * n) % 360
    g = math.radians((357.528 + 0.9856003 * n) % 360)

    # Ecliptic longitude
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))

    # Obliquity of ecliptic
    eps = math.radians(23.439 - 0.0000004 * n)

    # Declination
    dec = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))

    # Right ascension
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))) % 360

    # Greenwich Mean Sidereal Time → GHA → geographic longitude
    # GHA is measured westward; geographic lon is eastward, so lon = -GHA
    gmst = (280.46061837 + 360.98564736629 * n) % 360
    gha  = (gmst - ra) % 360
    lon  = -gha if gha <= 180 else 360 - gha

    return dec, lon


def _moon_geo_position(dt_utc: datetime) -> Tuple[float, float]:
    """Return approximate (lat, lon) of sub-lunar point. ~2° accuracy — fine for 64×32."""
    jd = dt_utc.timestamp() / 86400.0 + 2440587.5
    n  = jd - 2451545.0

    L0  = (218.316 + 13.176396 * n) % 360                   # mean longitude
    M   = math.radians((134.963 + 13.064993 * n) % 360)     # mean anomaly
    F   = math.radians((93.272  + 13.229350 * n) % 360)     # argument of latitude

    # Ecliptic longitude and latitude (simplified)
    lam = math.radians(L0 + 6.289 * math.sin(M))
    bet = math.radians(5.128 * math.sin(F))
    eps = math.radians(23.4393)

    # Equatorial coordinates
    ra = math.degrees(math.atan2(
        math.sin(lam) * math.cos(eps) - math.tan(bet) * math.sin(eps),
        math.cos(lam),
    )) % 360
    dec = math.degrees(math.asin(
        math.sin(bet) * math.cos(eps) + math.cos(bet) * math.sin(eps) * math.sin(lam)
    ))

    # GHA → geographic longitude (GHA is westward, lon is eastward)
    gmst = (280.46061837 + 360.98564736629 * n) % 360
    gha  = (gmst - ra) % 360
    lon  = -gha if gha <= 180 else 360 - gha

    return dec, lon


def _angular_dist(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine angular distance in degrees between two geographic points."""
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlat   = math.radians(lat2 - lat1)
    dlon   = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2)
    return math.degrees(2 * math.asin(math.sqrt(max(0.0, min(1.0, a)))))


def _geo_to_pixel(lat: float, lon: float) -> Tuple[int, int]:
    """Convert geographic (lat, lon) to display pixel (x, y)."""
    x = int((lon + 180.0) * MATRIX_W / 360.0) % MATRIX_W
    y = int((90.0 - lat) * MATRIX_H / 180.0)
    y = max(0, min(MATRIX_H - 1, y))
    return x, y


def _blend_rgb(c1: tuple, c2: tuple, t: float) -> tuple:
    """Linear blend two RGB tuples; t=0 → c1, t=1 → c2."""
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


# ------------------------------------------------------------------
# Pixel-push helper (matches sports/snow_report pattern)
# ------------------------------------------------------------------

def _push_pil_to_canvas(img: Image.Image, display_manager):
    canvas = display_manager.canvas
    for y in range(MATRIX_H):
        for x in range(MATRIX_W):
            r, g, b = img.getpixel((x, y))
            canvas.SetPixel(x, y, r, g, b)
    with display_manager._lock:
        display_manager._pil_image.paste(img, (0, 0))


# ------------------------------------------------------------------
# Moon phase drawing
# ------------------------------------------------------------------

def _draw_moon_phase(img: Image.Image, mx: int, my: int, phase: int):
    """
    Draw a 5×5-pixel moon phase marker centered on (mx, my).
    phase: 0=new, 1=wax crescent, 2=first qtr, 3=wax gibbous,
           4=full, 5=wan gibbous, 6=last qtr, 7=wan crescent
    """
    r = 2
    DARK = (40, 40, 35)

    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dx * dx + dy * dy > r * r:
                continue
            px, py = mx + dx, my + dy
            if not (0 <= px < MATRIX_W and 0 <= py < MATRIX_H):
                continue

            if phase == 0:   # new moon — dim outline only
                on_edge = (dx * dx + dy * dy) >= (r * r - 1)
                color = (55, 55, 48) if on_edge else (0, 0, 0)
            elif phase == 4:  # full moon
                color = COL_MOON
            else:
                # Waxing (1-3): right side lit; waning (5-7): left side lit
                # Thresholds in pixel columns relative to center:
                # phase 1 / 7 (crescent ~25%): ±1 pixel from far edge
                # phase 2 / 6 (quarter  ~50%): center column boundary
                # phase 3 / 5 (gibbous  ~75%): ±1 pixel from near edge
                thresholds = {1: 1, 2: 0, 3: -1, 5: -1, 6: 0, 7: 1}
                threshold = thresholds.get(phase, 0)

                if phase in (1, 2, 3):
                    lit = dx >= threshold
                else:
                    lit = dx <= threshold

                color = COL_MOON if lit else DARK

            img.putpixel((px, py), color)


# ------------------------------------------------------------------
# Sun marker drawing
# ------------------------------------------------------------------

def _draw_sun(img: Image.Image, sx: int, sy: int):
    """Draw a small + cross sun marker."""
    for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
        px, py = sx + dx, sy + dy
        if 0 <= px < MATRIX_W and 0 <= py < MATRIX_H:
            img.putpixel((px, py), COL_SUN)


# ------------------------------------------------------------------
# Night-mode helper (matches ClockScene logic)
# ------------------------------------------------------------------

def _is_local_night() -> bool:
    """Return True if local time falls in the configured night window."""
    try:
        night_start = datetime.strptime(NIGHT_START, "%H:%M").time()
        night_end   = datetime.strptime(NIGHT_END,   "%H:%M").time()
        now         = datetime.now().time()
        if night_start <= night_end:
            return night_start <= now < night_end
        else:
            return now >= night_start or now < night_end
    except Exception:
        return False


# ------------------------------------------------------------------
# Temperature text color (matches TemperatureScene gradient)
# ------------------------------------------------------------------

def _temp_color(humidity: Optional[float]) -> tuple:
    if humidity is None:
        return COL_WHITE
    t = max(0.0, min(1.0, humidity / 100.0))
    return _blend_rgb(COL_WHITE, COL_DARK_BLUE, t)


# ------------------------------------------------------------------
# Core render function
# ------------------------------------------------------------------

def _render_frame(
    dt_local: datetime,
    land_mask: list,
    moon_phase: Optional[int],
    temperature: Optional[float],
    humidity: Optional[float],
    show_time: bool,
    show_date: bool,
    show_temp: bool,
) -> Image.Image:

    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))

    dt_utc = dt_local.astimezone(timezone.utc)

    sun_lat, sun_lon   = _sun_geo_position(dt_utc)
    moon_lat, moon_lon = _moon_geo_position(dt_utc)

    # ------------------------------------------------------------------
    # Paint each pixel based on day/night and land/water
    # ------------------------------------------------------------------
    for y in range(MATRIX_H):
        lat = 90.0 - (y + 0.5) * (180.0 / MATRIX_H)
        for x in range(MATRIX_W):
            lon  = (x + 0.5) * (360.0 / MATRIX_W) - 180.0
            dist = _angular_dist(lat, lon, sun_lat, sun_lon)
            is_land = land_mask[y][x]

            if dist < _DAY_EDGE:
                color = COL_DAY_LAND if is_land else COL_DAY_WATER
            elif dist > _NIGHT_EDGE:
                color = COL_NIGHT_LAND if is_land else COL_NIGHT_WATER
            else:
                # Smooth twilight gradient
                t = (dist - _DAY_EDGE) / (_NIGHT_EDGE - _DAY_EDGE)
                day_col   = COL_DAY_LAND   if is_land else COL_DAY_WATER
                night_col = COL_NIGHT_LAND if is_land else COL_NIGHT_WATER
                color = _blend_rgb(day_col, night_col, t)
                # Add a hint of the terminator color near the midpoint
                if 0.3 < t < 0.7:
                    w = 1.0 - abs(t - 0.5) / 0.2
                    color = _blend_rgb(color, COL_TWILIGHT, w * 0.4)

            img.putpixel((x, y), color)

    # ------------------------------------------------------------------
    # Info overlay (top-right corner) — done BEFORE sun/moon so markers
    # always render on top and are never darkened or covered by text.
    # ------------------------------------------------------------------
    any_info = show_time or show_date or show_temp
    if any_info and _BDF:
        night = _is_local_night()
        time_col = COL_TIME_NIGHT if night else COL_TIME_DAY
        temp_col = _temp_color(humidity)

        # Build strings first so background can be sized to actual content (1px padding each side)
        time_str = date_str = temp_str = None
        if show_time:
            fmt = "%l:%M%p" if CLOCK_FORMAT == "12hr" else "%H:%M"
            time_str = dt_local.strftime(fmt).strip().replace("AM", "A").replace("PM", "P")
        if show_date:
            date_str = dt_local.strftime("%-m/%-d")
        if show_temp and temperature is not None:
            unit = "F" if TEMPERATURE_UNITS == "imperial" else "C"
            temp_str = f"{round(temperature)}\xb0{unit}"

        entries = []
        if time_str: entries.append((time_str, _TIME_KERN, time_col))
        if date_str: entries.append((date_str, None,       COL_DATE))
        if temp_str: entries.append((temp_str, None,       temp_col))

        n_rows = len(entries)
        overlay_h = 1 + n_rows * (_BDF_H + 1)
        min_tx = min(MATRIX_W - _bdf_text_width(s, _BDF, k) - 1 for s, k, _ in entries)
        _darken_region(img, max(0, min_tx - 1), 0, MATRIX_W, overlay_h)

        row_y = 1
        for text, kern, col in entries:
            tw = _bdf_text_width(text, _BDF, kern)
            _bdf_draw(img, MATRIX_W - tw - 1, row_y, text, _BDF, col, kern)
            row_y += _BDF_H + 1

    # ------------------------------------------------------------------
    # Sun marker — drawn last so it's always visible on top of everything
    # ------------------------------------------------------------------
    sx, sy = _geo_to_pixel(sun_lat, sun_lon)
    _draw_sun(img, sx, sy)

    # ------------------------------------------------------------------
    # Moon marker — drawn last so it's always visible on top of everything
    # ------------------------------------------------------------------
    mx, my = _geo_to_pixel(moon_lat, moon_lon)
    phase = moon_phase if moon_phase is not None else 4
    _draw_moon_phase(img, mx, my, phase)

    return img


def _darken_region(img: Image.Image, x0: int, y0: int, x1: int, y1: int, factor: float = 0.35):
    """Darken a rectangular region by multiplying pixel values."""
    for y in range(y0, min(y1, MATRIX_H)):
        for x in range(x0, min(x1, MATRIX_W)):
            r, g, b = img.getpixel((x, y))
            img.putpixel((x, y), (int(r * factor), int(g * factor), int(b * factor)))


# ------------------------------------------------------------------
# Plugin class
# ------------------------------------------------------------------

class WorldDaylightPlugin(BasePlugin):
    PLUGIN_ID = "world_daylight"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._lock = threading.Lock()
        self._temperature: Optional[float] = None
        self._humidity: Optional[float] = None
        self._moon_phase: Optional[int] = None
        self._data_version: int = 0
        self._last_render_key = None          # (truncated_minute, data_version)
        self._rendered_img: Optional[Image.Image] = None
        _load_land_mask()

    def reset(self):
        self._last_render_key = None      # force re-render on next draw

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        self._last_render_key = None      # overlay options may have changed

    def update(self):
        temp, hum = get_temperature_cached()
        phase = None
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            for day in get_forecast_cached():
                if day.get("startTime", "")[:10] == today:
                    phase = int(day["values"]["moonPhase"])
                    break
        except Exception as e:
            log.debug(f"[world_daylight] Moon phase fetch error: {e}")

        with self._lock:
            self._temperature = temp
            self._humidity    = hum
            self._moon_phase  = phase
            self._data_version += 1

    def draw(self) -> bool:
        with self._lock:
            temp    = self._temperature
            hum     = self._humidity
            phase   = self._moon_phase
            ver     = self._data_version

        now = datetime.now()
        render_key = (now.replace(second=0, microsecond=0), ver)

        if render_key != self._last_render_key:
            mask = _load_land_mask()
            img = _render_frame(
                dt_local  = now,
                land_mask = mask,
                moon_phase  = phase,
                temperature = temp,
                humidity    = hum,
                show_time = bool(self.config.get("show_time", True)),
                show_date = bool(self.config.get("show_date", True)),
                show_temp = bool(self.config.get("show_temp", True)),
            )
            self._rendered_img   = img
            self._last_render_key = render_key

        if self._rendered_img is not None:
            _push_pil_to_canvas(self._rendered_img, self.display_manager)
        return True
