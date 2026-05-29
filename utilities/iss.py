"""
iss.py — ISS overhead pass predictions.

Uses the free Pollux Labs ISS API (no key required).
Polls every 30 minutes for upcoming visible passes.
Shows alert when a visible pass is within 10 minutes.

Usage:
    from utilities.iss import get_iss_alert
    alert = get_iss_alert()
    # {"text": "ISS 3m", "color": "white"}  or  None
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_API_URL = "https://iss-api.polluxlabs.io/iss-pass"
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_BASE_DIR, ".cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "iss.json")
_POLL_INTERVAL = 1800  # 30 minutes
_ALERT_WINDOW = 600    # alert when pass within 10 minutes

# In-memory cache
_cached_passes = None
_cached_ts = 0.0


def _fetch(lat, lon):
    """Fetch upcoming visible ISS passes from Pollux Labs."""
    try:
        r = requests.get(
            _API_URL,
            params={"lat": lat, "lon": lon, "visible_only": "true"},
            timeout=(5, 15),
        )
        r.raise_for_status()
        passes = r.json().get("passes", [])

        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "passes": passes}, f)
        os.replace(tmp, _CACHE_FILE)

        logger.info(f"[ISS] Fetched {len(passes)} visible passes")
        return passes

    except Exception as e:
        logger.warning(f"[ISS] Fetch failed: {e}")
        return None


def _load_cache():
    """Load from disk cache if recent enough. Returns (passes, ts) or (None, 0)."""
    try:
        with open(_CACHE_FILE) as f:
            obj = json.load(f)
        ts = obj.get("ts", 0)
        if time.time() - ts < _POLL_INTERVAL * 2:
            return obj.get("passes", []), ts
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None, 0


def _refresh():
    """Refresh pass list if poll interval has elapsed."""
    global _cached_passes, _cached_ts

    import config as cfg
    location = cfg.LOCATION_HOME
    if location == [0.0, 0.0]:
        return []

    now = time.time()
    if _cached_passes is not None and (now - _cached_ts) < _POLL_INTERVAL:
        return _cached_passes

    if _cached_passes is None:
        disk, disk_ts = _load_cache()
        if disk is not None:
            _cached_passes = disk
            _cached_ts = disk_ts
            logger.info("[ISS] Loaded from disk cache")

    if (now - _cached_ts) >= _POLL_INTERVAL:
        passes = _fetch(location[0], location[1])
        if passes is not None:
            _cached_passes = passes
        # Always update timestamp so a failed fetch waits the full interval before retry
        _cached_ts = now

    return _cached_passes or []


def _azimuth_to_compass(degrees):
    """Convert azimuth degrees to 2-char compass direction string."""
    if degrees is None:
        return None
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((degrees + 22.5) / 45) % 8]


def get_iss_pass_data():
    """
    Return detailed metrics for the currently active ISS pass, or None.

    Returns:
        {
            "rise_time": datetime,
            "duration_sec": int,
            "elapsed_sec": float,
            "progress": float,       # 0.0–1.0
            "remaining_sec": float,
            "rise_azimuth": str|None,
            "set_azimuth":  str|None,
            "max_elevation": int|None,
        }
        or None if no pass is currently active.
    """
    passes = _refresh()
    if not passes:
        return None

    now = datetime.now(timezone.utc)

    for p in passes:
        try:
            rise_str = p.get("rise", {}).get("time", "")
            if not rise_str:
                continue
            rise_time = datetime.fromisoformat(rise_str.replace("Z", "+00:00"))
            duration = p.get("duration_sec", 0)
            elapsed = (now - rise_time).total_seconds()

            if 0 <= elapsed <= duration:
                el = p.get("max", {}).get("elevation")
                return {
                    "rise_time":     rise_time,
                    "duration_sec":  duration,
                    "elapsed_sec":   elapsed,
                    "progress":      elapsed / duration if duration > 0 else 0,
                    "remaining_sec": max(0.0, duration - elapsed),
                    "rise_azimuth":  _azimuth_to_compass(p.get("rise", {}).get("azimuth")),
                    "set_azimuth":   _azimuth_to_compass(p.get("set",  {}).get("azimuth")),
                    "max_elevation": int(el) if el is not None else None,
                }
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"[ISS] Pass data parse error: {e}")
            continue

    return None


def get_iss_alert():
    """
    Return alert dict if a visible ISS pass is within 10 minutes, else None.

    During an active pass, returns None — the full-screen takeover scene handles
    that state when iss_fullscreen is enabled; the alert text is suppressed to
    avoid the alert bar and the takeover scene fighting for the display.

    Returns {"text": "ISS 3m", "color": "white"} or None.
    """
    passes = _refresh()
    if not passes:
        return None

    now = datetime.now(timezone.utc)

    for p in passes:
        try:
            rise_str = p.get("rise", {}).get("time", "")
            if not rise_str:
                continue
            rise_time = datetime.fromisoformat(rise_str.replace("Z", "+00:00"))
            seconds_until = (rise_time - now).total_seconds()

            if seconds_until < 0:
                # Pass is active — suppress "ISS now!" regardless of fullscreen toggle.
                # ISSPassScene handles active-pass display; alerts.py handles pre-pass.
                continue

            if seconds_until <= _ALERT_WINDOW:
                mins = max(1, int(seconds_until / 60))
                return {"text": f"ISS {mins}m", "color": "white"}

        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"[ISS] Skipping pass with parse error: {e}")
            continue

    return None
