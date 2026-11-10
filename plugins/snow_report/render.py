"""
Snow Report rendering (64×32, 4x6 BDF font).

Card (one resort):
  y1-6    resort name; status dot top-right (green open, red closed)
  y7      grey separator
  left    today's weather icon (10×10, the clock module's icons) + high / low
  right   x16-63: three 8 px stat lines — NEW, BASE, RUNS, LIFTS (runs/lifts get
          a 1 px "% open" bar under them, and RUNS flips between 12/147 and 8%).
          More lines than fit become further pages; the last page is a 3-day
          forecast (day, icon, snow or high temp).
  Closed resorts: CLOSED / OPENS NOV 11 (+ new snow), then the forecast.

Rows (all resorts, like transit): header with column labels, one 8 px row per
resort (short name + up to 3 numbers: new, base, runs % or lifts), scrolling
bottom-to-top under the fixed header when there are more than three.
"""

import os
from datetime import date

from PIL import Image

from plugins.transit.render import _bdf_draw, _bdf_width, _parse_bdf
from utilities.paths import APP_ROOT

MATRIX_W, MATRIX_H = 64, 32
CHAR_W = 4
LINE_Y = (9, 17, 25)
LINE_H = 8
RIGHT_X = 16
ICON_X = 2
SEPARATOR_Y = 7
BODY_Y = 8

COL_NAME = (230, 230, 230)
COL_SEP = (60, 60, 60)
COL_LABEL = (110, 110, 110)
COL_NEW = (90, 190, 255)
COL_POWDER = (255, 255, 255)
COL_BASE = (230, 230, 230)
COL_OPEN = (0, 200, 90)
COL_TRACK = (45, 45, 45)
COL_CLOSED = (220, 40, 30)
COL_OPENS = (255, 150, 0)
COL_HIGH = (255, 170, 60)
COL_LOW = (100, 160, 255)
COL_DAY = (160, 160, 160)
COL_DOT_OPEN = (0, 220, 60)

_FONT = _parse_bdf(os.path.join(APP_ROOT, "fonts", "4x6.bdf"))

# OnTheSnow weather "type" → Tomorrow.io icon code (icons/<code>.png, as the clock uses)
_ICON_RULES = (
    ("THUNDER", "8000"), ("HAIL", "7000"), ("ICE", "7000"), ("SLEET", "6000"), ("FREEZ", "6000"),
    ("HEAVY_SNOW", "5101"), ("LIGHT_SNOW", "5100"), ("FLURR", "5001"), ("SNOW", "5000"),
    ("DRIZZLE", "4000"), ("LIGHT_RAIN", "4200"), ("HEAVY_RAIN", "4201"), ("RAIN", "4001"), ("SHOWER", "4000"),
    ("FOG", "2000"), ("MIST", "2000"), ("HAZE", "2000"),
    ("MOSTLY_CLOUD", "1102"), ("OVERCAST", "1001"), ("CLOUDY", "1001"),
    ("PARTLY", "1101"), ("SLIGHTLY_CLOUD", "1101"), ("MOSTLY_SUN", "1100"), ("MOSTLY_CLEAR", "1100"),
    ("SUN", "1000"), ("CLEAR", "1000"),
)
_icons: dict = {}


def icon_for(wtype: str):
    t = (wtype or "").upper()
    code = next((c for k, c in _ICON_RULES if k in t), "1101")
    if code not in _icons:
        try:
            _icons[code] = Image.open(os.path.join(APP_ROOT, "icons", f"{code}.png")).convert("RGB")
        except OSError:
            _icons[code] = None
    return _icons[code]


# ------------------------------------------------------------------
# Units
# ------------------------------------------------------------------

def depth(cm, units: str, unit_mark: bool = True) -> str:
    if cm is None:
        return "--"
    if units == "metric":
        return str(round(cm)) + ("cm" if unit_mark else "")
    return str(round(cm / 2.54)) + ('"' if unit_mark else "")


def temp(c, units: str) -> str:
    if c is None:
        return ""
    return f"{round(c if units == 'metric' else c * 9 / 5 + 32)}°"


def is_powder(cm) -> bool:
    return cm is not None and cm >= 15                  # ~6" — a powder day


def opens_text(d: str) -> str:
    try:
        dt = date.fromisoformat(d)
    except (TypeError, ValueError):
        return ""
    return f"OPENS {dt.strftime('%b').upper()} {dt.day}"


# ------------------------------------------------------------------
# Drawing helpers
# ------------------------------------------------------------------

def _text(img, x, y, text, colour):
    _bdf_draw(img, x, y, text, _FONT, colour)


def _right(img, right_x, y, text, colour):
    _text(img, right_x - _bdf_width(text, _FONT) + 1, y, text, colour)


