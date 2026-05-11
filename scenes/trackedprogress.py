from utilities.animator import Animator
from setup import colours, screen

PROGRESS_AREA_TOP = 20
PROGRESS_AREA_BOTTOM = 24
PROGRESS_LINE_Y = 22

FLOWN_COLOUR = colours.LIGHT_MID_GREEN
REMAINING_COLOUR = colours.LIGHT_LIGHT_RED
PLANE_COLOUR = colours.WHITE

DASH_ON = 2
DASH_OFF = 2


class TrackedProgressScene(object):
    def __init__(self):
        super().__init__()

    @Animator.KeyFrame.add(1)
    def tracked_progress(self, count):
        if len(self._data) == 0:
            return

        flight = self._data[self._data_index]
        progress = max(0.0, min(1.0, flight.get("progress", 0.0)))

        self.draw_square(0, PROGRESS_AREA_TOP, screen.WIDTH, PROGRESS_AREA_BOTTOM, colours.BLACK)

        split_x = int(progress * screen.WIDTH)

        # Dashed progress line (2px on, 2px off)
        for x in range(screen.WIDTH):
            if x % (DASH_ON + DASH_OFF) < DASH_ON:
                colour = FLOWN_COLOUR if x < split_x else REMAINING_COLOUR
                r, g, b = colour.red, colour.green, colour.blue
                self.canvas.SetPixel(x, PROGRESS_LINE_Y, r, g, b)

        # 3-pixel tall arrow at progress position
        ax = max(0, min(screen.WIDTH - 2, split_x))
        pw, ph = PLANE_COLOUR.red, PLANE_COLOUR.green
        pb = PLANE_COLOUR.blue
        self.canvas.SetPixel(ax,     PROGRESS_LINE_Y - 1, pw, ph, pb)
        self.canvas.SetPixel(ax,     PROGRESS_LINE_Y,     pw, ph, pb)
        self.canvas.SetPixel(ax,     PROGRESS_LINE_Y + 1, pw, ph, pb)
        self.canvas.SetPixel(ax + 1, PROGRESS_LINE_Y,     pw, ph, pb)
