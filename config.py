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
# Unified units setting — drives temperature, distance, speed, and snow depth.
# Falls back to legacy distance_units for backward compatibility.
UNITS = _loc.get("units", _loc.get("distance_units", "imperial"))
TEMPERATURE_UNITS = UNITS
DISTANCE_UNITS = UNITS
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

# Alert features (used as fallback; AlertsScene reads config_manager at runtime)
_cw_alerts = _plg.get("clock_weather", {}).get("alerts", {})
ALERTS_OWM_PRECIP   = _cw_alerts.get("owm_precip",    False)
ALERTS_OWM_WIND     = _cw_alerts.get("owm_wind",       False)
ALERTS_NWS          = _cw_alerts.get("nws_alerts",     False)
ALERTS_FAA          = _cw_alerts.get("faa_delays",     False)
ALERTS_ISS          = _cw_alerts.get("iss_passes",     False)
ALERTS_SUN          = _cw_alerts.get("sun_countdown",  False)

# OWM API key (dynamic reads via config_manager preferred in utilities)
OWM_API_KEY = _sec.get("owm_api_key", "")

# Additional airports to monitor for FAA delays (comma-separated IATA codes)
AIRPORT_STATUS_LIST = _loc.get("airport_status_list", "")

# Flight tracker plugin settings
_ft = _plg.get("flight_tracker", {})
MIN_ALTITUDE = _ft.get("min_altitude", 8000)

# Flights logging
MAX_FARTHEST = _flt.get("max_farthest", 5)
MAX_CLOSEST = _flt.get("max_closest", 5)
EMAIL = _flt.get("email", "")
