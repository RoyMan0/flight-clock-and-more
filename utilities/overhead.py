"""
Overhead — detects aircraft overhead using free public APIs.

Data sources (in order of fetch):
  Positions:    adsb.lol      free, no key required
  Route data:   adsbdb.com    free, no key required
  Schedule:     AirLabs       1,000 calls/month free (key in secrets.json)
  Schedule FB:  FlightAware   ~$5/month free credit  (key in secrets.json)

AirLabs and FlightAware are optional. If their keys are absent, the
journey scene shows grey delay indicators instead of colored ones.
"""

import os
import json
import math
import time
import hashlib
import subprocess
import logging
from threading import Thread, Lock
from datetime import datetime, timedelta
from typing import Optional

import requests

from core.config_manager import get_config
from setup import email_alerts
try:
    from web import map_generator, upload_helper
    _MAP_ENABLED = True
except Exception:
    _MAP_ENABLED = False

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------

ADSB_LOL_URL    = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"
ADSBDB_URL      = "https://api.adsbdb.com/v0/callsign/{callsign}"
AIRLABS_URL     = "https://airlabs.co/api/v9/flight"
FLIGHTAWARE_URL = "https://aeroapi.flightaware.com/aeroapi/flights/{ident}"

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

MAX_ALTITUDE       = 100000   # ft hard cap
ROUTE_CACHE_TTL    = 24 * 3600   # seconds — persisted to disk, wall-clock expiry
SCHEDULE_CACHE_TTL = 6 * 3600   # fallback when no arrival time known

EARTH_RADIUS_M = 3958.8   # miles

BLANK_FIELDS = {"", "N/A", "NONE"}

# Callsign prefixes whose adsbdb routes are unreliable (GA operators,
# carriers that frequently carry stale route data)
SKIP_ADSBDB_PREFIXES = {
    "KAP",   # Cape Air
    "EJA",   # NetJets
}

BASE_DIR          = os.path.dirname(os.path.dirname(__file__))
LOG_FILE          = os.path.join(BASE_DIR, "close.txt")
LOG_FILE_FARTHEST = os.path.join(BASE_DIR, "farthest.txt")
FA_USAGE_FILE     = os.path.join(BASE_DIR, "flightaware_usage.json")
AL_USAGE_FILE     = os.path.join(BASE_DIR, "data", "airlabs_usage.json")
AIRPORT_DB_FILE   = os.path.join(BASE_DIR, "data", "airports_cache.json")
API_CACHE_FILE    = os.path.join(BASE_DIR, "data", "api_cache.json")
HISTORY_FILE      = os.path.join(BASE_DIR, "data", "flight_history.json")

HISTORY_DAYS = 90

AIRLABS_MONTHLY_LIMIT = 1000

# Marketing carrier lookup for codeshare flights: IATA → (display name, ICAO)
# When AirLabs returns cs_airline_iata, use this to show the marketing brand
# (e.g. Delta instead of SkyWest) and load the correct logo.
_CODESHARE_CARRIERS = {
    "AA": ("American",   "AAL"),
    "AS": ("Alaska",     "ASA"),
    "B6": ("JetBlue",    "JBU"),
    "DL": ("Delta",      "DAL"),
    "F9": ("Frontier",   "FFT"),
    "G4": ("Allegiant",  "AAY"),
    "HA": ("Hawaiian",   "HAL"),
    "NK": ("Spirit",     "NKS"),
    "SY": ("Sun Country","SCX"),
    "UA": ("United",     "UAL"),
    "WN": ("Southwest",  "SWA"),
}

# OpenFlights airports CSV — fetched once and cached locally
OPENFLIGHTS_URL = (
    "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
)

# Module-level airport DB: iata → (lat, lon).  Populated lazily.
_airport_db: dict = {}
_airport_db_loaded: bool = False


def _load_airport_db():
    """Load IATA→(lat,lon) from local cache or fetch from OpenFlights."""
    global _airport_db, _airport_db_loaded
    if _airport_db_loaded:
        return

    cache_path = AIRPORT_DB_FILE
    # Try loading from local cache first
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _airport_db = {k: tuple(v) for k, v in raw.items()}
        _airport_db_loaded = True
        log.debug(f"[overhead] Loaded {len(_airport_db)} airports from cache")
        return
    except (FileNotFoundError, json.JSONDecodeError, Exception):
        pass

    # Fetch from OpenFlights
    try:
        resp = requests.get(OPENFLIGHTS_URL, timeout=15)
        resp.raise_for_status()
        db = {}
        import csv, io
        reader = csv.reader(io.StringIO(resp.text))
        for row in reader:
            try:
                iata = row[4].strip().strip('"')
                lat  = float(row[6])
                lon  = float(row[7])
                if iata and iata != r"\N" and len(iata) == 3:
                    db[iata] = (lat, lon)
            except (IndexError, ValueError):
                continue
        _airport_db = db
        _airport_db_loaded = True
        # Cache to disk
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({k: list(v) for k, v in db.items()}, f)
        log.info(f"[overhead] Fetched and cached {len(db)} airports from OpenFlights")
    except Exception as e:
        log.warning(f"[overhead] Could not load airport database: {e}")
        _airport_db_loaded = True  # don't retry on every call

# ------------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------------

def safe_load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def safe_load_json_dict(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def safe_write_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)


def ordinal(n: int) -> str:
    return f"{n}{'tsnrhtdd'[(n//10 % 10 != 1) * (n % 10 < 4) * n % 10::4]}"


