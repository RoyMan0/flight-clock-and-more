"""
fr24_lookup.py — Live flight position lookup via the unofficial FR24 gRPC package.

Install: pip install fr24 h2 hpack

Anonymous access works — no FR24 account or API key required.
Anonymous fields available: callsign, registration, origin/dest IATA, aircraft type,
lat/lon/alt/speed/heading. Vertical speed requires auth (defaults to 0 here; the
specific_flight_tracker's altitude-delta estimation handles this gap).

FR24 has Aireon satellite data, providing broader coverage than adsb.lol/OpenSky
especially for oceanic and remote-area flights.

If the fr24 package is not installed, is_available() returns False and all functions
return empty/None — existing adsb.lol → OpenSky chain is used unchanged.
"""

import asyncio
import logging
import time

log = logging.getLogger(__name__)

_AVAILABLE = False
try:
    from fr24 import FR24, BoundingBox
    from fr24.proto._live_feed_pb2 import (
        LiveFeedRequest, LiveFeedResponse, LocationBoundaries,
        VisibilitySettings, Filter,
    )
    from fr24.proto._common_pb2 import TrafficType, RestrictionVisibility
    from fr24.grpc import live_feed as _grpc_live_feed
    from fr24.proto import parse_data as _parse_data
    from google.protobuf.field_mask_pb2 import FieldMask
    _AVAILABLE = True
except ImportError:
    pass

# Bounding-box query cache — FR24 polling guideline is 90 seconds
_AREA_CACHE: dict = {}
_AREA_POLL_INTERVAL = 90  # seconds


def is_available() -> bool:
    """Return True if the fr24 package is installed and importable."""
    return _AVAILABLE


# ── Internal helpers ──────────────────────────────────────────────────────────

def _run(coro):
    """Run an async coroutine synchronously in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _with_fr24(async_func):
    """Open an FR24 context, attempt anonymous login, run async_func."""
    fr24 = FR24()
    async with fr24:
        try:
            await fr24.login("from_env")
        except Exception:
            pass  # anonymous access is fine
        return await async_func(fr24)


def _proto_to_ac(flights_list) -> list[dict]:
    """Convert FR24 proto flight list to adsb.lol ac-dict format."""
    result = []
    for f in flights_list:
        extra    = getattr(f, 'extra_info', None)
        route    = getattr(extra, 'route', None) if extra else None
        callsign = (getattr(f, 'callsign', '') or '').strip()
        if not callsign:
            continue
        if getattr(f, 'on_ground', False):
            continue
        reg    = (getattr(extra, 'reg',  '') or '') if extra else ''
        actype = (getattr(extra, 'type', '') or '') if extra else ''
        origin = (getattr(route, 'from', '') or '') if route else ''
        dest   = (getattr(route, 'to',   '') or '') if route else ''
        result.append({
            "flight":       callsign,
            "lat":          getattr(f, 'lat', 0),
            "lon":          getattr(f, 'lon', 0),
            "alt_baro":     getattr(f, 'alt', 0),
            "gs":           getattr(f, 'speed', 0),
            "track":        getattr(f, 'track', 0),
            "baro_rate":    0,        # vspeed requires auth
            "t":            actype,
            "r":            reg,
            "hex":          "",       # ICAO24 not in basic FR24 feed
            "origin":       origin,
            "destination":  dest,
        })
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def get_flights_in_area(zone: dict) -> list[dict]:
    """
    Return aircraft in the given bounding box (tl_y/tl_x/br_y/br_x format).
    Results are cached for 90 seconds per zone.
    Returns [] if fr24 is unavailable, on error, or when rate-limited (returns cache).
    """
    if not _AVAILABLE:
        return []

    cache_key = f"{zone.get('tl_y')},{zone.get('tl_x')},{zone.get('br_y')},{zone.get('br_x')}"
    cached = _AREA_CACHE.get(cache_key)
    now = time.time()

    if cached and (now - cached["ts"]) < _AREA_POLL_INTERVAL:
        log.debug(f"[fr24_lookup] area cache hit: {len(cached['flights'])} aircraft")
        return cached["flights"]

    try:
        async def _fetch(fr24):
            bbox = BoundingBox(
                south=zone["br_y"], north=zone["tl_y"],
                west=zone["tl_x"],  east=zone["br_x"],
            )
            result = await fr24.live_feed.fetch(
                bounding_box=bbox,
                limit=1500,
                fields={"flight", "reg", "route", "type"},
            )
            proto = result.to_proto()
            return _proto_to_ac(proto.flights_list)

        flights = _run(_with_fr24(_fetch))
        _AREA_CACHE[cache_key] = {"flights": flights, "ts": now}
        log.info(f"[fr24_lookup] bounding box: {len(flights)} aircraft")
        return flights

    except Exception as e:
        log.warning(f"[fr24_lookup] area query failed: {e}")
        # Return stale cache if available, otherwise empty
        return cached["flights"] if cached else []


def get_flight_position(callsign: str) -> dict | None:
    """
    Find a specific callsign worldwide via FR24 server-side gRPC filter.
    Returns an ac-dict with lat/lon/alt/gs/track/origin/destination, or None.
    Works globally including oceanic routes (FR24 has Aireon satellite data).
    """
    if not _AVAILABLE or not callsign:
        return None

    cs = callsign.strip().upper()
    try:
        async def _find(fr24):
            req = LiveFeedRequest(
                bounds=LocationBoundaries(north=90, south=-90, west=-180, east=180),
                settings=VisibilitySettings(
                    sources_list=range(10),
                    services_list=range(12),
                    traffic_type=TrafficType.ALL,
                ),
                filters_list=Filter(callsigns_list=[cs]),
                field_mask=FieldMask(paths=["flight", "reg", "route", "type"]),
                limit=10,
                maxage=14400,
                restriction_mode=RestrictionVisibility.NOT_VISIBLE,
            )
            response = await _grpc_live_feed(fr24.http.client, req, fr24.http.grpc_headers)
            result   = _parse_data(response.content, LiveFeedResponse)
            proto    = result.unwrap()
            flights  = _proto_to_ac(proto.flights_list)
            return flights[0] if flights else None

        pos = _run(_with_fr24(_find))
        if pos:
            log.info(f"[fr24_lookup] found {cs} at lat={pos['lat']:.2f} lon={pos['lon']:.2f}")
        return pos

    except Exception as e:
        log.debug(f"[fr24_lookup] {cs}: {e}")
        return None
