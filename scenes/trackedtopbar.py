from datetime import datetime
from rgbmatrix import graphics
from utilities.animator import Animator
from utilities.temperature import get_temperature_cached, grab_forecast
from setup import colours, fonts, frames, screen
from config import CLOCK_FORMAT, NIGHT_START, NIGHT_END
import logging

CLOCK_FONT = fonts.large_bold
CLOCK_X = 0
CLOCK_Y = 11
CLOCK_DAY_COLOUR = colours.LIGHT_ORANGE
CLOCK_NIGHT_COLOUR = colours.LIGHT_BLUE

TEMP_FONT = fonts.small
TEMP_Y = 6
TEMP_AREA_LEFT = 40
TEMP_AREA_RIGHT = screen.WIDTH
TEMP_CHAR_WIDTH = 5

DATE_FONT = fonts.extrasmall
DATE_X = 40
DATE_Y = 11
DATE_CHAR_WIDTH = 4

NIGHT_START_TIME = datetime.strptime(NIGHT_START, "%H:%M")
NIGHT_END_TIME = datetime.strptime(NIGHT_END, "%H:%M")

_MOON_COLORS = [
    [colours.DARK_PURPLE,   colours.DARK_PURPLE],
    [colours.DARK_PURPLE,   colours.DARK_MID_PURPLE],
    [colours.DARK_PURPLE,   colours.WHITE],
    [colours.DARK_MID_PURPLE, colours.WHITE],
    [colours.GREY,          colours.GREY],
    [colours.WHITE,         colours.DARK_MID_PURPLE],
    [colours.WHITE,         colours.DARK_PURPLE],
    [colours.DARK_MID_PURPLE, colours.DARK_PURPLE],
]


class TrackedTopBarScene(object):
    def __init__(self):
        super().__init__()
        self._topbar_last_time = None
        self._topbar_last_date = None
        self._topbar_last_temp_str = None
        self._topbar_moon_phase = None
        self._topbar_moon_fetched_day = None

    def _topbar_is_night(self):
        now = datetime.now().time()
        ns = NIGHT_START_TIME.time()
        ne = NIGHT_END_TIME.time()
        if ns > ne:  # wraps midnight
            return now >= ns or now < ne
        return ns <= now < ne

    def _topbar_temp_colour(self, humidity):
        ratio = humidity / 100.0
        return graphics.Color(
            int(colours.WHITE.red   + (colours.DARK_BLUE.red   - colours.WHITE.red)   * ratio),
            int(colours.WHITE.green + (colours.DARK_BLUE.green - colours.WHITE.green) * ratio),
            int(colours.WHITE.blue  + (colours.DARK_BLUE.blue  - colours.WHITE.blue)  * ratio),
        )

    def _topbar_fetch_moon_phase(self):
        now = datetime.now()
        if self._topbar_moon_fetched_day == now.day:
            return self._topbar_moon_phase
        try:
            forecast = grab_forecast(tag="TrackedTopBar")
            if forecast:
                for day in forecast:
                    if day["startTime"][:10] == now.strftime("%Y-%m-%d"):
                        self._topbar_moon_phase = int(day["values"]["moonPhase"])
                        self._topbar_moon_fetched_day = now.day
                        break
        except Exception as e:
            logging.debug(f"[trackedtopbar] moon phase fetch failed: {e}")
        return self._topbar_moon_phase

    def _topbar_moon_colors(self, phase):
        phase = min(max(phase, 0), 7)
        return _MOON_COLORS[phase]

    def _topbar_draw_gradient_text(self, text, x, y, start_color, end_color):
        n = len(text)
        for i, char in enumerate(text):
            pos = i / max(n - 1, 1)
            r = int(start_color.red   + (end_color.red   - start_color.red)   * pos)
            g = int(start_color.green + (end_color.green - start_color.green) * pos)
            b = int(start_color.blue  + (end_color.blue  - start_color.blue)  * pos)
            graphics.DrawText(self.canvas, DATE_FONT, x + i * DATE_CHAR_WIDTH, y,
                              graphics.Color(r, g, b), char)

    @Animator.KeyFrame.add(0)
    def topbar_init(self):
        self._topbar_draw()

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def topbar_update(self, count):
        self._topbar_draw()

    def _topbar_draw(self):
        now = datetime.now()
        clock_fmt = "%l:%M" if CLOCK_FORMAT == "12hr" else "%H:%M"
        current_time = now.strftime(clock_fmt).strip()
        current_date = now.strftime("%b %d")

        clock_color = CLOCK_NIGHT_COLOUR if self._topbar_is_night() else CLOCK_DAY_COLOUR

        # Clock
        if self._topbar_last_time and self._topbar_last_time != current_time:
            graphics.DrawText(self.canvas, CLOCK_FONT, CLOCK_X, CLOCK_Y,
                              colours.BLACK, self._topbar_last_time)
        self._topbar_last_time = current_time
        graphics.DrawText(self.canvas, CLOCK_FONT, CLOCK_X, CLOCK_Y, clock_color, current_time)

        # Date (moon-phase gradient, same as DateScene)
        moon = self._topbar_fetch_moon_phase()
        if moon is None:
            date_start = date_end = colours.RED
        else:
            date_start, date_end = self._topbar_moon_colors(moon)
        if self._topbar_last_date and self._topbar_last_date != current_date:
            graphics.DrawText(self.canvas, DATE_FONT, DATE_X, DATE_Y,
                              colours.BLACK, self._topbar_last_date)
        self._topbar_last_date = current_date
        self._topbar_draw_gradient_text(current_date, DATE_X, DATE_Y, date_start, date_end)

        # Temperature
        temp, humidity = get_temperature_cached()
        if temp is None:
            display_str = "ERR"
            temp_colour = colours.RED
        else:
            display_str = f"{round(temp)}°"
            temp_colour = self._topbar_temp_colour(humidity if humidity is not None else 50.0)

        if self._topbar_last_temp_str and self._topbar_last_temp_str != display_str:
            self.draw_square(TEMP_AREA_LEFT, 0, TEMP_AREA_RIGHT, TEMP_Y - 1, colours.BLACK)

        self._topbar_last_temp_str = display_str
        temp_w = len(display_str) * TEMP_CHAR_WIDTH
        temp_x = (TEMP_AREA_LEFT + TEMP_AREA_RIGHT) // 2 - temp_w // 2
        graphics.DrawText(self.canvas, TEMP_FONT, temp_x, TEMP_Y, temp_colour, display_str)
