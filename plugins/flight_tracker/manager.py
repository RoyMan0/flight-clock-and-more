"""
FlightTrackerPlugin — wraps the existing flight scenes and Overhead data fetcher.

Priority behavior:
  has_live_priority() always returns True (this plugin can always interrupt).
  has_live_content() returns True only when planes are actually overhead.
  The plugin_manager checks both every frame and switches to this plugin
  immediately when a plane is detected, then returns to normal rotation
  when the sky clears.
"""

import logging
from plugins.base_plugin import BasePlugin
from utilities.animator import Animator
from utilities.overhead import Overhead, play_plane_sound
from scenes.flightdetails import FlightDetailsScene
from scenes.flightlogo import FlightLogoScene
from scenes.journey import JourneyScene
from scenes.planedetails import PlaneDetailsScene
from scenes.loadingpulse import LoadingPulseScene
from scenes.radarscene import RadarScene
from core.config_manager import get_config
from setup import frames

log = logging.getLogger(__name__)


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

    def __init__(self, display_manager, overhead):
        self.canvas = display_manager.canvas
        self.matrix = display_manager.matrix
        self.overhead = overhead
        self._data = []
        self._data_index = 0
        self._data_all_looped = False
        self._scroll_complete = {"flight_details": False, "plane_details": False}

        super().__init__()
        self._show_additional_details = False

        self.delay = frames.PERIOD

    def mark_scroll_complete(self, region):
        if region in self._scroll_complete:
            self._scroll_complete[region] = True

    def reset_scroll_completion(self):
        self._scroll_complete = {"flight_details": False, "plane_details": False}

    @Animator.KeyFrame.add(1)
    def advance_completed_scroll(self, count):
        if len(self._data) <= 1:
            return
        if all(self._scroll_complete.values()):
            self._data_index = (self._data_index + 1) % len(self._data)
            self._data_all_looped = self._data_index == 0 or self._data_all_looped
            self.reset_scroll_completion()
            self.reset_scene()

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
        if self.enabled:
            self.overhead.grab_data()
        self._last_data: list = []
        self._radar_scene = RadarScene()
        self._radar_dirty = True

    def _ensure_display(self):
        if self._display is None:
            self._display = _FlightDisplay(self.display_manager, self.overhead)

    # ------------------------------------------------------------------
    # BasePlugin interface
    # ------------------------------------------------------------------

    @property
    def update_interval(self) -> float:
        if self.config.get("radar_mode", False):
            return 15.0
        return float(self.config.get("update_interval", 60))

    def update(self):
        """Periodic data refresh — called from plugin_manager background thread."""
        if not self.enabled:
            return
        radar_mode = self.config.get("radar_mode", False)
        if not self.overhead.processing and (
            radar_mode
            or self._display is None
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
        self._display._show_additional_details = self.config.get("show_additional_details", False)
        self._display.reset_scene()
        self._radar_dirty = True

    def draw(self) -> bool:
        self._ensure_display()
        self._display.canvas = self.display_manager.canvas
        self._display.matrix = self.display_manager.matrix

        # Pull in fresh data from Overhead if available
        if self.overhead.new_data:
            was_empty = len(self._display._data) == 0
            new_data = self.overhead.data
            log.info(f"[flight_tracker] draw: was_empty={was_empty}, flights={len(new_data)}")
            if _callsigns(self._display._data) != _callsigns(new_data):
                self._display._data_index = 0
                self._display._data_all_looped = False
                self._display.reset_scroll_completion()
                self._display._data = new_data
                self._display.reset_scene()
            self._radar_dirty = True
            if was_empty and len(new_data) > 0:
                play_plane_sound()

        if self.config.get("radar_mode", False):
            return self._draw_radar()

        self._display.tick()
        return len(self._display._data) > 0

    def _draw_radar(self) -> bool:
        flights = self._display._data if self._display else []
        if not flights:
            return False
        if not self._radar_dirty:
            return True
        cfg = get_config()
        loc = cfg.get("location") or {}
        home = loc.get("location_home", [39.725715, -105.203208])
        home_lat, home_lon = home[0], home[1]
        radius_nm = float(loc.get("search_radius_nm") or 50)
        self._radar_scene.draw(
            self.display_manager.canvas,
            flights,
            home_lat,
            home_lon,
            radius_nm,
        )
        self._radar_dirty = False
        return True

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
        if self.overhead.data_is_empty:
            if self._display is not None:
                self._display._data = []
            return False
        return True

    # ------------------------------------------------------------------
    # Public accessors (used by web dashboard)
    # ------------------------------------------------------------------

    def get_current_flights(self) -> list:
        if self._display is None:
            return []
        return list(self._display._data)
