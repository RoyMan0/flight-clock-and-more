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

            if self.frame > 0 and (
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

    def describe(self):
        from datetime import datetime, timezone
        from config import CLOCK_FORMAT
        from utilities.describe import rgb
        d = self._display
        now = datetime.now()
        out = {"view": "clock",
               "time": now.strftime("%l:%M" if CLOCK_FORMAT == "12hr" else "%H:%M").strip(),
               "ampm": now.strftime("%p") if CLOCK_FORMAT == "12hr" else "",
               "date": now.strftime("%b %d"), "weekday": now.strftime("%A")}
        if d is not None and getattr(d, "_iss_active", False):
            try:
                from utilities.iss import get_iss_pass_data
                data = get_iss_pass_data()
            except Exception:
                data = None
            if data:
                return {**out, "view": "iss", "progress": data.get("progress"),
                        "remaining_sec": data.get("remaining_sec"), "rise_azimuth": data.get("rise_azimuth"),
                        "set_azimuth": data.get("set_azimuth"), "max_elevation": data.get("max_elevation")}

        rise, sset = (d.today_sunrise, d.today_sunset) if d is not None else (None, None)
        now_utc = datetime.now(timezone.utc)
        out["daytime"] = None if rise is None or sset is None else bool(rise <= now_utc < sset)
        moon = d.today_moonphase if d is not None else None
        out["moon_phase"] = moon
        if d is not None and moon is not None:
            a, b = d.map_moon_phase_to_color(moon)
            out["date_colors"] = [rgb(a), rgb(b)]

        temp, hum = get_temperature_cached()
        out["temp"] = None if temp is None else round(temp)
        out["humidity"] = hum

        days = []
        today = datetime.now().astimezone().date()
        from scenes.daysforecast import current_forecast
        kind, entries = current_forecast()          # the next days, or the next parts of the day
        out["forecast_kind"] = kind
        for day in entries or []:
            try:
                local = datetime.fromisoformat(day["startTime"])
                if "label" not in day and local.date() < today:
                    continue
                v = day["values"]
                days.append({"day": day.get("label") or local.strftime("%a"), "code": v.get("weatherCodeFullDay"),
                             "max": round(v["temperatureMax"]), "min": round(v["temperatureMin"]),
                             "night": bool(day.get("night"))})
            except Exception:
                continue
            if len(days) == 3:
                break
        out["forecast"] = days

        alerts = list(getattr(d, "_active_alerts", []) or []) if d is not None else []
        if alerts:
            n = len(alerts)
            cycle = 3 if n > 4 else 4
            slot = max(0, getattr(d, "_alert_cycle_counter", 1) - 1) // cycle % n
            out["alerts"] = [{"text": t, "color": rgb(c)} for t, c in alerts]
            out["alert_index"] = slot
        return out

    def is_cycle_complete(self) -> bool:
        # Clock/weather runs continuously; never signals completion
        return False
