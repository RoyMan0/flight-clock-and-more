"""
meteor_showers.py — Annual meteor shower peak alerts.

Recurring database of major annual showers. Returns an alert starting
36 hours before the peak and expiring 12 hours after.

Usage:
    from utilities.meteor_showers import get_meteor_shower_alert
    alert = get_meteor_shower_alert()
    # {"text": "Prsd Pk", "color": "cyan"}  or  None
"""

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_ALERT_BEFORE_HOURS = 36
_ALERT_AFTER_HOURS  = 12

# Annual meteor shower database.
# hemisphere: "north" = N-hemisphere favored, "south" = S-hemisphere favored,
#             "both"  = visible from either.
# zhr: zenith hourly rate at peak — used to pick winner if two showers overlap.
_METEOR_DB = [
    {"name": "Quadrantids",   "short": "Quad", "peak_month": 1,  "peak_day": 3,  "peak_hour_utc": 14, "zhr": 120, "hemisphere": "north"},
    {"name": "Lyrids",        "short": "Lyrd", "peak_month": 4,  "peak_day": 22, "peak_hour_utc": 12, "zhr": 18,  "hemisphere": "both"},
    {"name": "Eta Aquariids", "short": "EtaQ", "peak_month": 5,  "peak_day": 6,  "peak_hour_utc": 1,  "zhr": 50,  "hemisphere": "south"},
    {"name": "Perseids",      "short": "Prsd", "peak_month": 8,  "peak_day": 12, "peak_hour_utc": 12, "zhr": 100, "hemisphere": "north"},
    {"name": "Draconids",     "short": "Drac", "peak_month": 10, "peak_day": 8,  "peak_hour_utc": 20, "zhr": 10,  "hemisphere": "north"},
    {"name": "Orionids",      "short": "Orid", "peak_month": 10, "peak_day": 21, "peak_hour_utc": 8,  "zhr": 20,  "hemisphere": "both"},
    {"name": "Leonids",       "short": "Leon", "peak_month": 11, "peak_day": 17, "peak_hour_utc": 14, "zhr": 15,  "hemisphere": "both"},
    {"name": "Geminids",      "short": "Gmnd", "peak_month": 12, "peak_day": 14, "peak_hour_utc": 6,  "zhr": 150, "hemisphere": "both"},
    {"name": "Ursids",        "short": "Ursd", "peak_month": 12, "peak_day": 22, "peak_hour_utc": 6,  "zhr": 10,  "hemisphere": "north"},
]


def get_meteor_shower_alert():
    """
    Return alert dict if a major shower peaks within the alert window, else None.

    Window: 36 hours before peak → 12 hours after peak.
    Within ±12h of peak: shows "Prsd Pk". Earlier: "Prsd 1d" / "Prsd 8h".
    Hemisphere-filtered by the configured home latitude.
    If two showers overlap, the higher-ZHR shower wins.
    """
    try:
        import config as cfg
        location = getattr(cfg, "LOCATION_HOME", [0.0, 0.0])
        lat = float(location[0])
    except Exception:
        lat = 0.0

    now = datetime.now(timezone.utc)
    best = None

    for shower in _METEOR_DB:
        hemi = shower["hemisphere"]
        if hemi == "north" and lat < 10:
            continue
        if hemi == "south" and lat > 10:
            continue

        # Check current year and adjacent years to handle year-boundary peaks
        for year_offset in (0, 1, -1):
            year = now.year + year_offset
            try:
                peak_dt = datetime(
                    year, shower["peak_month"], shower["peak_day"],
                    shower["peak_hour_utc"], 0, tzinfo=timezone.utc,
                )
            except ValueError:
                continue

            secs_to_peak = (peak_dt - now).total_seconds()
            if -_ALERT_AFTER_HOURS * 3600 <= secs_to_peak <= _ALERT_BEFORE_HOURS * 3600:
                if best is None or shower["zhr"] > best["zhr"]:
                    best = {**shower, "peak_dt": peak_dt, "secs_to_peak": secs_to_peak}
                break  # found the right year for this shower

    if best is None:
        return None

    short = best["short"]
    secs = best["secs_to_peak"]

    if secs <= 12 * 3600:  # within 12h of peak (or already past)
        text = f"{short} Pk"
    elif secs > 86400:
        d = max(1, int(secs / 86400))
        text = f"{short} {d}d"
    else:
        h = max(1, int(secs / 3600))
        text = f"{short} {h}h"

    return {"text": text, "color": "cyan"}
