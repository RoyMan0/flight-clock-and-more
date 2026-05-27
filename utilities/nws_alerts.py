"""
nws_alerts.py — Active weather alerts from the NWS API.

Free, no API key required. Polls every 15 minutes by lat/lon.
Returns abbreviated alert texts with severity-based color names
for display on a 64x32 LED matrix.

Usage:
    from utilities.nws_alerts import get_active_alerts
    alerts = get_active_alerts()
    # [{"text": "HiSurf", "color": "cyan"}, {"text": "RipCurr", "color": "cyan"}]
    # or []
"""

import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.weather.gov/alerts/active"
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_BASE_DIR, ".cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "nws_alerts.json")
_POLL_INTERVAL = 900  # 15 minutes
_USER_AGENT = "LEDMatrixClock/1.0 (flight tracker LED display)"

# Abbreviation map: NWS event name → (short text ≤9 chars, color name)
_ALERT_MAP = {
    # Life-threatening warnings — red
    "Tornado Warning":               ("Tornado!",  "red"),
    "Tornado Emergency":             ("TornEmrg",  "red"),
    "Severe Thunderstorm Warning":   ("SvrStorm",  "red"),
    "Hurricane Warning":             ("Hurrican",  "red"),
    "Hurricane Force Wind Warning":  ("HrcnWind",  "red"),
    "Flash Flood Warning":           ("FlFlood",   "red"),
    "Tsunami Warning":               ("Tsunami!",  "red"),
    "Extreme Wind Warning":          ("ExtWind",   "red"),
    "Storm Surge Warning":           ("StrmSrge",  "red"),
    # Significant warnings — orange
    "Flood Warning":                 ("Flood",     "orange"),
    "High Wind Warning":             ("HiWind",   "orange"),
    "Blizzard Warning":              ("Blizzard",  "orange"),
    "Ice Storm Warning":             ("IceStorm",  "orange"),
    "Winter Storm Warning":          ("WntStorm",  "orange"),
    "Heat Advisory":                 ("Heat Adv",  "orange"),
    "Excessive Heat Warning":        ("ExtHeat",   "orange"),
    "Tropical Storm Warning":        ("TropStrm",  "orange"),
    "Fire Weather Watch":            ("FireWthr",  "orange"),
    "Red Flag Warning":              ("RedFlag",   "orange"),
    "Wind Chill Warning":            ("WndChill",  "orange"),
    "Coastal Flood Warning":         ("CstFldW",   "orange"),
    # Advisories and statements — cyan
    "High Surf Advisory":            ("HiSurf",    "cyan"),
    "Rip Current Statement":         ("RipCurr",   "cyan"),
    "Wind Advisory":                 ("Wind Adv",  "cyan"),
    "Wind Chill Advisory":           ("WndChill",  "cyan"),
    "Freeze Warning":                ("Freeze",    "cyan"),
    "Frost Advisory":                ("Frost",     "cyan"),
    "Winter Weather Advisory":       ("WntWthr",   "cyan"),
    "Coastal Flood Advisory":        ("CstFlood",  "cyan"),
    "Dense Fog Advisory":            ("DenseFog",  "cyan"),
    "Beach Hazards Statement":       ("BeachHaz",  "cyan"),
    "Marine Weather Statement":      ("MarineWx",  "cyan"),
    "Small Craft Advisory":          ("SmCraft",   "cyan"),
    "Hazardous Seas Warning":        ("HazSeas",   "cyan"),
    # Air quality — yellow
    "Air Quality Alert":             ("AirQlty",   "yellow"),
    "Air Quality Action Day":        ("AirQlty",   "yellow"),
    # Watches — grey (lower urgency)
    "Tornado Watch":                 ("TornWtch",  "grey"),
    "Severe Thunderstorm Watch":     ("StrmWtch",  "grey"),
    "Flash Flood Watch":             ("FldWatch",  "grey"),
    "Winter Storm Watch":            ("WntWatch",  "grey"),
    "Hurricane Watch":               ("HrcnWtch",  "grey"),
    "Tropical Storm Watch":          ("TropWtch",  "grey"),
    "Special Weather Statement":     ("SpclWthr",  "grey"),
}

# In-memory cache
_cached_alerts = None
_cached_ts = 0.0


def _fetch(lat, lon):
    """Fetch active alerts from NWS for a location."""
    try:
        r = requests.get(
            _BASE_URL,
            params={"point": f"{lat},{lon}", "status": "actual", "message_type": "alert,update"},
            headers={"User-Agent": _USER_AGENT, "Accept": "application/geo+json"},
            timeout=(5, 15),
        )
        r.raise_for_status()
        features = r.json().get("features", [])

        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "features": features}, f)
        os.replace(tmp, _CACHE_FILE)

        logger.info(f"[NWS] Fetched {len(features)} active alerts")
        return features

    except Exception as e:
        logger.error(f"[NWS] Fetch failed: {e}")
        return None


def _load_cache():
    """Load from disk cache if recent enough. Returns (features, ts) or (None, 0)."""
    try:
        with open(_CACHE_FILE) as f:
            obj = json.load(f)
        ts = obj.get("ts", 0)
        if time.time() - ts < _POLL_INTERVAL * 2:
            return obj.get("features", []), ts
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None, 0


def _refresh():
    """Refresh data if poll interval has elapsed."""
    global _cached_alerts, _cached_ts

    import config as cfg
    location = cfg.LOCATION_HOME
    if location == [0.0, 0.0]:
        return []

    now = time.time()
    if _cached_alerts is not None and (now - _cached_ts) < _POLL_INTERVAL:
        return _cached_alerts

    if _cached_alerts is None:
        disk, disk_ts = _load_cache()
        if disk is not None:
            _cached_alerts = disk
            _cached_ts = disk_ts
            logger.info("[NWS] Loaded from disk cache")

    if (now - _cached_ts) >= _POLL_INTERVAL:
        features = _fetch(location[0], location[1])
        if features is not None:
            _cached_alerts = features
            _cached_ts = now

    return _cached_alerts or []


def get_active_alerts():
    """
    Return list of active alert dicts for display.

    Each dict: {"text": "HiSurf", "color": "cyan"}
    Returns [] if no alerts or no location configured.
    Deduplicates by event name.
    """
    features = _refresh()
    if not features:
        return []

    seen = set()
    alerts = []
    for feature in features:
        props = feature.get("properties", {})
        event = props.get("event", "")
        if not event or event in seen:
            continue
        seen.add(event)

        mapping = _ALERT_MAP.get(event)
        if mapping:
            text, color = mapping
        else:
            text = event[:9]
            color = "grey"
        alerts.append({"text": text, "color": color})

    return alerts
