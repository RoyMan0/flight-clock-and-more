"""
adsbdb_aircraft.py — Aircraft registration/owner lookup via api.adsbdb.com.
Free, no API key required. 30-day cache (registrations almost never change).

Accepts ICAO24 hex (from adsb.lol 'hex' field) or N-number / registration
(from FR24 'r' field) — adsbdb accepts both formats identically.

Returns registered owner name and ICAO aircraft type for GA flights that
have no airline info from callsign-based route lookups.
"""

import requests
import logging
from time import time

log = logging.getLogger(__name__)

_BASE = "https://api.adsbdb.com/v0/aircraft"
_TTL  = 60 * 60 * 24 * 30   # 30 days
_cache: dict = {}


def get_aircraft_info(identifier: str) -> dict:
    """
    Look up aircraft by ICAO24 hex (e.g. 'a9fb92') or registration (e.g. 'N742SK').
    Returns:
        {"operator": str, "icao_type": str, "registration": str}  or {}
    operator  — registered owner name (e.g. "NetJets Inc", "Southwest Airlines")
    icao_type — ICAO type code (e.g. "B738", "C172")
    """
    if not identifier:
        return {}
    key = identifier.strip().upper()
    cached = _cache.get(key)
    if cached is not None and (time() - cached["ts"]) < _TTL:
        return cached["data"] or {}
    try:
        r = requests.get(f"{_BASE}/{key}", timeout=8)
        if r.status_code == 404:
            _cache[key] = {"data": None, "ts": time()}
            return {}
        r.raise_for_status()
        raw = (r.json().get("response", {}) or {}).get("aircraft") or None
        if not raw:
            _cache[key] = {"data": None, "ts": time()}
            return {}
        data = {
            "operator":     raw.get("registered_owner", ""),
            "icao_type":    raw.get("icao_type", ""),
            "registration": raw.get("registration", ""),
        }
        _cache[key] = {"data": data, "ts": time()}
        log.debug(f"[adsbdb_aircraft] {key} → {data['icao_type']} {data['operator']}")
        return data
    except Exception as e:
        log.debug(f"[adsbdb_aircraft] {key}: {e}")
        return {}
