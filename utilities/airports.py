"""
airports.py — Local airport database backed by airports.json.
Provides reverse coordinate→IATA lookup for cases where an API returns
lat/lon for a non-standard airport with no IATA code.
"""

import json
import logging
import os

log = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(__file__))
_DB_PATH  = os.path.join(_BASE_DIR, "airports.json")
_db: dict = {}
_loaded   = False


def _load():
    global _db, _loaded
    if _loaded:
        return
    try:
        with open(_DB_PATH, "r", encoding="utf-8") as f:
            _db = json.load(f)
        log.debug(f"[airports] Loaded {len(_db)} entries")
    except Exception as e:
        log.warning(f"[airports] Could not load airports.json: {e}")
    _loaded = True


def get_nearest_airport(lat: float, lon: float, max_dist_km: float = 15) -> str | None:
    """
    Find the nearest IATA airport code within max_dist_km of the given coordinates.
    Returns 3-letter IATA code or None if nothing close enough.
    Uses simple degree-distance (1 degree ≈ 111 km) — sufficient for this purpose.
    """
    _load()
    if lat is None or lon is None:
        return None

    best_code = None
    best_dist = float("inf")

    for code, entry in _db.items():
        if len(code) != 3:
            continue
        dlat = entry.get("lat", 0) - lat
        dlon = entry.get("lon", 0) - lon
        dist = (dlat ** 2 + dlon ** 2) ** 0.5
        if dist < best_dist:
            best_dist = dist
            best_code = code

    if best_dist * 111 <= max_dist_km:
        return best_code
    return None