def _centre(img, cx, y, text, colour):
    """Draw text centred on column cx by its lit pixels (the ° glyph's blank right side
    would otherwise pull short temperatures off-centre)."""
    w = _bdf_width(text, _FONT)
    if w <= 0:
        return
    tmp = Image.new("RGB", (w + 2, 6), (0, 0, 0))
    _bdf_draw(tmp, 0, 0, text, _FONT, colour)
    box = tmp.getbbox()
    if not box:
        return
    ink = tmp.crop((box[0], 0, box[2], 6))
    x = int(round(cx - (ink.width - 1) / 2))
    region = img.crop((x, y, x + ink.width, y + 6))
    mask = ink.convert("L").point(lambda v: 255 if v else 0)
    region.paste(ink, (0, 0), mask)
    img.paste(region, (x, y))


def fit(text: str, max_px: int) -> str:
    text = text or ""
    while text and _bdf_width(text, _FONT) > max_px:
        text = text[:-1]
    return text.rstrip()


def _bar(img, x0, x1, y, frac):
    px = img.load()
    filled = x0 + int(round(max(0.0, min(1.0, frac)) * (x1 - x0 + 1)))
    for x in range(x0, x1 + 1):
        px[x, y] = COL_OPEN if x < filled else COL_TRACK


def draw_header(img, name: str, is_open: bool):
    _text(img, 1, 1, fit(name, MATRIX_W - 6), COL_NAME)
    px = img.load()
    for dx in (0, 1):
        for dy in (0, 1):
            px[61 + dx, 2 + dy] = COL_DOT_OPEN if is_open else COL_CLOSED
    for x in range(MATRIX_W):
        px[x, SEPARATOR_Y] = COL_SEP


# ------------------------------------------------------------------
# Card pages
# ------------------------------------------------------------------

def stat_lines(rep: dict, metrics: dict, windows, units: str) -> list:
    """Stat lines for the right column: [(kind, args)] in display order."""
    lines = []
    if not rep.get("open"):
        lines.append(("closed", {}))
        if rep.get("opening_date") and rep["opening_date"] >= date.today().isoformat():
            lines.append(("opens", {"text": opens_text(rep["opening_date"])}))
    if metrics.get("new", True):
        for w in ([windows] if isinstance(windows, str) else windows):
            lines.append(("new", {"cm": (rep.get("new_cm") or {}).get(w), "window": w}))
    if rep.get("open"):
        if metrics.get("base", True) and (rep.get("base_cm") is not None or rep.get("summit_cm") is not None):
            lines.append(("base", {}))
        if metrics.get("runs", True) and rep.get("runs_total"):
            lines.append(("runs", {}))
        if metrics.get("lifts", True) and rep.get("lifts_total"):
            lines.append(("lifts", {}))
    return lines


