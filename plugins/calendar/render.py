"""
Calendar rendering (64×32, 4x6 BDF font).

Header (both modes): 7×5 calendar icon · title, grey separator at y7.

Detailed — one event per 24 px panel under the header (y8-31):
  x1-21   time column: start over end ("7:30P"/"9:30P", "19:30"/"-21:30"),
          "ALL"/"DAY", "NOW"/"-5P"
  x23     vertical bar in the calendar's colour
  x25-63  title (2 wrapped lines), then TODAY / TOMORROW / FRI 10/30
          (a one-line title gets the location on line 2)
Panels slide up from one event to the next.

Compact — 8 px rows under the header: coloured time · title, with a dim
"─ TOMORROW ───" divider starting each new day (and the first day if it
isn't today). Scrolls bottom-to-top when it doesn't fit.
"""

from datetime import date, datetime, timedelta

from PIL import Image

from plugins.transit.render import _FONT, _bdf_draw, _bdf_width

MATRIX_W, MATRIX_H = 64, 32
LEFT = 1
CHAR_W = 4
SEPARATOR_Y = 7
BODY_Y = 8                  # panels / scrolling area start here (text rows at +1)
PANEL_H = MATRIX_H - BODY_Y  # 24
ROW_H = 8
TIME_W = 22                 # detailed time column (x1-22; glyphs end in a blank column)
BAR_X = 23
TEXT_X = 25
COMPACT_TITLE_X = 24

COL_TEXT    = (235, 235, 235)
COL_DIM     = (120, 120, 120)
COL_SEP     = (60, 60, 60)
COL_ICON    = (200, 200, 200)
COL_NOW     = (70, 210, 90)
COL_REMIND  = (255, 210, 0)
DEFAULT_COLOUR = (74, 144, 217)

_ICON = ["#######", "#.#.#.#", "#######", "#.#.#.#", "#######"]

# Times use a copy of 4x6 with a tight colon: the stock glyph pads its 1 px dot
# with 2 blank columns each side; shifted left with a 2 px advance it's 1 px
# each side, so "10:30P" fits the time column.
_TIME_FONT = dict(_FONT)
if ord(":") in _TIME_FONT:
    _g = _TIME_FONT[ord(":")]
    _TIME_FONT[ord(":")] = {**_g, "dw": 2, "rows": [(r << 1) & 0xFF for r in _g["rows"]]}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def hex_rgb(value) -> tuple:
    try:
        v = str(value).lstrip("#")
        rgb = tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, TypeError):
        return DEFAULT_COLOUR
    # Very dark calendar colours vanish on the panel — lift them
    if max(rgb) < 90:
        m = max(1, max(rgb))
        rgb = tuple(min(255, int(c * 90 / m)) for c in rgb) if m > 1 else (90, 90, 90)
    return rgb


def _dim(rgb, f=0.7) -> tuple:
    return tuple(int(c * f) for c in rgb)


def _width(text: str) -> int:
    return _bdf_width(text, _FONT)


def _time_width(text: str) -> int:
    return _bdf_width(text, _TIME_FONT)


def _fit(text: str, max_w: int) -> str:
    if _width(text) <= max_w:
        return text
    while text and _width(text + "..") > max_w:
        text = text[:-1]
    return text.rstrip() + ".."


