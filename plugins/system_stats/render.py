"""
System Stats rendering (64×32, 4x6 BDF font, 1 px top/left margin).

Each machine is a section: a hostname line, then one 8 px row per stat:
  rows 0-4  text — label (coloured) · value (white) · temperature (teal → orange → red)
  row  5    gap
  row  6    1 px bar: % for CPU/GPU/MEM; for DSK/NET the combined read+write
            (down+up) rate on a log scale
  row  7    gap
One machine with 3 stats (8 + 3×8 = 32 px) is a static board; anything taller
is stacked into a strip that scrolls bottom-to-top like Breaking News.
"""

import math

from PIL import Image

from plugins.transit.render import _FONT, _bdf_draw, _bdf_width

MATRIX_W, MATRIX_H = 64, 32
LEFT = 1
ROW_H = 8
DIVIDER_H = 4
VALUE_RIGHT = 39            # right edge of the middle column (%, read/down)
TEMP_RIGHT = MATRIX_W       # right edge of the right column (temp, write/up)
BAR_W = MATRIX_W - LEFT

COL_HOST    = (160, 160, 160)
COL_VALUE   = (240, 240, 240)
COL_DIM     = (110, 110, 110)
COL_TRACK   = (34, 34, 34)
COL_OFFLINE = (230, 40, 40)
COL_DIVIDER = (50, 50, 50)
COL_TEMP_OK, COL_TEMP_WARM, COL_TEMP_HOT = (80, 220, 200), (255, 150, 0), (255, 50, 40)

STATS = {
    # key: (label, colour)
    "cpu":    ("CPU", (80, 220, 220)),
    "gpu":    ("GPU", (160, 160, 255)),
    "mem":    ("MEM", (250, 210, 120)),
    "uptime": ("UP",  (170, 170, 170)),
    "disk":   ("DSK", (255, 150, 60)),
    "net":    ("NET", (90, 220, 90)),
}
BAR_OFFSET = 6              # bar row within the 8 px stat row (row 5 is the gap under the text)

# Small solid triangles for network down/up (the font's arrows look like "+" on the panel)
_TRIANGLES = {"down": ["#####", ".###.", "..#.."], "up": ["..#..", ".###.", "#####"]}
STAT_ORDER = ("cpu", "gpu", "mem", "uptime", "disk", "net")

LOG_FLOOR = 1_000.0         # 1 KB/s → empty bar


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------

def fmt_rate(bps) -> str:
    """Bytes/s → at most 4 chars: 999B, 1.2K, 340K, 12M, 1.2G."""
    if bps is None:
        return "..."
    v = float(bps)
    for unit in ("B", "K", "M", "G", "T"):
        if v < 1000 or unit == "T":
            if unit == "B":
                return f"{int(v)}B"
            return f"{v:.1f}{unit}" if v < 10 else f"{int(v)}{unit}"
        v /= 1000.0
    return "?"


def fmt_uptime(secs) -> str:
    if secs is None:
        return "--"
    s = int(secs)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}d {h:02d}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def fmt_temp(c, units: str) -> str:
    if c is None:
        return ""
    if units == "imperial":
        return f"{round(c * 9 / 5 + 32)}\xb0F"
    return f"{round(c)}\xb0C"


def temp_colour(c) -> tuple:
    if c is None or c < 70:
        return COL_TEMP_OK
    return COL_TEMP_WARM if c < 85 else COL_TEMP_HOT


def log_frac(bps, full_scale) -> float:
    """1 KB/s → 0, full_scale → 1, logarithmic in between."""
    if not bps or bps <= LOG_FLOOR or not full_scale or full_scale <= LOG_FLOOR:
        return 0.0
    return max(0.0, min(1.0, math.log10(bps / LOG_FLOOR) / math.log10(full_scale / LOG_FLOOR)))


# ------------------------------------------------------------------
# Drawing
# ------------------------------------------------------------------

def _right(img, right_x, y, text, colour):
    _bdf_draw(img, right_x - _bdf_width(text, _FONT), y, text, _FONT, colour)


def _triangle(img, x, y, kind, colour):
    """3 px tall triangle, vertically centred on a text row starting at y."""
    px = img.load()
    for r, line in enumerate(_TRIANGLES[kind]):
        for c, bit in enumerate(line):
            if bit == "#":
                px[x + c, y + 1 + r] = colour


def _bar(img, y, frac, colour, height=1):
    px = img.load()
    filled = int(round(max(0.0, min(1.0, frac)) * BAR_W))
    for dy in range(height):
        for i in range(BAR_W):
            px[LEFT + i, y + dy] = colour if i < filled else COL_TRACK


