"""
Address / place name → coordinates, for the home location (setup wizard and
config page).

OpenStreetMap's Nominatim first: it understands full street addresses. Its
usage policy asks for an identifying User-Agent and at most one request a
second; lookups here only happen when someone presses Find. If it fails or
finds nothing, Open-Meteo's geocoder (city / town names) is tried.
"""

import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_UA = "FlightClock/1.0 (+https://github.com/RoyMan0/flight-clock-and-more)"


class GeocodeError(Exception):
    pass


def _get(url: str, timeout: float = 10):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _nominatim(query: str, limit: int) -> list:
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "limit": limit})
    return [{"name": r.get("display_name") or query, "lat": float(r["lat"]), "lon": float(r["lon"])}
            for r in _get(url) if r.get("lat") and r.get("lon")]


def _open_meteo(query: str, limit: int) -> list:
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": query, "count": limit, "format": "json"})
    out = []
    for r in _get(url).get("results") or []:
        parts = [r.get("name"), r.get("admin1"), r.get("country")]
        name = ", ".join(dict.fromkeys(p for p in parts if p))     # drop repeats ("Singapore, Singapore")
        out.append({"name": name, "lat": float(r["latitude"]), "lon": float(r["longitude"])})
    return out


def search(query: str, limit: int = 5) -> list:
    """[{'name', 'lat', 'lon'}, …], best match first. Raises GeocodeError if no lookup worked."""
    query = (query or "").strip()
    if not query:
        raise GeocodeError("Type an address or place name")
    errors = []
    for source in (_nominatim, _open_meteo):
        try:
            results = source(query, limit)
            if results:
                return results
        except Exception as e:
            log.info(f"[geocode] {source.__name__} failed: {e}")
            errors.append(e)
    if errors and len(errors) == 2:
        raise GeocodeError("Couldn't reach the address lookup (is this device online?)")
    return []
