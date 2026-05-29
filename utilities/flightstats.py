import logging
import re
import requests
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

_FS_URL = (
    "https://www.flightstats.com/v2/api-next/flight-tracker"
    "/{iata}/{number}/{year}/{month}/{day}"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "x-requested-with": "XMLHttpRequest",
}
_NUM_RE = re.compile(r"\d+")


def _fetch_for_date(owner_iata: str, number: str, dt: datetime) -> dict:
    url = _FS_URL.format(
        iata=owner_iata.upper(),
        number=number,
        year=dt.year,
        month=dt.month,
        day=dt.day,
    )
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=8)
        if resp.status_code == 200:
            return resp.json().get("data") or {}
        log.debug(f"[flightstats] HTTP {resp.status_code} for {owner_iata}{number} on {dt.date()}")
    except Exception as e:
        log.debug(f"[flightstats] {owner_iata}{number}: {e}")
    return {}


def get_flight_info(callsign: str, owner_iata: str) -> dict | None:
    """Fetch route/schedule from FlightStats. Returns result dict or None."""
    if not owner_iata:
        return None
    m = _NUM_RE.search(callsign)
    if not m:
        return None
    number = str(int(m.group()))  # strip leading zeros: "006" → "6"
    today = datetime.now()
    data = _fetch_for_date(owner_iata, number, today)
    if not data:
        data = _fetch_for_date(owner_iata, number, today - timedelta(days=1))
    if not data:
        return None
    return _parse(data, callsign)


def _iso_utc_to_unix(s: str | None) -> int | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _parse(data: dict, callsign: str) -> dict | None:
    dep_iata = (data.get("departureAirport") or {}).get("iata", "")
    arr_iata = (data.get("arrivalAirport") or {}).get("iata", "")
    if not dep_iata or not arr_iata:
        return None
    sched = data.get("schedule") or {}
    result = {
        "al_origin":                dep_iata,
        "al_destination":           arr_iata,
        "time_scheduled_departure": _iso_utc_to_unix(sched.get("scheduledDepartureUTC")),
        "time_real_departure":      _iso_utc_to_unix(sched.get("estimatedActualDepartureUTC")),
        "time_scheduled_arrival":   _iso_utc_to_unix(sched.get("scheduledArrivalUTC")),
        "time_estimated_arrival":   _iso_utc_to_unix(sched.get("estimatedActualArrivalUTC")),
        "al_airline":    "",
        "al_owner_iata": "",
        "al_owner_icao": "",
    }
    if result["time_scheduled_arrival"] and result["time_scheduled_departure"]:
        if result["time_scheduled_arrival"] < result["time_scheduled_departure"]:
            result["time_scheduled_arrival"] += 86400
    if result["time_estimated_arrival"] and result["time_real_departure"]:
        if result["time_estimated_arrival"] < result["time_real_departure"]:
            result["time_estimated_arrival"] += 86400
    return result
