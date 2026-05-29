"""
ClockWeatherPlugin — wraps the existing ClockScene, DateScene, TemperatureScene,
and DaysForecastScene into the new plugin interface.

The existing scenes use the Animator keyframe pattern (play() infinite loop).
Here we replace play() with tick() — one frame per call — and drive it from
the plugin_manager's main loop instead.
"""

from plugins.base_plugin import BasePlugin
from utilities.animator import Animator
from utilities.temperature import get_temperature_cached, get_forecast_cached
from scenes.clock import ClockScene
from scenes.date import DateScene
from scenes.temperature import TemperatureScene
from scenes.daysforecast import DaysForecastScene
from scenes.alerts import AlertsScene
from scenes.isspass import ISSPassScene
from setup import frames


class _ClockWeatherDisplay(ClockScene, DateScene, TemperatureScene, DaysForecastScene, AlertsScene, ISSPassScene, Animator):
    """
    Internal display class using the existing multiple-inheritance scene pattern.
    All scenes share this single object via cooperative __init__ chaining.
    """

    def __init__(self, display_manager):
        # Set up shared state that all scenes expect on 'self'
        self.canvas = display_manager.canvas
        self.matrix = display_manager.matrix
        self._data = []           # No flight data — clock/weather only
        self._data_index = 0
        self._data_all_looped = True

        # Chain all scene __init__ methods + Animator.__init__
        super().__init__()

        self.delay = frames.PERIOD

    def draw_square(self, x0, y0, x1, y1, colour):
        """Helper expected by scenes; draws a filled rectangle."""
        try:
            from rgbmatrix import graphics
            for x in range(x0, x1):
                graphics.DrawLine(self.canvas, x, y0, x, y1, colour)
        except ImportError:
            pass

    def tick(self):
        """Run one animator frame (non-blocking replacement for play())."""
        for keyframe in self.keyframes:
            if self.frame == 0 and keyframe.properties["divisor"] == 0:
                keyframe()

            # No 'frame > 0' guard: at frame=0, 0 % N == 0 for all N, so every
            # keyframe fires immediately on the first tick after activation.
            if (
                keyframe.properties["divisor"]
                and not (
                    (self.frame - keyframe.properties["offset"])
                    % keyframe.properties["divisor"]
                )
            ):
                if keyframe(keyframe.properties["count"]):
                    keyframe.properties["count"] = 0
                else:
                    keyframe.properties["count"] += 1

        self.frame += 1


class ClockWeatherPlugin(BasePlugin):
    PLUGIN_ID = "clock_weather"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._display: _ClockWeatherDisplay | None = None

    def _ensure_display(self):
        if self._display is None:
            self._display = _ClockWeatherDisplay(self.display_manager)

    def reset(self):
        self._ensure_display()
        # Update canvas reference in case it changed after a swap
        self._display.canvas = self.display_manager.canvas
        self._display.matrix = self.display_manager.matrix
        self._display.frame = 0   # restart keyframe cycle so frame-0 fires all scenes immediately
        self._display.reset_scene()

    def draw(self) -> bool:
        self._ensure_display()
        # Keep canvas reference current (SwapOnVSync returns a new canvas each frame)
        self._display.canvas = self.display_manager.canvas
        self._display.matrix = self.display_manager.matrix
        self._display.tick()
        return True

    def update(self):
        # Pre-fetch in background so the render thread never blocks on an API call
        get_temperature_cached()
        get_forecast_cached()

    def is_cycle_complete(self) -> bool:
        # Clock/weather runs continuously; never signals completion
        return False
