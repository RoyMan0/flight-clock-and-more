import math

from rgbmatrix import graphics

from setup import fonts, screen

GRID_COLOR = graphics.Color(0, 40, 0)
TEXT_COLOR = graphics.Color(0, 200, 50)
LINE_COLOR = graphics.Color(0, 120, 30)

CENTER_X = screen.WIDTH // 2
CENTER_Y = screen.HEIGHT // 2
LINE_LEN = 3
LINE_HEIGHT = 6  # 5px glyph + 1px gap


class RadarScene:
    def draw(self, canvas, flights, home_lat, home_lon, search_radius_nm):
        canvas.Clear()
        self._draw_grid(canvas)

        # Pre-compute screen positions, sort by x so alternating sides work spatially
        positioned = []
        for flight in flights:
            lat = flight.get("plane_latitude")
            lon = flight.get("plane_longitude")
            if lat is None or lon is None:
                continue
            sx, sy = self._to_screen(lat, lon, home_lat, home_lon, search_radius_nm)
            positioned.append((sx, sy, flight))
        positioned.sort(key=lambda t: t[0])

        placed = []  # (x0, y0, x1, y1) bounding boxes of placed labels
        for idx, (sx, sy, flight) in enumerate(positioned):
            canvas.SetPixel(sx, sy, 0, 220, 60)
            self._draw_label(canvas, flight, sx, sy, idx, placed)

    def _draw_grid(self, canvas):
        for x in range(screen.WIDTH):
            canvas.SetPixel(x, CENTER_Y, *_rgb(GRID_COLOR))
        for y in range(screen.HEIGHT):
            canvas.SetPixel(CENTER_X, y, *_rgb(GRID_COLOR))
        hw, hh = screen.WIDTH // 4, screen.HEIGHT // 4
        x0, y0 = CENTER_X - hw, CENTER_Y - hh
        x1, y1 = CENTER_X + hw, CENTER_Y + hh
        for x in range(x0, x1 + 1):
            canvas.SetPixel(x, y0, *_rgb(GRID_COLOR))
            canvas.SetPixel(x, y1, *_rgb(GRID_COLOR))
        for y in range(y0, y1 + 1):
            canvas.SetPixel(x0, y, *_rgb(GRID_COLOR))
            canvas.SetPixel(x1, y, *_rgb(GRID_COLOR))

    def _to_screen(self, plane_lat, plane_lon, home_lat, home_lon, radius_nm):
        nm_per_lat = 60.0
        nm_per_lon = 60.0 * math.cos(math.radians(home_lat))
        scale_x = (screen.WIDTH / 2) / radius_nm
        scale_y = (screen.HEIGHT / 2) / radius_nm
        dx = (plane_lon - home_lon) * nm_per_lon * scale_x
        dy = (plane_lat - home_lat) * nm_per_lat * scale_y
        sx = int(CENTER_X + dx)
        sy = int(CENTER_Y - dy)
        return max(0, min(screen.WIDTH - 1, sx)), max(0, min(screen.HEIGHT - 1, sy))

    def _draw_label(self, canvas, flight, sx, sy, idx, placed):
        label = self._build_label(flight)
        w = _text_width(label)
        h = LINE_HEIGHT

        # Preferred horizontal side: alternate by sorted index, override if off-screen
        go_right = (idx % 2 == 0)
        if go_right and sx + LINE_LEN + 1 + w > screen.WIDTH:
            go_right = False
        elif not go_right and sx - LINE_LEN - w < 0:
            go_right = True

        text_x = (sx + LINE_LEN + 1) if go_right else (sx - LINE_LEN - w)

        # Find a vertical position that doesn't overlap any already-placed label
        preferred_y = sy - h // 2
        text_y = _find_clear_y(text_x, preferred_y, w, h, placed)

        placed.append((text_x, text_y, text_x + w, text_y + h))

        # Connector line from dot to the nearest horizontal edge of the text box,
        # at the vertical midpoint of the text box
        anchor_x = (text_x - 1) if go_right else (text_x + w)
        anchor_y = text_y + h // 2
        graphics.DrawLine(canvas, sx, sy, anchor_x, anchor_y, LINE_COLOR)

        # Draw label (baseline is bottom of cell)
        graphics.DrawText(canvas, fonts.tiny, text_x, text_y + h - 1, TEXT_COLOR, label)

    def _build_label(self, flight):
        callsign = flight.get("callsign") or ""
        reg = flight.get("registration") or ""
        label = callsign if (callsign and callsign != "N/A") else reg or "?"
        return label[:7]


def _find_clear_y(text_x, preferred_y, w, h, placed):
    seen = set()
    for delta in _nudge_sequence():
        y = max(0, min(screen.HEIGHT - h, preferred_y + delta))
        if y in seen:
            continue
        seen.add(y)
        if not any(_overlaps(text_x, y, text_x + w, y + h, *b) for b in placed):
            return y
    return max(0, min(screen.HEIGHT - h, preferred_y))


def _nudge_sequence():
    yield 0
    for i in range(1, screen.HEIGHT):
        yield -i
        yield i


def _overlaps(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    return not (ax1 < bx0 or ax0 > bx1 or ay1 < by0 or ay0 > by1)


def _rgb(color):
    return color.red, color.green, color.blue


def _text_width(text):
    return len(text) * 4  # tom-thumb: 3px glyph + 1px advance
