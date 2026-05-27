"""
rain.py – Minutely precipitation and wind alerts via OpenWeatherMap One Call 3.0.

Polls every 5 minutes, caches to disk. Returns rain/snow/sleet alerts and
wind alerts when sustained/gust speed exceeds 20 mph.

Usage:
    from utilities.rain import get_rain_alert, get_wind_info, get_owm_metrics
    alert = get_rain_alert()
    # {"type": "rain", "action": "starting", "minutes": 8}
    # {"type": "snow", "action": "stopping", "minutes": 20}
    # {"type": "rain", "action": "now"}   (raining, no stop within 60m)
    # None                                  (no precip expected)
"""

import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.openweathermap.org/data/3.0/onecall"
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_BASE_DIR, ".cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "rain.json")
_USAGE_FILE = os.path.join(_BASE_DIR, "data", "owm_usage.json")
_POLL_INTERVAL = 300        # 5 minutes
_PRECIP_THRESHOLD = 0.1     # mm/h – below this is dry
_WIND_THRESHOLD_MPH = 20
_MS_TO_MPH = 2.237
_DAILY_LIMIT = 1000

# In-memory cache
_cached_data = None
_cached_ts = 0.0


def _get_owm_key():
    try:
        from core.config_manager import config_manager
        return config_manager.get_secret("owm_api_key", "")
    except Exception:
        try:
            import config as cfg
            return getattr(cfg, "OWM_API_KEY", "")
        except Exception:
            return ""


def _record_call():
    """Increment daily OWM call counter in data/owm_usage.json."""
    today = time.strftime("%Y-%m-%d")
    try:
        os.makedirs(os.path.dirname(_USAGE_FILE), exist_ok=True)
        try:
            with open(_USAGE_FILE) as f:
                usage = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            usage = {}
        if usage.get("date") != today:
            usage = {"date": today, "calls": 0}
        usage["calls"] = usage.get("calls", 0) + 1
        tmp = _USAGE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(usage, f)
        os.replace(tmp, _USAGE_FILE)
    except OSError as e:
        logger.warning(f"[Rain] Could not record OWM call: {e}")


def get_owm_metrics():
    """Return daily usage dict for the web UI metrics endpoint."""
    today = time.strftime("%Y-%m-%d")
    try:
        with open(_USAGE_FILE) as f:
            usage = json.load(f)
        if usage.get("date") == today:
            return {"date": today, "calls": usage.get("calls", 0), "limit": _DAILY_LIMIT}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"date": today, "calls": 0, "limit": _DAILY_LIMIT}


def _fetch(lat, lon, api_key):
    """Fetch current + minutely data from OWM One Call 3.0."""
    try:
        r = requests.get(
            _BASE_URL,
            params={
                "lat": lat,
                "lon": lon,
                "exclude": "hourly,daily,alerts",
                "appid": api_key,
            },
            timeout=(5, 15),
        )
        if r.status_code == 429:
            logger.warning("[Rain] Rate limited (429) – using cache")
            return None
        r.raise_for_status()
        data = r.json()

        _record_call()

        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "data": data}, f)
        os.replace(tmp, _CACHE_FILE)

        logger.info(f"[Rain] Fetched {len(data.get('minutely', []))} minutely points")
        return data

    except requests.exceptions.HTTPError as e:
        logger.error(f"[Rain] HTTP error: {e}")
        return None
    except Exception as e:
        logger.error(f"[Rain] Fetch failed: {e}")
        return None


def _load_cache():
    """Load from disk cache if recent enough. Returns (data, ts) or (None, 0)."""
    try:
        with open(_CACHE_FILE) as f:
            obj = json.load(f)
        ts = obj.get("ts", 0)
        if time.time() - ts < _POLL_INTERVAL * 2:
            return obj.get("data"), ts
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None, 0


def _refresh():
    """Refresh data if poll interval has elapsed. Returns cached data dict or None."""
    global _cached_data, _cached_ts

    api_key = _get_owm_key()
    if not api_key:
        return None

    import config as cfg
    location = cfg.LOCATION_HOME
    if location == [0.0, 0.0]:
        return None

    now = time.time()
    if _cached_data is not None and (now - _cached_ts) < _POLL_INTERVAL:
        return _cached_data

    if _cached_data is None:
        disk, disk_ts = _load_cache()
        if disk:
            _cached_data = disk
            _cached_ts = disk_ts
            logger.info("[Rain] Loaded from disk cache")

    if (now - _cached_ts) >= _POLL_INTERVAL:
        data = _fetch(location[0], location[1], api_key)
        if data:
            _cached_data = data
            _cached_ts = now

    return _cached_data


def _precip_type(data):
    """Determine type from current weather code: 5xx=rain, 600-610=snow, 611-616=sleet."""
    try:
        weather_id = data["current"]["weather"][0]["id"]
        if 611 <= weather_id <= 616:
            return "sleet"
        if 600 <= weather_id < 700:
            return "snow"
    except (KeyError, IndexError, TypeError):
        pass
    return "rain"


def get_rain_alert():
    """
    Return precip alert dict or None.

    Returns:
        {"type": "rain"|"snow"|"sleet", "action": "starting"|"stopping"|"now", "minutes": int}
        or None if no precipitation expected within 60 minutes.
    """
    data = _refresh()
    if not data:
        return None

    minutely = data.get("minutely")
    if not minutely or len(minutely) < 2:
        return None

    precip_type = _precip_type(data)
    current_precip = minutely[0].get("precipitation", 0)
    is_raining = current_precip >= _PRECIP_THRESHOLD

    if not is_raining:
        for i, m in enumerate(minutely[1:], start=1):
            if m.get("precipitation", 0) >= _PRECIP_THRESHOLD:
                return {"type": precip_type, "action": "starting", "minutes": i}
        return None
    else:
        for i, m in enumerate(minutely[1:], start=1):
            if m.get("precipitation", 0) < _PRECIP_THRESHOLD:
                return {"type": precip_type, "action": "stopping", "minutes": i}
        return {"type": precip_type, "action": "now"}


def get_wind_info():
    """
    Return wind alert dict if wind exceeds threshold, else None.

    Returns {"text": "Wind 25", "color": "cyan"} or None.
    Uses existing OWM data — no extra API call.
    """
    data = _refresh()
    if not data:
        return None
    try:
        current = data.get("current", {})
        wind_speed_ms = current.get("wind_speed", 0) or 0
        wind_gust_ms = current.get("wind_gust", 0) or 0
        peak_mph = int(max(wind_speed_ms, wind_gust_ms) * _MS_TO_MPH)
        if peak_mph >= _WIND_THRESHOLD_MPH:
            return {"text": f"Wind {peak_mph}", "color": "cyan"}
    except (KeyError, TypeError, ValueError):
        pass
    return None
