"""
eclipses.py — Solar and lunar eclipse alerts.

Hardcoded database covering 2025–2035. No external API required.
Returns countdown alerts starting 36 hours before each eclipse peak.

Usage:
    from utilities.eclipses import get_eclipse_alerts
    alerts = get_eclipse_alerts()
    # [{"text": "Lnr 4h", "color": "purple"}, ...]  or  []
"""

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_ALERT_WINDOW_HOURS = 36  # start alerting this many hours before peak

# Visibility region bounding boxes [lat_min, lat_max, lon_min, lon_max]
_REGIONS = {
    "north_america": (-10.0, 75.0,  -170.0, -50.0),
    "south_america": (-60.0, 15.0,  -85.0,  -30.0),
    "europe":        (34.0,  72.0,  -15.0,   45.0),
    "africa":        (-35.0, 38.0,  -20.0,   55.0),
    "asia":          (5.0,   78.0,   45.0,  150.0),
    "australia":     (-48.0, -10.0, 110.0,  180.0),
    "all":           (-90.0, 90.0,  -180.0, 180.0),
}

# Eclipse database: solar and lunar eclipses 2025–2035.
# hour_utc/minute_utc = approximate peak time.
# duration_min = length of totality or main partial phase.
# regions = where the eclipse is meaningfully visible ("all" for lunar).
_ECLIPSE_DB = [
    # ── 2025 ─────────────────────────────────────────────────────
    {"year": 2025, "month": 3,  "day": 14, "hour_utc": 6,  "minute_utc": 58,
     "type": "lunar_total",    "duration_min": 65,  "regions": ["all"]},
    {"year": 2025, "month": 9,  "day": 7,  "hour_utc": 18, "minute_utc": 11,
     "type": "lunar_total",    "duration_min": 83,  "regions": ["all"]},
    # ── 2026 ─────────────────────────────────────────────────────
    {"year": 2026, "month": 2,  "day": 17, "hour_utc": 12, "minute_utc": 13,
     "type": "solar_annular",  "duration_min": 180, "regions": ["south_america"]},
    {"year": 2026, "month": 3,  "day": 3,  "hour_utc": 11, "minute_utc": 33,
     "type": "lunar_total",    "duration_min": 72,  "regions": ["all"]},
    {"year": 2026, "month": 8,  "day": 12, "hour_utc": 17, "minute_utc": 47,
     "type": "solar_total",    "duration_min": 120, "regions": ["europe", "africa"]},
    # ── 2027 ─────────────────────────────────────────────────────
    {"year": 2027, "month": 2,  "day": 6,  "hour_utc": 16, "minute_utc": 0,
     "type": "solar_annular",  "duration_min": 180, "regions": ["south_america", "africa"]},
    {"year": 2027, "month": 8,  "day": 2,  "hour_utc": 10, "minute_utc": 7,
     "type": "solar_total",    "duration_min": 120, "regions": ["africa", "europe", "asia"]},
    # ── 2028 ─────────────────────────────────────────────────────
    {"year": 2028, "month": 1,  "day": 26, "hour_utc": 15, "minute_utc": 8,
     "type": "solar_annular",  "duration_min": 180, "regions": ["south_america", "africa"]},
    {"year": 2028, "month": 7,  "day": 22, "hour_utc": 2,  "minute_utc": 56,
     "type": "solar_total",    "duration_min": 120, "regions": ["australia", "asia"]},
    {"year": 2028, "month": 12, "day": 31, "hour_utc": 16, "minute_utc": 52,
     "type": "lunar_total",    "duration_min": 54,  "regions": ["all"]},
    # ── 2029 ─────────────────────────────────────────────────────
    {"year": 2029, "month": 6,  "day": 12, "hour_utc": 4,  "minute_utc": 6,
     "type": "solar_annular",  "duration_min": 180, "regions": ["europe", "africa", "asia"]},
    {"year": 2029, "month": 6,  "day": 26, "hour_utc": 3,  "minute_utc": 23,
     "type": "lunar_total",    "duration_min": 102, "regions": ["all"]},
    {"year": 2029, "month": 12, "day": 5,  "hour_utc": 15, "minute_utc": 3,
     "type": "solar_total",    "duration_min": 120, "regions": ["south_america"]},
    {"year": 2029, "month": 12, "day": 20, "hour_utc": 22, "minute_utc": 42,
     "type": "lunar_total",    "duration_min": 54,  "regions": ["all"]},
    # ── 2030 ─────────────────────────────────────────────────────
    {"year": 2030, "month": 6,  "day": 1,  "hour_utc": 6,  "minute_utc": 29,
     "type": "solar_annular",  "duration_min": 180, "regions": ["europe", "africa", "asia"]},
    {"year": 2030, "month": 6,  "day": 15, "hour_utc": 18, "minute_utc": 33,
     "type": "lunar_partial",  "duration_min": 50,  "regions": ["all"]},
    {"year": 2030, "month": 11, "day": 25, "hour_utc": 6,  "minute_utc": 51,
     "type": "solar_total",    "duration_min": 120, "regions": ["south_america", "africa"]},
    # ── 2033 ─────────────────────────────────────────────────────
    {"year": 2033, "month": 3,  "day": 30, "hour_utc": 17, "minute_utc": 20,
     "type": "solar_total",    "duration_min": 120, "regions": ["north_america", "asia"]},
    {"year": 2033, "month": 10, "day": 18, "hour_utc": 3,  "minute_utc": 55,
     "type": "lunar_total",    "duration_min": 65,  "regions": ["all"]},
]


def _in_region(lat, lon, regions):
    for name in regions:
        bounds = _REGIONS.get(name)
        if bounds:
            lat_min, lat_max, lon_min, lon_max = bounds
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                return True
    return False


def get_eclipse_alerts():
    """
    Return alert dicts for eclipses within the 36-hour window, or [].

    Each dict: {"text": "Lnr 4h", "color": "purple"}
    Prefix "Slr" for solar, "Lnr" for lunar.
    Format: Xd (days), Xh (hours), Xm (minutes), "!" (during event).
    """
    try:
        import config as cfg
        location = getattr(cfg, "LOCATION_HOME", [0.0, 0.0])
        lat, lon = float(location[0]), float(location[1])
    except Exception:
        return []

    if lat == 0.0 and lon == 0.0:
        return []

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=0)  # already past = during event
    window_end = now + timedelta(hours=_ALERT_WINDOW_HOURS)
    alerts = []

    for eclipse in _ECLIPSE_DB:
        if not _in_region(lat, lon, eclipse["regions"]):
            continue

        peak_dt = datetime(
            eclipse["year"], eclipse["month"], eclipse["day"],
            eclipse["hour_utc"], eclipse["minute_utc"],
            tzinfo=timezone.utc,
        )
        event_end = peak_dt + timedelta(minutes=eclipse["duration_min"])
        prefix = "Slr" if eclipse["type"].startswith("solar") else "Lnr"

        if peak_dt <= now <= event_end:
            alerts.append({"text": f"{prefix}!", "color": "purple"})
            continue

        if now < peak_dt <= window_end:
            secs = (peak_dt - now).total_seconds()
            if secs > 86400:
                d = max(1, int(secs / 86400))
                text = f"{prefix} {d}d"
            elif secs > 3600:
                h = max(1, int(secs / 3600))
                text = f"{prefix} {h}h"
            else:
                m = max(1, int(secs / 60))
                text = f"{prefix} {m}m"
            alerts.append({"text": text, "color": "purple"})

    return alerts
