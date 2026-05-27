import time
import logging
from datetime import datetime, timezone

from utilities.animator import Animator
from setup import colours, fonts, frames
from rgbmatrix import graphics

logger = logging.getLogger(__name__)

# Color name strings (from NWS/FAA/ISS utilities) → RGB Color objects
_ALERT_COLOURS = {
    "red":    colours.RED,
    "orange": colours.ORANGE,
    "cyan":   colours.CYAN,
    "yellow": colours.YELLOW,
    "grey":   colours.GREY,
    "white":  colours.WHITE,
    "blue":   colours.LIGHT_BLUE,
}

_SUN_WINDOW_SECONDS = 1800  # 30 minutes


def _get_sun_alerts(sunrise, sunset):
    """Return countdown alert for upcoming sunrise or sunset within 30 minutes."""
    if not sunrise or not sunset:
        return []
    now = datetime.now(timezone.utc)
    alerts = []
    for dt, label in ((sunrise, "Rise"), (sunset, "Sun")):
        secs = (dt - now).total_seconds()
        if 0 < secs <= _SUN_WINDOW_SECONDS:
            mins = max(1, int(secs / 60))
            alerts.append((f"{label} {mins}m", colours.LIGHT_ORANGE))
    return alerts


class AlertsScene(object):
    def __init__(self):
        super().__init__()
        self._active_alerts = []        # list of (text, color_obj)
        self._has_active_alerts = False
        self._alert_cycle_counter = 0   # increments 1/sec; slot = counter // interval % len
        self._prev_had_alerts = False
        self._alert_last_fetch = 0.0

    def _build_alert_items(self, plugin_cfg):
        """Gather (text, color) tuples from all enabled alert sources."""
        items = []
        alerts_cfg = plugin_cfg.get("alerts", {})

        if alerts_cfg.get("owm_precip"):
            try:
                from utilities.rain import get_rain_alert
                ra = get_rain_alert()
                if ra:
                    label = {"rain": "Rain", "snow": "Snow", "sleet": "Sleet"}.get(ra["type"], "Rain")
                    if ra["action"] == "starting":
                        items.append((f"{label} {ra['minutes']}m", colours.LIGHT_BLUE))
                    elif ra["action"] == "stopping":
                        items.append((f"Stop {ra['minutes']}m", colours.YELLOW))
                    else:
                        items.append((label, colours.LIGHT_BLUE))
            except Exception as e:
                logger.debug(f"[Alerts] OWM precip error: {e}")

        if alerts_cfg.get("owm_wind"):
            try:
                from utilities.rain import get_wind_info
                wi = get_wind_info()
                if wi:
                    items.append((wi["text"], _ALERT_COLOURS.get(wi["color"], colours.WHITE)))
            except Exception as e:
                logger.debug(f"[Alerts] OWM wind error: {e}")

        if alerts_cfg.get("nws_alerts"):
            try:
                from utilities.nws_alerts import get_active_alerts
                for a in get_active_alerts():
                    items.append((a["text"], _ALERT_COLOURS.get(a["color"], colours.GREY)))
            except Exception as e:
                logger.debug(f"[Alerts] NWS error: {e}")

        if alerts_cfg.get("faa_delays"):
            try:
                from utilities.airport_status import get_airport_alerts
                for a in get_airport_alerts():
                    items.append((a["text"], _ALERT_COLOURS.get(a["color"], colours.ORANGE)))
            except Exception as e:
                logger.debug(f"[Alerts] FAA error: {e}")

        if alerts_cfg.get("iss_passes"):
            try:
                from utilities.iss import get_iss_alert
                ia = get_iss_alert()
                if ia:
                    items.append((ia["text"], colours.WHITE))
            except Exception as e:
                logger.debug(f"[Alerts] ISS error: {e}")

        if alerts_cfg.get("sun_countdown"):
            try:
                items += _get_sun_alerts(
                    getattr(self, "today_sunrise", None),
                    getattr(self, "today_sunset", None),
                )
            except Exception as e:
                logger.debug(f"[Alerts] Sun countdown error: {e}")

        return items

    @Animator.KeyFrame.add(frames.PER_SECOND * 1)
    def alerts(self, count):
        if getattr(self, '_iss_active', False):
            return
        from core.config_manager import get_config as _get_config
        config_manager = _get_config()
        now = time.time()

        # Refresh alert list every 60 seconds
        if now - self._alert_last_fetch >= 60:
            try:
                plugin_cfg = config_manager.get_plugin_config("clock_weather")
                self._active_alerts = self._build_alert_items(plugin_cfg)
                self._has_active_alerts = bool(self._active_alerts)
            except Exception as e:
                logger.error(f"[Alerts] Build failed: {e}")
                self._active_alerts = []
                self._has_active_alerts = False
            self._alert_last_fetch = now

        # Detect mode change (alerts appeared or disappeared)
        if self._has_active_alerts != self._prev_had_alerts:
            self.draw_square(0, 0, 64, 12, colours.BLACK)
            self._redraw_time = True
            self._redraw_temp = True
            self._redraw_date = True
            self._prev_had_alerts = self._has_active_alerts
            self._alert_cycle_counter = 0

        if not self._active_alerts:
            return

        # Rotation: 3s per item when >4 active, else 4s
        n = len(self._active_alerts)
        cycle_secs = 3 if n > 4 else 4
        slot = (self._alert_cycle_counter // cycle_secs) % n

        # Clear alert row when slot changes
        if self._alert_cycle_counter > 0:
            prev_slot = ((self._alert_cycle_counter - 1) // cycle_secs) % n
            if slot != prev_slot:
                self.draw_square(0, 6, 64, 12, colours.BLACK)

        self._alert_cycle_counter += 1

        text, color = self._active_alerts[slot]
        graphics.DrawText(self.canvas, fonts.extrasmall, 1, 11, color, text)