def haversine(lat1, lon1, lat2, lon2, units="imperial") -> float:
    lat1, lon1 = map(math.radians, (lat1, lon1))
    lat2, lon2 = map(math.radians, (lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    miles = EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return miles * 1.609 if units == "metric" else miles


def degrees_to_cardinal(deg) -> str:
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return dirs[int((deg + 22.5) / 45) % 8]


def bearing_to(home_lat, home_lon, plane_lat, plane_lon) -> float:
    lat1, lon1 = map(math.radians, (home_lat, home_lon))
    lat2, lon2 = map(math.radians, (plane_lat, plane_lon))
    b = math.atan2(
        math.sin(lon2 - lon1) * math.cos(lat2),
        math.cos(lat1) * math.sin(lat2)
        - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1),
    )
    return (math.degrees(b) + 360) % 360


def iso_to_unix(iso_str: Optional[str]) -> Optional[int]:
    """Convert ISO 8601 / UTC datetime string → Unix timestamp, or None.
    Naive datetimes (no timezone) are assumed UTC, as returned by AirLabs *_utc fields."""
    if not iso_str:
        return None
    try:
        from datetime import timezone as _tz
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _clean(val) -> str:
    if not val or str(val).upper() in BLANK_FIELDS:
        return ""
    return str(val)


# ------------------------------------------------------------------
# Key helpers
# ------------------------------------------------------------------

def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _get_keys(secrets: dict, array_key: str, single_key: str) -> list[str]:
    """Return list of API keys, supporting both array and legacy single-key formats."""
    keys = secrets.get(array_key)
    if isinstance(keys, list):
        return [k for k in keys if k]
    single = secrets.get(single_key, "")
    return [single] if single else []


def _get_reset_days(secrets: dict, key: str, count: int) -> list[int]:
    """Return per-key reset days, padding with 1 (calendar month) for missing entries."""
    days = secrets.get(key, [])
    if not isinstance(days, list):
        days = []
    return [int(days[i]) if i < len(days) and days[i] else 1 for i in range(count)]


def _billing_period(reset_day: int) -> str:
    """Return the billing period identifier for a key that resets on reset_day each month.
    E.g. reset_day=4, today=May 7 → '2026-05'; today=May 2 → '2026-04'."""
    today = datetime.now()
    if today.day >= reset_day:
        return today.strftime("%Y-%m")
    prev = today.replace(day=1) - timedelta(days=1)
    return prev.strftime("%Y-%m")


# ------------------------------------------------------------------
# FlightAware budget tracking (per-key)
# ------------------------------------------------------------------

FA_COST_PER_CALL = 0.005

def _fa_load_usage() -> dict:
    try:
        with open(FA_USAGE_FILE, "r") as f:
            d = json.load(f)
        # Migrate old flat format {"calls": N, "cost": X} or old per-key with top-level month
        if "keys" not in d:
            month = d.get("month", datetime.now().strftime("%Y-%m"))
            d = {"keys": {"legacy": {"calls": d.get("calls", 0), "cost": d.get("cost", 0.0), "period": month}}}
        elif "month" in d:
            # Old format stored period at top level — push it into each entry
            month = d.pop("month")
            for entry in d["keys"].values():
                entry.setdefault("period", month)
        return d
    except Exception:
        return {"keys": {}}


def _fa_key_calls(entry: dict, period: str) -> tuple[int, float]:
    """Return (calls, cost) for an entry, zeroing if the billing period has rolled over."""
    if entry.get("period") != period:
        return 0, 0.0
    return entry.get("calls", 0), entry.get("cost", 0.0)


def _fa_record_call(key: str, reset_day: int = 1, cost_per_call: float = FA_COST_PER_CALL):
    usage = _fa_load_usage()
    h = _key_hash(key)
    period = _billing_period(reset_day)
    # Absorb legacy entry into this key
    if "legacy" in usage["keys"]:
        leg = usage["keys"].pop("legacy")
        leg_period = leg.get("period", period)
        existing = usage["keys"].setdefault(h, {"calls": 0, "cost": 0.0, "period": period})
        if leg_period == period:
            existing["calls"] += leg.get("calls", 0)
            existing["cost"] = round(existing["cost"] + leg.get("cost", 0.0), 4)
    entry = usage["keys"].setdefault(h, {"calls": 0, "cost": 0.0, "period": period})
    # Reset if billing period rolled over
    if entry.get("period") != period:
        entry["calls"] = 0
        entry["cost"] = 0.0
        entry["period"] = period
    entry["calls"] += 1
    entry["cost"] = round(entry["cost"] + cost_per_call, 4)
    with open(FA_USAGE_FILE, "w") as f:
        json.dump(usage, f)


def _fa_get_active_key(keys: list[str], budget: float, reset_days: list[int] = None) -> Optional[str]:
    """Return the first FlightAware key still under budget, or None."""
    if not keys:
        return None
    if reset_days is None:
        reset_days = []
    usage = _fa_load_usage()
    key_data = usage["keys"]
    legacy = key_data.get("legacy", {})
    for i, key in enumerate(keys):
        reset_day = reset_days[i] if i < len(reset_days) else 1
        period = _billing_period(reset_day)
        _, cost = _fa_key_calls(key_data.get(_key_hash(key), {}), period)
        if i == 0:
            _, leg_cost = _fa_key_calls(legacy, period)
            cost += leg_cost
        if cost < budget:
            return key
    return None


def get_fa_metrics(keys: list[str], budget: float, reset_days: list[int] = None) -> dict:
    """Return FlightAware usage metrics for the metrics endpoint."""
    if reset_days is None:
        reset_days = []
    usage = _fa_load_usage()
    key_data = usage["keys"]
    legacy = key_data.get("legacy", {})
    key_metrics = []
    for i, key in enumerate(keys):
        reset_day = reset_days[i] if i < len(reset_days) else 1
        period = _billing_period(reset_day)
        calls, cost = _fa_key_calls(key_data.get(_key_hash(key), {}), period)
        if i == 0:
            leg_calls, leg_cost = _fa_key_calls(legacy, period)
            calls += leg_calls
            cost = round(cost + leg_cost, 4)
        key_metrics.append({
            "index": i,
            "calls": calls,
            "cost": round(cost, 4),
            "remaining_budget": round(max(0.0, budget - cost), 4),
            "exhausted": cost >= budget,
            "reset_day": reset_day,
        })
    return {"budget": budget, "keys": key_metrics}


# ------------------------------------------------------------------
# AirLabs call tracking (per-key)
# ------------------------------------------------------------------

def _al_load_usage() -> dict:
    try:
        with open(AL_USAGE_FILE, "r") as f:
            d = json.load(f)
        # Migrate old flat format {"calls": N}
        if "calls" in d and "keys" not in d:
            d = {"keys": {"legacy": {"calls": d["calls"]}}}
        elif "month" in d and "keys" in d:
            month = d.pop("month")
            for entry in d["keys"].values():
                entry.setdefault("period", month)
        return d
    except Exception:
        return {"keys": {}}


def _al_key_calls(entry: dict, period: str) -> int:
    if entry.get("period") != period:
        return 0
    return entry.get("calls", 0)


def _al_record_call(key: str, reset_day: int = 1):
    usage = _al_load_usage()
    h = _key_hash(key)
    period = _billing_period(reset_day)
    # Absorb legacy entry into this key
    if "legacy" in usage["keys"]:
        leg = usage["keys"].pop("legacy")
        leg_period = leg.get("period", period)
        existing = usage["keys"].setdefault(h, {"calls": 0, "period": period})
        if leg_period == period:
            existing["calls"] += leg.get("calls", 0)
    entry = usage["keys"].setdefault(h, {"calls": 0, "period": period})
    # Reset if billing period rolled over
    if entry.get("period") != period:
        entry["calls"] = 0
        entry["period"] = period
    entry["calls"] += 1
    os.makedirs(os.path.dirname(AL_USAGE_FILE), exist_ok=True)
    with open(AL_USAGE_FILE, "w") as f:
        json.dump(usage, f)
    calls = entry["calls"]
    if calls >= AIRLABS_MONTHLY_LIMIT:
        log.warning(f"[overhead] AirLabs key exhausted ({calls} calls this billing period)")
    elif calls > AIRLABS_MONTHLY_LIMIT * 0.8:
        log.warning(f"[overhead] AirLabs: {calls}/{AIRLABS_MONTHLY_LIMIT} calls on active key")


def _al_get_active_key(keys: list[str], reset_days: list[int] = None) -> Optional[str]:
    """Return the first AirLabs key under the monthly limit, or None."""
    if not keys:
        return None
    if reset_days is None:
        reset_days = []
    usage = _al_load_usage()
    key_data = usage["keys"]
    legacy = key_data.get("legacy", {})
    for i, key in enumerate(keys):
        reset_day = reset_days[i] if i < len(reset_days) else 1
        period = _billing_period(reset_day)
        calls = _al_key_calls(key_data.get(_key_hash(key), {}), period)
        if i == 0:
            calls += _al_key_calls(legacy, period)
        if calls < AIRLABS_MONTHLY_LIMIT:
            return key
    log.warning("[overhead] All AirLabs keys exhausted for this billing period")
    return None


def _al_available(keys: list[str], reset_days: list[int] = None) -> bool:
    return _al_get_active_key(keys, reset_days) is not None


def get_al_metrics(keys: list[str], reset_days: list[int] = None) -> dict:
    """Return AirLabs usage metrics for the metrics endpoint."""
    if reset_days is None:
        reset_days = []
    usage = _al_load_usage()
    key_data = usage["keys"]
    legacy = key_data.get("legacy", {})
    key_metrics = []
    for i, key in enumerate(keys):
        reset_day = reset_days[i] if i < len(reset_days) else 1
        period = _billing_period(reset_day)
        calls = _al_key_calls(key_data.get(_key_hash(key), {}), period)
        if i == 0:
            calls += _al_key_calls(legacy, period)
        key_metrics.append({
            "index": i,
            "calls": calls,
            "remaining": max(0, AIRLABS_MONTHLY_LIMIT - calls),
            "exhausted": calls >= AIRLABS_MONTHLY_LIMIT,
            "reset_day": reset_day,
        })
    return {"limit": AIRLABS_MONTHLY_LIMIT, "keys": key_metrics}


# ------------------------------------------------------------------
# Audio alert
# ------------------------------------------------------------------

def _play_mp3(filename: str):
    def _play():
        try:
            mp3 = os.path.join(os.path.dirname(os.path.dirname(__file__)), filename)
            log.info(f"[overhead] Playing sound: {mp3}")
            uid = os.getuid()
            env = os.environ.copy()
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
            subprocess.Popen(
                ["mpg123", "-q", mp3],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except Exception as e:
            log.error(f"[overhead] Audio failed: {e}")
    Thread(target=_play, daemon=True).start()


def play_plane_sound():
    _play_mp3("airbus.mp3")


def play_espn_sound():
    _play_mp3("espn.mp3")


# ------------------------------------------------------------------
# Flight logging helpers (unchanged logic from original)
# ------------------------------------------------------------------

def log_flight_data(entry: dict, max_closest: int):
    try:
        entry["timestamp"] = email_alerts.get_timestamp()
        lst = safe_load_json(LOG_FILE)
        callsigns = {f.get("callsign"): f for f in lst}
        new_call  = entry.get("callsign")
        new_dist  = entry.get("distance", float("inf"))
        notify    = False

        if new_call in callsigns:
            idx = next(i for i, f in enumerate(lst) if f.get("callsign") == new_call)
            if new_dist < lst[idx].get("distance", float("inf")):
                lst[idx] = entry
            else:
                return
        else:
            lst.append(entry)

        lst.sort(key=lambda x: x.get("distance", float("inf")))
        top_n = lst[:max_closest]

        if new_call not in [f["callsign"] for f in top_n]:
            return
        rank = next(i + 1 for i, f in enumerate(top_n) if f["callsign"] == new_call)
        if new_call not in callsigns:
            notify = True

        safe_write_json(LOG_FILE, top_n)

        if notify:
            url = None
            if _MAP_ENABLED:
                html = map_generator.generate_closest_map(top_n, filename="closest.html")
                url  = upload_helper.upload_map_to_server(html)
            subject = f"New {ordinal(rank)} Closest Flight - {entry.get('callsign', 'Unknown')}"
            email_alerts.send_flight_summary(subject, entry, map_url=url)

    except Exception as e:
        log.warning(f"[overhead] log_flight_data error: {e}")


def log_farthest_flight(entry: dict, max_farthest: int):
    try:
        d_o = entry.get("distance_origin", -1)
        d_d = entry.get("distance_destination", -1)
        if d_o < 0 and d_d < 0:
            return

        reason  = "origin" if d_o >= d_d else "destination"
        far     = d_o if reason == "origin" else d_d
        airport = entry.get(reason)
        if not airport:
            return

        entry["timestamp"]      = email_alerts.get_timestamp()
        entry["reason"]         = reason
        entry["farthest_value"] = far
        entry["_airport"]       = airport

        lst         = safe_load_json(LOG_FILE_FARTHEST)
        airport_map = {f["_airport"]: f for f in lst}
        existing    = airport_map.get(airport)
        notify = updated = False

        if existing:
            if entry["distance"] < existing.get("distance", 9e9):
                lst     = [entry if f["_airport"] == airport else f for f in lst]
                updated = True
            else:
                return
        else:
            if len(lst) >= max_farthest:
                if far <= min(f["farthest_value"] for f in lst):
                    return
            lst.append(entry)
            notify = True

        lst.sort(key=lambda x: x["farthest_value"], reverse=True)
        lst = lst[:max_farthest]
        safe_write_json(LOG_FILE_FARTHEST, lst)

        if notify or updated:
            html = map_generator.generate_farthest_map(lst, filename="farthest.html") if _MAP_ENABLED else None
        if notify:
            url  = upload_helper.upload_map_to_server(html) if _MAP_ENABLED and html else None
            rank = next(i for i, f in enumerate(lst) if f["_airport"] == airport) + 1
            cs   = entry.get("callsign", "UNKNOWN")
            subject = (
                f"New Farthest Flight ({reason}) - {cs}"
                if rank == 1
                else f"{ordinal(rank)}-Farthest Flight ({reason}) - {cs}"
            )
            email_alerts.send_flight_summary(subject, entry, reason, map_url=url)

    except Exception as e:
        log.warning(f"[overhead] log_farthest_flight error: {e}")


def log_flight_count(entry: dict, home_airport: str):
    """Append this flight to the daily history log. Deduplicates by callsign per day."""
    try:
        callsign = entry.get("callsign", "").strip()
        if not callsign:
            return

        os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
        history = safe_load_json_dict(HISTORY_FILE)

        today = datetime.now().strftime("%Y-%m-%d")
        day = history.setdefault(today, {
            "date":                today,
            "count":               0,
            "first_seen":          None,
            "first_seen_callsign": None,
            "last_seen":           None,
            "last_seen_callsign":  None,
            "flights":             [],
        })

        # Deduplicate — only log each callsign once per day
        if any(f["callsign"] == callsign for f in day["flights"]):
            return

        now_str = datetime.now().strftime("%H:%M:%S")
        hour    = datetime.now().hour
        origin  = entry.get("origin", "")
        dest    = entry.get("destination", "")

        day["flights"].append({
            "callsign":      callsign,
            "time":          now_str,
            "hour":          hour,
            "origin":        origin,
            "destination":   dest,
            "airline_icao":  entry.get("owner_icao", ""),
            "airline":       entry.get("airline", ""),
            "aircraft_type": entry.get("plane", ""),
        })
        day["count"] = len(day["flights"])

        if day["first_seen"] is None:
            day["first_seen"]          = now_str
            day["first_seen_callsign"] = callsign
        day["last_seen"]          = now_str
        day["last_seen_callsign"] = callsign

        # Prune entries older than HISTORY_DAYS
        cutoff = (datetime.now() - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
        history = {k: v for k, v in history.items() if k >= cutoff}

        # Atomic write
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f)
        os.replace(tmp, HISTORY_FILE)

    except Exception as e:
        log.warning(f"[overhead] log_flight_count error: {e}")


# ------------------------------------------------------------------
# Geographic sanity check
# ------------------------------------------------------------------

def _route_makes_sense(plane_lat, plane_lon,
                       orig_lat, orig_lon,
                       dest_lat, dest_lon) -> bool:
    """
    Rough check that the aircraft is plausibly between its reported airports.
    Rejects stale/mismatched adsbdb data.
    """
    if None in (orig_lat, orig_lon, dest_lat, dest_lon):
        return True

    dist_orig  = haversine(plane_lat, plane_lon, orig_lat, orig_lon)
    dist_dest  = haversine(plane_lat, plane_lon, dest_lat, dest_lon)
    route_dist = haversine(orig_lat, orig_lon, dest_lat, dest_lon)

    log.debug(
        f"[overhead] sanity: dist_orig={dist_orig:.0f} dist_dest={dist_dest:.0f} "
        f"route_dist={route_dist:.0f} sum={dist_orig+dist_dest:.0f}"
    )

    if route_dist < 50:
        return True   # very short hop — skip sanity check

    # Plane should be within roughly 1.3× route dist of its two endpoints combined
    if dist_orig + dist_dest > route_dist * 1.3:
        return False

    # Plane shouldn't be further from origin than 2× the full route length
    if dist_orig > route_dist * 2.0:
        return False

    return True


# ------------------------------------------------------------------
# Overhead class
# ------------------------------------------------------------------

class Overhead:
    def __init__(self, secrets: dict = None):
        self._secrets  = secrets or {}
        self._lock     = Lock()
        self._data     = []
        self._new_data    = False
        self._processing  = False
        self._alerted_callsigns: set = set()
        self._route_cache: dict    = {}   # callsign → {"data": {...}, "expires": wall-clock unix}
        self._schedule_cache: dict = {}   # callsign → {"data": {...}, "expires": wall-clock unix}
        self._load_disk_cache()

    # ------------------------------------------------------------------
    # Persistent cache — survives service restarts
    # ------------------------------------------------------------------

    def _load_disk_cache(self):
        try:
            with open(API_CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            now = time.time()
            self._route_cache = {
                k: v for k, v in raw.get("routes", {}).items()
                if v.get("expires", 0) > now
            }
            self._schedule_cache = {
                k: v for k, v in raw.get("schedules", {}).items()
                if v.get("expires", 0) > now
            }
            log.info(
                f"[overhead] Cache loaded: {len(self._route_cache)} routes, "
                f"{len(self._schedule_cache)} schedules"
            )
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as e:
            log.debug(f"[overhead] Cache load error: {e}")

    def _save_disk_cache(self):
        try:
            os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
            with open(API_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"routes": self._route_cache, "schedules": self._schedule_cache}, f)
        except Exception as e:
            log.debug(f"[overhead] Cache save error: {e}")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def grab_data(self):
        Thread(target=self._grab, daemon=True).start()

    @property
    def new_data(self) -> bool:
        with self._lock:
            return self._new_data

    @property
    def processing(self) -> bool:
        with self._lock:
            return self._processing

    @property
    def data(self) -> list:
        with self._lock:
            self._new_data = False
            return list(self._data)

    @property
    def data_is_empty(self) -> bool:
        return len(self._data) == 0

    # ------------------------------------------------------------------
    # Main fetch
    # ------------------------------------------------------------------

    def _grab(self):
        with self._lock:
            self._new_data   = False
            self._processing = True

        cfg         = get_config()
        loc         = cfg.get("location") or {}
        ft_cfg      = cfg.get_plugin_config("flight_tracker")
        flights_cfg = cfg.get("flights") or {}

        home = loc.get("location_home", [39.725715, -105.203208])
        home_lat, home_lon = home[0], home[1]
        units        = loc.get("units", loc.get("distance_units", "imperial"))
        min_alt      = ft_cfg.get("min_altitude",    8000)
        max_alt      = ft_cfg.get("max_altitude",    MAX_ALTITUDE)
        max_lookup   = ft_cfg.get("max_flight_lookup", 5)
        max_closest  = flights_cfg.get("max_closest",  5)
        max_farthest = flights_cfg.get("max_farthest", 5)
        journey_code = loc.get("journey_code", "")

        # Derive search radius from zone_home bounding box if not configured
        radius_nm = loc.get("search_radius_nm")
        if not radius_nm:
            zone      = loc.get("zone_home", {})
            lat_span  = abs(zone.get("tl_y", 0) - zone.get("br_y", 0)) / 2
            lon_span  = abs(zone.get("tl_x", 0) - zone.get("br_x", 0)) / 2
            radius_nm = max(10.0, min(max(lat_span, lon_span) * 60, 100.0))

        data = []
        try:
            # ── Step 1: Positions ─────────────────────────────────────────────
            # adsb.lol is primary (accurate full callsigns + ICAO24 hex).
            # FR24 supplements with aircraft adsb.lol missed (satellite coverage).
            # FR24 truncates some callsigns so we never let it override adsb.lol.
            from utilities.fr24_lookup import get_flights_in_area as _fr24_area, is_available as _fr24_avail
            from utilities.opensky import get_aircraft_in_area as _opensky_area
            aircraft_list = None

            try:
                url  = ADSB_LOL_URL.format(lat=home_lat, lon=home_lon, dist=int(radius_nm))
                resp = requests.get(url, timeout=10, headers={"User-Agent": "LEDMatrix/1.0"})
                if resp.status_code == 200:
                    aircraft_list = resp.json().get("ac", [])
                    log.debug(f"[overhead] adsb.lol: {len(aircraft_list)} aircraft in radius")
                else:
                    log.warning(f"[overhead] adsb.lol {resp.status_code} — trying OpenSky")
            except Exception as e:
                log.warning(f"[overhead] adsb.lol error ({e}) — trying OpenSky")

            # FR24 supplement: add aircraft adsb.lol doesn't see (dedup by proximity)
            if _fr24_avail():
                zone = loc.get("zone_home", {})
                if zone:
                    fr24_list = _fr24_area(zone)
                    if fr24_list:
                        if aircraft_list is not None:
                            # Only add FR24 aircraft with no adsb.lol match within 2 nm
                            for fr_ac in fr24_list:
                                fr_lat = fr_ac.get("lat")
                                fr_lon = fr_ac.get("lon")
                                if fr_lat is None or fr_lon is None:
                                    continue
                                nearby = any(
                                    haversine(fr_lat, fr_lon,
                                              a.get("lat", 0), a.get("lon", 0), units) < 2.0
                                    for a in aircraft_list
                                    if a.get("lat") is not None
                                )
                                if not nearby:
                                    aircraft_list.append(fr_ac)
                                    log.debug(f"[overhead] FR24 supplement: {fr_ac.get('flight','?')}")
                        else:
                            # adsb.lol failed — use FR24 as fallback
                            aircraft_list = fr24_list
                            log.info(f"[overhead] FR24 fallback: {len(fr24_list)} aircraft")

            if aircraft_list is None:
                aircraft_list = _opensky_area(home_lat, home_lon, radius_nm)
                if not aircraft_list:
                    self._finish(data)
                    return
                log.info(f"[overhead] OpenSky fallback: {len(aircraft_list)} aircraft")

            # Filter by altitude, sort by distance, slice to max_lookup
            candidates = []
            for ac in aircraft_list:
                alt = ac.get("alt_baro")
                if alt == "ground" or not isinstance(alt, (int, float)):
                    continue
                if not (min_alt < alt < max_alt):
                    continue
                lat = ac.get("lat")
                lon = ac.get("lon")
                if lat is None or lon is None:
                    continue
                dist = haversine(lat, lon, home_lat, home_lon, units)
                candidates.append((dist, ac))

            candidates.sort(key=lambda x: x[0])
            candidates = candidates[:max_lookup]

            # ── Step 2–3: Enrich each aircraft ───────────────────────────
            for dist, ac in candidates:
                callsign = (ac.get("flight") or "").strip()
                if not callsign:
                    continue

                plane_lat  = ac.get("lat")
                plane_lon  = ac.get("lon")
                vert_speed = ac.get("baro_rate") or 0
                plane_type = ac.get("t", "") or ""

                # adsbdb aircraft lookup by hex (adsb.lol) or registration (FR24)
                _icao24 = (ac.get("hex") or "").strip().lower()
                _reg    = (ac.get("r")   or "").strip()
                from utilities.adsbdb_aircraft import get_aircraft_info as _get_aircraft_info
                _aircraft_info = _get_aircraft_info(_icao24 or _reg)

                # FR24 provides origin/destination IATA in the position dict
                _fr24_origin = (ac.get("origin") or "").strip()
                _fr24_dest   = (ac.get("destination") or "").strip()

                if callsign not in self._alerted_callsigns:
                    log.info(f"[overhead] New flight: {callsign}")
                    self._alerted_callsigns.add(callsign)

                # Route (adsbdb)
                route = self._get_route(callsign, plane_lat, plane_lon)
                owner_iata  = route.get("owner_iata", "")

                # Schedule / delay (FlightStats → AirLabs → FlightAware)
                # AirLabs and FlightAware are skipped when FR24 already provided
                # origin/destination — they are only fallbacks for unknown routes.
                plane_vs = ac.get("baro_rate")  # ft/min from adsb.lol; 0 for FR24 anon
                sched = self._get_schedule(callsign, owner_iata, plane_lat, plane_lon,
                                           fr24_has_route=bool(_fr24_origin and _fr24_dest),
                                           plane_vs=plane_vs,
                                           plane_reg=_reg)

                # Merge sources — AirLabs has live daily assignments, adsbdb is
                # historical. AirLabs wins for origin/destination; adsbdb
                # coordinates are only used when its route matches AirLabs.
                airline    = route.get("airline", "")    or sched.get("al_airline", "")
                airline    = airline or _aircraft_info.get("operator", "")
                owner_iata = owner_iata                  or sched.get("al_owner_iata", "")
                owner_icao = route.get("owner_icao", "") or sched.get("al_owner_icao", "")
                plane_type = plane_type or _aircraft_info.get("icao_type", "")

                al_origin = sched.get("al_origin", "")
                al_dest   = sched.get("al_destination", "")
                db_origin = route.get("origin", "")
                db_dest   = route.get("destination", "")

                if al_origin and al_dest:
                    origin      = al_origin
                    destination = al_dest
                    # Only use adsbdb coordinates when routes agree
                    if al_origin == db_origin and al_dest == db_dest:
                        origin_lat = route.get("origin_lat")
                        origin_lon = route.get("origin_lon")
                        dest_lat   = route.get("dest_lat")
                        dest_lon   = route.get("dest_lon")
                    else:
                        if db_origin and db_dest:
                            log.debug(f"[overhead] Route conflict {callsign}: "
                                      f"adsbdb={db_origin}→{db_dest} AirLabs={al_origin}→{al_dest} "
                                      f"(using AirLabs)")
                        origin_lat = origin_lon = dest_lat = dest_lon = None
                elif al_origin or al_dest:
                    # Partial route — one airport known (e.g. VFR flight with no filed destination)
                    origin      = al_origin or db_origin
                    destination = al_dest   or db_dest
                    origin_lat = origin_lon = dest_lat = dest_lon = None
                    log.info(f"[overhead] {callsign}: partial route: "
                             f"{origin or '?'}→{destination or '?'}")
                else:
                    origin      = db_origin
                    destination = db_dest
                    origin_lat  = route.get("origin_lat")
                    origin_lon  = route.get("origin_lon")
                    dest_lat    = route.get("dest_lat")
                    dest_lon    = route.get("dest_lon")
                    # FR24 position data includes route as last-resort source
                    if not origin and not destination and _fr24_origin and _fr24_dest:
                        origin      = _fr24_origin
                        destination = _fr24_dest
                        origin_lat = origin_lon = dest_lat = dest_lon = None
                        log.debug(f"[overhead] FR24 route for {callsign}: {origin}→{destination}")
                    if not origin or not destination:
                        log.warning(
                            f"[overhead] {callsign}: no route "
                            f"(sched={al_origin or '?'}→{al_dest or '?'} "
                            f"adsbdb={db_origin or '?'}→{db_dest or '?'})"
                        )

                # Airport coordinate fallback — look up via OpenFlights when we have
                # IATA codes but no coordinates (e.g. AirLabs won over adsbdb route)
                if origin and not origin_lat:
                    origin_lat, origin_lon = self._get_airport_coords(origin)
                if destination and not dest_lat:
                    dest_lat, dest_lon = self._get_airport_coords(destination)

                # Sanity-check AirLabs route — flight numbers can rotate to
                # different city pairs daily and the 24h cache can serve stale
                # data.  If the plane is nowhere near the reported airports,
                # discard and fall back to adsbdb (which was already checked).
                if al_origin and al_dest and not _route_makes_sense(
                        plane_lat, plane_lon, origin_lat, origin_lon, dest_lat, dest_lon):
                    log.debug(f"[overhead] AirLabs route {callsign} "
                              f"({al_origin}→{al_dest}) failed sanity check "
                              f"— falling back to adsbdb")
                    origin      = db_origin
                    destination = db_dest
                    origin_lat  = route.get("origin_lat")
                    origin_lon  = route.get("origin_lon")
                    dest_lat    = route.get("dest_lat")
                    dest_lon    = route.get("dest_lon")

                # Discard route if origin == destination — any source can
                # return this for positioning/turnaround flights, and it
                # bypasses the <50mi short-circuit in _route_makes_sense.
                if origin and origin == destination:
                    log.debug(f"[overhead] {callsign}: origin == destination "
                              f"({origin}), discarding route")
                    origin = destination = ""
                    origin_lat = origin_lon = dest_lat = dest_lon = None

                dist_o = haversine(plane_lat, plane_lon, origin_lat, origin_lon, units) if origin_lat else 0
                dist_d = haversine(plane_lat, plane_lon, dest_lat, dest_lon, units) if dest_lat else 0

                direction = degrees_to_cardinal(
                    bearing_to(home_lat, home_lon, plane_lat, plane_lon)
                )

                ac_alt = ac.get("alt_baro")
                ac_gs  = ac.get("gs")
                entry = {
                    "callsign":              callsign,
                    "airline":               airline,
                    "plane":                 plane_type,
                    "owner_iata":            owner_iata,
                    "owner_icao":            owner_icao,
                    "origin":                origin,
                    "origin_latitude":       origin_lat,
                    "origin_longitude":      origin_lon,
                    "destination":           destination,
                    "destination_latitude":  dest_lat,
                    "destination_longitude": dest_lon,
                    "plane_latitude":        plane_lat,
                    "plane_longitude":       plane_lon,
                    "vertical_speed":        vert_speed,
                    "altitude":              int(ac_alt) if isinstance(ac_alt, (int, float)) else 0,
                    "ground_speed":          int(ac_gs)  if isinstance(ac_gs,  (int, float)) else 0,
                    "heading":               int(ac.get("track", 0)) if isinstance(ac.get("track"), (int, float)) else 0,
                    "registration":          _clean(ac.get("r", "")),
                    "distance":              dist,
                    "distance_origin":       dist_o,
                    "distance_destination":  dist_d,
                    "direction":             direction,
                    "time_scheduled_departure": sched.get("time_scheduled_departure"),
                    "time_real_departure":      sched.get("time_real_departure"),
                    "time_scheduled_arrival":   sched.get("time_scheduled_arrival"),
                    "time_estimated_arrival":   sched.get("time_estimated_arrival"),
                }

                data.append(entry)
                log_flight_data(dict(entry), max_closest)
                log_farthest_flight(dict(entry), max_farthest)
                log_flight_count(dict(entry), journey_code)

        except Exception as e:
            log.error(f"[overhead] Fetch error: {e}", exc_info=True)

        self._finish(data)

    def _finish(self, data: list):
        with self._lock:
            self._data       = data
            self._new_data   = True
            self._processing = False

    # ------------------------------------------------------------------
    # Route lookup — adsbdb.com (12 h cache)
    # ------------------------------------------------------------------

    def _get_route(self, callsign: str, plane_lat: float, plane_lon: float) -> dict:
        now    = time.time()
        cached = self._route_cache.get(callsign)
        if cached and now < cached["expires"]:
            return cached["data"]

        result: dict = {}

        prefix3 = callsign[:3].upper()
        if prefix3 in SKIP_ADSBDB_PREFIXES:
            self._cache_route(callsign, result, now)
            return result

        try:
            resp = requests.get(
                ADSBDB_URL.format(callsign=callsign),
                timeout=8,
                headers={"User-Agent": "LEDMatrix/1.0"},
            )
            if resp.status_code == 200:
                body = resp.json()
                fr   = (body.get("response") or {}).get("flightroute") or {}
                if fr:
                    airline_info = fr.get("airline") or {}
                    orig         = fr.get("origin") or {}
                    dest         = fr.get("destination") or {}

                    o_lat = orig.get("latitude")
                    o_lon = orig.get("longitude")
                    d_lat = dest.get("latitude")
                    d_lon = dest.get("longitude")

                    # Airline info (name/codes) is reliable even when the specific
                    # route is stale — always extract it.
                    result = {
                        "airline":     _clean(airline_info.get("name", "")),
                        "owner_iata":  _clean(airline_info.get("iata", "")),
                        "owner_icao":  _clean(airline_info.get("icao", "")),
                        "origin":      "",
                        "destination": "",
                        "origin_lat":  None,
                        "origin_lon":  None,
                        "dest_lat":    None,
                        "dest_lon":    None,
                    }

                    # Only trust origin/destination if the plane is plausibly on this route
                    if _route_makes_sense(plane_lat, plane_lon, o_lat, o_lon, d_lat, d_lon):
                        result.update({
                            "origin":      _clean(orig.get("iata_code", "")),
                            "destination": _clean(dest.get("iata_code", "")),
                            "origin_lat":  o_lat,
                            "origin_lon":  o_lon,
                            "dest_lat":    d_lat,
                            "dest_lon":    d_lon,
                        })
                    else:
                        log.debug(f"[overhead] adsbdb route for {callsign} failed sanity check — keeping airline info")
        except Exception as e:
            log.debug(f"[overhead] adsbdb error ({callsign}): {e}")

        self._cache_route(callsign, result, now)
        return result

    def _cache_route(self, callsign: str, data: dict, now: float):
        has_route = bool(data.get("origin") and data.get("destination"))
        ttl = ROUTE_CACHE_TTL if has_route else 1800
        self._route_cache[callsign] = {"data": data, "expires": now + ttl}
        self._save_disk_cache()

    # ------------------------------------------------------------------
    # Airport coordinate lookup — OpenFlights DB (lazy-loaded, cached to disk)
    # ------------------------------------------------------------------

    def _get_airport_coords(self, iata: str):
        """Return (lat, lon) for an IATA airport code, or (None, None) on failure."""
        if not iata:
            return None, None
        _load_airport_db()
        return _airport_db.get(iata, (None, None))

    # ------------------------------------------------------------------
    # Schedule lookup — AirLabs → FlightAware (12 h cache)
    # Also returns origin/destination from AirLabs when adsbdb data is stale.
    # ------------------------------------------------------------------

    def _get_schedule(self, callsign: str, owner_iata: str,
                      plane_lat: float = None, plane_lon: float = None,
                      fr24_has_route: bool = False,
                      plane_vs: float = None,
                      plane_reg: str = None) -> dict:
        now    = time.time()
        cached = self._schedule_cache.get(callsign)
        if cached and now < cached["expires"]:
            d = cached["data"]
            if not (d.get("al_origin") and d.get("al_destination")):
                log.warning(
                    f"[overhead] Schedule cache hit for {callsign}: no route "
                    f"(expires in {int(cached['expires'] - now)}s)"
                )
            return d

        result: dict = {}
        al_keys      = _get_keys(self._secrets, "airlabs_api_keys", "airlabs_api_key")
        fa_keys      = _get_keys(self._secrets, "flightaware_api_keys", "flightaware_api_key")
        al_reset_days = _get_reset_days(self._secrets, "airlabs_reset_days", len(al_keys))
        fa_reset_days = _get_reset_days(self._secrets, "flightaware_reset_days", len(fa_keys))
        fa_budget    = float(self._secrets.get("flightaware_monthly_budget", 4.50))

        # ── FlightStats (free, no quota) ──────────────────────────────
        from utilities.flightstats import get_flight_info as _fs_get
        fs = _fs_get(callsign, owner_iata)
        if fs and fs.get("al_origin") and fs.get("al_destination"):
            # Sanity-check FlightStats route against actual aircraft position.
            # Some codeshare flight numbers (e.g. UA 8000-series) have stale/wrong
            # entries in FlightStats that would otherwise skip AirLabs entirely.
            fs_ok = True
            if plane_lat is not None and plane_lon is not None:
                o_lat, o_lon = self._get_airport_coords(fs["al_origin"])
                d_lat, d_lon = self._get_airport_coords(fs["al_destination"])
                if not _route_makes_sense(plane_lat, plane_lon, o_lat, o_lon, d_lat, d_lon):
                    fs_ok = False
                elif plane_vs is not None and o_lat and d_lat:
                    d_mi = haversine(plane_lat, plane_lon, d_lat, d_lon)
                    o_mi = haversine(plane_lat, plane_lon, o_lat, o_lon)
                    if (d_mi < 60 and plane_vs > 500) or (o_mi < 60 and plane_vs < -500):
                        fs_ok = False
                if not fs_ok:
                    log.warning(
                        f"[overhead] FlightStats {callsign}: "
                        f"{fs['al_origin']}→{fs['al_destination']} failed sanity check — trying AirLabs"
                    )
            if fs_ok:
                log.info(f"[overhead] FlightStats {callsign}: "
                         f"{fs['al_origin']}→{fs['al_destination']}")
                result = fs

        # ── AirLabs (primary) ─────────────────────────────────────────
        # Try flight_icao=callsign first (no IATA mapping needed), then
        # fall back to flight_iata=owner_iata+digits if that returns nothing.
        airlabs_key = _al_get_active_key(al_keys, al_reset_days)
        if not result and airlabs_key and not fr24_has_route:
            for params in self._airlabs_params(callsign, owner_iata):
                try:
                    resp = requests.get(AIRLABS_URL, params={**params, "api_key": airlabs_key}, timeout=8)
                    if resp.status_code != 200:
                        continue
                    _al_record_call(airlabs_key, al_reset_days[al_keys.index(airlabs_key)])
                    r = resp.json().get("response") or {}
                    if not r:
                        continue

                    sched_dep = r.get("dep_time_ts")
                    real_dep  = r.get("dep_actual_ts") or r.get("dep_estimated_ts")
                    sched_arr = r.get("arr_time_ts")
                    est_arr   = r.get("arr_estimated_ts") or r.get("arr_actual_ts")

                    # Codeshare detection: prefer the marketing carrier (e.g. Delta)
                    # over the operating carrier (e.g. SkyWest) for name and logo.
                    cs_iata = _clean(r.get("cs_airline_iata", ""))
                    if cs_iata and cs_iata in _CODESHARE_CARRIERS:
                        al_airline, al_owner_icao = _CODESHARE_CARRIERS[cs_iata]
                        al_owner_iata = cs_iata
                        log.debug(f"[overhead] Codeshare: {callsign} operating as {al_airline} ({cs_iata})")
                    else:
                        al_airline    = _clean(r.get("airline_name", ""))
                        al_owner_iata = _clean(r.get("airline_iata", ""))
                        al_owner_icao = _clean(r.get("airline_icao", ""))

                    result = {
                        "time_scheduled_departure": sched_dep,
                        "time_real_departure":      real_dep,
                        "time_scheduled_arrival":   sched_arr,
                        "time_estimated_arrival":   est_arr,
                        # Route fallback — used when adsbdb data fails sanity check
                        "al_origin":      _clean(r.get("dep_iata", "")),
                        "al_destination": _clean(r.get("arr_iata", "")),
                        "al_airline":     al_airline,
                        "al_owner_iata":  al_owner_iata,
                        "al_owner_icao":  al_owner_icao,
                    }
                    delay = (
                        f"{(real_dep - sched_dep) // 60:+.0f} min"
                        if real_dep and sched_dep else "N/A"
                    )
                    log.warning(f"[overhead] AirLabs {list(params.values())[0]}: dep delay {delay}, "
                               f"{result['al_origin'] or '?'}→{result['al_destination'] or '?'}")
                    # Sanity-check: routes can be stale for operators that rotate
                    # flight numbers daily (e.g. NetJets EJA, Flexjet LXJ).
                    if (result.get("al_origin") and result.get("al_destination")
                            and plane_lat is not None and plane_lon is not None):
                        o_lat, o_lon = self._get_airport_coords(result["al_origin"])
                        d_lat, d_lon = self._get_airport_coords(result["al_destination"])
                        al_ok = _route_makes_sense(plane_lat, plane_lon, o_lat, o_lon, d_lat, d_lon)
                        if al_ok and plane_vs is not None and o_lat and d_lat:
                            d_mi = haversine(plane_lat, plane_lon, d_lat, d_lon)
                            o_mi = haversine(plane_lat, plane_lon, o_lat, o_lon)
                            if (d_mi < 60 and plane_vs > 500) or (o_mi < 60 and plane_vs < -500):
                                al_ok = False
                        if not al_ok:
                            log.warning(
                                f"[overhead] AirLabs {callsign}: "
                                f"{result['al_origin']}→{result['al_destination']} failed sanity check"
                                f" — trying FlightAware"
                            )
                            result = None
                    break
                except Exception as e:
                    log.debug(f"[overhead] AirLabs error ({callsign}): {e}")

        # ── FlightAware (fallback) ────────────────────────────────────
        # Also try FA when AirLabs returned partial data (origin but no destination)
        airlabs_incomplete = result and not result.get("al_destination")
        fa_key = _fa_get_active_key(fa_keys, fa_budget, fa_reset_days)
        if (not result or airlabs_incomplete) and not fr24_has_route:
            if not fa_key:
                log.warning(
                    f"[overhead] FA skipped for {callsign}: "
                    f"{'no keys configured' if not fa_keys else f'all {len(fa_keys)} key(s) over budget ${fa_budget}'}"
                )
        if (not result or airlabs_incomplete) and fa_key and not fr24_has_route:
            from datetime import timezone as _tz
            today_pfx = datetime.now(_tz.utc).strftime("%Y-%m-%d")
            twelve_hrs_ago = now - 43200

            def _fa_dep_ts(f):
                return iso_to_unix(f.get("actual_out") or f.get("scheduled_out"))

            def _fa_iata(a: dict) -> str:
                iata = _clean(a.get("code_iata", ""))
                if iata:
                    return iata
                icao = _clean(a.get("code_icao", "") or a.get("code", ""))
                if icao and len(icao) == 4 and icao[0] == "K":
                    return icao[1:]
                # Last resort: reverse-lookup by coordinates (private strips, military fields)
                _lat = a.get("latitude")
                _lon = a.get("longitude")
                if _lat is not None and _lon is not None:
                    from utilities.airports import get_nearest_airport as _nearest
                    _nearby = _nearest(_lat, _lon)
                    if _nearby:
                        log.info(f"[overhead] FA reverse airport lookup: ({_lat:.3f},{_lon:.3f}) → {_nearby}")
                        return _nearby
                return ""

            # Try callsign first, then registration as fallback for non-standard
            # callsigns (charter, medical, repositioning) that FA indexes by tail number.
            _fa_idents = [callsign]
            if plane_reg and plane_reg.upper() != callsign.upper():
                _fa_idents.append(plane_reg)

            for _fa_ident in _fa_idents:
                try:
                    resp = requests.get(
                        FLIGHTAWARE_URL.format(ident=_fa_ident),
                        headers={"x-apikey": fa_key},
                        timeout=8,
                    )
                    if resp.status_code != 200:
                        log.warning(f"[overhead] FlightAware {resp.status_code} for {_fa_ident}")
                        continue
                    flights = resp.json().get("flights", [])
                    fa = (
                        next((f for f in flights
                              if f.get("status") == "En Route"
                              and (_fa_dep_ts(f) or 0) > twelve_hrs_ago), None)
                        or next((f for f in flights
                                 if f.get("status") == "En Route"), None)
                        or next((f for f in flights
                                 if (f.get("progress_percent") or 0) > 0
                                 and (f.get("scheduled_out") or "").startswith(today_pfx)), None)
                        or next((f for f in flights
                                 if (f.get("scheduled_out") or "").startswith(today_pfx)), None)
                        or next((f for f in flights
                                 if (f.get("progress_percent") or 0) > 0), None)
                        or (flights[0] if flights else None)
                    )
                    if fa:
                        orig = (fa.get("origin") or {})
                        dest = (fa.get("destination") or {})
                        log.warning(
                            f"[overhead] FA airports for {callsign} (ident={_fa_ident}): "
                            f"orig={orig.get('code_iata')} / {orig.get('code_icao')} / {orig.get('code')} "
                            f"dest={dest.get('code_iata')} / {dest.get('code_icao')} / {dest.get('code')}"
                        )
                        result = {
                            "time_scheduled_departure": iso_to_unix(fa.get("scheduled_out")),
                            "time_real_departure":      iso_to_unix(fa.get("actual_out")),
                            "time_scheduled_arrival":   iso_to_unix(fa.get("scheduled_in")),
                            "time_estimated_arrival":   iso_to_unix(
                                fa.get("estimated_in") or fa.get("actual_in")
                            ),
                            "al_origin":      _fa_iata(orig),
                            "al_destination": _fa_iata(dest),
                            "al_airline":     "",
                            "al_owner_iata":  "",
                            "al_owner_icao":  "",
                        }
                        fa_idx = fa_keys.index(fa_key)
                        _fa_record_call(fa_key, fa_reset_days[fa_idx])
                        log.info(f"[overhead] FlightAware schedule for {callsign} (ident={_fa_ident}): "
                                 f"{result['al_origin']}→{result['al_destination']}")
                        break
                    else:
                        log.info(f"[overhead] FlightAware returned no flights for {_fa_ident}")
                except Exception as e:
                    log.warning(f"[overhead] FlightAware error ({_fa_ident}): {e}")

        arr_ts    = result.get("time_scheduled_arrival") if result else None
        has_route = bool(result.get("al_origin") or result.get("al_destination")) if result else False
        if has_route and arr_ts:
            smart_expires = max(arr_ts + 7200, now + 3 * 3600)
            smart_expires = min(smart_expires, now + 24 * 3600)
        elif has_route:
            smart_expires = now + SCHEDULE_CACHE_TTL
        else:
            smart_expires = now + 1800  # retry soon if no route found
        self._schedule_cache[callsign] = {"data": result, "expires": smart_expires}
        self._save_disk_cache()
        return result

    @staticmethod
    def _airlabs_params(callsign: str, owner_iata: str):
        """Yield AirLabs query param dicts to try in order."""
        import re
        # Bare registrations (N9764W, C-FABC) have no AirLabs schedule data.
        # Only try flight_icao for airline-style callsigns (3+ letters + digit).
        if owner_iata or re.match(r'^[A-Z]{3,}\d', callsign):
            yield {"flight_icao": callsign}
        if owner_iata:
            digits = "".join(c for c in callsign if c.isdigit())
            if digits:
                yield {"flight_iata": owner_iata + digits}


# ------------------------------------------------------------------
# Standalone test
# ------------------------------------------------------------------

if __name__ == "__main__":
    import time as _time
    logging.basicConfig(level=logging.DEBUG)
    o = Overhead()
    o.grab_data()
    while not o.new_data:
        print("fetching...")
        _time.sleep(1)
    for entry in o.data:
        print(entry)