def available_stats(conn: dict, stats: dict) -> list:
    """Stats this connection asked for that this machine can actually report."""
    want = conn.get("metrics") or {"cpu": True, "gpu": True, "mem": True}
    keys = [k for k in STAT_ORDER if want.get(k)]
    if stats is None:
        return keys
    have = {
        "cpu": stats.get("cpu_pct") is not None or stats.get("warming_up"),
        "gpu": stats.get("gpu_pct") is not None,
        "mem": stats.get("mem_pct") is not None,
        "uptime": stats.get("uptime_s") is not None,
        "disk": "disk" not in stats.get("notes", {}),
        "net": "net" not in stats.get("notes", {}),
    }
    return [k for k in keys if have.get(k)]


def draw_host_line(img, y, name: str, offline_msg: str = None):
    _bdf_draw(img, LEFT, y + 1, name[:15] if not offline_msg else name[:7], _FONT, COL_HOST)
    if offline_msg:
        _right(img, TEMP_RIGHT, y + 1, offline_msg, COL_OFFLINE)


def draw_stat_row(img, y, key: str, stats: dict, conn: dict, units: str):
    label, colour = STATS[key]
    _bdf_draw(img, LEFT, y, label, _FONT, colour)
    bar_y = y + BAR_OFFSET
    if key in ("cpu", "gpu", "mem"):
        pct = stats.get(f"{key}_pct")
        _right(img, VALUE_RIGHT, y, "..." if pct is None else f"{round(pct)}%", COL_VALUE)
        temp = stats.get(f"{key}_temp") if key != "mem" else None
        if temp is not None:
            _right(img, TEMP_RIGHT, y, fmt_temp(temp, units), temp_colour(temp))
        _bar(img, bar_y, (pct or 0) / 100.0, colour)
    elif key == "uptime":
        _right(img, TEMP_RIGHT, y, fmt_uptime(stats.get("uptime_s")), COL_VALUE)
    else:
        a, b = ("disk_r", "disk_w") if key == "disk" else ("net_rx", "net_tx")
        full = float(conn.get("disk_max" if key == "disk" else "net_max")
                     or (7e9 if key == "disk" else 1.25e9))
        for val_key, mark, right in ((a, "R" if key == "disk" else "down", VALUE_RIGHT),
                                     (b, "W" if key == "disk" else "up", TEMP_RIGHT)):
            text = fmt_rate(stats.get(val_key))
            _right(img, right, y, text, COL_VALUE)
            tx = right - _bdf_width(text, _FONT)
            if key == "disk":
                _bdf_draw(img, tx - _bdf_width(mark, _FONT) - 1, y, mark, _FONT, COL_DIM)
            else:
                _triangle(img, tx - 6, y, mark, COL_DIM)
        total = None if stats.get(a) is None else (stats.get(a) or 0) + (stats.get(b) or 0)
        _bar(img, bar_y, log_frac(total, full), colour)


def section_height(n_stats: int) -> int:
    return ROW_H * (1 + n_stats)


def build_sections(results: list, units: str) -> list:
    """results: [(conn, stats | None, error | None)] → [(height, draw_fn(img, y))]."""
    sections = []
    for conn, stats, error in results:
        name = (conn.get("name") or "").strip() or (stats or {}).get("host") or conn.get("host") or "This Pi"
        if error or stats is None:
            sections.append((ROW_H, lambda img, y, n=name: draw_host_line(img, y, n, "OFFLINE")))
            continue
        keys = available_stats(conn, stats)

        def draw(img, y, n=name, k=keys, s=stats, c=conn):
            draw_host_line(img, y, n)
            for i, key in enumerate(k):
                draw_stat_row(img, y + ROW_H * (i + 1), key, s, c, units)
        sections.append((section_height(len(keys)), draw))
    return sections


def needs_scroll(sections: list) -> bool:
    return len(sections) > 1 or (sections and sections[0][0] > MATRIX_H)


def render_static(sections: list) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    if sections:
        sections[0][1](img, 0)
    return img


def build_strip(sections: list) -> Image.Image:
    """Every section stacked, each followed by a faint dashed divider."""
    h = sum(sh + DIVIDER_H for sh, _ in sections)
    img = Image.new("RGB", (MATRIX_W, max(1, h)), (0, 0, 0))
    px = img.load()
    y = 0
    for sh, draw in sections:
        draw(img, y)
        dy = y + sh + DIVIDER_H // 2 - 1
        for x in range(8, MATRIX_W - 8, 3):
            px[x, dy] = COL_DIVIDER
            px[x + 1, dy] = COL_DIVIDER
        y += sh + DIVIDER_H
    return img


def render_strip_frame(strip: Image.Image, offset: int) -> Image.Image:
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
