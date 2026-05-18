import json
import logging
import re
import time
from threading import Thread, Lock

import requests

from plugins.base_plugin import BasePlugin
from utilities.animator import Animator
from utilities.flightstats import get_flight_info
from utilities.opensky import get_flight_position
from utilities.overhead import (
    haversine, AIRPORT_DB_FILE, iso_to_unix,
    _get_keys, _get_reset_days,
    _al_get_active_key, _al_record_call,
    _fa_get_active_key, _fa_record_call,
    FLIGHTAWARE_URL,
)
from scenes.trackedtopbar import TrackedTopBarScene
from scenes.trackedheader import TrackedHeaderScene
from scenes.trackedprogress import TrackedProgressScene
from scenes.trackedstats import TrackedStatsScene
from setup import frames

log = logging.getLogger(__name__)

PLUGIN_ID = "specific_flight_tracker"

STATE_IDLE     = "IDLE"
STATE_ACTIVE   = "ACTIVE"
STATE_COMPLETE = "COMPLETE"

IDLE_POLL_INTERVAL   =  5 * 60   # seconds between polls when not yet airborne
ACTIVE_POLL_INTERVAL =       60   # seconds between position polls when airborne
ROUTE_CACHE_TTL      =      3600  # fallback TTL when no arrival time known

AIRLABS_URL          = "https://airlabs.co/api/v9/flight?flight_icao={callsign}&api_key={key}"
ADSBDB_URL           = "https://api.adsbdb.com/v0/callsign/{callsign}"
ADSB_LOL_CALLSIGN_URL = "https://api.adsb.lol/v2/callsign/{callsign}"

_HEADERS = {"User-Agent": "LEDMatrix/1.0"}
_NUM_RE  = re.compile(r"\d+")


def _load_airport_coords():
    try:
        with open(AIRPORT_DB_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: tuple(v) for k, v in raw.items()}
    except Exception:
        return {}


def _format_time_remaining(minutes: float) -> str:
    if minutes < 0:
        minutes = 0
    h = int(minutes) // 60
    m = int(minutes) % 60
    return f"{h}:{m:02d}"


class _TrackedDisplay(
    TrackedTopBarScene,
    TrackedHeaderScene,
    TrackedProgressScene,
    TrackedStatsScene,
    Animator,
):
    def __init__(self, display_manager):
        self.canvas = display_manager.canvas
        self.matrix = display_manager.matrix
        self._data = []
        self._data_index = 0
        self._data_all_looped = False

        super().__init__()

        self.delay = frames.PERIOD

    def draw_square(self, x0, y0, x1, y1, colour):
        try:
            from rgbmatrix import graphics
            for x in range(x0, x1):
                graphics.DrawLine(self.canvas, x, y0, x, y1, colour)
        except ImportError:
            pass

    def tick(self):
        for keyframe in self.keyframes:
            if self.frame == 0 and keyframe.properties["divisor"] == 0:
                keyframe()

            if (
                self.frame > 0
                and keyframe.properties["divisor"]
                and not (
                    (self.frame - keyframe.properties["offset"])
                    % keyframe.properties["divisor"]
                )
            ):
                if keyframe(keyframe.properties["count"]):
                    keyframe.properties["count"] = 0
                else:
                    keyframe.properties["count"] += 1

        self.frame += 1


def _new_flight_entry():
    return {
        "state":          STATE_IDLE,
        "data":           None,
        "last_polled":    0.0,
        "arr_time_ts":    None,
        "total_distance": None,
        # Route cache (populated by _poll_route, reused between position polls)
        "route_data":     None,
        "route_expires":  0.0,
        # Airline IATA code discovered via adsbdb (persists so we can call FlightStats)
        "airline_iata":   None,
        # Aircraft type and registration — cached once found, survive across polls
        "plane_type":     "",
        "registration":   "",
        # Altitude history for VS estimation when API doesn't report vertical rate
        "last_altitude":  None,
        "last_alt_ts":    None,
    }


