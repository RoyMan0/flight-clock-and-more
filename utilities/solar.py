"""
Sun position helpers shared by the world daylight map and the world clock.

Pure-Python approximations (well under a degree) — no network or ephemeris data.
"""

import math
from datetime import datetime
from typing import Tuple

# A place is in daylight when the sun is above the horizon: angular distance from the
# sub-solar point < 90°, plus ~0.83° for refraction and the sun's radius.
DAYLIGHT_ANGLE = 90.83


def sun_geo_position(dt_utc: datetime) -> Tuple[float, float]:
    """Return (lat, lon) of sub-solar point (where sun is directly overhead)."""
    jd = dt_utc.timestamp() / 86400.0 + 2440587.5
    n  = jd - 2451545.0   # days from J2000.0

    # Mean longitude and anomaly
    L = (280.460 + 0.9856474 * n) % 360
    g = math.radians((357.528 + 0.9856003 * n) % 360)

    # Ecliptic longitude
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))

    # Obliquity of ecliptic
    eps = math.radians(23.439 - 0.0000004 * n)

    # Declination
    dec = math.degrees(math.asin(math.sin(eps) * math.sin(lam)))

    # Right ascension
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))) % 360

    # Greenwich Mean Sidereal Time → GHA → geographic longitude
    # GHA is measured westward; geographic lon is eastward, so lon = -GHA
    gmst = (280.46061837 + 360.98564736629 * n) % 360
    gha  = (gmst - ra) % 360
    lon  = -gha if gha <= 180 else 360 - gha

    return dec, lon


def angular_dist(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine angular distance in degrees between two geographic points."""
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    dlat   = math.radians(lat2 - lat1)
    dlon   = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2)
    return math.degrees(2 * math.asin(math.sqrt(max(0.0, min(1.0, a)))))


def is_daylight(lat: float, lon: float, dt_utc: datetime) -> bool:
    """True when the sun is up at (lat, lon) at the given UTC time."""
    sun_lat, sun_lon = sun_geo_position(dt_utc)
    return angular_dist(lat, lon, sun_lat, sun_lon) < DAYLIGHT_ANGLE
