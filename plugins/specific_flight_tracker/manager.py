import json
import logging
import time
from threading import Thread, Lock

import requests

from plugins.base_plugin import BasePlugin
from utilities.animator import Animator
from utilities.opensky import get_flight_position
from utilities.overhead import (
    haversine, AIRPORT_DB_FILE, iso_to_unix,
    _get_keys, _get_reset_days, _al_get_active_key, _al_record_call,
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

IDLE_POLL_INTERVAL   = 15 * 60   # seconds between AirLabs polls when not yet airborne
ACTIVE_POLL_INTERVAL =  3 * 60   # seconds between OpenSky position polls when airborne
ROUTE_CACHE_TTL      =      3600  # fallback cache duration when no arrival time known

AIRLABS_URL = "https://airlabs.co/api/v9/flight?flight_icao={callsign}&api_key={key}"


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
        "state":           STATE_IDLE,
        "data":            None,
        "last_polled":     0.0,
        "arr_time_ts":     None,
        "total_distance":  None,
        # Route cache fields (populated once from AirLabs, reused by OpenSky position polls)
        "route_data":      None,   # dict: dep_iata, arr_iata, plane_type, 4 timestamps
        "route_expires":   0.0,    # wall-clock time when route_data should be re-fetched
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

    # ── Route caching (AirLabs) ───────────────────────────────────────────────

    def _route_expired(self, callsign: str) -> bool:
        with self._lock:
            info = self._flights[callsign]
            return info["route_data"] is None or time.time() >= info["route_expires"]

    def _poll_route(self, callsign: str):
        """Fetch route/schedule from AirLabs and cache it. No-ops if cache still valid."""
        if not self._route_expired(callsign):
            return

        al_keys = _get_keys(self.secrets, "airlabs_api_keys", "airlabs_api_key")
        al_reset_days = _get_reset_days(self.secrets, "airlabs_reset_days", len(al_keys))
        key = _al_get_active_key(al_keys, al_reset_days)
        if not key:
            log.warning("[specific_flight_tracker] No AirLabs key available for route fetch")
            return

        url = AIRLABS_URL.format(callsign=callsign, key=key)
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            log.warning(f"[specific_flight_tracker] Route fetch failed for {callsign}: {e}")
            return

        data = body.get("response")
        if not data:
            return

        key_reset_day = al_reset_days[al_keys.index(key)]
        _al_record_call(key, key_reset_day)

        now = time.time()
        sched_dep  = iso_to_unix(data.get("dep_time"))
        actual_dep = iso_to_unix(data.get("dep_actual"))
        sched_arr  = iso_to_unix(data.get("arr_time"))
        est_arr    = iso_to_unix(data.get("arr_estimated") or data.get("eta"))

        route = {
            "dep_iata":   data.get("dep_iata", ""),
            "arr_iata":   data.get("arr_iata", ""),
            "plane_type": data.get("aircraft_icao", "") or data.get("aircraft_iata", ""),
            "sched_dep":  sched_dep,
            "actual_dep": actual_dep,
            "sched_arr":  sched_arr,
            "est_arr":    est_arr,
            # Also carry raw position so IDLE→ACTIVE detection still works
            "lat":        data.get("lat"),
            "lng":        data.get("lng"),
            "alt":        data.get("alt", 0) or 0,
            "speed":      data.get("speed", 0) or 0,
        }

        # Cache until arrival + 2 h, or ROUTE_CACHE_TTL if no arrival time
        if est_arr:
            expires = max(est_arr + 7200, now + ROUTE_CACHE_TTL)
        else:
            expires = now + ROUTE_CACHE_TTL

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
        # Always refresh route cache if stale (cheap when cache is warm)
        self._poll_route(callsign)

        with self._lock:
            state = self._flights[callsign]["state"]
            route = self._flights[callsign]["route_data"]

        if state == STATE_ACTIVE:
            # Use OpenSky (free) for live position; route data already cached
            pos = get_flight_position(callsign)
            if pos is None:
                self._handle_no_response(callsign)
                return
            if route:
                merged = {**pos, **{k: route[k] for k in (
                    "dep_iata", "arr_iata", "plane_type",
                    "sched_dep", "actual_dep", "sched_arr", "est_arr",
                )}}
            else:
                merged = pos
            self._handle_active_response(callsign, merged)
        else:
            # IDLE — use AirLabs response to detect departure (includes position if airborne)
            if route and (route.get("lat") is not None) and (route.get("lng") is not None):
                merged = {
                    "lat":      route["lat"],
                    "lng":      route["lng"],
                    "altitude": route["alt"],
                    "ground_speed": route["speed"],
                    "heading":  0,
                    "vertical_speed": 0,
                    "dep_iata":   route["dep_iata"],
                    "arr_iata":   route["arr_iata"],
                    "plane_type": route["plane_type"],
                    "sched_dep":  route["sched_dep"],
                    "actual_dep": route["actual_dep"],
                    "sched_arr":  route["sched_arr"],
                    "est_arr":    route["est_arr"],
                }
                self._handle_active_response(callsign, merged)
            else:
                self._handle_no_response(callsign)

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
        if delay_min is None:         return colours.LIGHT_GREY
        if delay_min <= 20:           return colours.LIGHT_MID_GREEN
        if delay_min <= 40:           return colours.LIGHT_YELLOW
        if delay_min <= 60:           return colours.LIGHT_MID_ORANGE
        if delay_min <= 240:          return colours.LIGHT_RED
        if delay_min <= 480:          return colours.LIGHT_PURPLE
        return colours.LIGHT_DARK_BLUE

    @staticmethod
    def _delay_color_dest(delay_min):
        from setup import colours
        if delay_min is None:         return colours.LIGHT_GREY
        if delay_min <= 15:           return colours.LIGHT_MID_GREEN
        if delay_min <= 30:           return colours.LIGHT_YELLOW
        if delay_min <= 60:           return colours.LIGHT_MID_ORANGE
        if delay_min <= 240:          return colours.LIGHT_RED
        if delay_min <= 480:          return colours.LIGHT_PURPLE
        return colours.LIGHT_DARK_BLUE

    def _handle_active_response(self, callsign: str, data: dict):
        """Build display entry from a merged position+route dict and update flight state."""
        now = time.time()
        lat = data.get("lat")
        lng = data.get("lng")

        if lat is None or lng is None:
            self._handle_no_response(callsign)
            return

        dep_iata    = data.get("dep_iata", "")
        arr_iata    = data.get("arr_iata", "")
        plane_type  = data.get("plane_type", "") or data.get("aircraft_icao", "") or data.get("aircraft_iata", "")
        altitude    = data.get("altitude", 0) or data.get("alt", 0) or 0
        ground_speed = data.get("ground_speed", 0) or data.get("speed", 0) or 0

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
            "callsign":           callsign,
            "origin":             dep_iata,
            "destination":        arr_iata,
            "origin_color":       self._delay_color_origin(dep_delay_min),
            "destination_color":  self._delay_color_dest(arr_delay_min),
            "plane_type":         plane_type,
            "progress":           progress,
            "dist_remaining":     dist_remaining,
            "time_remaining":     _format_time_remaining(time_remaining_min),
            "altitude":           int(altitude),
            "ground_speed":       int(ground_speed),
        }

        with self._lock:
            info = self._flights[callsign]
            info["state"]       = STATE_ACTIVE
            info["data"]        = display_entry
            info["last_polled"] = now
            if arr_ts:
                info["arr_time_ts"] = arr_ts

        log.info(
            f"[specific_flight_tracker] {callsign} -> ACTIVE "
            f"({dep_iata}->{arr_iata}, {progress:.0%})"
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
        self._display.reset_scene()

    def draw(self) -> bool:
        self._ensure_display()
        self._display.canvas = self.display_manager.canvas
        self._display.matrix = self.display_manager.matrix

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