class SpecificFlightTrackerPlugin(BasePlugin):
    PLUGIN_ID = PLUGIN_ID

    def __init__(self, display_manager, config: dict, secrets: dict):
        super().__init__(display_manager, config, secrets)
        self._display: _TrackedDisplay | None = None
        self._lock = Lock()
        self._flights: dict[str, dict] = {}
        self._airport_db: dict = _load_airport_coords()
        self._poll_thread: Thread | None = None

        tracked = self.config.get("tracked_callsigns", [])
        for cs in tracked:
            self._flights[cs.upper()] = _new_flight_entry()

        if self.enabled and tracked:
            self._start_poll_thread()

    def _start_poll_thread(self):
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_thread = Thread(
                target=self._poll_loop, daemon=True, name="specific-flight-poller"
            )
            self._poll_thread.start()
            log.info("[specific_flight_tracker] Poll thread started")

    def _ensure_display(self):
        if self._display is None:
            self._display = _TrackedDisplay(self.display_manager)

    def _poll_loop(self):
        while True:
            if not self.enabled:
                time.sleep(30)
                continue
            now = time.time()
            for callsign in list(self._flights.keys()):
                with self._lock:
                    info = self._flights[callsign]
                    state = info["state"]
                    last_polled = info["last_polled"]

                if state == STATE_COMPLETE:
                    continue

                interval = ACTIVE_POLL_INTERVAL if state == STATE_ACTIVE else IDLE_POLL_INTERVAL
                if now - last_polled < interval:
                    continue

                self._poll_callsign(callsign)

            time.sleep(30)

    # ── Position: adsb.lol callsign → adsb.lol area → OpenSky ───────────────

    @staticmethod
    def _parse_adsb_lol_ac(callsign: str, ac: dict) -> dict:
        baro_rate    = int(ac.get("baro_rate", 0) or 0)
        alt_baro     = int(ac.get("alt_baro", 0) or 0)
        plane_type   = (ac.get("t") or "").strip().upper()
        registration = (ac.get("r") or "").strip().upper()
        log.info(f"[specific_flight_tracker] adsb.lol {callsign}: alt={alt_baro} baro_rate={baro_rate} type={plane_type} reg={registration}")
        return {
            "lat":            ac["lat"],
            "lng":            ac.get("lon", ac.get("lng", 0)),
            "altitude":       alt_baro,
            "ground_speed":   int(ac.get("gs", 0) or 0),
            "heading":        ac.get("track", 0) or 0,
            "vertical_speed": baro_rate,
            "plane_type":     plane_type,
            "registration":   registration,
        }

    def _get_position(self, callsign: str) -> dict | None:
        """Fetch real-time position: FR24 → adsb.lol callsign → OpenSky."""
        cs_upper = callsign.upper()

        # 1. FR24 — worldwide coverage including satellite/oceanic
        from utilities.fr24_lookup import get_flight_position as _fr24_pos, is_available as _fr24_avail
        if _fr24_avail():
            ac = _fr24_pos(callsign)
            if ac:
                log.info(f"[specific_flight_tracker] FR24 hit for {callsign}")
                return self._parse_adsb_lol_ac(callsign, ac)

        # 2. adsb.lol callsign endpoint
        try:
            resp = requests.get(
                ADSB_LOL_CALLSIGN_URL.format(callsign=callsign),
                timeout=8, headers=_HEADERS,
            )
            if resp.status_code == 200:
                for ac in resp.json().get("ac", []):
                    if (ac.get("flight") or "").strip().upper() == cs_upper:
                        return self._parse_adsb_lol_ac(callsign, ac)
        except Exception as e:
            log.debug(f"[specific_flight_tracker] adsb.lol failed for {callsign}: {e}")

        # 3. OpenSky fallback (returns vertical_speed in m/s → convert to ft/min)
        pos = get_flight_position(callsign)
        if pos is None:
            return None
        vs_ms = pos.get("vertical_speed", 0) or 0
        pos["vertical_speed"] = int(vs_ms * 196.85)
        log.debug(f"[specific_flight_tracker] OpenSky hit for {callsign}")
        return pos

    # ── Route/schedule: adsbdb → FlightStats → AirLabs → FlightAware ────────

    def _route_expired(self, callsign: str) -> bool:
        with self._lock:
            info = self._flights[callsign]
            return info["route_data"] is None or time.time() >= info["route_expires"]

    def _fetch_adsbdb_iata(self, callsign: str) -> str | None:
        """Query adsbdb for airline IATA code (needed for FlightStats). Caches result."""
        with self._lock:
            cached = self._flights[callsign].get("airline_iata")
        if cached:
            return cached
        try:
            resp = requests.get(
                ADSBDB_URL.format(callsign=callsign), timeout=8, headers=_HEADERS
            )
            if resp.status_code == 200:
                fr = ((resp.json().get("response") or {}).get("flightroute") or {})
                iata = (fr.get("airline") or {}).get("iata", "") or ""
                if iata:
                    with self._lock:
                        self._flights[callsign]["airline_iata"] = iata
                    log.debug(f"[specific_flight_tracker] adsbdb airline IATA={iata} for {callsign}")
                    return iata
        except Exception as e:
            log.debug(f"[specific_flight_tracker] adsbdb failed for {callsign}: {e}")
        return None

    def _fetch_airlabs(self, callsign: str) -> dict | None:
        """Fetch live route + position data from AirLabs. Returns raw response dict or None."""
        al_keys = _get_keys(self.secrets, "airlabs_api_keys", "airlabs_api_key")
        al_reset_days = _get_reset_days(self.secrets, "airlabs_reset_days", len(al_keys))
        key = _al_get_active_key(al_keys, al_reset_days)
        if not key:
            log.warning("[specific_flight_tracker] No AirLabs key available")
            return None
        try:
            resp = requests.get(
                AIRLABS_URL.format(callsign=callsign, key=key), timeout=15
            )
            resp.raise_for_status()
            data = resp.json().get("response")
            if data:
                key_reset_day = al_reset_days[al_keys.index(key)]
                _al_record_call(key, key_reset_day)
                return data
            log.info(f"[specific_flight_tracker] AirLabs: no data for {callsign}")
        except Exception as e:
            log.warning(f"[specific_flight_tracker] AirLabs failed for {callsign}: {e}")
        return None

    def _fetch_flightaware(self, callsign: str) -> dict | None:
        """Fetch schedule from FlightAware as last-resort fallback. Returns partial route dict or None."""
        fa_keys = _get_keys(self.secrets, "flightaware_api_keys", "flightaware_api_key")
        fa_reset_days = _get_reset_days(self.secrets, "flightaware_reset_days", len(fa_keys))
        fa_budget = float(self.secrets.get("flightaware_monthly_budget", 4.50))
        fa_key = _fa_get_active_key(fa_keys, fa_budget, fa_reset_days)
        if not fa_key:
            return None
        try:
            resp = requests.get(
                FLIGHTAWARE_URL.format(ident=callsign),
                headers={**_HEADERS, "x-apikey": fa_key},
                timeout=12,
            )
            resp.raise_for_status()
            flights = resp.json().get("flights") or []
        except Exception as e:
            log.debug(f"[specific_flight_tracker] FlightAware failed for {callsign}: {e}")
            return None

        twelve_hrs_ago = time.time() - 43200

        def _fa_dep_ts(f):
            return iso_to_unix(f.get("actual_out") or f.get("scheduled_out"))

        flight = (
            next((f for f in flights if f.get("status") == "En Route"
                  and (_fa_dep_ts(f) or 0) > twelve_hrs_ago), None)
            or next((f for f in flights if f.get("status") == "En Route"), None)
            or next((f for f in flights
                     if (f.get("progress_percent") or 0) > 0), None)
            or (flights[0] if flights else None)
        )
        if not flight:
            return None

        def _fa_iata(a: dict) -> str:
            iata = (a.get("code_iata") or "").strip()
            if iata:
                return iata
            icao = (a.get("code_icao") or a.get("code") or "").strip()
            if icao and len(icao) == 4 and icao[0] == "K":
                return icao[1:]
            return ""

        orig = flight.get("origin") or {}
        dest = flight.get("destination") or {}

        fa_idx = fa_keys.index(fa_key)
        _fa_record_call(fa_key, fa_reset_days[fa_idx] if fa_reset_days else 1)

        return {
            "dep_iata":   _fa_iata(orig),
            "arr_iata":   _fa_iata(dest),
            "plane_type": "",
            "sched_dep":  iso_to_unix(flight.get("scheduled_out")),
            "actual_dep": iso_to_unix(flight.get("actual_out")),
            "sched_arr":  iso_to_unix(flight.get("scheduled_in")),
            "est_arr":    iso_to_unix(flight.get("estimated_in") or flight.get("actual_in")),
            "lat": None, "lng": None, "alt": 0, "speed": 0,
        }

    def _poll_route(self, callsign: str):
        """Tiered route/schedule fetch: adsbdb → FlightStats → AirLabs → FlightAware."""
        if not self._route_expired(callsign):
            return

        # Tier 0: adsbdb — airline IATA lookup (enables FlightStats)
        airline_iata = self._fetch_adsbdb_iata(callsign)

        # Tier 1: FlightStats — schedule timing (free, no quota)
        fs_result = None
        if airline_iata:
            fs_result = get_flight_info(callsign, airline_iata)
            if fs_result:
                log.info(f"[specific_flight_tracker] FlightStats hit for {callsign}: "
                         f"{fs_result.get('al_origin')}→{fs_result.get('al_destination')}")

        # Tier 2: AirLabs — skip only when FlightStats succeeded AND we already
        # have aircraft type cached (first fetch always calls AirLabs to populate type/reg)
        with self._lock:
            cached_type = self._flights[callsign].get("plane_type", "")
        al_data = None
        if not fs_result or not fs_result.get("time_estimated_arrival") or not cached_type:
            al_data = self._fetch_airlabs(callsign)

        # Build route dict, merging all available data (AirLabs wins for live fields)
        route = None
        if al_data:
            now = time.time()
            sched_dep  = iso_to_unix(al_data.get("dep_time"))
            actual_dep = iso_to_unix(al_data.get("dep_actual"))
            sched_arr  = iso_to_unix(al_data.get("arr_time"))
            est_arr    = iso_to_unix(al_data.get("arr_estimated") or al_data.get("eta"))

            # Fill timing gaps from FlightStats if AirLabs is missing them
            if fs_result:
                sched_dep  = sched_dep  or fs_result.get("time_scheduled_departure")
                actual_dep = actual_dep or fs_result.get("time_real_departure")
                sched_arr  = sched_arr  or fs_result.get("time_scheduled_arrival")
                est_arr    = est_arr    or fs_result.get("time_estimated_arrival")

            route = {
                "dep_iata":     al_data.get("dep_iata", ""),
                "arr_iata":     al_data.get("arr_iata", ""),
                "plane_type":   al_data.get("aircraft_icao", "") or al_data.get("aircraft_iata", ""),
                "registration": (al_data.get("reg_number") or "").strip().upper(),
                "sched_dep":    sched_dep,
                "actual_dep":   actual_dep,
                "sched_arr":    sched_arr,
                "est_arr":      est_arr,
                "lat":          al_data.get("lat"),
                "lng":          al_data.get("lng"),
                "alt":          al_data.get("alt", 0) or 0,
                "speed":        al_data.get("speed", 0) or 0,
            }
        elif fs_result:
            # AirLabs failed but FlightStats has route info
            route = {
                "dep_iata":   fs_result.get("al_origin", ""),
                "arr_iata":   fs_result.get("al_destination", ""),
                "plane_type": "",
                "sched_dep":  fs_result.get("time_scheduled_departure"),
                "actual_dep": fs_result.get("time_real_departure"),
                "sched_arr":  fs_result.get("time_scheduled_arrival"),
                "est_arr":    fs_result.get("time_estimated_arrival"),
                "lat": None, "lng": None, "alt": 0, "speed": 0,
            }

        # Tier 3: FlightAware — last resort when AirLabs failed or est_arr still missing
        if route is None or not route.get("est_arr"):
            fa_route = self._fetch_flightaware(callsign)
            if fa_route:
                if route is None:
                    route = fa_route
                    log.debug(f"[specific_flight_tracker] FlightAware provided route for {callsign}")
                else:
                    for field in ("sched_dep", "actual_dep", "sched_arr", "est_arr"):
                        if not route.get(field) and fa_route.get(field):
                            route[field] = fa_route[field]

        if not route:
            log.info(f"[specific_flight_tracker] No route data for {callsign} from any source")
            return

        now = time.time()
        est_arr = route.get("est_arr")
        expires = max(est_arr + 7200, now + ROUTE_CACHE_TTL) if est_arr else now + ROUTE_CACHE_TTL

        with self._lock:
            info = self._flights[callsign]
            info["route_data"]    = route
            info["route_expires"] = expires
            if est_arr and not info["arr_time_ts"]:
                info["arr_time_ts"] = est_arr

        log.debug(
            f"[specific_flight_tracker] Route cached for {callsign}: "
            f"{route['dep_iata']}→{route['arr_iata']}"
        )

    # ── Polling orchestration ─────────────────────────────────────────────────

    def _poll_callsign(self, callsign: str):
        self._poll_route(callsign)

        with self._lock:
            state = self._flights[callsign]["state"]
            route = self._flights[callsign]["route_data"]

        pos = self._get_position(callsign)

        if pos is None:
            self._handle_no_response(callsign)
            return

        if state == STATE_IDLE:
            log.info(f"[specific_flight_tracker] {callsign} found via position API while IDLE")

        # adsbdb aircraft lookup — enriches plane_type and registration for GA flights
        from utilities.adsbdb_aircraft import get_aircraft_info as _get_aircraft_info
        _reg = (pos.get("r") or pos.get("registration") or "").strip()
        _aircraft_info = _get_aircraft_info(_reg) if _reg else {}

        # FR24 route fallback — use origin/destination from position dict when
        # all route lookups failed (GA flights without a filed plan on other sources)
        if route is None:
            _fr24_dep = (pos.get("origin") or "").strip()
            _fr24_arr = (pos.get("destination") or "").strip()
            if _fr24_dep and _fr24_arr:
                route = {
                    "dep_iata": _fr24_dep, "arr_iata": _fr24_arr,
                    "plane_type": _aircraft_info.get("icao_type", ""),
                    "registration": _aircraft_info.get("registration", ""),
                    "sched_dep": None, "actual_dep": None,
                    "sched_arr": None, "est_arr": None,
                    "lat": None, "lng": None, "alt": 0, "speed": 0,
                }
                log.info(f"[specific_flight_tracker] FR24 route for {callsign}: {_fr24_dep}→{_fr24_arr}")

        if route:
            merged = {**pos, **{k: route[k] for k in (
                "dep_iata", "arr_iata", "plane_type", "registration",
                "sched_dep", "actual_dep", "sched_arr", "est_arr",
            ) if route.get(k)}}
            # Fill gaps in plane_type / registration from adsbdb_aircraft
            if not merged.get("plane_type"):
                merged["plane_type"] = _aircraft_info.get("icao_type", "")
            if not merged.get("registration"):
                merged["registration"] = _aircraft_info.get("registration", "")
        else:
            merged = {**pos,
                      "plane_type":   _aircraft_info.get("icao_type", ""),
                      "registration": _aircraft_info.get("registration", "") or pos.get("r", "")}
        self._handle_active_response(callsign, merged)

    def _handle_no_response(self, callsign: str):
        now = time.time()
        with self._lock:
            info = self._flights[callsign]
            info["last_polled"] = now
            if info["state"] == STATE_ACTIVE:
                arr_ts = info.get("arr_time_ts")
                if arr_ts and now > arr_ts:
                    info["state"] = STATE_COMPLETE
                    log.info(f"[specific_flight_tracker] {callsign} -> COMPLETE")
                else:
                    info["state"] = STATE_IDLE
                    info["data"] = None
                    log.info(f"[specific_flight_tracker] {callsign} -> IDLE (no data)")

    @staticmethod
    def _delay_color_origin(delay_min):
        from setup import colours
        if delay_min is None:    return colours.LIGHT_GREY
        if delay_min <= 20:      return colours.LIGHT_MID_GREEN
        if delay_min <= 40:      return colours.LIGHT_YELLOW
        if delay_min <= 60:      return colours.LIGHT_MID_ORANGE
        if delay_min <= 240:     return colours.LIGHT_RED
        if delay_min <= 480:     return colours.LIGHT_PURPLE
        return colours.LIGHT_DARK_BLUE

    @staticmethod
    def _delay_color_dest(delay_min):
        from setup import colours
        if delay_min is None:    return colours.LIGHT_GREY
        if delay_min <= 15:      return colours.LIGHT_MID_GREEN
        if delay_min <= 30:      return colours.LIGHT_YELLOW
        if delay_min <= 60:      return colours.LIGHT_MID_ORANGE
        if delay_min <= 240:     return colours.LIGHT_RED
        if delay_min <= 480:     return colours.LIGHT_PURPLE
        return colours.LIGHT_DARK_BLUE

    def _handle_active_response(self, callsign: str, data: dict):
        """Build display entry from merged position+route dict and update flight state."""
        now = time.time()
        lat = data.get("lat")
        lng = data.get("lng")

        if lat is None or lng is None:
            self._handle_no_response(callsign)
            return

        dep_iata     = data.get("dep_iata", "")
        arr_iata     = data.get("arr_iata", "")
        plane_type   = data.get("plane_type", "") or data.get("aircraft_icao", "") or data.get("aircraft_iata", "")
        altitude      = data.get("altitude", 0) or data.get("alt", 0) or 0
        ground_speed  = data.get("ground_speed", 0) or data.get("speed", 0) or 0
        vertical_speed = data.get("vertical_speed", 0) or 0  # ft/min (converted in _get_position)

        # Use cached plane_type/registration as fallback when position APIs don't supply them
        with self._lock:
            cached_type = self._flights[callsign].get("plane_type", "")
            cached_reg  = self._flights[callsign].get("registration", "")
        plane_type   = plane_type or cached_type
        registration = data.get("registration", "") or cached_reg

        # Update cache whenever we get a better (non-empty) value
        with self._lock:
            if plane_type:
                self._flights[callsign]["plane_type"] = plane_type
            if registration:
                self._flights[callsign]["registration"] = registration

        # Estimate VS from altitude change if the API didn't report it
        with self._lock:
            last_alt    = self._flights[callsign].get("last_altitude")
            last_alt_ts = self._flights[callsign].get("last_alt_ts")
        if vertical_speed == 0 and altitude and last_alt is not None and last_alt_ts is not None:
            elapsed_min = (now - last_alt_ts) / 60.0
            if elapsed_min >= 0.5:  # need at least 30s between samples
                vertical_speed = int((altitude - last_alt) / elapsed_min)

        sched_dep  = data.get("sched_dep")  or iso_to_unix(data.get("dep_time"))
        actual_dep = data.get("actual_dep") or iso_to_unix(data.get("dep_actual"))
        sched_arr  = data.get("sched_arr")  or iso_to_unix(data.get("arr_time"))
        est_arr    = data.get("est_arr")    or iso_to_unix(data.get("arr_estimated") or data.get("eta"))

        arr_ts = est_arr

        dep_delay_min = (actual_dep - sched_dep) / 60 if actual_dep and sched_dep else None
        arr_delay_min = (est_arr - sched_arr) / 60 if est_arr and sched_arr else None

        dep_coords = self._airport_db.get(dep_iata)
        arr_coords = self._airport_db.get(arr_iata)

        dist_remaining = 0.0
        total_distance = None
        progress = 0.0

        if arr_coords:
            dist_remaining = haversine(lat, lng, arr_coords[0], arr_coords[1])

        with self._lock:
            info = self._flights[callsign]
            existing_total = info.get("total_distance")

            if existing_total is None and dep_coords and arr_coords:
                total_distance = haversine(dep_coords[0], dep_coords[1], arr_coords[0], arr_coords[1])
                info["total_distance"] = total_distance
            else:
                total_distance = existing_total

        if total_distance and total_distance > 0:
            progress = max(0.0, min(1.0, (total_distance - dist_remaining) / total_distance))

        time_remaining_min = 0.0
        if ground_speed and ground_speed > 0:
            time_remaining_min = (dist_remaining / ground_speed) * 60

        display_entry = {
            "callsign":          callsign,
            "origin":            dep_iata,
            "destination":       arr_iata,
            "origin_color":      self._delay_color_origin(dep_delay_min),
            "destination_color": self._delay_color_dest(arr_delay_min),
            "plane_type":        plane_type,
            "registration":      registration,
            "progress":          progress,
            "dist_remaining":    dist_remaining,
            "time_remaining":    _format_time_remaining(time_remaining_min),
            "altitude":          int(altitude),
            "ground_speed":      int(ground_speed),
            "vertical_speed":    int(vertical_speed),
            "lat":               lat,
            "lng":               lng,
        }

        with self._lock:
            info = self._flights[callsign]
            info["state"]         = STATE_ACTIVE
            info["data"]          = display_entry
            info["last_polled"]   = now
            info["last_altitude"] = altitude
            info["last_alt_ts"]   = now
            if arr_ts:
                info["arr_time_ts"] = arr_ts

        log.info(
            f"[specific_flight_tracker] {callsign} -> ACTIVE "
            f"({dep_iata}->{arr_iata}, {progress:.0%}) "
            f"alt={int(altitude)} vs={int(vertical_speed)}"
        )

    def _build_display_data(self) -> list:
        result = []
        with self._lock:
            for cs, info in self._flights.items():
                if info["state"] == STATE_ACTIVE and info["data"]:
                    result.append(info["data"])
        return result

    def update(self):
        if not self.enabled:
            return

    def reset(self):
        self._ensure_display()
        self._display.canvas = self.display_manager.canvas
        self._display.matrix = self.display_manager.matrix
        self._display._data_index = 0
        self._display._data_all_looped = False
        self._display._stats_all_looped = False
        self._display._show_additional_details = self.config.get("show_additional_details", True)
        self._display._show_nearby_locations = self.config.get("show_nearby_locations", True)
        self._display.reset_scene()

    def draw(self) -> bool:
        self._ensure_display()
        self._display.canvas = self.display_manager.canvas
        self._display.matrix = self.display_manager.matrix
        self._display._show_additional_details = self.config.get("show_additional_details", True)
        self._display._show_nearby_locations = self.config.get("show_nearby_locations", True)

        fresh = self._build_display_data()
        if fresh != self._display._data:
            self._display._data = fresh
            self._display._data_index = 0
            self._display._data_all_looped = False

        self._display.tick()
        return len(self._display._data) > 0

    def has_content(self) -> bool:
        with self._lock:
            return any(
                info["state"] == STATE_ACTIVE
                for info in self._flights.values()
            )

    def has_live_priority(self) -> bool:
        return False

    def has_live_content(self) -> bool:
        with self._lock:
            return any(
                info["state"] == STATE_ACTIVE
                for info in self._flights.values()
            )

    def is_cycle_complete(self) -> bool:
        if self._display is None:
            return True
        return getattr(self._display, "_stats_all_looped", False)

    def on_config_change(self, new_config: dict):
        super().on_config_change(new_config)
        new_callsigns = [cs.upper() for cs in new_config.get("tracked_callsigns", [])]
        with self._lock:
            for cs in new_callsigns:
                if cs not in self._flights:
                    self._flights[cs] = _new_flight_entry()
            for cs in list(self._flights.keys()):
                if cs not in new_callsigns:
                    del self._flights[cs]
        if self.enabled and new_callsigns:
            self._start_poll_thread()
