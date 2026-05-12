import logging
import math
import requests

log = logging.getLogger(__name__)

OPENSKY_URL = "https://opensky-network.org/api/states/all"

# State vector field indexes
_IDX_LON        = 5
_IDX_LAT        = 6
_IDX_BARO_ALT   = 7   # metres
_IDX_ON_GROUND  = 8
_IDX_VELOCITY   = 9   # m/s
_IDX_HEADING    = 10  # degrees
_IDX_VERT_RATE  = 11  # m/s


def get_flight_position(callsign: str) -> dict | None:
    """Query OpenSky for a single callsign. Returns position dict or None if not found/airborne."""
    padded = callsign.ljust(8)
    try:
        resp = requests.get(OPENSKY_URL, params={"callsign": padded}, timeout=10)
        resp.raise_for_status()
        states = resp.json().get("states") or []
    except Exception as e:
        log.debug(f"[opensky] Error for {callsign}: {e}")
        return None

    for s in states:
        if not s or s[_IDX_ON_GROUND]:
            continue
        lat = s[_IDX_LAT]
        lon = s[_IDX_LON]
        if lat is None or lon is None:
            continue
        baro_alt = s[_IDX_BARO_ALT]
        velocity = s[_IDX_VELOCITY]
        return {
            "lat": lat,
            "lng": lon,
            "altitude":       round(baro_alt * 3.28084) if baro_alt is not None else 0,
            "ground_speed":   round(velocity * 1.94384) if velocity is not None else 0,
            "heading":        s[_IDX_HEADING] or 0,
            "vertical_speed": s[_IDX_VERT_RATE] or 0,
        }

    return None


def get_aircraft_in_area(lat: float, lon: float, radius_nm: float) -> list[dict]:
    """Return nearby aircraft as a list of dicts matching adsb.lol 'ac' format."""
    deg     = radius_nm / 60.0
    lon_deg = radius_nm / (60.0 * math.cos(math.radians(lat)))
    params  = {
        "lamin": lat - deg,   "lomin": lon - lon_deg,
        "lamax": lat + deg,   "lomax": lon + lon_deg,
    }
    try:
        resp = requests.get(OPENSKY_URL, params=params, timeout=15)
        if resp.status_code != 200:
            log.debug(f"[opensky] area query HTTP {resp.status_code}")
            return []
        ac = []
        for s in (resp.json().get("states") or []):
            if not s:
                continue
            if s[_IDX_ON_GROUND] or s[_IDX_LAT] is None or s[_IDX_LON] is None:
                continue
            callsign = (s[1] or "").strip()
            if not callsign:
                continue
            baro_m = s[_IDX_BARO_ALT] or 0
            vel_ms = s[_IDX_VELOCITY] or 0
            vrate  = s[_IDX_VERT_RATE] or 0
            ac.append({
                "flight":    callsign,
                "alt_baro":  round(baro_m * 3.28084),    # m → ft
                "lat":       s[_IDX_LAT],
                "lon":       s[_IDX_LON],
                "gs":        round(vel_ms * 1.94384),     # m/s → knots
                "track":     s[_IDX_HEADING] or 0,
                "baro_rate": round(vrate * 196.85),       # m/s → ft/min
                "t":         "",
                "r":         s[0] or "",
            })
        return ac
    except Exception as e:
        log.debug(f"[opensky] area query failed: {e}")
        return []
