from rgbmatrix import graphics
from utilities.animator import Animator
from setup import colours, fonts, screen

CALLSIGN_ROW = 6
ROUTE_ROW = 14
HEADER_FONT = fonts.small
CALLSIGN_COLOUR = colours.WHITE
ARROW_COLOUR = colours.GREY


class TrackedHeaderScene(object):
    def __init__(self):
        super().__init__()
        self.route_position = screen.WIDTH

    @Animator.KeyFrame.add(0)
    def tracked_header_callsign(self):
        if len(self._data) == 0:
            return

        flight = self._data[self._data_index]
        callsign = flight.get("callsign", "")

        self.draw_square(0, 0, screen.WIDTH, CALLSIGN_ROW, colours.BLACK)

        graphics.DrawText(
            self.canvas, HEADER_FONT, 0, CALLSIGN_ROW,
            CALLSIGN_COLOUR, callsign,
        )

    @Animator.KeyFrame.add(1)
    def tracked_header_route(self, count):
        if len(self._data) == 0:
            return

        flight = self._data[self._data_index]
        origin = flight.get("origin", "???")
        destination = flight.get("destination", "???")
        origin_color = flight.get("origin_color", colours.LIGHT_GREY)
        destination_color = flight.get("destination_color", colours.LIGHT_GREY)

        segments = [
            (origin, origin_color),
            (" > ", ARROW_COLOUR),
            (destination, destination_color),
        ]

        self.draw_square(0, CALLSIGN_ROW + 1, screen.WIDTH, ROUTE_ROW, colours.BLACK)

        x = self.route_position
        total_width = 0
        for text, colour in segments:
            w = graphics.DrawText(self.canvas, HEADER_FONT, x + total_width, ROUTE_ROW, colour, text)
            total_width += w

        self.route_position -= 1
        if self.route_position + total_width < 0:
            self.route_position = screen.WIDTH

    @Animator.KeyFrame.add(0)
    def reset_tracked_header(self):
        self.route_position = screen.WIDTH
