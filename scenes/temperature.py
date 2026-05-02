from datetime import datetime
import colorsys
from rgbmatrix import graphics
from utilities.animator import Animator
from setup import colours, fonts, frames, screen
from utilities.temperature import get_temperature_cached
from config import NIGHT_START, NIGHT_END

TEMPERATURE_FONT = fonts.small
TEMPERATURE_FONT_HEIGHT = 6
NIGHT_START_TIME = datetime.strptime(NIGHT_START, "%H:%M")
NIGHT_END_TIME = datetime.strptime(NIGHT_END, "%H:%M")

class TemperatureScene(object):
    def __init__(self):
        super().__init__()
        self._last_temperature = None
        self._last_temperature_str = None
        self._redraw_temp = True

    @Animator.KeyFrame.add(0)
    def _temp_on_reset(self, count):
        """Fired by reset_scene() so temperature redraws after a canvas clear."""
        self._redraw_temp = True

    def colour_gradient(self, colour_A, colour_B, ratio):
        return graphics.Color(
            int(colour_A.red + ((colour_B.red - colour_A.red) * ratio)),
            int(colour_A.green + ((colour_B.green - colour_A.green) * ratio)),
            int(colour_A.blue + ((colour_B.blue - colour_A.blue) * ratio)),
        )

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def temperature(self, count):
        now = datetime.now().replace(microsecond=0).time()
        if now == NIGHT_START_TIME.time() or now == NIGHT_END_TIME.time():
            self._redraw_temp = True
            return

        if len(self._data):
            self._redraw_temp = True
            return

        current_temperature, current_humidity = get_temperature_cached()

        # Nothing to show yet and no forced redraw pending
        if current_temperature is None and self._last_temperature_str is None and not self._redraw_temp:
            return

        # Clear old temperature
        if self._last_temperature_str is not None:
            self.draw_square(40, 0, 64, 5, colours.BLACK)

        if current_temperature is None or current_humidity is None:
            display_str = "ERR"
            temp_colour = colours.RED
        else:
            display_str = f"{round(current_temperature)}°"
            humidity_ratio = current_humidity / 100.0
            temp_colour = self.colour_gradient(colours.WHITE, colours.DARK_BLUE, humidity_ratio)

        self._last_temperature_str = display_str
        self._last_temperature = current_temperature
        self._redraw_temp = False

        font_character_width = 5
        temperature_string_width = len(display_str) * font_character_width
        middle_x = (40 + 64) // 2
        start_x = middle_x - temperature_string_width // 2
        TEMPERATURE_POSITION = (start_x, TEMPERATURE_FONT_HEIGHT)

        graphics.DrawText(
            self.canvas,
            TEMPERATURE_FONT,
            TEMPERATURE_POSITION[0],
            TEMPERATURE_POSITION[1],
            temp_colour,
            display_str,
        )
