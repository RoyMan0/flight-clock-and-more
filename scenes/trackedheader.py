import re
from PIL import Image
from rgbmatrix import graphics
from utilities.animator import Animator
from setup import colours, fonts, screen

INFO_ROW_TOP = 12
INFO_ROW_BOTTOM = 19
INFO_FONT = fonts.small
INFO_BASELINE = 19

LOGO_SIZE = 8
LOGO_GAP = 2

ARROW_COLOUR = colours.GREY
CALLSIGN_COLOUR = colours.WHITE
AIRLINE_COLOUR = colours.LIGHT_GREY

DEFAULT_LOGO = "default"

AIRLINE_NAMES = {
    "UAL": "United",
    "DAL": "Delta",
    "AAL": "American",
    "SWA": "Southwest",
    "ASA": "Alaska",
    "JBU": "JetBlue",
    "FFT": "Frontier",
    "NKS": "Spirit",
    "HAL": "Hawaiian",
    "WJA": "WestJet",
    "ACA": "Air Canada",
    "BAW": "British",
    "DLH": "Lufthansa",
    "AFR": "Air France",
    "KLM": "KLM",
    "IBE": "Iberia",
    "RPA": "Republic",
    "SKW": "SkyWest",
    "ENY": "Envoy",
    "PDT": "Piedmont",
    "CPZ": "Compass",
    "OO":  "SkyWest",
}


def _airline_from_callsign(callsign):
    m = re.match(r'^([A-Z]+)', callsign.upper())
    return m.group(1) if m else ""


def _load_logo(icao):
    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:
        resample = Image.ANTIALIAS
    for name in (icao, DEFAULT_LOGO):
        try:
            img = Image.open(f"logos/{name}.png")
            img = img.resize((LOGO_SIZE, LOGO_SIZE), resample)
            return img.convert("RGB")
        except Exception:
            continue
    return None


class TrackedHeaderScene(object):
    def __init__(self):
        super().__init__()
        self.route_position = screen.WIDTH
        self._logo_cache = {}

    def _get_logo(self, icao):
        if icao not in self._logo_cache:
            self._logo_cache[icao] = _load_logo(icao)
        return self._logo_cache[icao]

    def _draw_logo(self, logo, x):
        if logo is None:
            return
        for lx in range(LOGO_SIZE):
            sx = x + lx
            if sx < 0 or sx >= screen.WIDTH:
                continue
            for ly in range(LOGO_SIZE):
                pixel = logo.getpixel((lx, ly))
                self.canvas.SetPixel(sx, INFO_ROW_TOP + ly, pixel[0], pixel[1], pixel[2])

    @Animator.KeyFrame.add(1)
    def tracked_header_route(self, count):
        if len(self._data) == 0:
            return

        flight = self._data[self._data_index]
        callsign = flight.get("callsign", "")
        origin = flight.get("origin", "???")
        destination = flight.get("destination", "???")
        origin_color = flight.get("origin_color", colours.LIGHT_GREY)
        destination_color = flight.get("destination_color", colours.LIGHT_GREY)

        airline_icao = _airline_from_callsign(callsign)
        airline_name = AIRLINE_NAMES.get(airline_icao, airline_icao)
        flight_num = callsign[len(airline_icao):]

        logo = self._get_logo(airline_icao)

        segments = [
            (airline_name,  AIRLINE_COLOUR),
            (" ",            ARROW_COLOUR),
            (flight_num,    CALLSIGN_COLOUR),
            ("  ",           ARROW_COLOUR),
            (origin,        origin_color),
            (" > ",          ARROW_COLOUR),
            (destination,   destination_color),
        ]

        # Clear info row
        self.draw_square(0, INFO_ROW_TOP, screen.WIDTH, INFO_ROW_BOTTOM, colours.BLACK)

        # Draw scrolling logo
        self._draw_logo(logo, self.route_position)

        # Draw scrolling text
        text_x = self.route_position + LOGO_SIZE + LOGO_GAP
        total_text_width = 0
        for text, colour in segments:
            w = graphics.DrawText(
                self.canvas, INFO_FONT, text_x + total_text_width,
                INFO_BASELINE, colour, text,
            )
            total_text_width += w

        total_width = LOGO_SIZE + LOGO_GAP + total_text_width
        self.route_position -= 1
        if self.route_position + total_width < 0:
            self.route_position = screen.WIDTH

    @Animator.KeyFrame.add(0)
    def reset_tracked_header(self):
        self.route_position = screen.WIDTH
