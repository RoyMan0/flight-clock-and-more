"""
airport_status.py – FAA NAS airport delay/ground stop alerts.

Free, no API key. Polls every 5 minutes. FAA endpoint returns XML.
Only major airports appear in the FAA feed (JFK, LGA, EWR, ORD, etc. —
not small GA fields).

Usage:
    from utilities.airport_status import get_airport_alerts
    alerts = get_airport_alerts()
    # [{"text": "JFK Delay", "color": "orange"}, {"text": "EWR GStop", "color": "red"}]
"""

import json
import logging
import os
import time
import xml.etree.ElementTree as ET

import requests

logger = logging.getLogger(__name__)

_API_URL = "https://nasstatus.faa.gov/api/airport-status-information"
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_DIR = os.path.join(_BASE_DIR, ".cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "airport_status.json")
_POLL_INTERVAL = 300  # 5 minutes

# In-memory cache
_cached_data = None   # raw response text
_cached_ts = 0.0


def _fetch():
    """Fetch current airport status XML from FAA NAS."""
    try:
        r = requests.get(_API_URL, timeout=(5, 15))
        r.raise_for_status()
        content = r.text

        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "content": content}, f)
        os.replace(tmp, _CACHE_FILE)

        logger.info("[AirportStatus] Fetched FAA status")
        return content

    except Exception as e:
        logger.error(f"[AirportStatus] Fetch failed: {e}")
        return None


def _load_cache():
    """Load from disk cache if recent enough. Returns (content, ts) or (None, 0)."""
    try:
        with open(_CACHE_FILE) as f:
            obj = json.load(f)
        ts = obj.get("ts", 0)
        if time.time() - ts < _POLL_INTERVAL * 2:
            return obj.get("content"), ts
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass
    return None, 0


def _refresh():
    """Refresh data if poll interval has elapsed."""
    global _cached_data, _cached_ts

    now = time.time()
    if _cached_data is not None and (now - _cached_ts) < _POLL_INTERVAL:
        return _cached_data

    if _cached_data is None:
        disk, disk_ts = _load_cache()
        if disk is not None:
            _cached_data = disk
            _cached_ts = disk_ts
            logger.info("[AirportStatus] Loaded from disk cache")

    if (now - _cached_ts) >= _POLL_INTERVAL:
        content = _fetch()
        if content is not None:
            _cached_data = content
            _cached_ts = now

    return _cached_data


def _parse_xml(content, watch_set):
    """Parse FAA XML response into alert dicts for watched airports."""
    alerts = []
    seen = set()

    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        logger.error(f"[AirportStatus] XML parse error: {e}")
        return []

    # Helper: iterate all elements with a given tag anywhere in tree
    def find_all(tag):
        return root.iter(tag)

    # Ground Stops (highest severity)
    for gs in find_all("Ground_Stop"):
        arpt = (gs.findtext("ARPT") or "").strip().upper()
        if arpt in watch_set and arpt not in seen:
            seen.add(arpt)
            alerts.append({"text": f"{arpt} GStop", "color": "red"})

    # Ground Delays
    for gd in find_all("Ground_Delay"):
        arpt = (gd.findtext("ARPT") or "").strip().upper()
        if arpt in watch_set and arpt not in seen:
            seen.add(arpt)
            alerts.append({"text": f"{arpt} Delay", "color": "orange"})

    # Arrival/Departure Delays — tag may be "Delay" inside Arrival_Departure_Delay_List
    for delay_list in root.iter("Arrival_Departure_Delay_List"):
        for item in delay_list:
            arpt = (item.findtext("ARPT") or "").strip().upper()
            if arpt in watch_set and arpt not in seen:
                seen.add(arpt)
                alerts.append({"text": f"{arpt} Delay", "color": "orange"})

    # Airport Closures
    for cl in find_all("Airport_Closure"):
        arpt = (cl.findtext("ARPT") or "").strip().upper()
        if arpt in watch_set and arpt not in seen:
            seen.add(arpt)
            alerts.append({"text": f"{arpt} Closd", "color": "red"})

    return alerts


def get_airport_alerts():
    """
    Return list of alert dicts for configured airports.

    Each dict: {"text": "JFK Delay", "color": "orange"}
    Returns [] if no delays or no airports configured.

    Reads JOURNEY_CODE_SELECTED (home airport) from config, plus optional
    AIRPORT_STATUS_LIST for additional airports.
    """
    import config as cfg

    watch = set()
    home = getattr(cfg, "JOURNEY_CODE_SELECTED", "").strip().upper()
    if home:
        watch.add(home)
    extra = getattr(cfg, "AIRPORT_STATUS_LIST", "")
    for a in extra.split(","):
        a = a.strip().upper()
        if a:
            watch.add(a)

    if not watch:
        return []

    content = _refresh()
    if not content:
        return []

    return _parse_xml(content, watch)
