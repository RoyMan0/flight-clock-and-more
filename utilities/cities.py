"""
Nearest-city lookup using the GeoNames cities5000 dataset.

On first call the zip is downloaded in a background thread (~29 MB) and
parsed into data/cities.json.  Subsequent calls load from the JSON cache.
Returns None gracefully while the download is in progress.

get_region() names what's below a point with no city nearby, offline: the sea or
ocean (from the world map's land mask and named boxes) or, over remote land, the
country of the nearest city (GeoNames countryInfo, fetched with the cities).
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

from utilities.paths import DATA_ROOT as _BASE_DIR
_CITIES_FILE  = os.path.join(_BASE_DIR, "data", "cities.json")
_GEONAMES_URL = "https://download.geonames.org/export/dump/cities5000.zip"
_COUNTRY_URL  = "https://download.geonames.org/export/dump/countryInfo.txt"
_CACHE_VER    = 2       # 2: each city carries its country code, plus a country-name table

_db:   list = []        # [[name, lat, lon, country code], ...]
_countries: dict = {}   # ISO code → country name
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
            cc = fields[8].strip().upper() if len(fields) > 8 else ""
            cities.append([name, lat, lon, cc])
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
        countries = {}
        try:
            r = requests.get(_COUNTRY_URL, timeout=60)
            r.raise_for_status()
            for line in r.text.splitlines():
                if line.startswith("#"):
                    continue
                f = line.split("\t")
                if len(f) > 4 and f[0].strip() and f[4].strip():
                    countries[f[0].strip().upper()] = f[4].strip()
        except Exception as e:
            log.warning(f"[cities] Country names unavailable: {e}")
        os.makedirs(os.path.dirname(_CITIES_FILE), exist_ok=True)
        with open(_CITIES_FILE, "w", encoding="utf-8") as f:
            json.dump({"_version": _CACHE_VER, "cities": cities, "countries": countries}, f)
        with _lock:
            _db[:] = cities
            _countries.clear()
            _countries.update(countries)
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
                _countries.clear()
                _countries.update(cached.get("countries") or {})
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

    best, best_dist = None, float("inf")
    for row in snapshot:
        d = _haversine_km(lat, lon, row[1], row[2])
        if d < best_dist:
            best_dist, best = d, row

    if best is None or best_dist > max_km:
        return None
    cc = best[3] if len(best) > 3 else ""
    return {"name": best[0], "distance_km": best_dist, "country_code": cc,
            "country": _countries.get(cc, "")}


# ── What's below, away from any city (oceans, seas, remote land) ─────────────
from utilities.paths import APP_ROOT as _APP_ROOT
_LAND_MASK = os.path.join(_APP_ROOT, "assets", "world_daylight", "land_mask.png")
_mask = None

# (name, lat_min, lat_max, lon_min, lon_max) — seas and gulfs first, then the oceans
_WATERS = (
    ("Gulf of Mexico", 18, 31, -98, -80), ("Caribbean Sea", 8, 22, -88, -59),
    ("Hudson Bay", 51, 66, -95, -76), ("Labrador Sea", 50, 66, -64, -44),
    ("Gulf of Alaska", 50, 61, -155, -130), ("Bering Sea", 52, 66, 162, 180),
    ("Bering Sea", 52, 66, -180, -157), ("Mediterranean", 30, 46, -6, 36),
    ("Black Sea", 40, 47, 27, 42), ("Red Sea", 12, 30, 32, 44), ("Persian Gulf", 23, 30, 47, 57),
    ("North Sea", 51, 61, -4, 9), ("Baltic Sea", 53, 66, 9, 30), ("Norwegian Sea", 62, 75, -12, 20),
    ("Barents Sea", 68, 81, 20, 60), ("Arabian Sea", 5, 25, 51, 75), ("Bay of Bengal", 5, 23, 79, 95),
    ("South China Sea", 0, 23, 103, 121), ("East China Sea", 24, 33, 119, 130),
    ("Sea of Japan", 33, 52, 127, 142), ("Sea of Okhotsk", 43, 62, 135, 163),
    ("Coral Sea", -25, -9, 145, 165), ("Tasman Sea", -45, -28, 150, 172),
    ("Arctic Ocean", 66, 90, -180, 180), ("Southern Ocean", -90, -55, -180, 180),
    ("Atlantic", -55, 66, -80, 20), ("Indian Ocean", -55, 30, 20, 147),
    ("Pacific", -55, 66, 100, 180), ("Pacific", -55, 66, -180, -66),
)

_FIRST_OCEAN = next(i for i, w in enumerate(_WATERS) if w[0] == "Arctic Ocean")   # seas come before it


def _is_water(lat: float, lon: float) -> bool:
    """From the world map's 64×32 land mask (5.6° cells): coarse, so only asked away from cities."""
    global _mask
    if _mask is None:
        try:
            from PIL import Image
            img = Image.open(_LAND_MASK).convert("L")
            _mask = (img.size, img.load())
        except Exception:
            _mask = False
    if not _mask:
        return False
    (w, h), px = _mask
    x = min(w - 1, max(0, int((lon + 180) / 360 * w)))
    y = min(h - 1, max(0, int((90 - lat) / 180 * h)))
    return px[x, y] <= 127


def get_region(lat: float, lon: float, max_km: float = 2000) -> dict | None:
    """For a point with no city nearby: {"name", "kind": "water"} for a sea or ocean, or
    {"name", "kind": "land"} with the country of the nearest city (within max_km)."""
    box = next((w for w in _WATERS if w[1] <= lat <= w[2] and w[3] <= lon <= w[4]), None)
    if _is_water(lat, lon):
        return {"name": box[0], "kind": "water"} if box else None
    near = get_nearest_city(lat, lon, max_km=max_km)
    # The mask's cells are big: an enclosed sea (the Caribbean, the North Sea) can read as
    # land. Inside a sea with no city for 150 km, it's the sea.
    if box and _WATERS.index(box) < _FIRST_OCEAN and (not near or near["distance_km"] > 150):
        return {"name": box[0], "kind": "water"}
    if near and near.get("country"):
        return {"name": near["country"], "kind": "land"}
    return None
