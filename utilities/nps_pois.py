"""
Nearest National Park / Monument / etc. lookup using the NPS API.

Fetched once at startup (or on first call) and cached to data/nps_pois.json.
Falls back to DEMO_KEY when no nps_api_key is set in secrets.json.
"""

import json
import logging
import math
import os

import requests

log = logging.getLogger(__name__)

_BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_POI_FILE    = os.path.join(_BASE_DIR, "data", "nps_pois.json")
_SECRETS     = os.path.join(_BASE_DIR, "config", "secrets.json")
_NPS_URL     = "https://developer.nps.gov/api/v1/parks"
_CACHE_VER   = 1

# Strip these suffixes (longest first) to get display name
_STRIP = [
    " National Park & Preserve",
    " National Park and Preserve",
    " National Park",
    " National Monument & Preserve",
    " National Monument",
    " National Recreation Area",
    " National Forest",
    " National Seashore",
    " National Lakeshore",
    " National Historic Site",
    " National Historical Park",
    " National Preserve",
    " National Parkway",
    " National Memorial",
    " National Battlefield",
    " National Military Park",
]

_db: list = []    # [[name, lat, lon], ...]
_loaded = False


def _truncate_name(full_name: str) -> str:
    for suffix in _STRIP:
        if full_name.endswith(suffix):
            return full_name[: -len(suffix)].strip()
    return full_name.strip()


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1 = math.radians(lat1), math.radians(lon1)
    lat2, lon2 = math.radians(lat2), math.radians(lon2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _read_api_key() -> str:
    try:
        with open(_SECRETS, "r") as f:
            return json.load(f).get("nps_api_key", "DEMO_KEY") or "DEMO_KEY"
    except Exception:
        return "DEMO_KEY"


def _fetch_and_cache():
    global _db, _loaded
    api_key = _read_api_key()
    pois = []
    start = 0
    limit = 500
    while True:
        try:
            resp = requests.get(
                _NPS_URL,
                params={"limit": limit, "start": start, "api_key": api_key},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            log.warning(f"[nps_pois] Fetch failed (start={start}): {e}")
            break

        parks = body.get("data") or []
        for park in parks:
            full_name = park.get("fullName", "")
            short_name = park.get("name", "")
            # Use short name if it's reasonable, else truncate full name
            display = short_name if short_name and len(short_name) <= 25 else _truncate_name(full_name)
            if not display:
                continue
            try:
                lat = float(park.get("latitude", "0") or "0")
                lon = float(park.get("longitude", "0") or "0")
            except (ValueError, TypeError):
                continue
            if lat == 0.0 and lon == 0.0:
                continue
            pois.append([display, lat, lon])

        total = int(body.get("total", 0))
        start += len(parks)
        if start >= total or not parks:
            break

    if pois:
        os.makedirs(os.path.dirname(_POI_FILE), exist_ok=True)
        with open(_POI_FILE, "w", encoding="utf-8") as f:
            json.dump({"_version": _CACHE_VER, "pois": pois}, f)
        _db[:] = pois
        log.info(f"[nps_pois] Cached {len(pois)} POIs")
    else:
        log.warning("[nps_pois] No POIs fetched — NPS data will be unavailable")
    _loaded = True


def _ensure_loaded():
    global _db, _loaded
    if _loaded:
        return

    try:
        with open(_POI_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("_version") == _CACHE_VER and cached.get("pois"):
            _db[:] = cached["pois"]
            _loaded = True
            log.debug(f"[nps_pois] Loaded {len(_db)} POIs from cache")
            return
    except (FileNotFoundError, Exception):
        pass

    # Fetch synchronously (small dataset, ~500 parks, fast)
    _fetch_and_cache()


def get_nearest_poi(lat: float, lon: float, max_km: float = 160) -> dict | None:
    """Return {'name': str, 'distance_km': float} for nearest NPS site within max_km, or None."""
    _ensure_loaded()
    if not _db:
        return None

    best_name = None
    best_dist = float("inf")
    for name, plat, plon in _db:
        d = _haversine_km(lat, lon, plat, plon)
        if d < best_dist:
            best_dist = d
            best_name = name

    if best_name is None or best_dist > max_km:
        return None
    return {"name": best_name, "distance_km": best_dist}
