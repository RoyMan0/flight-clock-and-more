from rgbmatrix import graphics
from utilities.animator import Animator
from setup import colours, fonts, screen

STATS_ROW = 31
STATS_FONT = fonts.extrasmall
TIME_COLOUR = colours.LIGHT_YELLOW
DIST_COLOUR = colours.LIGHT_PINK
TYPE_COLOUR = colours.LIGHT_MID_BLUE
NUMBER_COLOUR = colours.LIGHT_ORANGE
SUFFIX_COLOUR = colours.GREY


class TrackedStatsScene(object):
    def __init__(self):
        super().__init__()
        self.stats_position = screen.WIDTH
        self._stats_all_looped = False

    @Animator.KeyFrame.add(1)
    def tracked_stats(self, count):
        if len(self._data) == 0:
            return

        flight = self._data[self._data_index]
        time_remaining = flight.get("time_remaining", "0:00")
        dist_remaining = flight.get("dist_remaining", 0)
        plane_type = flight.get("plane_type", "")
        altitude = flight.get("altitude", 0)
        ground_speed = flight.get("ground_speed", 0)

        segments = [
            (time_remaining, TIME_COLOUR),
            ("  ", SUFFIX_COLOUR),
            (f"{int(dist_remaining)}", DIST_COLOUR),
            ("mi", SUFFIX_COLOUR),
            ("  ", SUFFIX_COLOUR),
            (plane_type, TYPE_COLOUR),
            ("  ", SUFFIX_COLOUR),
            (f"{int(altitude)}", NUMBER_COLOUR),
            ("ft", SUFFIX_COLOUR),
            ("  ", SUFFIX_COLOUR),
            (f"{int(ground_speed)}", NUMBER_COLOUR),
            ("kt", SUFFIX_COLOUR),
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
