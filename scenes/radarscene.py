import math

from rgbmatrix import graphics

from setup import fonts, screen

GRID_COLOR = graphics.Color(0, 40, 0)
PLANE_COLOR = graphics.Color(0, 220, 60)
TEXT_COLOR = graphics.Color(0, 200, 50)
LINE_COLOR = graphics.Color(0, 120, 30)

CENTER_X = screen.WIDTH // 2
CENTER_Y = screen.HEIGHT // 2
LINE_LEN = 3
LINE_HEIGHT = 6  # 5px glyph + 1px gap


class RadarScene:
    def draw(self, canvas, flights, home_lat, home_lon, search_radius_nm):
        self._clear(canvas)
        self._draw_grid(canvas)

        # Pre-compute positions, sort by x so alternating sides spread labels apart
        positioned = []
        for flight in flights:
            lat = flight.get("plane_latitude")
            lon = flight.get("plane_longitude")
            if lat is None or lon is None:
                continue
            sx, sy = self._to_screen(lat, lon, home_lat, home_lon, search_radius_nm)
            positioned.append((sx, sy, flight))
        positioned.sort(key=lambda t: t[0])

        for idx, (sx, sy, flight) in enumerate(positioned):
            canvas.SetPixel(sx, sy, 0, 220, 60)
            self._draw_label(canvas, flight, sx, sy, idx)

    def _clear(self, canvas):
        canvas.Clear()

    def _draw_grid(self, canvas):
        # Cross-hair
        for x in range(screen.WIDTH):
            canvas.SetPixel(x, CENTER_Y, *_rgb(GRID_COLOR))
        for y in range(screen.HEIGHT):
            canvas.SetPixel(CENTER_X, y, *_rgb(GRID_COLOR))
        # Inner range ring (half-scale rectangle)
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
        sx = max(0, min(screen.WIDTH - 1, sx))
        sy = max(0, min(screen.HEIGHT - 1, sy))
        return sx, sy

    def _draw_label(self, canvas, flight, sx, sy, idx=0):
        lines = self._build_lines(flight)
        text_height = len(lines) * LINE_HEIGHT

        # Alternate sides by sorted index; fall back to screen-edge avoidance
        go_right = (idx % 2 == 0)
        max_w = max((_text_width(l) for l in lines), default=0)
        if go_right and sx + LINE_LEN + 1 + max_w > screen.WIDTH:
            go_right = False
        elif not go_right and sx - LINE_LEN - max_w < 0:
            go_right = True

        if go_right:
            text_x = sx + LINE_LEN + 1
        else:
            text_x = sx - LINE_LEN - max_w

        # Connector line
        if go_right:
            for lx in range(sx + 1, sx + LINE_LEN + 1):
                canvas.SetPixel(lx, sy, *_rgb(LINE_COLOR))
        else:
            for lx in range(sx - LINE_LEN, sx):
                canvas.SetPixel(lx, sy, *_rgb(LINE_COLOR))

        # Vertical start: center label on dot, clamped to screen
        text_start_y = sy - text_height // 2
        text_start_y = max(0, min(screen.HEIGHT - text_height, text_start_y))

        for i, line in enumerate(lines):
            y = text_start_y + i * LINE_HEIGHT + LINE_HEIGHT - 1  # baseline
            graphics.DrawText(canvas, fonts.tiny, text_x, y, TEXT_COLOR, line)

    def _build_lines(self, flight):
        callsign = flight.get("callsign") or ""
        reg = flight.get("registration") or ""
        label = callsign if (callsign and callsign != "N/A") else reg or "?"
        return [label[:7]]


def _rgb(color):
    return color.red, color.green, color.blue


def _text_width(text):
    return len(text) * 4  # tom-thumb: 3px glyph + 1px advance = 4px per char