def _draw_line(img, y, kind, args, rep, window, units, frame):
    if kind == "closed":
        _text(img, RIGHT_X, y, "CLOSED", COL_CLOSED)
    elif kind == "opens":
        _text(img, RIGHT_X, y, fit(args["text"], MATRIX_W - RIGHT_X), COL_OPENS)
    elif kind == "new":
        _text(img, RIGHT_X, y, "NEW", COL_LABEL)
        _text(img, RIGHT_X + 14, y, f"{args['window']}H", (70, 70, 70))
        cm = args["cm"]
        _right(img, 63, y, depth(cm or 0, units), COL_POWDER if is_powder(cm) else COL_NEW)
    elif kind == "base":
        _text(img, RIGHT_X, y, "BASE", COL_LABEL)
        b, s = rep.get("base_cm"), rep.get("summit_cm")
        if b is not None and s is not None and round(s) != round(b):
            val = f"{depth(b, units, False)}-{depth(s, units)}"
            if _bdf_width(val, _FONT) > 63 - RIGHT_X - 18:
                val = depth(b, units)
        else:
            val = depth(b if b is not None else s, units)
        _right(img, 63, y, val, COL_BASE)
    elif kind in ("runs", "lifts"):
        op, tot = (rep.get("runs_open"), rep.get("runs_total")) if kind == "runs" else \
                  (rep.get("lifts_open"), rep.get("lifts_total"))
        op = op or 0
        pct = rep.get("runs_pct") if kind == "runs" and rep.get("runs_pct") is not None else \
            (100 * op / tot if tot else 0)
        _text(img, RIGHT_X, y, "RUNS" if kind == "runs" else "LIFT", COL_LABEL)
        # Runs flip between the count and the percentage every 3 s
        show_pct = kind == "runs" and (frame // 30) % 2 == 1
        val = f"{round(pct)}%" if show_pct else f"{round(op)}/{round(tot)}"
        _right(img, 63, y, val, COL_OPEN if op else COL_LABEL)
        _bar(img, RIGHT_X, 63, y + 6, pct / 100)


def draw_left(img, rep: dict, units: str, show_temp: bool):
    icon = icon_for((rep.get("weather") or {}).get("type"))
    if icon is not None:
        img.paste(icon, (ICON_X, 9))
    if show_temp:
        w = rep.get("weather") or {}
        hi, lo = temp(w.get("high"), units), temp(w.get("low"), units)
        cx = ICON_X + 4.5                                   # centre of the 10 px icon
        if hi:
            _centre(img, cx, 20, hi, COL_HIGH)
        if lo:
            _centre(img, cx, 26, lo, COL_LOW)


def render_lines_page(rep, name, lines, window, units, show_temp, frame) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw_header(img, name, rep.get("open"))
    draw_left(img, rep, units, show_temp)
    for (kind, args), y in zip(lines, LINE_Y):
        _draw_line(img, y, kind, args, rep, window, units, frame)
    return img


def render_forecast_page(rep, name, units) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw_header(img, name, rep.get("open"))
    days = (rep.get("forecast") or [])[1:4] or (rep.get("forecast") or [])[:3]
    for i, d in enumerate(days):
        x0 = i * 21 + (1 if i else 0)
        try:
            label = date.fromisoformat(d["date"]).strftime("%a").upper()
        except (KeyError, ValueError):
            label = "---"
        cx = x0 + 10
        _centre(img, cx - 0.5, 9, label, COL_DAY)
        icon = icon_for(d.get("type"))
        if icon is not None:
            img.paste(icon, (cx - 5, 16))
        if d.get("snow"):
            val, col = depth(d["snow"], units), (COL_POWDER if is_powder(d["snow"]) else COL_NEW)
            if val in ('0"', "0cm"):
                val = '<1"' if units != "metric" else "<1cm"
        else:
            val, col = temp(d.get("high"), units), COL_HIGH
        _centre(img, cx - 0.5, 27, val, col)
    return img


def card_pages(rep, name, metrics, window, units) -> list:
    """Page specs for one resort: ("lines", [3 lines]) … then ("forecast",)."""
    lines = stat_lines(rep, metrics, window, units)
    pages = [("lines", lines[i:i + 3]) for i in range(0, len(lines), 3)] or [("lines", [])]
    if metrics.get("forecast", True) and rep.get("forecast"):
        pages.append(("forecast", None))
    return pages


def render_page(rep, name, page, window, units, show_temp, frame) -> Image.Image:
    if page[0] == "forecast":
        return render_forecast_page(rep, name, units)
    return render_lines_page(rep, name, page[1], window, units, show_temp, frame)


def render_error(name: str, message: str) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw_header(img, name, False)
    words, line, y = message.upper().split(), "", 9
    for w in words:
        t = (line + " " + w).strip()
        if _bdf_width(t, _FONT) > MATRIX_W - 2:
            _text(img, 1, y, line, COL_LABEL)
            line, y = w, y + 8
            if y > 25:
                break
        else:
            line = t
    if y <= 25 and line:
        _text(img, 1, y, line, COL_LABEL)
    return img


def compose_body_scroll(a: Image.Image, b: Image.Image, offset: int, x0: int = 0) -> Image.Image:
    """Body of `a` (from column x0) sliding up and out while `b`'s slides in; the header
    and anything left of x0 come from `b` (x0 = RIGHT_X keeps the weather column still)."""
    img = b.copy()
    body_h = MATRIX_H - BODY_Y
    img.paste((0, 0, 0), (x0, BODY_Y, MATRIX_W, MATRIX_H))
    if offset < body_h:
        img.paste(a.crop((x0, BODY_Y + offset, MATRIX_W, MATRIX_H)), (x0, BODY_Y))
    img.paste(b.crop((x0, BODY_Y, MATRIX_W, MATRIX_H - max(0, body_h - offset))), (x0, BODY_Y + max(0, body_h - offset)))
    return img


def compose_full_scroll(a: Image.Image, b: Image.Image, offset: int) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    img.paste(a.crop((0, offset, MATRIX_W, MATRIX_H)), (0, 0))
    img.paste(b.crop((0, 0, MATRIX_W, offset)), (0, MATRIX_H - offset))
    return img


# ------------------------------------------------------------------
# Rows
# ------------------------------------------------------------------

ROW_COLS = ("new", "base", "runs", "lifts")
_COL_HEAD = {"new": "NEW", "base": "BASE", "runs": "RUN", "lifts": "LIFT"}
COL_GAP = 3


def row_columns(metrics: dict) -> list:
    return [c for c in ROW_COLS if metrics.get(c, True)][:3]


def _row_values(rep, cols, window, units):
    out = []
    for c in cols:
        if c == "new":
            cm = (rep.get("new_cm") or {}).get(window)
            out.append((depth(cm or 0, units, False), COL_POWDER if is_powder(cm) else COL_NEW))
        elif c == "base":
            b = rep.get("base_cm") if rep.get("base_cm") is not None else rep.get("summit_cm")
            out.append((depth(b, units, False) if b is not None else "--", COL_BASE))
        elif c == "runs":
            p = rep.get("runs_pct")
            out.append((f"{round(p)}%" if p is not None else "--", COL_OPEN if p else COL_LABEL))
        elif c == "lifts":
            o, t = rep.get("lifts_open"), rep.get("lifts_total")
            out.append((f"{round(o or 0)}/{round(t)}" if t else "--", COL_OPEN if o else COL_LABEL))
    return out


def row_layout(entries, cols, window, units) -> list:
    """Right edge x of each column, sized to the widest header/value (rightmost ends at x63)."""
    widths = []
    for i, c in enumerate(cols):
        w = _bdf_width(_COL_HEAD[c], _FONT)
        for rep, _name in entries:
            if rep.get("open") and not rep.get("error"):
                w = max(w, _bdf_width(_row_values(rep, cols, window, units)[i][0], _FONT))
        widths.append(w)
    xs, right = [], 63
    for w in reversed(widths):
        xs.append(right)
        right -= w + COL_GAP
    return list(reversed(xs)), right + 1          # column right edges, name area width


def draw_rows_header(img, cols, xs, name_w):
    if _bdf_width("RESORT", _FONT) <= name_w - 1:
        _text(img, 1, 1, "RESORT", COL_LABEL)
    for c, rx in zip(cols, xs):
        _right(img, rx, 1, _COL_HEAD[c], COL_LABEL)
    px = img.load()
    for x in range(MATRIX_W):
        px[x, SEPARATOR_Y] = COL_SEP


def draw_row(img, y, rep, name, cols, xs, name_w, window, units):
    col_name = COL_NAME if rep.get("open") else COL_LABEL
    if rep.get("error") or not rep.get("open"):
        if rep.get("error"):
            txt, col = "NO DATA", COL_LABEL
        else:
            t, txt, col = rep.get("opening_date"), "CLOSED", COL_CLOSED
            if t and t >= date.today().isoformat():
                dt = date.fromisoformat(t)
                txt, col = f"OPENS {dt.month}/{dt.day}", COL_OPENS
                # Keep at least ~6 letters of the name; drop "OPENS" if it would squeeze it
                if MATRIX_W - 2 - _bdf_width(txt, _FONT) - 3 < 6 * CHAR_W:
                    txt = f"{dt.month}/{dt.day}"
        tw = _bdf_width(txt, _FONT)
        _right(img, 63, y, txt, col)
        _text(img, 1, y, fit(name, MATRIX_W - 2 - tw - 3), col_name)
        return
    _text(img, 1, y, fit(name, name_w - 2), col_name)
    for (val, col), rx in zip(_row_values(rep, cols, window, units), xs):
        _right(img, rx, y, val, col)


def build_rows_strip(entries, cols, window, units) -> Image.Image:
    """entries: [(rep, name)] → strip of 8 px rows plus a dashed divider where it loops."""
    xs, name_w = row_layout(entries, cols, window, units)
    h = len(entries) * LINE_H + 2
    img = Image.new("RGB", (MATRIX_W, h), (0, 0, 0))
    for i, (rep, name) in enumerate(entries):
        draw_row(img, i * LINE_H, rep, name, cols, xs, name_w, window, units)
    px = img.load()
    for x in range(0, MATRIX_W, 2):
        px[x, h - 2] = (50, 50, 50)
    return img


def render_rows_static(entries, cols, window, units) -> Image.Image:
    xs, name_w = row_layout(entries, cols, window, units)
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    draw_rows_header(img, cols, xs, name_w)
    for (rep, name), y in zip(entries, LINE_Y):
        draw_row(img, y, rep, name, cols, xs, name_w, window, units)
    return img


def render_rows_frame(strip, entries, cols, window, units, offset: int) -> Image.Image:
    xs, name_w = row_layout(entries, cols, window, units)
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    area_y, area_h = 9, MATRIX_H - 9
    sh = strip.height
    offset %= sh
    y = 0
    while y < area_h:
        part = strip.crop((0, offset, MATRIX_W, min(sh, offset + area_h - y)))
        img.paste(part, (0, area_y + y))
        y += part.height
        offset = 0
    img.paste((0, 0, 0), (0, 0, MATRIX_W, area_y))
    draw_rows_header(img, cols, xs, name_w)
    return img
