"""
FlightTrackerPlugin — wraps the existing flight scenes and Overhead data fetcher.

Priority behavior:
  has_live_priority() always returns True (this plugin can always interrupt).
  has_live_content() returns True only when planes are actually overhead.
  The plugin_manager checks both every frame and switches to this plugin
  immediately when a plane is detected, then returns to normal rotation
  when the sky clears.
"""

from plugins.base_plugin import BasePlugin
from utilities.animator import Animator
from utilities.overhead import Overhead
from scenes.flightdetails import FlightDetailsScene
from scenes.flightlogo import FlightLogoScene
from scenes.journey import JourneyScene
from scenes.planedetails import PlaneDetailsScene
from scenes.loadingpulse import LoadingPulseScene
from setup import frames


def _callsigns(flights):
    return set((f["callsign"], f["direction"]) for f in flights)


class _FlightDisplay(
    FlightDetailsScene,
    FlightLogoScene,
    JourneyScene,
    PlaneDetailsScene,
    LoadingPulseScene,
    Animator,
):
    """Internal display class for flight scenes."""

    def __init__(self, display_manager):
        self.canvas = display_manager.canvas
        self.matrix = display_manager.matrix
        self._data = []
        self._data_index = 0
        self._data_all_looped = False

        super().__init__()

        self.delay = frames.PERIOD

    def draw_square(self, x0, y0, x1, y1, colour):
        try:
            from rgbmatrix import graphics
            for x in range(x0, x1):
                graphics.DrawLine(self.canvas, x, y0, x, y1, colour)
        except ImportError:
            pass

    def tick(self):
        for keyframe in self.keyframes:
            if self.frame == 0 and keyframe.properties["divisor"] == 0:
                keyframe()

            if (
                self.frame > 0
                and keyframe.properties["divisor"]
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


class FlightTrackerPlugin(BasePlugin):
    PLUGIN_ID = "flight_tracker"

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._display: _FlightDisplay | None = None
        self.overhead = Overhead(secrets=secrets)
        self.overhead.grab_data()
        self._last_data: list = []

    def _ensure_display(self):
        if self._display is None:
            self._display = _FlightDisplay(self.display_manager)

    # ------------------------------------------------------------------
    # BasePlugin interface
    # ------------------------------------------------------------------

    def update(self):
        """Periodic data refresh — called from plugin_manager background thread."""
        if not (self.overhead.processing and self.overhead.new_data) and (
            self._display is None
            or self._display._data_all_looped
            or len(self._display._data) <= 1
        ):
            self.overhead.grab_data()

    def reset(self):
        self._ensure_display()
        self._display.canvas = self.display_manager.canvas
        self._display.matrix = self.display_manager.matrix
        self._display._data_index = 0
        self._display._data_all_looped = False
        self._display.reset_scene()

    def draw(self) -> bool:
        self._ensure_display()
        self._display.canvas = self.display_manager.canvas
        self._display.matrix = self.display_manager.matrix

        # Pull in fresh data from Overhead if available
        if self.overhead.new_data:
            new_data = self.overhead.data
            if _callsigns(self._display._data) != _callsigns(new_data):
                self._display._data_index = 0
                self._display._data_all_looped = False
                self._display._data = new_data
                self._display.reset_scene()

        self._display.tick()
        return len(self._display._data) > 0

    def is_cycle_complete(self) -> bool:
        if self._display is None:
            return True
        return self._display._data_all_looped

    # ------------------------------------------------------------------
    # Priority
    # ------------------------------------------------------------------

    def has_live_priority(self) -> bool:
        return self.enabled

    def has_live_content(self) -> bool:
        return not self.overhead.data_is_empty

    # ------------------------------------------------------------------
    # Public accessors (used by web dashboard)
    # ------------------------------------------------------------------

    def get_current_flights(self) -> list:
        if self._display is None:
            return []
        return list(self._display._data)
