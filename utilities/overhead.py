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
import subprocess
import logging
from threading import Thread, Lock
from datetime import datetime
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
ROUTE_CACHE_TTL    = 12 * 3600   # seconds
SCHEDULE_CACHE_TTL = 12 * 3600

EARTH_RADIUS_M = 3958.8   # miles

BLANK_FIELDS = {"", "N/A", "NONE"}

# Callsign prefixes whose adsbdb routes are unreliable (GA operators,
# carriers that frequently carry stale route data)
SKIP_ADSBDB_PREFIXES = {
    "KAP",   # Cape Air
    "EJA",   # NetJets
    "SWA",   # Southwest (ICAO)
}

BASE_DIR          = os.path.dirname(os.path.dirname(__file__))
LOG_FILE          = os.path.join(BASE_DIR, "close.txt")
LOG_FILE_FARTHEST = os.path.join(BASE_DIR, "farthest.txt")
FA_USAGE_FILE     = os.path.join(BASE_DIR, "flightaware_usage.json")

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
    """Convert ISO 8601 string → Unix timestamp, or None."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _clean(val) -> str:
    if not val or str(val).upper() in BLANK_FIELDS:
        return ""
    return str(val)


# ------------------------------------------------------------------
# FlightAware budget tracking
# ------------------------------------------------------------------

def _fa_load_usage() -> dict:
    try:
        with open(FA_USAGE_FILE, "r") as f:
            d = json.load(f)
        if d.get("month") != datetime.now().strftime("%Y-%m"):
            raise ValueError("month changed")
        return d
    except Exception:
        return {"month": datetime.now().strftime("%Y-%m"), "calls": 0, "cost": 0.0}


def _fa_record_call(cost_per_call: float = 0.01):
    usage = _fa_load_usage()
    usage["calls"] += 1
    usage["cost"]  = round(usage["cost"] + cost_per_call, 4)
    with open(FA_USAGE_FILE, "w") as f:
        json.dump(usage, f)


def _fa_budget_ok(limit: float) -> bool:
    return _fa_load_usage().get("cost", 0.0) < limit


# ------------------------------------------------------------------
# Audio alert
# ------------------------------------------------------------------

def play_plane_sound():
    try:
        subprocess.Popen(
            ["paplay", "/home/royman/its-a-plane-python/airbus.mp3"],
            start_new_session=True,
        )
    except Exception as e:
        log.debug(f"[overhead] Audio failed: {e}")


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

    if route_dist < 50:
        return True   # very short hop — skip sanity check

    # Plane should be within roughly 1.5× route dist of its two endpoints combined
    if dist_orig + dist_dest > route_dist * 2.0:
        return False

    # Plane shouldn't be further from origin than the full route length
    if dist_orig > route_dist * 1.5:
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
        self._route_cache: dict    = {}   # callsign → {"data": {...}, "expires": monotonic}
        self._schedule_cache: dict = {}   # callsign → {"data": {...}, "expires": monotonic}

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
        units        = loc.get("distance_units", "imperial")
        min_alt      = ft_cfg.get("min_altitude",    8000)
        max_alt      = ft_cfg.get("max_altitude",    MAX_ALTITUDE)
        max_lookup   = ft_cfg.get("max_flight_lookup", 5)
        max_closest  = flights_cfg.get("max_closest",  5)
        max_farthest = flights_cfg.get("max_farthest", 5)

        # Derive search radius from zone_home bounding box if not configured
        radius_nm = loc.get("search_radius_nm")
        if not radius_nm:
            zone      = loc.get("zone_home", {})
            lat_span  = abs(zone.get("tl_y", 0) - zone.get("br_y", 0)) / 2
            lon_span  = abs(zone.get("tl_x", 0) - zone.get("br_x", 0)) / 2
            radius_nm = max(10.0, min(max(lat_span, lon_span) * 60, 100.0))

        data = []
        try:
            # ── Step 1: Positions (adsb.lol) ─────────────────────────────
            url  = ADSB_LOL_URL.format(lat=home_lat, lon=home_lon, dist=int(radius_nm))
            resp = requests.get(url, timeout=10, headers={"User-Agent": "LEDMatrix/1.0"})
            if resp.status_code != 200:
                log.warning(f"[overhead] adsb.lol {resp.status_code}")
                self._finish(data)
                return

            aircraft_list = resp.json().get("ac", [])
            log.debug(f"[overhead] adsb.lol: {len(aircraft_list)} aircraft in radius")

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

                # Audio / deduplication alert
                if callsign not in self._alerted_callsigns:
                    log.info(f"[overhead] New flight: {callsign}")
                    play_plane_sound()
                    self._alerted_callsigns.add(callsign)

                # Route (adsbdb)
                route = self._get_route(callsign, plane_lat, plane_lon)
                airline     = route.get("airline", "")
                owner_iata  = route.get("owner_iata", "")
                owner_icao  = route.get("owner_icao", "")
                origin      = route.get("origin", "")
                destination = route.get("destination", "")
                origin_lat  = route.get("origin_lat")
                origin_lon  = route.get("origin_lon")
                dest_lat    = route.get("dest_lat")
                dest_lon    = route.get("dest_lon")

                dist_o = haversine(plane_lat, plane_lon, origin_lat, origin_lon, units) if origin_lat else 0
                dist_d = haversine(plane_lat, plane_lon, dest_lat, dest_lon, units) if dest_lat else 0

                # Schedule / delay (AirLabs → FlightAware)
                sched = self._get_schedule(callsign, owner_iata)

                direction = degrees_to_cardinal(
                    bearing_to(home_lat, home_lon, plane_lat, plane_lon)
                )

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
        now    = time.monotonic()
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

                    if _route_makes_sense(plane_lat, plane_lon, o_lat, o_lon, d_lat, d_lon):
                        result = {
                            "airline":     _clean(airline_info.get("name", "")),
                            "owner_iata":  _clean(airline_info.get("iata", "")),
                            "owner_icao":  _clean(airline_info.get("icao", "")),
                            "origin":      _clean(orig.get("iata_code", "")),
                            "destination": _clean(dest.get("iata_code", "")),
                            "origin_lat":  o_lat,
                            "origin_lon":  o_lon,
                            "dest_lat":    d_lat,
                            "dest_lon":    d_lon,
                        }
                    else:
                        log.debug(f"[overhead] adsbdb route for {callsign} failed sanity check")
        except Exception as e:
            log.debug(f"[overhead] adsbdb error ({callsign}): {e}")

        self._cache_route(callsign, result, now)
        return result

    def _cache_route(self, callsign: str, data: dict, now: float):
        self._route_cache[callsign] = {"data": data, "expires": now + ROUTE_CACHE_TTL}

    # ------------------------------------------------------------------
    # Schedule lookup — AirLabs → FlightAware (12 h cache)
    # ------------------------------------------------------------------

    def _get_schedule(self, callsign: str, owner_iata: str) -> dict:
        now    = time.monotonic()
        cached = self._schedule_cache.get(callsign)
        if cached and now < cached["expires"]:
            return cached["data"]

        result: dict = {}
        airlabs_key = self._secrets.get("airlabs_api_key", "")
        fa_key      = self._secrets.get("flightaware_api_key", "")
        fa_budget   = float(self._secrets.get("flightaware_monthly_budget", 4.50))

        # ── AirLabs (primary) ─────────────────────────────────────────
        if airlabs_key and owner_iata:
            # Build IATA flight number from airline IATA code + numeric suffix
            # e.g. callsign "UAL1234", owner_iata "UA" → "UA1234"
            digits      = "".join(c for c in callsign if c.isdigit())
            iata_flight = owner_iata + digits if digits else ""

            if iata_flight:
                try:
                    resp = requests.get(
                        AIRLABS_URL,
                        params={"flight_iata": iata_flight, "api_key": airlabs_key},
                        timeout=8,
                    )
                    if resp.status_code == 200:
                        r = resp.json().get("response") or {}
                        sched_dep = iso_to_unix(r.get("dep_scheduled"))
                        real_dep  = iso_to_unix(r.get("dep_actual") or r.get("dep_estimated"))
                        sched_arr = iso_to_unix(r.get("arr_scheduled"))
                        est_arr   = iso_to_unix(r.get("arr_estimated") or r.get("arr_actual"))

                        if sched_dep or sched_arr:
                            result = {
                                "time_scheduled_departure": sched_dep,
                                "time_real_departure":      real_dep,
                                "time_scheduled_arrival":   sched_arr,
                                "time_estimated_arrival":   est_arr,
                            }
                            delay = (
                                f"{(real_dep - sched_dep) // 60:+.0f} min"
                                if real_dep and sched_dep else "N/A"
                            )
                            log.debug(f"[overhead] AirLabs {iata_flight}: dep delay {delay}")
                except Exception as e:
                    log.debug(f"[overhead] AirLabs error ({callsign}): {e}")

        # ── FlightAware (fallback) ────────────────────────────────────
        if not result and fa_key and _fa_budget_ok(fa_budget):
            try:
                resp = requests.get(
                    FLIGHTAWARE_URL.format(ident=callsign),
                    headers={"x-apikey": fa_key},
                    timeout=8,
                )
                if resp.status_code == 200:
                    flights = resp.json().get("flights", [])
                    # Prefer an in-progress flight; fall back to most recent
                    fa = next(
                        (f for f in flights if f.get("progress_percent", 0) > 0),
                        flights[0] if flights else None,
                    )
                    if fa:
                        result = {
                            "time_scheduled_departure": iso_to_unix(fa.get("scheduled_out")),
                            "time_real_departure":      iso_to_unix(fa.get("actual_out")),
                            "time_scheduled_arrival":   iso_to_unix(fa.get("scheduled_in")),
                            "time_estimated_arrival":   iso_to_unix(
                                fa.get("estimated_in") or fa.get("actual_in")
                            ),
                        }
                        _fa_record_call()
                        log.debug(f"[overhead] FlightAware schedule for {callsign}")
            except Exception as e:
                log.debug(f"[overhead] FlightAware error ({callsign}): {e}")

        self._schedule_cache[callsign] = {"data": result, "expires": now + SCHEDULE_CACHE_TTL}
        return result


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
