from rgbmatrix import graphics
from utilities.animator import Animator
from setup import colours, fonts, screen
from config import DISTANCE_UNITS

# Setup
PLANE_COLOUR = colours.LIGHT_MID_BLUE
PLANE_DISTANCE_COLOUR = colours.LIGHT_PINK
PLANE_DISTANCE_FROM_TOP = 31
PLANE_TEXT_HEIGHT = 6
PLANE_FONT = fonts.small

class PlaneDetailsScene(object):
    def __init__(self):
        super().__init__()
        self.plane_position = screen.WIDTH
        self._data_all_looped = False
        self._show_additional_details = False

    @Animator.KeyFrame.add(1)
    def plane_details(self, count):
        # Guard against no data
        if len(self._data) == 0:
            return

        # Extract data
        plane_data = self._data[self._data_index]
        plane_name = plane_data["plane"]
        distance = plane_data["distance"]
        direction = plane_data["direction"]
        distance_units = "mi" if DISTANCE_UNITS == "imperial" else "KM"

        # Construct the plane details strings
        plane_name_text = f'{plane_name} '
        distance_text = f'{distance:.2f}{distance_units} {direction}'

        # Draw background
        self.draw_square(
            0,
            PLANE_DISTANCE_FROM_TOP - PLANE_TEXT_HEIGHT,
            screen.WIDTH,
            screen.HEIGHT,
            colours.BLACK,
        )

        # Render chained text segments; DrawText returns the pixel width drawn
        x = self.plane_position

        plane_name_width = graphics.DrawText(
            self.canvas, PLANE_FONT, x, PLANE_DISTANCE_FROM_TOP,
            PLANE_COLOUR, plane_name_text,
        )
        x += plane_name_width

        distance_text_width = graphics.DrawText(
            self.canvas, PLANE_FONT, x, PLANE_DISTANCE_FROM_TOP,
            PLANE_DISTANCE_COLOUR, distance_text,
        )
        x += distance_text_width

        total_text_width = plane_name_width + distance_text_width

        if self._show_additional_details:
            reg = plane_data.get("registration", "")
            altitude = plane_data.get("altitude", 0)
            speed = plane_data.get("ground_speed", 0)

            segments = []
            if reg:
                segments.append((f'  {reg}', colours.LIGHT_PURPLE))
            if altitude:
                segments.append(('  ', colours.GREY))
                segments.append((str(altitude), colours.LIGHT_ORANGE))
                segments.append(('ft', colours.GREY))
            if speed:
                segments.append(('  ', colours.GREY))
                segments.append((str(speed), colours.LIGHT_ORANGE))
                segments.append(('kt', colours.GREY))

            for text, color in segments:
                w = graphics.DrawText(
                    self.canvas, PLANE_FONT, x, PLANE_DISTANCE_FROM_TOP,
                    color, text,
                )
                x += w
                total_text_width += w

        # Handle scrolling
        self.plane_position -= 1

        # Check if the text has completely scrolled off the screen
        if self.plane_position + total_text_width < 0:
            self.plane_position = screen.WIDTH
            if len(self._data) > 1:
                self._data_index = (self._data_index + 1) % len(self._data)
                self._data_all_looped = (not self._data_index) or self._data_all_looped
                self.reset_scene()

    @Animator.KeyFrame.add(0)
    def reset_scrolling(self):
        self.plane_position = screen.WIDTH
