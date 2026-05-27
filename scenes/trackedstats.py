from rgbmatrix import graphics
from utilities.animator import Animator
from utilities.cities import get_nearest_city
from utilities.nps_pois import get_nearest_poi
from setup import colours, fonts, screen
from config import UNITS

STATS_ROW = 31   # baseline; rows 26-31 = 6px extrasmall font, rows 25-31 cleared each frame
STATS_FONT = fonts.extrasmall
TIME_COLOUR   = colours.LIGHT_YELLOW
DIST_COLOUR   = colours.LIGHT_PINK
TYPE_COLOUR   = colours.LIGHT_MID_BLUE
NUMBER_COLOUR = colours.LIGHT_ORANGE
SUFFIX_COLOUR = colours.GREY
CITY_COLOUR   = colours.WHITE
POI_COLOUR    = colours.LIGHT_TEAL
REG_COLOUR    = colours.LIGHT_PURPLE
UP_COLOUR     = colours.LIGHT_MID_GREEN
DOWN_COLOUR   = colours.LIGHT_LIGHT_RED

VS_THRESHOLD_FPM = 150
LOC_CACHE_DEG    = 0.01   # ~1 km; skip re-lookup until aircraft moves this far


def _fmt_altitude(altitude: int) -> str:
    if altitude >= 18000:
        return f"FL{altitude // 100}"
    if UNITS == "metric":
        return f"{int(altitude * 0.3048)}m"
    return f"{altitude}ft"


class TrackedStatsScene(object):
    def __init__(self):
        super().__init__()
        self.stats_position = screen.WIDTH
        self._stats_all_looped = False
        self._loc_cache = {"lat": None, "lng": None, "city": None, "poi": None}
        self._show_additional_details = True
        self._show_nearby_locations = True

    @Animator.KeyFrame.add(1)
    def tracked_stats(self, count):
        if len(self._data) == 0:
            return

        flight = self._data[self._data_index]
        time_remaining = flight.get("time_remaining", "0:00")
        dist_remaining = flight.get("dist_remaining", 0)
        plane_type     = flight.get("plane_type", "")
        registration   = flight.get("registration", "")
        altitude       = flight.get("altitude", 0)
        ground_speed   = flight.get("ground_speed", 0)
        vs             = flight.get("vertical_speed", 0)
        lat            = flight.get("lat")
        lng            = flight.get("lng")

        show_details = getattr(self, "_show_additional_details", True)
        show_nearby  = getattr(self, "_show_nearby_locations", True)

        # City / POI lookup with positional caching (only when needed)
        if show_nearby and lat is not None and lng is not None:
            c = self._loc_cache
            if (c["lat"] is None
                    or abs(lat - c["lat"]) > LOC_CACHE_DEG
                    or abs(lng - c["lng"]) > LOC_CACHE_DEG):
                c["lat"]  = lat
                c["lng"]  = lng
                c["city"] = get_nearest_city(lat, lng)
                c["poi"]  = get_nearest_poi(lat, lng)
            city_result = c["city"]
            poi_result  = c["poi"]
        else:
            city_result = poi_result = None

        dist_suffix = "mi" if UNITS == "imperial" else "km"
        speed_val = int(ground_speed)
        speed_suffix = "kt"

        segments = [
            (time_remaining,       TIME_COLOUR),
            ("  ",                 SUFFIX_COLOUR),
            (f"{int(dist_remaining)}", DIST_COLOUR),
            (dist_suffix,          SUFFIX_COLOUR),
        ]
        if show_details:
            alt_str = _fmt_altitude(altitude)
            segments += [
                ("  ",             SUFFIX_COLOUR),
                (plane_type,       TYPE_COLOUR),
            ]
            if registration:
                segments += [
                    ("  ",         SUFFIX_COLOUR),
                    (registration, REG_COLOUR),
                ]
            segments += [
                ("  ",             SUFFIX_COLOUR),
                (alt_str,          NUMBER_COLOUR),
            ]
            if vs > VS_THRESHOLD_FPM:
                segments.append(("^", UP_COLOUR))
            elif vs < -VS_THRESHOLD_FPM:
                segments.append(("v", DOWN_COLOUR))
            segments += [
                ("  ",             SUFFIX_COLOUR),
                (f"{speed_val}",   NUMBER_COLOUR),
                (speed_suffix,     SUFFIX_COLOUR),
            ]
        if city_result:
            segments += [
                ("  ",             SUFFIX_COLOUR),
                (f"nr {city_result['name']}", CITY_COLOUR),
            ]
        if poi_result:
            segments += [
                ("  ",             SUFFIX_COLOUR),
                (f"nr {poi_result['name']}", POI_COLOUR),
            ]

        self.draw_square(0, STATS_ROW - 6, screen.WIDTH, screen.HEIGHT, colours.BLACK)

        x = self.stats_position
        total_width = 0
        for text, colour in segments:
            w = graphics.DrawText(self.canvas, STATS_FONT, x + total_width, STATS_ROW, colour, text)
            total_width += w

        self.stats_position -= 1

        if self.stats_position + total_width < 0:
            self.stats_position = screen.WIDTH
            self._stats_all_looped = True

    @Animator.KeyFrame.add(0)
    def reset_tracked_stats(self):
        self.stats_position = screen.WIDTH
        self._stats_all_looped = False
        self._loc_cache = {"lat": None, "lng": None, "city": None, "poi": None}
