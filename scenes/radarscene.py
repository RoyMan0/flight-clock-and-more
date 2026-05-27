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
LINE_HEIGHT = 6  # 5px glyph + 1px gap between lines
NUM_LINES = 4


class RadarScene:
    def draw(self, canvas, flights, home_lat, home_lon, search_radius_nm):
        self._clear(canvas)
        self._draw_grid(canvas)
        for flight in flights:
            lat = flight.get("plane_latitude")
            lon = flight.get("plane_longitude")
            if lat is None or lon is None:
                continue
            sx, sy = self._to_screen(lat, lon, home_lat, home_lon, search_radius_nm)
            canvas.SetPixel(sx, sy, 0, 220, 60)
            self._draw_label(canvas, flight, sx, sy)

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

    def _draw_label(self, canvas, flight, sx, sy):
        lines = self._build_lines(flight)
        text_height = len(lines) * LINE_HEIGHT

        # Decide left vs right placement
        go_right = sx < CENTER_X
        if go_right:
            text_x = sx + LINE_LEN + 1
        else:
            char_widths = [_text_width(l) for l in lines]
            max_w = max(char_widths) if char_widths else 0
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

        origin = flight.get("origin") or "?"
        dest = flight.get("destination") or "?"
        aircraft = flight.get("plane") or "?"
        alt_ft = flight.get("altitude") or 0
        fl = f"FL{alt_ft // 100:03d}"

        return [label[:7], f"{origin}-{dest}", aircraft[:4], fl]


def _rgb(color):
    return color.red, color.green, color.blue


def _text_width(text):
    return len(text) * 4  # 4x6 font is 4px per character
