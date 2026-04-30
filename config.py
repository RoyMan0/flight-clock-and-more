"""
Compatibility shim — reads from config/config.json and config/secrets.json.
All existing scenes continue to import from this module unchanged.
"""
import json
import os

_BASE = os.path.dirname(os.path.abspath(__file__))


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


_cfg = _load(os.path.join(_BASE, "config", "config.json"))
_sec = _load(os.path.join(_BASE, "config", "secrets.json"))

_loc = _cfg.get("location", {})
_disp = _cfg.get("display", {})
_flt = _cfg.get("flights", {})
_plg = _cfg.get("plugins", {})

# Location / units
ZONE_HOME = _loc.get("zone_home", {
    "tl_y": 39.796827, "tl_x": -105.298648,
    "br_y": 39.666880, "br_x": -105.110076,
})
LOCATION_HOME = _loc.get("location_home", [39.725715, -105.203208])
TEMPERATURE_LOCATION = _loc.get("temperature_location", "39.725715,-105.203208")
TEMPERATURE_UNITS = _loc.get("temperature_units", "imperial")
DISTANCE_UNITS = _loc.get("distance_units", "imperial")
CLOCK_FORMAT = _loc.get("clock_format", "12hr")
JOURNEY_CODE_SELECTED = _loc.get("journey_code", "DEN")
JOURNEY_BLANK_FILLER = _loc.get("journey_blank_filler", " ? ")

# Display
BRIGHTNESS = _disp.get("brightness", 100)
BRIGHTNESS_NIGHT = _disp.get("brightness_night", 50)
NIGHT_BRIGHTNESS = _disp.get("night_brightness", True)
NIGHT_START = _disp.get("night_start", "18:00")
NIGHT_END = _disp.get("night_end", "07:00")
GPIO_SLOWDOWN = _disp.get("gpio_slowdown", 2)
HAT_PWM_ENABLED = _disp.get("hat_pwm_enabled", False)

# Secrets
TOMORROW_API_KEY = _sec.get("tomorrow_api_key", "")

# Forecast
FORECAST_DAYS = _plg.get("clock_weather", {}).get("forecast_days", 3)

# Flight tracker plugin settings
_ft = _plg.get("flight_tracker", {})
MIN_ALTITUDE = _ft.get("min_altitude", 8000)

# Flights logging
MAX_FARTHEST = _flt.get("max_farthest", 5)
MAX_CLOSEST = _flt.get("max_closest", 5)
EMAIL = _flt.get("email", "")
