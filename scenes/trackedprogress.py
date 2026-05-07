from rgbmatrix import graphics
from utilities.animator import Animator
from setup import colours, fonts, screen

PROGRESS_TOP = 16
PROGRESS_BOTTOM = 20
PLANE_CHAR = ">"
PLANE_FONT = fonts.extrasmall
PLANE_CHAR_ROW = 20
FLOWN_COLOUR = colours.LIGHT_MID_GREEN
REMAINING_COLOUR = colours.LIGHT_LIGHT_RED
PLANE_COLOUR = colours.WHITE


class TrackedProgressScene(object):
    def __init__(self):
        super().__init__()

    @Animator.KeyFrame.add(0)
    def tracked_progress(self):
        if len(self._data) == 0:
            return

        flight = self._data[self._data_index]
        progress = max(0.0, min(1.0, flight.get("progress", 0.0)))

        self.draw_square(0, PROGRESS_TOP, screen.WIDTH, PROGRESS_BOTTOM + 1, colours.BLACK)

        split_x = int(progress * screen.WIDTH)

        if split_x > 0:
            self.draw_square(0, PROGRESS_TOP, split_x, PROGRESS_BOTTOM + 1, FLOWN_COLOUR)

        if split_x < screen.WIDTH:
            self.draw_square(split_x, PROGRESS_TOP, screen.WIDTH, PROGRESS_BOTTOM + 1, REMAINING_COLOUR)

        plane_x = max(0, min(screen.WIDTH - 5, split_x - 2))
        graphics.DrawText(
            self.canvas, PLANE_FONT, plane_x, PLANE_CHAR_ROW,
            PLANE_COLOUR, PLANE_CHAR,
        )