def wrap(text: str, max_w: int, max_lines: int) -> list:
    """Greedy word wrap by pixel width; long words are split; overflow gets '..'."""
    words, lines, cur = text.split(), [], ""
    while words:
        w = words[0]
        trial = f"{cur} {w}" if cur else w
        if _width(trial) <= max_w:
            cur = trial
            words.pop(0)
        elif not cur:                       # single word wider than the line
            n = max(1, max_w // CHAR_W)
            cur, words[0] = w[:n], w[n:]
            lines.append(cur)
            cur = ""
        else:
            lines.append(cur)
            cur = ""
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if words and lines:
        lines[-1] = _fit(lines[-1] + " " + " ".join(words), max_w)
    return lines[:max_lines]


def is_today(d: date) -> bool:
    return d == datetime.now().date()


def event_day(ev) -> date:
    """The day the event is listed under: today for anything already running."""
    s = ev["start"]
    d = s if not isinstance(s, datetime) else s.date()
    return max(d, datetime.now().date())


def day_label(d: date) -> str:
    today = datetime.now().date()
    if d == today:
        return "TODAY"
    if d == today + timedelta(days=1):
        return "TOMORROW"
    return f"{d:%a} {d.month}/{d.day}".upper()


def in_progress(ev, now=None) -> bool:
    if ev["all_day"]:
        return False
    now = now or datetime.now().astimezone()
    return ev["start"] <= now < ev["end"]


def _short_clock(dt: datetime, clock_format: str) -> str:
    """Compact time: '9:00A', '10:30P', '16:00'."""
    if clock_format == "24hr":
        return dt.strftime("%H:%M")
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d}{'A' if dt.hour < 12 else 'P'}"


def _end_label(dt: datetime, clock_format: str) -> str:
    """When an event ends: '-9:30P', '-21:30'; the dash goes if it won't fit ('10:30P')."""
    label = _short_clock(dt, clock_format)
    return "-" + label if _time_width("-" + label) <= TIME_W else label


def time_column(ev, clock_format: str, now=None) -> tuple:
    """((main, colour), (sub, colour)) for the detailed view."""
    if ev["all_day"]:
        return ("ALL", COL_TEXT), ("DAY", COL_DIM)
    if in_progress(ev, now):
        return ("NOW", COL_NOW), (_end_label(ev["end"], clock_format), COL_DIM)
    end = _end_label(ev["end"], clock_format) if ev["end"] > ev["start"] else ""
    return (_short_clock(ev["start"], clock_format), COL_TEXT), (end, COL_DIM)


# ------------------------------------------------------------------
# Header
# ------------------------------------------------------------------

def draw_header(img, title: str, colour: tuple, title_colour=COL_TEXT, right: str = ""):
    px = img.load()
    for r, line in enumerate(_ICON):
        for c, bit in enumerate(line):
            if bit == "#":
                px[LEFT + c, 1 + r] = colour if r == 0 else COL_ICON
    room = MATRIX_W - (LEFT + 9)
    if right:
        rw = _width(right)
        _bdf_draw(img, MATRIX_W - rw, 1, right, _FONT, COL_DIM)
        room -= rw + 2
    _bdf_draw(img, LEFT + 9, 1, _fit(title.upper(), room), _FONT, title_colour)
    for x in range(MATRIX_W):
        px[x, SEPARATOR_Y] = COL_SEP


# ------------------------------------------------------------------
# Detailed
# ------------------------------------------------------------------

def render_panel(ev, clock_format: str, now=None) -> Image.Image:
    """One event's 64×24 panel (drawn at y=BODY_Y)."""
    img = Image.new("RGB", (MATRIX_W, PANEL_H), (0, 0, 0))
    colour = hex_rgb(ev.get("color"))
    (main, mcol), (sub, scol) = time_column(ev, clock_format, now)
    _bdf_draw(img, LEFT, 1, main, _TIME_FONT, mcol)
    if sub:
        _bdf_draw(img, LEFT, 9, sub, _TIME_FONT, scol)
    px = img.load()
    for y in range(1, PANEL_H):
        px[BAR_X, y] = colour
    text_w = MATRIX_W - TEXT_X
    lines = wrap(ev["title"], text_w, 2)
    for i, line in enumerate(lines):
        _bdf_draw(img, TEXT_X, 1 + ROW_H * i, line, _FONT, COL_TEXT)
    if len(lines) == 1 and ev.get("location"):
        _bdf_draw(img, TEXT_X, 9, _fit(ev["location"], text_w), _FONT, COL_DIM)
    _bdf_draw(img, TEXT_X, 17, day_label(event_day(ev)), _FONT, _dim(colour, 0.85))
    return img


