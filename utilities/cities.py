"""
Nearest-city lookup using the GeoNames cities5000 dataset.

On first call the zip is downloaded in a background thread (~29 MB) and
parsed into data/cities.json.  Subsequent calls load from the JSON cache.
Returns None gracefully while the download is in progress.
"""

import io
import json
import logging
import math
import os
import zipfile
from threading import Thread, Lock

import requests

log = logging.getLogger(__name__)

_BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CITIES_FILE  = os.path.join(_BASE_DIR, "data", "cities.json")
_GEONAMES_URL = "https://download.geonames.org/export/dump/cities5000.zip"
_CACHE_VER    = 1

_db:   list = []        # [[name, lat, lon], ...]
_lock: Lock = Lock()
_ready        = False
_downloading  = False


def _haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1 = math.radians(lat1), math.radians(lon1)
    lat2, lon2 = math.radians(lat2), math.radians(lon2)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _parse_txt(raw_bytes: bytes) -> list:
    cities = []
    for line in raw_bytes.splitlines():
        try:
            fields = line.decode("utf-8").split("\t")
            if len(fields) < 6:
                continue
            name = fields[2].strip() or fields[1].strip()
            if not name:
                continue
            lat = float(fields[4])
            lon = float(fields[5])
            cities.append([name, lat, lon])
        except Exception:
            continue
    return cities


def _download_and_cache():
    global _db, _ready, _downloading
    try:
        log.info("[cities] Downloading GeoNames cities5000.zip …")
        resp = requests.get(_GEONAMES_URL, timeout=120, stream=True)
        resp.raise_for_status()
        raw = resp.content
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            txt_name = next(n for n in zf.namelist() if n.endswith(".txt"))
            txt_bytes = zf.read(txt_name)
        cities = _parse_txt(txt_bytes)
        os.makedirs(os.path.dirname(_CITIES_FILE), exist_ok=True)
        with open(_CITIES_FILE, "w", encoding="utf-8") as f:
            json.dump({"_version": _CACHE_VER, "cities": cities}, f)
        with _lock:
            _db[:] = cities
            _ready = True
        log.info(f"[cities] Cached {len(cities):,} cities to {_CITIES_FILE}")
    except Exception as e:
        log.warning(f"[cities] Download failed: {e}")
    finally:
        with _lock:
            _downloading = False


def _ensure_loaded():
    global _db, _ready, _downloading
    with _lock:
        if _ready:
            return
        if _downloading:
            return

    # Try loading from disk first
    try:
        with open(_CITIES_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("_version") == _CACHE_VER and cached.get("cities"):
            with _lock:
                _db[:] = cached["cities"]
                _ready = True
            log.debug(f"[cities] Loaded {len(_db):,} cities from cache")
            return
    except (FileNotFoundError, Exception):
        pass

    # Kick off background download
    with _lock:
        _downloading = True
    Thread(target=_download_and_cache, daemon=True, name="cities-downloader").start()
    log.info("[cities] Started background city data download")


def get_nearest_city(lat: float, lon: float, max_km: float = 80) -> dict | None:
    """Return {'name': str, 'distance_km': float} for nearest city within max_km, or None."""
    _ensure_loaded()
    with _lock:
        snapshot = _db[:]
    if not snapshot:
        return None

    best_name = None
    best_dist = float("inf")
    for name, clat, clon in snapshot:
        d = _haversine_km(lat, lon, clat, clon)
        if d < best_dist:
            best_dist = d
            best_name = name

    if best_name is None or best_dist > max_km:
        return None
    return {"name": best_name, "distance_km": best_dist}