def render_detailed(header_ev, panels: list, offset: int = 0, header_title: str = None,
                    header_colour=None) -> Image.Image:
    """Header for header_ev; panels[0] scrolled up by `offset` px with panels[1] below it."""
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    colour = hex_rgb(header_ev.get("color"))
    if header_title is None:
        draw_header(img, header_ev.get("source") or "Calendar", colour)
    else:
        draw_header(img, header_title, colour, header_colour or COL_TEXT)
    first = panels[0].crop((0, offset, MATRIX_W, PANEL_H))
    img.paste(first, (0, BODY_Y))
    if offset and len(panels) > 1:
        img.paste(panels[1].crop((0, 0, MATRIX_W, offset)), (0, BODY_Y + PANEL_H - offset))
    return img


def reminder_title(ev, now=None) -> str:
    now = now or datetime.now().astimezone()
    mins = int((ev["start"] - now).total_seconds() // 60) + 1
    return "STARTING NOW" if mins <= 0 else f"IN {mins} MIN"


# ------------------------------------------------------------------
# Compact
# ------------------------------------------------------------------

def compact_rows(events: list) -> list:
    """[('day', label) | ('event', ev)] with a divider per day (first only if not today)."""
    rows, last = [], None
    for ev in events:
        d = event_day(ev)
        if d != last and (last is not None or not is_today(d)):
            rows.append(("day", day_label(d)))
        last = d
        rows.append(("event", ev))
    return rows


def _draw_compact_row(img, y: int, row, clock_format: str, now=None):
    kind, val = row
    if kind == "day":
        _bdf_draw(img, LEFT + 4, y, val, _FONT, COL_DIM)
        px = img.load()
        for x in list(range(0, LEFT + 3)) + list(range(LEFT + 5 + _width(val) + 1, MATRIX_W)):
            px[x, y + 2] = COL_SEP
        return
    ev = val
    colour = hex_rgb(ev.get("color"))
    if ev["all_day"]:
        t = "ALL"
    elif in_progress(ev, now):
        t, colour = "NOW", COL_NOW
    else:
        t = _short_clock(ev["start"], clock_format)
    _bdf_draw(img, LEFT, y, t, _TIME_FONT, colour)
    _bdf_draw(img, COMPACT_TITLE_X, y, _fit(ev["title"], MATRIX_W - COMPACT_TITLE_X), _FONT, COL_TEXT)


def compact_header(img, events: list):
    today = datetime.now().date()
    colour = hex_rgb(events[0].get("color")) if events else DEFAULT_COLOUR
    draw_header(img, "Calendar", colour, right=f"{today.month}/{today.day}")


def compact_fits(rows: list) -> bool:
    return len(rows) * ROW_H <= MATRIX_H - BODY_Y


def render_compact_static(events: list, rows: list, clock_format: str) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    compact_header(img, events)
    for i, row in enumerate(rows):
        _draw_compact_row(img, BODY_Y + 1 + ROW_H * i, row, clock_format)
    return img


LOOP_GAP = 4


def build_compact_strip(rows: list, clock_format: str) -> Image.Image:
    """Every row stacked plus a gap with a faint dashed line where the list loops."""
    h = len(rows) * ROW_H + LOOP_GAP
    img = Image.new("RGB", (MATRIX_W, h), (0, 0, 0))
    for i, row in enumerate(rows):
        _draw_compact_row(img, 1 + ROW_H * i, row, clock_format)
    px = img.load()
    for x in range(8, MATRIX_W - 8, 3):
        px[x, h - 2] = px[x + 1, h - 2] = (50, 50, 50)
    return img


def render_compact_frame(events: list, strip: Image.Image, offset: int) -> Image.Image:
    img = Image.new("RGB", (MATRIX_W, MATRIX_H), (0, 0, 0))
    sh = strip.height
    offset %= sh
    y = BODY_Y
    while y < MATRIX_H:
        part = strip.crop((0, offset, MATRIX_W, min(sh, offset + MATRIX_H - y)))
        img.paste(part, (0, y))
        y += part.height
        offset = 0
    compact_header(img, events)          # header last so scrolling rows never cover it
    return img
